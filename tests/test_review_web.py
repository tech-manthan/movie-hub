import json
import re

import pytest
from fastapi.testclient import TestClient
from test_publish import FUTURE, packaged  # noqa: F401  (fixture)

from cinekahani.review import queue
from cinekahani.review.web import create_app, host_name, local_time

LOCAL = "http://127.0.0.1:8765"


@pytest.fixture
def client(small, packaged):
    c = TestClient(create_app(small), base_url=LOCAL)
    page = c.get("/").text
    c.token = re.search(r'name="csrf" content="([^"]+)"', page).group(1)
    c.project = packaged
    return c


def post(c, path, body, token=True, headers=None):
    h = {"X-CSRF": c.token} if token else {}
    h.update(headers or {})
    return c.post(path, json=body, headers=h)


def test_pages_render_and_escape(client, small):
    page = client.get("/").text
    assert "Review queue" in page and "Inception explained" in page and "<img" in page
    queue.save_edit(small, client.project, "long:review.en", title='X <script>alert(1)</script> (English)')
    idx = client.get("/").text
    assert "<script>alert(1)</script>" not in idx and "&lt;script&gt;" in idx
    detail = client.get("/item", params={"slug": client.project.slug, "id": "long:review.en"}).text
    assert "<video" in detail and "&lt;script&gt;" in detail and "Checks" in detail and "Title options" in detail
    short = client.get("/item", params={"slug": client.project.slug, "id": "short:review.en:1"}).text
    assert "Tags (comma" not in short
    assert client.get("/item", params={"slug": client.project.slug, "id": "nope"}).status_code == 404
    assert client.get("/item", params={"slug": "../etc", "id": "x"}).status_code in (400, 404)


