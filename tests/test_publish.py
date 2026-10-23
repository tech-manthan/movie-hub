import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from conftest import FakeLlm
from test_package import reply

from cinekahani.assemble.build import render_paths
from cinekahani.core.config import YoutubeCfg, UploadPostCfg
from cinekahani.package.pack import build_package
from cinekahani.publish import run
from cinekahani.publish.state import PublishState, ScheduleError, parse_when
from cinekahani.publish.uploadpost import UploadPostClient, UploadPostError
from cinekahani.publish.youtube import YoutubeAuth, YoutubeError, YoutubeUploader
from cinekahani.review import queue
from cinekahani.visuals.plan import Plan

FUTURE = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d 18:00")


@pytest.fixture
def packaged(small, narrated):
    project, meta = narrated
    video, plan_path, report = render_paths(project, "review", "en")
    video.write_bytes(os.urandom(2_600_000))
    Plan(project.slug, "review", "en", "clean", 1, 1.5, 58, 60.5, meta.title, meta.year).save(plan_path)
    report.write_text(json.dumps({"checks": "passed"}))
    clips = project.root / "clips" / "review.en"
    clips.mkdir(parents=True)
    (clips / "short-01.mp4").write_bytes(b"short")
    (clips / "clips.json").write_text(json.dumps({"clips": [{"file": "short-01.mp4", "title": "S1", "trailer_check": "ok"}]}))
    build_package(small, project, meta, "review", "en", FakeLlm(reply()))
    return project


# ---------------- review queue ----------------

def test_discover_checks_and_approval_lifecycle(small, packaged):
    items = queue.discover(small, packaged)
    assert [i.id for i in items] == ["long:review.en", "short:review.en:1"]
    long, short = items
    assert not long.blocked and long.status == "pending" and long.thumbnail and short.hashes["video"]
    assert any(c.name == "trailer guardrails" and c.ok for c in long.checks)
    with pytest.raises(queue.ReviewError, match="without your approval"):
        queue.require_approved(small, packaged, long.id)
    queue.decide(small, packaged, long.id, "approved", "looks good")
    assert queue.require_approved(small, packaged, long.id).note == "looks good"
    queue.save_edit(small, packaged, long.id, title="A new title (English)")          # editing withdraws it
    assert queue.find(small, packaged, long.id).status == "changed"
    with pytest.raises(queue.ReviewError, match="edited after you approved"):
        queue.require_approved(small, packaged, long.id)
    queue.decide(small, packaged, long.id, "approved")
    assert queue.require_approved(small, packaged, long.id).title == "A new title (English)"
    queue.decide(small, packaged, long.id, "rejected", "re-cut the intro")
    assert queue.find(small, packaged, long.id).status == "rejected"


def test_approval_withdrawn_when_video_or_thumbnail_changes(small, packaged):
    queue.decide(small, packaged, "long:review.en", "approved")
    (packaged.root / "renders" / "review.en.mp4").write_bytes(b"different")
    assert queue.find(small, packaged, "long:review.en").status == "changed"
    queue.decide(small, packaged, "long:review.en", "approved")
    (packaged.root / "package" / "review.en" / "thumbnail.jpg").write_bytes(b"x")
    assert queue.find(small, packaged, "long:review.en").status == "changed"


def test_blocking_checks_stop_approval(small, packaged):
    sp = packaged.root / "script" / "review.en.md"
    sp.write_text(sp.read_text().replace("status: approved", "status: draft"))
    with pytest.raises(queue.ReviewError, match="script approved"):
        queue.decide(small, packaged, "long:review.en", "approved")
    sp.write_text(sp.read_text().replace("status: draft", "status: approved") + "\nclaim [VERIFY: x]\n")
    with pytest.raises(queue.ReviewError, match="VERIFY"):
        queue.decide(small, packaged, "long:review.en", "approved")
    with pytest.raises(queue.ReviewError, match="No item"):
        queue.find(small, packaged, "long:nope.en")


def test_title_label_and_shorts_warning(small, packaged):
    queue.save_edit(small, packaged, "long:review.en", title="No label here")
    long = queue.find(small, packaged, "long:review.en")
    assert any(c.name == "language label in title" for c in long.blocked)
    pkg = packaged.root / "package" / "review.en" / "package.json"
    d = json.loads(pkg.read_text())
    d["shorts"][0]["trailer_check"] = "unverified"
    pkg.write_text(json.dumps(d))
    short = queue.find(small, packaged, "short:review.en:1")
    assert not short.blocked and any(not c.ok and not c.blocking for c in short.checks)
    queue.save_edit(small, packaged, short.id, title="Better short", description="d")
    assert queue.find(small, packaged, short.id).title == "Better short"


