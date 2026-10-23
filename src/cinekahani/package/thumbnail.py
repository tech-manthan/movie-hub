"""1280x720 thumbnail: artwork, readable big text, poster inset, channel badge."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, features

from ..visuals.fonts import find_font
from ..visuals.styles import Style

W, H = 1280, 720
MAX_BYTES = 2_000_000                    # YouTube's thumbnail limit is 2 MB


def _font(script: str, size: int):
    path = find_font(script)
    try:
        return ImageFont.truetype(path, size) if path else ImageFont.load_default(size)
    except OSError:
        return ImageFont.load_default(size)


def can_render_native() -> bool:
    return bool(features.check("raqm")) and find_font("devanagari") is not None


def _fit(draw: ImageDraw.ImageDraw, text: str, script: str, box_w: int, box_h: int) -> tuple[list[str], ImageFont.FreeTypeFont]:
    words = text.split()
    for size in range(190, 40, -6):
        font = _font(script, size)
        lines, cur = [], ""
        for w in words:
            trial = f"{cur} {w}".strip()
            if cur and draw.textlength(trial, font=font) > box_w:
                lines.append(cur)
                cur = w
            else:
                cur = trial
        lines.append(cur)
        if len(lines) * size * 1.15 <= box_h and all(draw.textlength(x, font=font) <= box_w for x in lines):
            return lines, font
    return [text], _font(script, 40)


def render_thumbnail(out: Path, text: str, *, style: Style, backdrop: Path | None, poster: Path | None,
                     channel: str, subtitle: str = "", layout: str = "left", native: bool = False) -> Path:
    script = "devanagari" if native and can_render_native() else "latin"
    if script == "latin":
        text = text.upper()
    if backdrop and backdrop.is_file():
        with Image.open(backdrop) as src:
            bg = src.convert("RGB")
            s = max(W / bg.width, H / bg.height)
            bg = bg.resize((int(bg.width * s) + 1, int(bg.height * s) + 1), Image.LANCZOS)
            bg = bg.crop(((bg.width - W) // 2, (bg.height - H) // 2, (bg.width - W) // 2 + W, (bg.height - H) // 2 + H))
    else:
        bg = Image.new("RGB", (W, H), style.card_bg)
        d0 = ImageDraw.Draw(bg)
        for y in range(H):
            f = y / H
            d0.line([(0, y), (W, y)], fill=tuple(int(c * (1 - 0.5 * f)) for c in style.card_bg))
    shade = Image.new("L", (W, H), 0)
    ds = ImageDraw.Draw(shade)
    for x in range(W):
        edge = x / W if layout == "left" else 1 - x / W
        ds.line([(x, 0), (x, H)], fill=int(215 * max(0.0, 1 - edge * 1.35)))
    bg = Image.composite(Image.new("RGB", (W, H), (0, 0, 0)), bg, shade)

    d = ImageDraw.Draw(bg)
    margin = 60
    poster_w = 0
    if poster and poster.is_file():
        with Image.open(poster) as p:
            p = p.convert("RGB")
            ph = 520
            p = p.resize((int(p.width * ph / p.height), ph), Image.LANCZOS)
            poster_w = p.width + 40
            px = W - margin - p.width if layout == "left" else margin
            frame = Image.new("RGB", (p.width + 12, p.height + 12), style.accent)
            frame.paste(p, (6, 6))
            shadow = Image.new("RGBA", (frame.width + 40, frame.height + 40), (0, 0, 0, 0))
            ImageDraw.Draw(shadow).rectangle((20, 24, frame.width + 20, frame.height + 24), fill=(0, 0, 0, 150))
            shadow = shadow.filter(ImageFilter.GaussianBlur(14))
            bg.paste(shadow, (px - 20, 70 - 14), shadow)
            bg.paste(frame, (px, 70))
    box_w = W - margin * 2 - poster_w - 40
    tx = margin if layout == "left" else W - margin - box_w
    lines, font = _fit(d, text, script, box_w, 460)
    y = 130
    for ln in lines:
        for dx, dy in ((-4, 0), (4, 0), (0, -4), (0, 4), (3, 3)):
            d.text((tx + dx, y + dy), ln, font=font, fill=(0, 0, 0))
        d.text((tx, y), ln, font=font, fill=(255, 255, 255) if lines.index(ln) % 2 == 0 else style.accent)
        y += int(font.size * 1.15)
    if subtitle:
        f2 = _font("latin", 44)
        d.text((tx, H - 165), subtitle, font=f2, fill=style.accent)
    f3 = _font("latin", 34)
    d.rounded_rectangle((margin - 14, H - 84, margin + d.textlength(channel.upper(), font=f3) + 14, H - 30), 10, fill=style.accent)
    d.text((margin, H - 80), channel.upper(), font=f3, fill=(0, 0, 0))

    out.parent.mkdir(parents=True, exist_ok=True)
    for q in (90, 82, 74, 66, 58):
        bg.save(out, "JPEG", quality=q, optimize=True)
        if out.stat().st_size <= MAX_BYTES:
            break
    return out
