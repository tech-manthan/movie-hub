"""Keep videos from looking mass-produced: rotate styles, music and formats."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .hashing import sha

KEEP = 50


class Variety:
    def __init__(self, projects_dir: Path):
        self.path = projects_dir / ".variety.json"
        self.history: list[dict] = []
        if self.path.is_file():
            self.history = json.loads(self.path.read_text(encoding="utf-8")).get("history", [])

    def recent(self, field: str, n: int) -> list[str]:
        return [h[field] for h in self.history[-n:] if h.get(field)] if n > 0 else []

    def record(self, **fields) -> None:
        entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields}
        self.history = [h for h in self.history
                        if not (h.get("slug") == fields.get("slug") and h.get("format") == fields.get("format")
                                and h.get("lang") == fields.get("lang"))]
        self.history.append(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"history": self.history[-KEEP:]}, indent=2), encoding="utf-8")

    def pick(self, options: list[str], seed: str, field: str, avoid_last: int = 1) -> str:
        """Deterministic pick that skips values used in the last `avoid_last` videos when it can."""
        if not options:
            raise ValueError("nothing to pick from")
        banned = set(self.recent(field, avoid_last))
        pool = [o for o in options if o not in banned] or options
        return pool[int(sha(f"{seed}:{field}")[:8], 16) % len(pool)]

    def suggest_format(self, formats: list[str], lookback: int = 6) -> str:
        """The format used longest ago (or never) among the last `lookback` videos."""
        recent = self.recent("format", lookback)
        unused = [f for f in formats if f not in recent]
        if unused:
            return unused[0]
        return max(formats, key=lambda f: len(recent) - 1 - max(i for i, r in enumerate(recent) if r == f))
