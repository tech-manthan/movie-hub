"""Client for a locally running OpenShorts backend (a separate service we only call over HTTP)."""
from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..core.config import ShortsCfg
from ..core.logs import get_logger
from ..core.retry import retry_call

log = get_logger("openshorts")


class ShortsError(RuntimeError):
    pass


class _Retryable(ShortsError):
    pass


class OpenShortsClient:
    def __init__(self, cfg: ShortsCfg, env: dict[str, str] | None = None, *,
                 transport: httpx.BaseTransport | None = None, sleep=time.sleep, clock=time.monotonic):
        env = env or {}
        self.cfg, self.sleep, self.clock = cfg, sleep, clock
        self.gemini = env.get("GEMINI_API_KEY", "")
        self.http = httpx.Client(base_url=cfg.base_url.rstrip("/"), timeout=300, transport=transport)

    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        def once() -> httpx.Response:
            try:
                r = self.http.request(method, path, **kw)
            except httpx.ConnectError as exc:
                raise ShortsError(f"Cannot reach OpenShorts at {self.cfg.base_url}. Start it with "
                                  "`cinekahani shorts up`.") from exc
            except httpx.TransportError as exc:
                raise _Retryable(f"network error: {exc}") from exc
            if r.status_code >= 500:
                raise _Retryable(f"OpenShorts returned {r.status_code}: {r.text[:300]}")
            if r.status_code >= 400:
                raise ShortsError(f"OpenShorts rejected {method} {path} ({r.status_code}): {r.text[:400]}")
            return r

        return retry_call(once, attempts=3, base_delay=2, retry_on=(_Retryable,), sleep=self.sleep,
                          what=f"OpenShorts {path}")

    def healthy(self) -> bool:
        try:
            return self.http.get("/health").status_code == 200
        except httpx.HTTPError:
            return False

    def upload(self, video: Path) -> str:
        slot = self._req("POST", "/api/uploads", json={"filename": video.name}).json()
        path = urlparse(slot["upload_url"]).path          # always talk to our own base URL
        with open(video, "rb") as f:
            self._req("PUT", path, content=f)
        return slot["upload_id"]

    def process(self, upload_id: str) -> str:
        c = self.cfg
        headers = {"X-Gemini-Key": self.gemini} if self.gemini else {}
        body = {"upload_id": upload_id, "acknowledged": True, "output_format": "vertical",
                "target_clips": c.target_clips, "clip_min_seconds": c.min_seconds,
                "clip_max_seconds": c.max_seconds, "auto_hook": c.auto_hook, "captions": c.captions}
        return self._req("POST", "/api/process", json=body, headers=headers).json()["job_id"]

    def wait(self, job_id: str) -> list[dict]:
        start = self.clock()
        last_log = 0
        while True:
            st = self._req("GET", f"/api/status/{job_id}").json()
            logs = st.get("logs") or []
            for line in logs[last_log:]:
                log.debug("openshorts: %s", line)
            last_log = len(logs)
            if st["status"] == "completed":
                return (st.get("result") or {}).get("clips") or []
            if st["status"] == "failed":
                tail = " | ".join(str(x) for x in logs[-3:])
                raise ShortsError(f"OpenShorts job failed: {tail}")
            if self.clock() - start > self.cfg.timeout_s:
                raise ShortsError(f"OpenShorts job still running after {self.cfg.timeout_s}s; "
                                  f"check job {job_id} in its logs")
            self.sleep(self.cfg.poll_seconds)

    def download(self, video_url: str, dest: Path) -> Path:
        path = urlparse(video_url).path or video_url
        r = self._req("GET", path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(r.content)
        tmp.replace(dest)
        return dest
