from __future__ import annotations

from pathlib import Path

LATIN = ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc",
         "/Library/Fonts/Arial Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]
DEVANAGARI = ["/System/Library/Fonts/Kohinoor.ttc", "/System/Library/Fonts/Supplemental/DevanagariMT.ttc",
              "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
              "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf"]


def find_font(script: str = "latin") -> str | None:
    for p in (DEVANAGARI if script == "devanagari" else LATIN):
        if Path(p).is_file():
            return p
    return None