def test_posts_need_token_local_host_and_origin(client, small):
    body = {"slug": client.project.slug, "id": "long:review.en"}
    assert post(client, "/api/approve", body, token=False).status_code == 403
    assert client.post("/api/approve", json=body, headers={"X-CSRF": "wrong"}).status_code == 403
    assert post(client, "/api/approve", body, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/", headers={"Host": "localhost:8765"}).status_code == 200
    assert queue.find(small, client.project, "long:review.en").status == "pending"      # nothing slipped through


def test_approve_save_reject_schedule_via_api(client, small):
    b = {"slug": client.project.slug, "id": "long:review.en"}
    assert post(client, "/api/approve", {**b, "note": "ok"}).json() == {"ok": True}
    assert queue.find(small, client.project, b["id"]).status == "approved"
    r = post(client, "/api/save", {**b, "title": "Changed (English)", "description": "new", "tags": "a, b"})
    assert r.status_code == 200 and queue.find(small, client.project, b["id"]).status == "changed"
    bad = post(client, "/api/save", {**b, "title": ""})
    assert bad.status_code == 200
    r = post(client, "/api/approve", b)
    assert r.status_code == 400 and "title length" in r.json()["error"]            # empty title blocks approval
    post(client, "/api/save", {**b, "title": "Fine (English)"})
    assert post(client, "/api/approve", b).status_code == 200
    s = post(client, "/api/schedule", {**b, "at": FUTURE, "tz": "Asia/Kolkata"})
    assert s.status_code == 200 and s.json()["at_utc"].endswith("+00:00")
    assert post(client, "/api/schedule", {**b, "at": "2020-01-01 10:00", "tz": "UTC"}).status_code == 400
    assert post(client, "/api/reject", {**b, "note": "no"}).status_code == 200
    assert queue.find(small, client.project, b["id"]).status == "rejected"
    assert post(client, "/api/approve", {"slug": "../x", "id": "y"}).status_code == 400
    assert client.post("/api/approve", content=b"notjson", headers={"X-CSRF": client.token}).status_code == 400
    page = client.get("/item", params={"slug": b["slug"], "id": b["id"]}).text
    assert re.search(r'id="at"[^>]*value="\d{4}-\d\d-\d\d 18:00"', page)             # shown in the chosen timezone


def test_media_is_confined_to_the_project(client):
    slug = client.project.slug
    ok = client.get("/media", params={"slug": slug, "path": "package/review.en/thumbnail.jpg"})
    assert ok.status_code == 200 and ok.headers["content-type"] == "image/jpeg"
    vid = client.get("/media", params={"slug": slug, "path": "renders/review.en.mp4"}, headers={"Range": "bytes=0-99"})
    assert vid.status_code == 206 and len(vid.content) == 100                        # seeking works
    for bad in ("../../config/studio.yaml", "package/review.en/package.json", "../inception-2010/../../x.mp4",
                "/etc/passwd"):
        assert client.get("/media", params={"slug": slug, "path": bad}).status_code == 404
    assert client.get("/media", params={"slug": "..", "path": "x.jpg"}).status_code in (400, 404)


def test_helpers():
    assert host_name("127.0.0.1:8765") == "127.0.0.1" and host_name("[::1]:80") == "[::1]" and host_name(None) == ""
    assert local_time("2026-11-03T12:30:00+00:00", "Asia/Kolkata") == "2026-11-03 18:00"
    assert local_time("garbage", "UTC") == "garbage"


def test_cli_review_and_publish_flow(small, packaged, monkeypatch):
    from typer.testing import CliRunner
    from cinekahani import cli
    r = CliRunner()
    base = ["--root", str(small.root)]
    out = r.invoke(cli.app, base + ["review", "list"])
    assert "pending" in out.output and "long:review.en" in out.output
    assert "Checks" not in out.output and "ok" in r.invoke(cli.app, base + ["review", "show", packaged.slug, "long:review.en"]).output
    blocked = r.invoke(cli.app, base + ["publish", "now", packaged.slug, "long:review.en"])
    assert blocked.exit_code == 1 and "--yes" in blocked.output
    unapproved = r.invoke(cli.app, base + ["publish", "now", packaged.slug, "long:review.en", "--yes"])
    assert unapproved.exit_code == 1 and "approval" in unapproved.output
    assert r.invoke(cli.app, base + ["review", "approve", packaged.slug, "long:review.en"]).exit_code == 0
    sch = r.invoke(cli.app, base + ["publish", "schedule", packaged.slug, "long:review.en", "--at", FUTURE])
    assert sch.exit_code == 0 and "scheduled" in sch.output
    dry = r.invoke(cli.app, base + ["publish", "run", "--dry-run"])
    assert dry.exit_code == 0 and "dry-run" in dry.output
    past = r.invoke(cli.app, base + ["publish", "schedule", packaged.slug, "long:review.en", "--at", "2020-01-01 10:00"])
    assert past.exit_code == 1 and "future" in past.output
    assert r.invoke(cli.app, base + ["review", "reject", packaged.slug, "long:review.en"]).exit_code == 0
    assert r.invoke(cli.app, base + ["review", "approve", "nope-1", "x"]).exit_code == 1


def test_status_and_doctor_commands(small, packaged):
    from typer.testing import CliRunner
    from cinekahani import cli
    r = CliRunner()
    base = ["--root", str(small.root)]
    out = r.invoke(cli.app, base + ["status"]).output
    assert "review.en:" in out and "[x]script" in out and "[x]approved" in out and "[x]package" in out
    assert "[ ]reviewed" in out and "next: reviewed" in out
    queue.decide(small, packaged, "long:review.en", "approved")
    assert "[x]reviewed" in r.invoke(cli.app, base + ["status", packaged.slug]).output
    assert "next: published" in r.invoke(cli.app, base + ["status", packaged.slug]).output
    d = r.invoke(cli.app, base + ["doctor"])
    assert "ffmpeg" in d.output and "VoiceStudio" in d.output and "YouTube signed in" in d.output
    assert r.invoke(cli.app, base + ["status", "nope-1"]).exit_code == 1


def test_favicon_is_quiet(client):
    assert client.get("/favicon.ico").status_code == 204