# ---------------- schedule ----------------

def test_parse_when():
    now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    w = parse_when("2026-11-03 18:00", "Asia/Kolkata", now=now)
    assert w == datetime(2026, 11, 3, 12, 30, tzinfo=timezone.utc)
    with pytest.raises(ScheduleError, match="future"):
        parse_when("2026-11-01 12:10", "UTC", now=now)
    with pytest.raises(ScheduleError, match="timezone"):
        parse_when("2026-11-03 18:00", "Mars/Base")
    with pytest.raises(ScheduleError, match="form"):
        parse_when("tomorrow", "UTC")


# ---------------- YouTube ----------------

def fake_google(log):
    state = {"puts": 0, "interrupt": False}

    def handler(req: httpx.Request):
        url = str(req.url)
        log.append((req.method, req.url.path, dict(req.headers)))
        if url.startswith("https://oauth2.googleapis.com/token"):
            form = urllib.parse.parse_qs(req.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                assert form["code"] == ["abc"] and form["code_verifier"]
                return httpx.Response(200, json={"access_token": "A1", "refresh_token": "R1", "expires_in": 3600})
            assert form["refresh_token"] == ["R1"]
            return httpx.Response(200, json={"access_token": "A2", "expires_in": 3600})
        if req.url.path == "/upload/youtube/v3/videos" and req.method == "POST":
            log[-1] += (json.loads(req.content),)
            return httpx.Response(200, headers={"location": "https://upload.example/session1"})
        if req.url.host == "upload.example":
            cr = req.headers["content-range"]
            if cr.startswith("bytes */"):
                return httpx.Response(308, headers={"range": f"bytes=0-{state['sent'] - 1}"} if state.get("sent") else {})
            start, end, total = map(int, re.match(r"bytes (\d+)-(\d+)/(\d+)", cr).groups())
            state["puts"] += 1
            if state["interrupt"] and state["puts"] == 2:
                state["interrupt"] = False
                raise httpx.ReadError("connection dropped")
            state["sent"] = end + 1
            if end + 1 >= total:
                return httpx.Response(200, json={"id": "VID1", "status": {"privacyStatus": "private"}})
            return httpx.Response(308, headers={"range": f"bytes=0-{end}"})
        if req.url.path == "/upload/youtube/v3/thumbnails/set":
            return httpx.Response(200, json={})
        return httpx.Response(404)

    return handler, state


def make_uploader(tmp_path, log, cfg=None):
    handler, state = fake_google(log)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    t = [1000.0]
    auth = YoutubeAuth({"YOUTUBE_CLIENT_ID": "id", "YOUTUBE_CLIENT_SECRET": "sec"}, tmp_path / "token.json",
                       http=http, clock=lambda: t[0])
    return YoutubeUploader(auth, cfg or YoutubeCfg(chunk_mb=1), http=http, sleep=lambda s: None), auth, state, t


def test_oauth_flow_with_simulated_browser_and_refresh(tmp_path):
    log = []
    _, auth, _, t = make_uploader(tmp_path, log)

    def browser(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert q["code_challenge_method"] == ["S256"] and "youtube.upload" in q["scope"][0]
        urllib.request.urlopen(f"{q['redirect_uri'][0]}?code=abc&state={q['state'][0]}", timeout=5).read()

    auth.authorize(open_browser=browser, timeout=10)
    assert oct(os.stat(tmp_path / "token.json").st_mode)[-3:] == "600"
    assert auth.access_token() == "A1"
    t[0] += 4000                                                    # token expired
    assert auth.access_token() == "A2"
    assert json.loads((tmp_path / "token.json").read_text())["refresh_token"] == "R1"   # refresh token kept


def test_oauth_rejects_forged_state_and_missing_setup(tmp_path):
    log = []
    _, auth, _, _ = make_uploader(tmp_path, log)

    def bad(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        urllib.request.urlopen(f"{q['redirect_uri'][0]}?code=abc&state=forged", timeout=5).read()

    with pytest.raises(YoutubeError, match="state"):
        auth.authorize(open_browser=bad, timeout=10)
    with pytest.raises(YoutubeError, match="YOUTUBE_CLIENT_ID"):
        YoutubeAuth({}, tmp_path / "t.json").access_token()
    with pytest.raises(YoutubeError, match="auth"):
        YoutubeAuth({"YOUTUBE_CLIENT_ID": "a", "YOUTUBE_CLIENT_SECRET": "b"}, tmp_path / "none.json").access_token()


def signed_in(tmp_path, log, cfg=None):
    up, auth, state, t = make_uploader(tmp_path, log, cfg)
    (tmp_path / "token.json").write_text(json.dumps({"refresh_token": "R1", "access_token": "A1", "expires_at": 10**9}))
    return up, state


def test_resumable_upload_sends_metadata_and_resumes_after_drop(tmp_path):
    log = []
    up, state = signed_in(tmp_path, log)
    video = tmp_path / "v.mp4"
    video.write_bytes(os.urandom(2_600_000))
    state["interrupt"] = True
    when = datetime(2026, 11, 3, 12, 30, tzinfo=timezone.utc)
    res = up.upload(video, title="T <b>", description="D > x", tags=["a"], language="hi", publish_at=when)
    assert res["id"] == "VID1"
    init = next(e for e in log if len(e) == 4)[3]
    assert init["status"]["publishAt"] == "2026-11-03T12:30:00Z" and init["status"]["privacyStatus"] == "private"
    assert init["status"]["containsSyntheticMedia"] is True and init["snippet"]["defaultAudioLanguage"] == "hi"
    assert "<" not in init["snippet"]["title"] and ">" not in init["snippet"]["description"]
    assert any(e[2].get("content-range", "").startswith("bytes */") for e in log)         # asked where to resume
    up.set_thumbnail("VID1", video)


def test_upload_privacy_rules_and_errors(tmp_path):
    log = []
    cfg = YoutubeCfg(chunk_mb=1, synthetic_media_flag=False)
    up, state = signed_in(tmp_path, log, cfg)
    video = tmp_path / "v.mp4"
    video.write_bytes(os.urandom(300_000))
    up.upload(video, title="t", description="d", tags=[], language="en")
    init = next(e for e in log if len(e) == 4)[3]
    assert init["status"]["privacyStatus"] == "private" and "containsSyntheticMedia" not in init["status"]
    up.upload(video, title="t", description="d", tags=[], language="en", public=True)
    assert [e for e in log if len(e) == 4][-1][3]["status"]["privacyStatus"] == "public"

    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}})))
    up.http = http
    with pytest.raises(YoutubeError, match="quota"):
        up.upload(video, title="t", description="d", tags=[], language="en")
    with pytest.raises(YoutubeError, match="verified channel"):
        up.set_thumbnail("V", video)


