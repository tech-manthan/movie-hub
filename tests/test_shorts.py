import json
import subprocess

import httpx
import pytest

from cinekahani.core.cache import Decision
from cinekahani.shorts import service
from cinekahani.shorts.client import OpenShortsClient, ShortsError
from cinekahani.shorts.make import make_shorts, trailer_share
from cinekahani.visuals.plan import Plan, Shot

PLAN = Plan("x", "review", "en", "clean", 1, 1, 100, 102, "T", 2010,
            shots=[Shot("card", 0, 1, "intro"), Shot("trailer", 30, 3, "/t.mp4"), Shot("image", 33, 69, "/i.jpg")])


class FakeOpenShorts:
    def __init__(self, clips, polls=2, fail=False):
        self.clips, self.polls, self.fail = clips, polls, fail
        self.calls, self.uploaded, self.process_body, self.headers = [], b"", None, None
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, req):
        path = req.url.path
        self.calls.append((req.method, path))
        if path == "/health":
            return httpx.Response(200, json={"ok": True})
        if path == "/api/uploads" and req.method == "POST":
            return httpx.Response(200, json={"upload_id": "u1", "upload_url": "http://other-host:9/api/uploads/u1"})
        if path == "/api/uploads/u1" and req.method == "PUT":
            self.uploaded = req.read()
            return httpx.Response(200, json={"bytes": len(self.uploaded)})
        if path == "/api/process":
            self.process_body, self.headers = json.loads(req.content), req.headers
            return httpx.Response(200, json={"job_id": "j1", "status": "queued"})
        if path == "/api/status/j1":
            self.polls -= 1
            if self.polls > 0:
                return httpx.Response(200, json={"status": "processing", "logs": ["working"]})
            if self.fail:
                return httpx.Response(200, json={"status": "failed", "logs": ["a", "gemini key missing"]})
            return httpx.Response(200, json={"status": "completed", "result": {"clips": self.clips}})
        if path.startswith("/videos/"):
            return httpx.Response(200, content=b"MP4DATA" + path.encode())
        return httpx.Response(404)


CLIPS = [{"video_url": "/videos/j1/a.mp4", "title": "Good", "start": 40, "end": 70},
         {"video_url": "/videos/j1/b.mp4", "title": "Trailer heavy", "start": 29, "end": 35},
         {"video_url": "/videos/j1/c.mp4", "title": "No times"}]


def client_for(settings, fake):
    settings.shorts.poll_seconds = 0
    return OpenShortsClient(settings.shorts, {"GEMINI_API_KEY": "gk"}, transport=fake.transport, sleep=lambda s: None)


def test_trailer_share_math():
    assert trailer_share({"start": 40, "end": 70}, PLAN) == 0
    assert trailer_share({"start": 29, "end": 35}, PLAN) == pytest.approx(0.5)
    assert trailer_share({"start": 30, "end": 33}, PLAN) == 1.0
    assert trailer_share({}, PLAN) is None and trailer_share({"start": 5, "end": 5}, PLAN) is None


@pytest.fixture
def built(settings, sample):
    project, _ = sample
    video = project.dir("renders") / "review.en.mp4"
    video.write_bytes(b"fakevideo")
    PLAN.save(project.dir("renders") / "plan.review.en.json")
    return project


def test_make_shorts_flow_and_trailer_filter(settings, built):
    fake = FakeOpenShorts(CLIPS)
    r = make_shorts(settings, built, "review", "en", client_for(settings, fake), manage_service=False)
    assert fake.uploaded == b"fakevideo"
    assert ("PUT", "/api/uploads/u1") in fake.calls                     # used our host, not the one in upload_url
    assert fake.process_body["upload_id"] == "u1" and fake.process_body["output_format"] == "vertical"
    assert fake.process_body["target_clips"] == 4 and fake.process_body["acknowledged"] is True
    assert fake.headers["x-gemini-key"] == "gk"
    assert [c["title"] for c in r.clips] == ["Good", "No times"] and len(r.rejected) == 1
    assert "trailer" in r.rejected[0]["rejected"]
    assert r.clips[1]["trailer_check"].startswith("unverified") and r.clips[0]["trailer_check"] == "ok"
    assert (r.folder / "short-01.mp4").read_bytes().startswith(b"MP4DATA")
    assert not (r.folder / "short-02.mp4").exists()
    again = make_shorts(settings, built, "review", "en", client_for(settings, fake), manage_service=False)
    assert again.decision is Decision.CACHED and len(again.clips) == 2


