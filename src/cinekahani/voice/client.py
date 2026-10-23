"""Client for a locally running VoiceStudio backend (kept as a separate service)."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..core.config import VoiceCfg
from ..core.logs import get_logger
from ..core.retry import retry_call

log = get_logger("voice")


class VoiceError(RuntimeError):
    pass


class _Retryable(VoiceError):
    pass


@dataclass
class Speech:
    wav: bytes
    audio_id: str = ""
    seconds: float | None = None


class VoiceStudioClient:
    def __init__(self, cfg: VoiceCfg, env: dict[str, str] | None = None, *,
                 transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        env = env or {}
        headers = {}
        if env.get("OMNIVOICE_API_KEY"):
            headers["Authorization"] = f"Bearer {env['OMNIVOICE_API_KEY']}"
        self.cfg = cfg
        self.sleep = sleep
        self.http = httpx.Client(base_url=cfg.base_url.rstrip("/"), headers=headers,
                                 timeout=cfg.timeout_s, transport=transport)

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        def once() -> httpx.Response:
            try:
                r = self.http.request(method, path, **kw)
            except httpx.ConnectError as exc:
                raise VoiceError(f"Cannot reach VoiceStudio at {self.cfg.base_url}. Open the VoiceStudio "
                                 "app (or start its backend) and try again.") from exc
            except httpx.TransportError as exc:
                raise _Retryable(f"network error: {exc}") from exc
            if r.status_code >= 500:
                raise _Retryable(f"VoiceStudio returned {r.status_code}: {r.text[:300]}")
            if r.status_code >= 400:
                raise VoiceError(f"VoiceStudio rejected {method} {path} ({r.status_code}): {r.text[:300]}")
            return r

        return retry_call(once, attempts=self.cfg.retries + 1, base_delay=self.cfg.retry_delay_s,
                          retry_on=(_Retryable,), sleep=self.sleep, what=f"VoiceStudio {path}")

    def profiles(self) -> list[dict]:
        data = self._request("GET", "/profiles").json()
        return data if isinstance(data, list) else data.get("profiles", [])

    def engines(self) -> list[dict]:
        data = self._request("GET", "/engines/tts").json()
        return data if isinstance(data, list) else data.get("engines", [])

    def create_clone(self, name: str, wav_path: Path, ref_text: str = "", language: str = "Auto") -> str:
        with open(wav_path, "rb") as f:
            r = self._request("POST", "/profiles", data={
                "name": name, "ref_text": ref_text, "language": language, "kind": "clone"},
                files={"ref_audio": (Path(wav_path).name, f.read(), "audio/wav")})
        body = r.json()
        pid = body.get("id") or body.get("profile_id")
        if not pid:
            raise VoiceError(f"VoiceStudio created a profile but returned no id: {body}")
        return str(pid)

    def generate(self, text: str, *, language: str, profile_id: str) -> Speech:
        form = {"text": text, "language": language, "profile_id": profile_id,
                "speed": str(self.cfg.speed), "seed": str(self.cfg.seed), "wav_bits": "16"}
        if self.cfg.engine:
            form["engine"] = self.cfg.engine
        r = self._request("POST", "/generate", data=form)
        if not r.content:
            raise VoiceError("VoiceStudio returned empty audio")
        dur = r.headers.get("X-Audio-Duration")
        try:
            seconds = float(dur) if dur else None
        except ValueError:
            seconds = None
        return Speech(r.content, r.headers.get("X-Audio-Id", ""), seconds)