# ---------------- Upload-Post ----------------

def test_uploadpost_fields_and_errors(tmp_path):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"request_id": "r1"})

    cfg = UploadPostCfg(user="cine", platforms=["instagram", "youtube"])
    c = UploadPostClient({"UPLOADPOST_API_KEY": "K"}, cfg, http=httpx.Client(transport=httpx.MockTransport(handler)),
                         sleep=lambda s: None)
    v = tmp_path / "s.mp4"
    v.write_bytes(b"clip")
    when = datetime(2026, 11, 3, 12, 30, tzinfo=timezone.utc)
    assert c.upload(v, title="Title", description="Desc", when=when)["request_id"] == "r1"
    body = seen[0].content.decode("latin1")
    assert seen[0].headers["authorization"] == "Apikey K"
    for needle in ['name="user"', "cine", 'name="platform[]"', "instagram", "youtube", "REELS", "2026-11-03T12:30:00",
                   'name="timezone"', 'filename="s.mp4"']:
        assert needle in body
    assert 'name="privacyStatus"' in body and "public" in body
    c.upload(v, title="T", description="D")
    assert "private" in seen[1].content.decode("latin1")
    with pytest.raises(UploadPostError, match="UPLOADPOST_API_KEY"):
        UploadPostClient({}, cfg)
    with pytest.raises(UploadPostError, match="user"):
        UploadPostClient({"UPLOADPOST_API_KEY": "K"}, UploadPostCfg())
    n = []
    bad = UploadPostClient({"UPLOADPOST_API_KEY": "K"}, cfg, sleep=lambda s: None, http=httpx.Client(
        transport=httpx.MockTransport(lambda r: (n.append(1), httpx.Response(503))[1])))
    with pytest.raises(UploadPostError):
        bad.upload(v, title="T", description="D")
    assert len(n) == 3
    reject = UploadPostClient({"UPLOADPOST_API_KEY": "K"}, cfg, http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad key"))))
    with pytest.raises(UploadPostError, match="bad key"):
        reject.upload(v, title="T", description="D")


# ---------------- publish gate ----------------

class FakeYT:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def upload(self, video, **kw):
        if self.fail:
            raise YoutubeError("boom")
        self.calls.append(kw)
        return {"id": "VID9", "status": {"privacyStatus": "private"}}

    def set_thumbnail(self, vid, path):
        self.calls.append(("thumb", vid))


class FakeUP:
    def __init__(self):
        self.calls = []

    def upload(self, video, **kw):
        self.calls.append(kw)
        return {"request_id": "r"}


def test_publish_requires_approval_and_a_schedule(small, packaged):
    prov = run.Providers(youtube=FakeYT(), uploadpost=FakeUP())
    with pytest.raises(queue.ReviewError, match="without your approval"):
        run.publish_item(small, packaged, "long:review.en", prov, now=True)
    assert prov.youtube.calls == []
    queue.decide(small, packaged, "long:review.en", "approved")
    with pytest.raises(run.PublishError, match="no schedule"):
        run.publish_item(small, packaged, "long:review.en", prov)
    out = run.publish_item(small, packaged, "long:review.en", prov, now=True, dry_run=True)
    assert out[0].status == "dry-run" and prov.youtube.calls == []


def test_publish_now_and_scheduled_paths(small, packaged):
    yt, up = FakeYT(), FakeUP()
    prov = run.Providers(youtube=yt, uploadpost=up)
    queue.decide(small, packaged, "long:review.en", "approved")
    out = run.publish_item(small, packaged, "long:review.en", prov, now=True)
    assert out[0].status == "uploaded" and yt.calls[0]["publish_at"] is None and yt.calls[0]["public"] is False
    assert ("thumb", "VID9") in yt.calls
    again = run.publish_item(small, packaged, "long:review.en", prov, now=True)
    assert again[0].status == "skipped" and len([c for c in yt.calls if isinstance(c, dict)]) == 1

    queue.decide(small, packaged, "short:review.en:1", "approved")
    when = parse_when(FUTURE, "Asia/Kolkata")
    PublishState(packaged).schedule("short:review.en:1", when, "Asia/Kolkata", ["uploadpost"])
    out = run.publish_item(small, packaged, "short:review.en:1", prov)
    assert out[0].status == "scheduled" and out[0].target == "uploadpost" and up.calls[0]["when"] == when
    st = json.loads((packaged.root / "publish.json").read_text())["items"]
    assert st["long:review.en"]["results"]["youtube"]["video_id"] == "VID9"


def test_failed_upload_is_recorded_not_raised(small, packaged):
    queue.decide(small, packaged, "long:review.en", "approved")
    out = run.publish_item(small, packaged, "long:review.en", run.Providers(youtube=FakeYT(fail=True)), now=True)
    assert out[0].status == "failed" and "boom" in out[0].detail
    assert PublishState(packaged).get("long:review.en")["results"]["youtube"]["status"] == "failed"
    ok = run.publish_item(small, packaged, "long:review.en", run.Providers(youtube=FakeYT()), now=True)
    assert ok[0].status == "uploaded"                                # a failed attempt can be retried


def test_run_due_only_uploads_approved_scheduled_items(small, packaged):
    yt = FakeYT()
    prov = run.Providers(youtube=yt, uploadpost=FakeUP())
    when = parse_when(FUTURE, "UTC")
    state = PublishState(packaged)
    state.schedule("long:review.en", when, "UTC", ["youtube"])        # scheduled but NOT approved
    state.schedule("short:review.en:1", when, "UTC", ["uploadpost"])
    assert run.run_due(small, [packaged], prov) == []
    queue.decide(small, packaged, "long:review.en", "approved")
    res = run.run_due(small, [packaged], prov)
    assert [(r.item, r.status) for r in res] == [("long:review.en", "scheduled")]
    assert run.run_due(small, [packaged], prov) == []                 # nothing left to do
    queue.save_edit(small, packaged, "short:review.en:1", title="edited")
    assert run.run_due(small, [packaged], prov) == []                 # unapproved edits never go out


def test_expired_schedule_is_refused(small, packaged):
    queue.decide(small, packaged, "long:review.en", "approved")
    PublishState(packaged).schedule("long:review.en", datetime(2020, 1, 1, tzinfo=timezone.utc), "UTC", ["youtube"])
    with pytest.raises(run.PublishError, match="passed"):
        run.publish_item(small, packaged, "long:review.en", run.Providers(youtube=FakeYT()))
