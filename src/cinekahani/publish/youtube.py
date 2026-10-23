"""YouTube Data API v3 upload, written directly on httpx (OAuth installed-app flow + resumable upload)."""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx

from ..core.config import YoutubeCfg
from ..core.logs import get_logger
from ..core.retry import retry_call

log = get_logger("youtube")
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMB_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube"
RETRY_STATUS = {500, 502, 503, 504}


class YoutubeError(RuntimeError):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class YoutubeAuth:
    def __init__(self, env: dict[str, str], token_path: Path, *, http: httpx.Client | None = None,
                 clock: Callable[[], float] = time.time):
        self.cid, self.secret = env.get("YOUTUBE_CLIENT_ID", ""), env.get("YOUTUBE_CLIENT_SECRET", "")
        self.token_path, self.clock = token_path, clock
        self.http = http or httpx.Client(timeout=30)

    def _need_client(self) -> None:
        if not (self.cid and self.secret):
            raise YoutubeError("Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in .env (Google Cloud Console > "
                               "APIs & Services > Credentials > OAuth client ID > Desktop app; enable the YouTube Data API v3).")

    def _store(self, tok: dict) -> None:
        old = self._read()
        keep = {"refresh_token": tok.get("refresh_token") or old.get("refresh_token"),
                "access_token": tok["access_token"], "expires_at": self.clock() + int(tok.get("expires_in", 3600)) - 60}
        self.token_path.write_text(json.dumps(keep), encoding="utf-8")
        os.chmod(self.token_path, 0o600)

    def _read(self) -> dict:
        return json.loads(self.token_path.read_text(encoding="utf-8")) if self.token_path.is_file() else {}

    def authorize(self, *, open_browser: Callable[[str], object] = webbrowser.open, timeout: float = 300) -> None:
        self._need_client()
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(16)
        got: dict = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                got.update({k: v[0] for k, v in q.items()})
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h3>Cine Kahani is connected to YouTube. You can close this tab.</h3>")

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), H)
        server.timeout = 1
        redirect = f"http://127.0.0.1:{server.server_port}"
        url = AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": self.cid, "redirect_uri": redirect, "response_type": "code", "scope": SCOPES,
            "access_type": "offline", "prompt": "consent", "state": state,
            "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"})
        threading.Thread(target=open_browser, args=(url,), daemon=True).start()
        deadline = self.clock() + timeout
        while "code" not in got and "error" not in got and self.clock() < deadline:
            server.handle_request()
        server.server_close()
        if "error" in got:
            raise YoutubeError(f"Google refused the sign-in: {got['error']}")
        if "code" not in got:
            raise YoutubeError("Timed out waiting for the browser sign-in")
        if got.get("state") != state:
            raise YoutubeError("Sign-in state did not match (possible forged redirect); try again")
        r = self.http.post(TOKEN_URL, data={"client_id": self.cid, "client_secret": self.secret, "code": got["code"],
                                           "redirect_uri": redirect, "grant_type": "authorization_code",
                                           "code_verifier": verifier})
        if r.status_code != 200:
            raise YoutubeError(f"Token exchange failed ({r.status_code}): {r.text[:300]}")
        self._store(r.json())

    def access_token(self) -> str:
        self._need_client()
        tok = self._read()
        if not tok.get("refresh_token"):
            raise YoutubeError("Not signed in to YouTube. Run `cinekahani youtube auth` first.")
        if tok.get("access_token") and tok.get("expires_at", 0) > self.clock():
            return tok["access_token"]
        r = self.http.post(TOKEN_URL, data={"client_id": self.cid, "client_secret": self.secret,
                                           "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
        if r.status_code != 200:
            raise YoutubeError(f"Could not refresh the YouTube login ({r.status_code}). Run `cinekahani youtube auth` again.")
        self._store(r.json())
        return self._read()["access_token"]


def _explain(r: httpx.Response) -> str:
    text = r.text[:500]
    if "quotaExceeded" in text:
        return "YouTube API daily quota is used up (an upload costs 1600 of 10,000 units). Try again tomorrow."
    if "uploadLimitExceeded" in text:
        return "YouTube upload limit reached for this channel today."
    if "forbidden" in text.lower() or r.status_code == 403:
        return f"YouTube refused ({r.status_code}). Check the account has a channel and the API is enabled: {text}"
    return f"YouTube error {r.status_code}: {text}"


class YoutubeUploader:
    def __init__(self, auth: YoutubeAuth, cfg: YoutubeCfg, *, http: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.auth, self.cfg, self.sleep = auth, cfg, sleep
        self.http = http or httpx.Client(timeout=httpx.Timeout(120, connect=30))

    def _h(self, **extra) -> dict:
        return {"Authorization": f"Bearer {self.auth.access_token()}", **extra}

    def upload(self, video: Path, *, title: str, description: str, tags: list[str], language: str,
               publish_at: datetime | None = None, public: bool = False) -> dict:
        clean = lambda s: s.replace("<", "").replace(">", "")
        privacy = "private" if (publish_at or not public) else "public"
        if self.cfg.privacy == "unlisted" and not publish_at and not public:
            privacy = "unlisted"
        status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": self.cfg.made_for_kids}
        if publish_at:
            status["publishAt"] = publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if self.cfg.synthetic_media_flag:
            status["containsSyntheticMedia"] = True
        body = {"snippet": {"title": clean(title)[:100], "description": clean(description)[:5000],
                            "tags": tags, "categoryId": self.cfg.category_id,
                            "defaultLanguage": language, "defaultAudioLanguage": language}, "status": status}
        size = video.stat().st_size
        init = self.http.post(UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                              headers=self._h(**{"X-Upload-Content-Length": str(size),
                                                 "X-Upload-Content-Type": "video/mp4"}), json=body)
        if init.status_code != 200 or "location" not in init.headers:
            raise YoutubeError(_explain(init))
        session = init.headers["location"]
        chunk = max(256 * 1024, self.cfg.chunk_mb * 1024 * 1024 // (256 * 1024) * 256 * 1024)
        pos, failures = 0, 0
        with open(video, "rb") as f:
            while True:
                f.seek(pos)
                data = f.read(chunk)
                end = pos + len(data) - 1
                try:
                    r = self.http.put(session, content=data, headers={
                        "Content-Length": str(len(data)), "Content-Range": f"bytes {pos}-{end}/{size}"})
                except httpx.TransportError as exc:
                    r, failures = None, failures + 1
                    log.warning("upload interrupted (%s); asking YouTube where to resume", exc)
                if r is not None and r.status_code in RETRY_STATUS:
                    failures += 1
                if r is None or r.status_code in RETRY_STATUS:
                    if failures > 6:
                        raise YoutubeError("YouTube upload kept failing; try again later")
                    self.sleep(min(2 ** failures, 60))
                    r = self.http.put(session, content=b"", headers={"Content-Length": "0",
                                                                      "Content-Range": f"bytes */{size}"})
                if r.status_code in (200, 201):
                    return r.json()
                if r.status_code == 308:
                    rng = r.headers.get("range")
                    pos = int(rng.split("-")[1]) + 1 if rng else 0
                    if pos >= size:
                        raise YoutubeError("YouTube says all bytes arrived but did not return the video")
                    continue
                raise YoutubeError(_explain(r))

    def set_thumbnail(self, video_id: str, image: Path) -> None:
        r = self.http.post(THUMB_URL, params={"videoId": video_id, "uploadType": "media"},
                           headers=self._h(**{"Content-Type": "image/jpeg"}), content=image.read_bytes())
        if r.status_code != 200:
            raise YoutubeError("Thumbnail not set: " + _explain(r) +
                               " (custom thumbnails need a verified channel; set it in YouTube Studio.)")
