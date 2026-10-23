"""Small WAV helpers on the stdlib `wave` module (16-bit PCM)."""
from __future__ import annotations

import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Fmt:
    channels: int
    width: int
    rate: int


def read(path: Path) -> tuple[Fmt, bytes]:
    with wave.open(str(path), "rb") as w:
        return Fmt(w.getnchannels(), w.getsampwidth(), w.getframerate()), w.readframes(w.getnframes())


def duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def silence(ms: int, fmt: Fmt) -> bytes:
    return b"\0" * (int(fmt.rate * ms / 1000) * fmt.channels * fmt.width)


def frames_seconds(data: bytes, fmt: Fmt) -> float:
    return len(data) / (fmt.channels * fmt.width * fmt.rate)


def write(path: Path, fmt: Fmt, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(fmt.channels)
        w.setsampwidth(fmt.width)
        w.setframerate(fmt.rate)
        w.writeframes(data)


def conform(path: Path, fmt: Fmt, out: Path) -> Path:
    """Re-encode `path` to `fmt` with ffmpeg (used when chunks come back in different formats)."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-ar", str(fmt.rate),
           "-ac", str(fmt.channels), "-c:a", "pcm_s16le", str(out)]
    subprocess.run(cmd, check=True, capture_output=True)
    return out
