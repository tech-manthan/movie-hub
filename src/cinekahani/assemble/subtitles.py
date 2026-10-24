"""Burned-in subtitles from the narration timeline (script text + chunk-exact timing)."""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..core.config import Settings
from ..visuals.styles import Style

MIN_SHOW = 0.9
MAX_LINES = 2


def wrap(text: str, width: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        lines.append(cur)
    return lines


def events(timeline: dict, width: int, offset: float) -> list[tuple[float, float, str]]:
    out: list[tuple[float, float, str]] = []
    for sec in timeline["sections"]:
        for para in sec["paragraphs"]:
            for sent in para["sentences"]:
                lines = wrap(sent["text"], width)
                groups = [lines[i:i + MAX_LINES] for i in range(0, len(lines), MAX_LINES)]
                total = sum(len(" ".join(g)) for g in groups) or 1
                t = sent["start"]
                span = sent["end"] - sent["start"]
                for gi, g in enumerate(groups):
                    chars = len(" ".join(g))
                    end = sent["end"] if gi == len(groups) - 1 else t + span * chars / total
                    out.append((t + offset, end + offset, "\\N".join(g)))
                    t = end
    # hold very short lines a little longer, never past the next line
    fixed = []
    for i, (s, e, text) in enumerate(out):
        nxt = out[i + 1][0] if i + 1 < len(out) else e + MIN_SHOW
        fixed.append((s, max(e, min(s + MIN_SHOW, nxt - 0.02)), text))
    return fixed


def _ts(t: float) -> str:
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def build_ass(timeline: dict, settings: Settings, style: Style, lang: str, offset: float) -> str:
    sub = settings.video.subtitles
    k = settings.video.height / 1080          # sizes below are designed for 1080p
    border = 3 if style.sub_box else 1
    back = "&H99000000" if style.sub_box else "&H00000000"
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {settings.video.width}
PlayResY: {settings.video.height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{sub.font.get(lang, 'Arial')},{round(sub.size * k)},{style.sub_primary},&H000000FF,{style.sub_outline},{back},-1,0,0,0,100,100,0,0,{border},{max(1, round(3 * k))},0,2,{round(120 * k)},{round(120 * k)},{round(90 * k)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    width = sub.max_chars.get(lang, 40)
    body = "".join(f"Dialogue: 0,{_ts(s)},{_ts(e)},Default,,0,0,0,,{text.replace('{', '(').replace('}', ')')}\n"
                   for s, e, text in events(timeline, width, offset))
    return head + body


def write_ass(path: Path, timeline: dict, settings: Settings, style: Style, lang: str, offset: float) -> Path:
    path.write_text(build_ass(timeline, settings, style, lang, offset), encoding="utf-8")
    return path


# ---- fallback: draw subtitles ourselves when ffmpeg has no libass ('subtitles' filter) ----

def _ass_rgba(ass: str) -> tuple[int, int, int, int]:
    h = ass.strip("&Hh")
    h = h.rjust(8, "0")
    a, b, g, r = (int(h[i:i + 2], 16) for i in (0, 2, 4, 6))
    return r, g, b, 255 - a


def overlay_ready(lang: str) -> str | None:
    """None when this machine can draw `lang` subtitles itself, else a message saying what is missing."""
    from PIL import features
    from ..visuals.fonts import find_font
    if lang == "hi":
        if find_font("devanagari") is None:
            return "No Devanagari font found for Hindi subtitles."
        if not features.check("raqm"):
            return ("Pillow cannot shape Devanagari without libraqm (letters would come out wrong). "
                    "Try `brew install fribidi harfbuzz` and `pip install --force-reinstall pillow`, "
                    "or install an ffmpeg that has libass.")
    return None


def _draw_event(text: str, out: Path, settings: Settings, style: Style, lang: str) -> None:
    from PIL import Image, ImageDraw
    from ..visuals.fonts import load_font
    v, k = settings.video, settings.video.height / 1080
    font = load_font("devanagari" if lang == "hi" else "latin", max(10, round(settings.video.subtitles.size * k)))
    img = Image.new("RGBA", (v.width, v.height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    lines = text.split("\\N")
    gap = round(font.size * 0.28)
    heights = [d.textbbox((0, 0), ln, font=font)[3] for ln in lines]
    total = sum(heights) + gap * (len(lines) - 1)
    y = v.height - round(90 * k) - total
    fill, stroke = _ass_rgba(style.sub_primary), _ass_rgba(style.sub_outline)
    widths = [d.textlength(ln, font=font) for ln in lines]
    if style.sub_box:
        pad = round(14 * k)
        d.rounded_rectangle((v.width / 2 - max(widths) / 2 - pad, y - pad, v.width / 2 + max(widths) / 2 + pad,
                             y + total + pad), round(10 * k), fill=(0, 0, 0, 150))
    for ln, w, h in zip(lines, widths, heights):
        d.text(((v.width - w) / 2, y), ln, font=font, fill=fill, stroke_width=max(1, round(3 * k)), stroke_fill=stroke)
        y += h + gap
    img.save(out, "PNG")


def write_overlay(timeline: dict, settings: Settings, style: Style, lang: str, offset: float, total: float,
                  folder: Path) -> Path:
    """Transparent PNG per subtitle + an ffmpeg concat list that covers 0..total. Returns the list file."""
    problem = overlay_ready(lang)
    if problem:
        raise RuntimeError(problem)
    from PIL import Image
    folder.mkdir(parents=True, exist_ok=True)
    blank = folder / "blank.png"
    if not blank.is_file():
        Image.new("RGBA", (settings.video.width, settings.video.height), (0, 0, 0, 0)).save(blank, "PNG")
    sub = settings.video.subtitles
    evs = events(timeline, sub.max_chars.get(lang, 40), offset)
    lines, t = ["ffconcat version 1.0"], 0.0
    last = blank
    for s, e, text in evs:
        s, e = max(s, t), min(e, total)
        if e <= s:
            continue
        key = hashlib.sha1(f"{text}|{settings.video.width}x{settings.video.height}|{sub.size}|{style.key}|{lang}"
                           .encode()).hexdigest()[:16]
        png = folder / f"{key}.png"
        if not png.is_file():
            _draw_event(text, png, settings, style, lang)
        if s > t:
            lines += [f"file '{blank.resolve()}'", f"duration {s - t:.3f}"]
        lines += [f"file '{png.resolve()}'", f"duration {e - s:.3f}"]
        t, last = e, png
    if total > t:
        lines += [f"file '{blank.resolve()}'", f"duration {total - t:.3f}"]
        last = blank
    lines.append(f"file '{last.resolve()}'")          # concat demuxer quirk: repeat the final file without a duration
    out = folder / "subs.ffconcat"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
