"""Optional trailer snippets. Supporting footage only: short, capped, never continuous."""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from ..core.config import Settings
from ..core.logs import get_logger
from ..core.project import Project

log = get_logger("trailer")
NOTICE = ("Downloading a YouTube video is against YouTube's Terms of Service, and trailers are "
          "copyrighted. Cine Kahani only uses short supporting snippets (see guardrails in config), "
          "but the download itself is your decision and your risk.")


class TrailerError(RuntimeError):
    pass


def source_path(project: Project) -> Path:
    return project.dir("visuals") / "trailer" / "source.mp4"


def fetch_trailer(settings: Settings, project: Project, youtube_id: str | None, *, runner=subprocess.run) -> Path:
    dest = source_path(project)
    if dest.is_file():
        return dest
    if not settings.guardrails.trailer.enabled:
        raise TrailerError("Trailer footage is switched off (guardrails.trailer.enabled: false).")
    if not youtube_id or youtube_id.startswith("SAMPLE"):
        raise TrailerError("TMDB lists no trailer for this title (or it is sample data).")
    cmd = list(settings.video.trailer_download_command)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd += [str(dest), f"https://www.youtube.com/watch?v={youtube_id}"]
    try:
        p = runner(cmd, capture_output=True, text=True, timeout=900)
    except FileNotFoundError as exc:
        raise TrailerError(f"'{cmd[0]}' is not installed (brew install yt-dlp).") from exc
    except subprocess.TimeoutExpired as exc:
        raise TrailerError("Trailer download timed out") from exc
    if p.returncode != 0 or not dest.is_file():
        raise TrailerError(f"Trailer download failed: {(p.stderr or '').strip()[-300:]}")
    return dest


def trailer_length(src: Path, *, runner=subprocess.run) -> float:
    p = runner(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)],
               capture_output=True, text=True)
    try:
        return float(p.stdout.strip())
    except ValueError as exc:
        raise TrailerError(f"Cannot read the length of {src.name}") from exc


def pick_moments(count: int, length: float, snippet: float, overrides: list[float] | None = None) -> list[float]:
    """Start times in the trailer. Trailers roughly follow the story, so spread them evenly in order.
    `overrides` (from visuals/trailer_moments.yaml) win, in order, when you want exact moments."""
    latest = max(0.0, length - snippet)
    chosen = [min(max(0.0, t), latest) for t in (overrides or [])][:count]
    rest = count - len(chosen)
    if rest > 0 and length > snippet:
        lo, hi = length * 0.08, max(length * 0.08, length * 0.92 - snippet)
        step = (hi - lo) / rest
        chosen += [round(lo + step * (i + 0.5), 2) for i in range(rest)]
    elif rest > 0:
        chosen += [0.0] * rest
    return chosen


def load_overrides(project: Project) -> list[float]:
    p = project.dir("visuals") / "trailer_moments.yaml"
    if not p.is_file():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return [float(x) for x in data.get("moments", [])]
