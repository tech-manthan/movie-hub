from __future__ import annotations

from pathlib import Path

LATIN = ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc",
         "/Library/Fonts/Arial Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]
DEVANAGARI = ["/System/Library/Fonts/Supplemental/DevanagariMT.ttc", "/System/Library/Fonts/Kohinoor.ttc",
              "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
              "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf"]


def find_font(script: str = "latin") -> str | None:
    for p in (DEVANAGARI if script == "devanagari" else LATIN):
        if Path(p).is_file():
            return p
    return None


def load_font(script: str, size: int):
    """A Pillow font for the script. In .ttc collections (Kohinoor.ttc has Bangla, Devanagari, Telugu...) pick the
    face whose name matches the script."""
    from PIL import ImageFont
    path = find_font(script)
    if not path:
        return ImageFont.load_default(size)
    want = "devanagari" if script == "devanagari" else ""
    for index in range(12):
        try:
            f = ImageFont.truetype(path, size, index=index)
        except OSError:
            break
        family = " ".join(f.getname()).lower()
        if not want or want in family:
            return f
    return ImageFont.truetype(path, size)
