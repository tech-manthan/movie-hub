"""Per-project manifest so re-runs skip steps whose inputs did not change.

Each step records the hash of its inputs and of the output it wrote. On the next
run `decide` says whether to run, reuse the output, or protect hand edits.
"""
from __future__ import annotations

import enum
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


class Decision(enum.Enum):
    RUN = "run"                    # nothing cached, or inputs changed and no edits to lose
    CACHED = "cached"              # same inputs, output untouched
    EDITED_KEPT = "edited_kept"    # same inputs, but you edited the output: keep it
    EDITED_BLOCKED = "edited_blocked"  # inputs changed AND you edited the output: needs --force


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Manifest:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"steps": {}}
        if path.is_file():
            self.data = json.loads(path.read_text(encoding="utf-8"))
            self.data.setdefault("steps", {})

    def get(self, key: str) -> dict | None:
        return self.data["steps"].get(key)

    def record(self, key: str, input_hash: str, output_hash: str, outputs: list[str],
               **extra) -> None:
        self.data["steps"][key] = {
            "input_hash": input_hash, "output_hash": output_hash, "outputs": outputs,
            "at": _now().isoformat(timespec="seconds"), **extra,
        }
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def decide(self, key: str, input_hash: str, current_output_hash: str | None, *,
               force: bool = False, max_age_days: int | None = None) -> Decision:
        """current_output_hash is None when the output file does not exist."""
        entry = self.get(key)
        if force or entry is None or current_output_hash is None:
            return Decision.RUN
        edited = current_output_hash != entry["output_hash"]
        if entry["input_hash"] == input_hash:
            if max_age_days is not None and not edited:
                age = _now() - datetime.fromisoformat(entry["at"])
                if age > timedelta(days=max_age_days):
                    return Decision.RUN
            return Decision.EDITED_KEPT if edited else Decision.CACHED
        return Decision.EDITED_BLOCKED if edited else Decision.RUN
