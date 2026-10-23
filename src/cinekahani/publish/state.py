"""publish.json per project: when each approved item should go out, and what happened."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..core.project import Project


class ScheduleError(ValueError):
    pass


def parse_when(text: str, tz: str, *, now: datetime | None = None, min_lead_minutes: int = 20) -> datetime:
    """'2026-11-03 18:00' in timezone tz -> aware UTC datetime. Must be in the future."""
    try:
        zone = ZoneInfo(tz)
    except ZoneInfoNotFoundError as exc:
        raise ScheduleError(f"Unknown timezone '{tz}' (example: Asia/Kolkata)") from exc
    try:
        local = datetime.fromisoformat(text.strip().replace("T", " "))
    except ValueError as exc:
        raise ScheduleError("Use the form '2026-11-03 18:00'") from exc
    when = (local if local.tzinfo else local.replace(tzinfo=zone)).astimezone(timezone.utc)
    now = now or datetime.now(timezone.utc)
    if when < now + timedelta(minutes=min_lead_minutes):
        raise ScheduleError(f"{text} is not at least {min_lead_minutes} minutes in the future")
    return when


class PublishState:
    def __init__(self, project: Project):
        self.path = project.root / "publish.json"
        self.data: dict = {"items": {}}
        if self.path.is_file():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            self.data.setdefault("items", {})

    def item(self, item_id: str) -> dict:
        return self.data["items"].setdefault(item_id, {"results": {}})

    def get(self, item_id: str) -> dict:
        return self.data["items"].get(item_id, {"results": {}})

    def schedule(self, item_id: str, when_utc: datetime, tz: str, targets: list[str]) -> None:
        it = self.item(item_id)
        it["schedule"] = {"at": when_utc.isoformat(timespec="seconds"), "tz": tz, "targets": targets}
        self.save()

    def record(self, item_id: str, target: str, status: str, **info) -> None:
        self.item(item_id)["results"][target] = {
            "status": status, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **info}
        self.save()

    def done(self, item_id: str, target: str) -> bool:
        return self.get(item_id)["results"].get(target, {}).get("status") in ("uploaded", "scheduled")

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)
