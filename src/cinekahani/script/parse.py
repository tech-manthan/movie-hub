"""Script file = YAML front matter + markdown body. Humans edit the body freely."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

VERIFY_RE = re.compile(r"\[VERIFY:\s*([^\]]*)\]")
CUE_RE = re.compile(r"^\s*\[VISUAL:\s*([^\]]*)\]\s*$", re.M)
HEAD_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)


@dataclass
class Section:
    heading: str
    text: str                      # narration only, cues removed
    cues: list[str] = field(default_factory=list)


def split_front_matter(text: str) -> tuple[dict, str]:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return yaml.safe_load(text[4:end]) or {}, text[end + 5:]
    return {}, text


def render(meta: dict, body: str) -> str:
    fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{fm}\n---\n{body.strip()}\n"


def read_script(path: Path) -> tuple[dict, str]:
    return split_front_matter(path.read_text(encoding="utf-8"))


def sections(body: str) -> list[Section]:
    parts = HEAD_RE.split(body)
    out: list[Section] = []
    for i in range(1, len(parts), 2):
        raw = parts[i + 1]
        cues = [c.strip() for c in CUE_RE.findall(raw)]
        narration = CUE_RE.sub("", raw)
        narration = VERIFY_RE.sub(lambda m: m.group(1), narration)
        narration = re.sub(r"\n{3,}", "\n\n", narration).strip()
        out.append(Section(parts[i].strip(), narration, cues))
    return out


def word_count(body: str) -> int:
    return sum(len(s.text.split()) for s in sections(body))


def verify_tags(body: str) -> list[str]:
    return [m.strip() for m in VERIFY_RE.findall(body)]
