"""Royalty-free music library with a license log. Only tracks with a known safe license are used."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..core.config import MusicCfg
from ..core.variety import Variety

AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
MOODS = {"review": ["upbeat", "calm"], "ending_explained": ["tense", "dramatic"],
         "things_you_missed": ["mysterious", "tense"], "cast_facts": ["upbeat", "calm"],
         "should_you_watch": ["upbeat"], "comparison": ["upbeat", "dramatic"]}


class MusicError(ValueError):
    pass


@dataclass
class Track:
    file: str
    title: str = ""
    artist: str = ""
    source: str = ""
    license: str = ""
    credit: str = ""
    mood: str = ""


def log_path(music_dir: Path) -> Path:
    return music_dir / "licenses.yaml"


def load_tracks(music_dir: Path) -> list[Track]:
    p = log_path(music_dir)
    if not p.is_file():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return [Track(**t) for t in data.get("tracks", [])]


def _save(music_dir: Path, tracks: list[Track]) -> None:
    log_path(music_dir).write_text(yaml.safe_dump(
        {"tracks": [t.__dict__ for t in tracks]}, sort_keys=False, allow_unicode=True), encoding="utf-8")


def normalize_license(text: str) -> str:
    return text.strip().lower().replace(" ", "-").replace("_", "-")


def check_license(lic: str, credit: str, cfg: MusicCfg) -> str:
    lic = normalize_license(lic)
    if lic not in cfg.allowed_licenses:
        raise MusicError(f"License '{lic}' is not allowed. Allowed: {', '.join(cfg.allowed_licenses)}. "
                         "NonCommercial (NC) and NoDerivatives (ND) tracks are not safe on a monetized channel.")
    if lic == "cc-by" and not credit.strip():
        raise MusicError("CC-BY tracks need a credit line (--credit), because it goes into the description.")
    return lic


def add_track(music_dir: Path, src: Path, cfg: MusicCfg, *, license: str, source: str = "", title: str = "",
              artist: str = "", credit: str = "", mood: str = "") -> Track:
    if src.suffix.lower() not in AUDIO_EXT:
        raise MusicError(f"{src.name} is not an audio file ({', '.join(sorted(AUDIO_EXT))})")
    if not src.is_file():
        raise MusicError(f"{src} does not exist")
    lic = check_license(license, credit, cfg)
    if not source.strip() and lic != "own":
        raise MusicError("Say where you got it (--source URL) so the license can be checked later.")
    music_dir.mkdir(parents=True, exist_ok=True)
    dest = music_dir / src.name
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    tracks = [t for t in load_tracks(music_dir) if t.file != dest.name]
    track = Track(dest.name, title or src.stem, artist, source, lic, credit, mood.lower())
    _save(music_dir, tracks + [track])
    return track


def eligible(music_dir: Path, cfg: MusicCfg) -> list[Track]:
    out = []
    for t in load_tracks(music_dir):
        try:
            check_license(t.license, t.credit, cfg)
        except MusicError:
            continue
        if (music_dir / t.file).is_file():
            out.append(t)
    return out


def choose_track(music_dir: Path, cfg: MusicCfg, variety: Variety, seed: str, fmt: str) -> Track | None:
    """Prefer a track matching the format's mood that was not used lately; relax in that order."""
    tracks = eligible(music_dir, cfg)
    if not tracks:
        return None
    recent = set(variety.recent("music", cfg.avoid_recent))
    moody = [t for t in tracks if t.mood in MOODS.get(fmt, [])]
    for pool in ([t for t in moody if t.file not in recent], [t for t in tracks if t.file not in recent],
                 moody, tracks):
        if pool:
            name = variety.pick([t.file for t in pool], seed, "music", 0)
            return next(t for t in pool if t.file == name)
    return None


def credit_line(track: Track | None) -> str:
    if track is None:
        return ""
    if track.credit.strip():
        return track.credit.strip()
    if track.license == "cc-by":
        return f"Music: {track.title} by {track.artist}"
    return ""
