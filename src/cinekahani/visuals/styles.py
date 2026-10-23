"""Visual styles. Rotated per video so the channel does not look mass-produced."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Style:
    key: str
    grade: str                 # ffmpeg filter chain applied to every shot
    shot_scale: float          # multiplies configured shot length (<1 = faster cutting)
    transition: str            # "cut" or "dip" (short fade through black)
    card_bg: tuple[int, int, int]
    card_fg: tuple[int, int, int]
    accent: tuple[int, int, int]
    sub_primary: str           # ASS colours are &HAABBGGRR
    sub_outline: str
    sub_box: bool
    poster_card_every: int     # use a poster-on-blur card about every N shots
    motions: tuple[str, ...]


STYLES: dict[str, Style] = {s.key: s for s in [
    Style("noir", "eq=saturation=0.8:contrast=1.1:brightness=-0.03,vignette=PI/5", 1.15, "dip",
          (12, 12, 16), (240, 240, 240), (200, 50, 50), "&H00FFFFFF", "&H00000000", False, 6,
          ("in", "out", "left")),
    Style("pop", "eq=saturation=1.3:contrast=1.08", 0.8, "cut",
          (20, 20, 60), (255, 255, 255), (255, 200, 0), "&H0000E0FF", "&H00201010", False, 4,
          ("in", "left", "right", "out")),
    Style("clean", "eq=saturation=1.0:contrast=1.03", 1.0, "cut",
          (245, 245, 245), (20, 20, 20), (30, 90, 200), "&H00FFFFFF", "&H00000000", True, 5,
          ("in", "out", "right")),
    Style("warm", "colorbalance=rs=0.08:gs=0.02:bs=-0.06,eq=saturation=1.1", 1.0, "dip",
          (40, 24, 16), (255, 240, 220), (255, 150, 60), "&H00D0F0FF", "&H00102030", False, 5,
          ("out", "right", "in")),
]}


def get_style(key: str) -> Style:
    if key not in STYLES:
        raise KeyError(f"Unknown style '{key}'. Choose one of: {', '.join(STYLES)}")
    return STYLES[key]
