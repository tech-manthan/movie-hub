"""Burned-in subtitles from the narration timeline (script text + chunk-exact timing)."""
from __future__ import annotations

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