def test_make_shorts_errors(settings, built, sample):
    with pytest.raises(ShortsError, match="build"):
        make_shorts(settings, sample[0], "review", "hi", client_for(settings, FakeOpenShorts([])), manage_service=False)
    with pytest.raises(ShortsError, match="gemini key missing"):
        make_shorts(settings, built, "review", "en", client_for(settings, FakeOpenShorts([], fail=True)),
                    manage_service=False)
    with pytest.raises(ShortsError, match="no usable clips"):
        make_shorts(settings, built, "review", "en", client_for(settings, FakeOpenShorts([{"video_url": ""}])),
                    manage_service=False, force=True)
    only_bad = [CLIPS[1]]
    with pytest.raises(ShortsError, match="1 rejected"):
        make_shorts(settings, built, "review", "en", client_for(settings, FakeOpenShorts(only_bad)),
                    manage_service=False, force=True)


def test_timeout_and_unreachable(settings):
    settings.shorts.timeout_s = 5
    ticks = iter(range(0, 100, 3))
    c = OpenShortsClient(settings.shorts, {}, transport=FakeOpenShorts([], polls=99).transport,
                         sleep=lambda s: None, clock=lambda: next(ticks))
    with pytest.raises(ShortsError, match="still running"):
        c.wait("j1")

    def refuse(req):
        raise httpx.ConnectError("no")

    c = OpenShortsClient(settings.shorts, {}, transport=httpx.MockTransport(refuse), sleep=lambda s: None)
    assert c.healthy() is False
    with pytest.raises(ShortsError, match="shorts up"):
        c.upload(__file__ and __import__("pathlib").Path(__file__))


def test_service_start_stop_and_cloud_guard(settings, tmp_path):
    d = settings.root / settings.shorts.compose_dir
    with pytest.raises(ShortsError, match="setup_openshorts"):
        service.compose_dir(settings)
    d.mkdir(parents=True)
    (d / "docker-compose.yml").write_text("services: {}")
    (d / "cloud").mkdir()
    with pytest.raises(ShortsError, match="commercial license"):
        service.compose_dir(settings)
    (d / "cloud").rmdir()

    state = {"up": False}
    cmds = []

    class P:
        returncode, stderr = 0, ""

    def runner(cmd, **kw):
        cmds.append((cmd, kw.get("cwd")))
        state["up"] = cmd[:3] == ["docker", "compose", "up"]
        return P()

    class C:
        def healthy(self):
            return state["up"]

    assert service.start(settings, C(), runner=runner, sleep=lambda s: None) is True
    assert cmds[0][1] == d and "backend" in cmds[0][0]
    assert service.start(settings, C(), runner=runner, sleep=lambda s: None) is False      # already up
    service.stop(settings, runner=runner)
    assert cmds[-1][0][:3] == ["docker", "compose", "down"]

    class Bad:
        returncode, stderr = 1, "no docker daemon"

    state["up"] = False
    with pytest.raises(ShortsError, match="no docker daemon"):
        service.start(settings, C(), runner=lambda *a, **k: Bad(), sleep=lambda s: None)
    with pytest.raises(ShortsError, match="Docker is not installed"):
        service.start(settings, C(), runner=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(ShortsError, match="did not become healthy"):
        service.start(settings, C(), runner=lambda *a, **k: P(), sleep=lambda s: None, wait_s=10)


def test_make_shorts_starts_and_stops_service(settings, built):
    d = settings.root / settings.shorts.compose_dir
    d.mkdir(parents=True)
    (d / "docker-compose.yml").write_text("services: {}")
    fake = FakeOpenShorts([CLIPS[0]])
    up = {"v": False}
    calls = []

    class P:
        returncode, stderr = 0, ""

    def runner(cmd, **kw):
        calls.append(cmd[2])
        up["v"] = cmd[2] == "up"
        return P()

    client = client_for(settings, fake)
    client.healthy = lambda: up["v"]
    make_shorts(settings, built, "review", "en", client, runner=runner, sleep=lambda s: None)
    assert calls == ["up", "down"]
