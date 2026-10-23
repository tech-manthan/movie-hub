"""Upload-Post: send a Short/Reel from a laptop to Instagram, YouTube and TikTok (needs no public URL)."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..core.config import UploadPostCfg
from ..core.logs import get_logger
from ..core.retry import retry_call

log = get_logger("uploadpost")
URL = "https://api.upload-post.com/api/upload"


class UploadPostError(RuntimeError):
    pass


class _Retryable(UploadPostError):
    pass


class UploadPostClient:
    def __init__(self, env: dict[str, str], cfg: UploadPostCfg, *, http: httpx.Client | None = None,
                 sleep=time.sleep):
        self.key, self.cfg, self.sleep = env.get("UPLOADPOST_API_KEY", ""), cfg, sleep
        if not self.key:
            raise UploadPostError("Set UPLOADPOST_API_KEY in .env (upload-post.com; the free tier allows 10 uploads a month).")
        if not cfg.user:
            raise UploadPostError("Set publish.uploadpost.user in config/studio.local.yaml (your Upload-Post profile name).")
        self.http = http or httpx.Client(timeout=httpx.Timeout(180, connect=30))

    def upload(self, video: Path, *, title: str, description: str, platforms: list[str] | None = None,
               when: datetime | None = None, public: bool = False) -> dict:
        platforms = platforms or self.cfg.platforms
        data = {"user": self.cfg.user, "title": title, "platform[]": platforms, "async_upload": "true"}
        if when:
            data["scheduled_date"] = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            data["timezone"] = "UTC"
        if "instagram" in platforms:
            data.update({"instagram_title": description, "media_type": "REELS"})
        if "tiktok" in platforms:
            data.update({"tiktok_title": description, "post_mode": "MEDIA_UPLOAD"})
        if "youtube" in platforms:
            data.update({"youtube_title": title[:100], "youtube_description": description,
                         "privacyStatus": "public" if (public or when) else "private"})

        def once() -> dict:
            try:
                with open(video, "rb") as f:
                    r = self.http.post(URL, headers={"Authorization": f"Apikey {self.key}"}, data=data,
                                       files={"video": (video.name, f.read(), "video/mp4")})
            except httpx.TransportError as exc:
                raise _Retryable(f"network error: {exc}") from exc
            if r.status_code >= 500 or r.status_code == 429:
                raise _Retryable(f"Upload-Post returned {r.status_code}")
            if r.status_code not in (200, 201, 202):
                raise UploadPostError(f"Upload-Post rejected the upload ({r.status_code}): {r.text[:400]}")
            return r.json()

        return retry_call(once, attempts=3, base_delay=3, retry_on=(_Retryable,), sleep=self.sleep, what="Upload-Post")
