"""Instagram Reels, posted directly with the Instagram API (Facebook Login flavour, free).

The upload is resumable straight from disk, so the video never needs a public URL. Auth is a Page access
token derived from a long-lived user token (Page tokens made this way do not expire). Ported from the
approach in the owner's reel-engine project, which uses it for daily posts.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

import httpx

from ..core.config import InstagramCfg
from ..core.logs import get_logger
from ..core.retry import retry_call

log = get_logger("instagram")
CAPTION_MAX = 2200
HASHTAG_MAX = 30


class InstagramError(RuntimeError):
    pass


class _Retryable(InstagramError):
    pass


def check_caption(caption: str) -> str:
    caption = caption.strip()
    if len(caption) > CAPTION_MAX:
        raise InstagramError(f"Instagram captions are limited to {CAPTION_MAX} characters (this one has {len(caption)})")
    if len(re.findall(r"#\w+", caption)) > HASHTAG_MAX:
        raise InstagramError(f"Instagram allows at most {HASHTAG_MAX} hashtags")
    return caption


def normalize_for_upload(src: Path, dst: Path, *, runner=subprocess.run) -> Path:
    """Instagram rejects many encoders' output. This is the spec that works for reel-engine: H.264 High yuv420p
    (limited range), 30 fps, 1080x1920, AAC LC 128k 48 kHz stereo, moov atom first, no edit lists."""
    if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".part.mp4")
    vf = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vf", vf,
           "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-profile:v", "high", "-level", "4.1",
           "-pix_fmt", "yuv420p", "-color_range", "tv", "-r", "30", "-g", "60",
           "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
           "-movflags", "+faststart", "-use_editlist", "0", str(tmp)]
    try:
        p = runner(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise InstagramError("ffmpeg is not installed (brew install ffmpeg)") from exc
    if p.returncode != 0:
        raise InstagramError(f"Could not re-encode {src.name} for Instagram: {(p.stderr or '').strip()[-300:]}")
    tmp.replace(dst)
    return dst


class InstagramClient:
    def __init__(self, cfg: InstagramCfg, token_path: Path, *, http: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.cfg, self.token_path, self.sleep = cfg, token_path, sleep
        self.graph = f"https://graph.facebook.com/{cfg.api_version}"
        self.rupload = f"https://rupload.facebook.com/ig-api-upload/{cfg.api_version}"
        self.http = http or httpx.Client(timeout=httpx.Timeout(600, connect=30))

    @staticmethod
    def _json(r: httpx.Response) -> dict:
        try:
            data = r.json()
        except ValueError:
            raise InstagramError(f"HTTP {r.status_code}: {r.text[:300]}")
        if r.status_code >= 400 or "error" in data:
            err = data.get("error", data)
            raise InstagramError(f"{err.get('message', err)} (code {err.get('code')}, subcode {err.get('error_subcode')})")
        return data

    def _call(self, method: str, url: str, **kw) -> dict:
        def once() -> dict:
            try:
                r = self.http.request(method, url, **kw)
            except httpx.TransportError as exc:
                raise _Retryable(f"network error: {exc}") from exc
            if r.status_code >= 500:
                raise _Retryable(f"HTTP {r.status_code}")
            return self._json(r)

        return retry_call(once, attempts=3, base_delay=3, retry_on=(_Retryable,), sleep=self.sleep, what="Instagram")

    def setup(self, user_token: str, page_name: str | None = None) -> dict:
        """Find the Facebook Page linked to the Instagram account and store its (non-expiring) token."""
        if not user_token.strip():
            raise InstagramError("Set META_USER_TOKEN in .env (long-lived user token from the Graph API Explorer; "
                                 "see README, Instagram setup).")
        pages = self._call("GET", f"{self.graph}/me/accounts", params={
            "fields": "name,access_token,instagram_business_account{id,username}",
            "access_token": user_token.strip()})["data"]
        linked = [p for p in pages if p.get("instagram_business_account") and (not page_name or p["name"] == page_name)]
        if not linked:
            names = ", ".join(p["name"] for p in pages) or "none"
            raise InstagramError("No Page with a linked Instagram account found "
                                 f"(Pages visible to this token: {names}). Link the Instagram account to the Page first.")
        page = linked[0]
        ig = page["instagram_business_account"]
        stored = {"page_id": page["id"], "page_name": page["name"], "page_token": page["access_token"],
                  "ig_user_id": ig["id"], "ig_username": ig.get("username")}
        self.token_path.write_text(json.dumps(stored, indent=2), encoding="utf-8")
        os.chmod(self.token_path, 0o600)
        return stored

    def _creds(self) -> dict:
        if not self.token_path.is_file():
            raise InstagramError("Instagram is not connected. Run `cinekahani instagram setup` first.")
        return json.loads(self.token_path.read_text(encoding="utf-8"))

    def whoami(self) -> dict:
        c = self._creds()
        me = self._call("GET", f"{self.graph}/{c['ig_user_id']}",
                        params={"fields": "username,media_count", "access_token": c["page_token"]})
        return {"username": me.get("username"), "page": c["page_name"], "media_count": me.get("media_count")}

    def publish_reel(self, video: Path, caption: str) -> dict:
        """Create container, upload the file, wait for processing, publish. Posts publicly (Instagram has no private mode)."""
        caption = check_caption(caption)
        c = self._creds()
        tok, uid = c["page_token"], c["ig_user_id"]
        size = video.stat().st_size
        data = {"media_type": "REELS", "upload_type": "resumable", "caption": caption,
                "share_to_feed": str(self.cfg.share_to_feed).lower(),
                "thumb_offset": str(self.cfg.thumb_offset_ms), "access_token": tok}
        try:
            container = self._call("POST", f"{self.graph}/{uid}/media", data=data)
        except InstagramError as exc:
            log.info("cover offset rejected (%s); retrying without it", exc)
            del data["thumb_offset"]
            container = self._call("POST", f"{self.graph}/{uid}/media", data=data)
        cid = container["id"]
        log.info("container %s created, uploading %.1f MB", cid, size / 1e6)
        try:
            self._call("POST", container.get("uri") or f"{self.rupload}/{cid}", content=video.read_bytes(), headers={
                "Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(size),
                "Content-Type": "application/octet-stream"})
        except InstagramError as exc:
            raise InstagramError(f"upload failed: {exc}") from exc
        for _ in range(self.cfg.poll_attempts):
            st = self._call("GET", f"{self.graph}/{cid}", params={"fields": "status_code,status", "access_token": tok})
            code = st.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise InstagramError(f"Instagram could not process the video: {st.get('status')}")
            log.info("processing (%s)", code)
            self.sleep(self.cfg.poll_seconds)
        else:
            raise InstagramError("Instagram processing timed out")
        try:                                  # publishing is not retried: a repeat could double-post
            r = self.http.post(f"{self.graph}/{uid}/media_publish", data={"creation_id": cid, "access_token": tok})
            media = self._json(r)
        except httpx.TransportError as exc:
            raise InstagramError(f"Lost the connection while publishing. Check Instagram before retrying: {exc}") from exc
        info = self._call("GET", f"{self.graph}/{media['id']}", params={"fields": "permalink,timestamp", "access_token": tok})
        return {"media_id": media["id"], "permalink": info.get("permalink"), "username": c.get("ig_username")}
