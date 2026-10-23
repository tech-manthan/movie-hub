"""Publishing. Only approved, unchanged items go out, and only with an explicit schedule or --now."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..core.config import Settings
from ..core.logs import get_logger
from ..core.project import Project
from ..review import queue
from .state import PublishState
from .uploadpost import UploadPostClient, UploadPostError
from .youtube import YoutubeError, YoutubeUploader

log = get_logger("publish")


class PublishError(RuntimeError):
    pass


@dataclass
class Providers:
    youtube: YoutubeUploader | None = None
    uploadpost: UploadPostClient | None = None


@dataclass
class Outcome:
    item: str
    target: str
    status: str                 # uploaded | scheduled | skipped | failed | dry-run
    detail: str = ""
    info: dict = field(default_factory=dict)


def targets_for(settings: Settings, item: queue.Item) -> list[str]:
    if item.kind == "long":
        return ["youtube"]
    return ["youtube"] if settings.publish.shorts_via == "youtube" else ["uploadpost"]


def publish_item(settings: Settings, project: Project, item_id: str, providers: Providers, *,
                 now: bool = False, public: bool = False, dry_run: bool = False) -> list[Outcome]:
    item = queue.require_approved(settings, project, item_id)          # the approval gate
    state = PublishState(project)
    sched = state.get(item_id).get("schedule")
    if not sched and not now:
        raise PublishError(f"{item_id} has no schedule. Set one with `cinekahani publish schedule`, "
                           "or publish immediately with --now.")
    when = datetime.fromisoformat(sched["at"]) if sched else None
    if when and when <= datetime.now(timezone.utc):
        raise PublishError(f"The scheduled time {sched['at']} has passed. Pick a new time with `publish schedule`.")
    video = project.root / item.video
    outcomes: list[Outcome] = []
    for target in targets_for(settings, item):
        if state.done(item_id, target):
            outcomes.append(Outcome(item_id, target, "skipped", "already uploaded"))
            continue
        if dry_run:
            outcomes.append(Outcome(item_id, target, "dry-run", f"would upload {item.title!r} at {when or 'now'}"))
            continue
        try:
            if target == "youtube":
                outcomes.append(_to_youtube(settings, project, item, video, providers, state, when, public))
            else:
                outcomes.append(_to_uploadpost(item, video, providers, state, when, public))
        except (YoutubeError, UploadPostError) as exc:
            state.record(item_id, target, "failed", error=str(exc))
            outcomes.append(Outcome(item_id, target, "failed", str(exc)))
    return outcomes


def _to_youtube(settings, project, item, video, providers, state, when, public) -> Outcome:
    if providers.youtube is None:
        raise YoutubeError("YouTube is not configured (run `cinekahani youtube auth`).")
    res = providers.youtube.upload(video, title=item.title, description=item.description, tags=item.tags,
                                   language=item.lang, publish_at=when, public=public)
    vid = res.get("id", "")
    warn = ""
    if item.thumbnail and vid:
        try:
            providers.youtube.set_thumbnail(vid, project.root / item.thumbnail)
        except YoutubeError as exc:
            warn = str(exc)
            log.warning(warn)
    status = "scheduled" if when else "uploaded"
    state.record(item.id, "youtube", status, video_id=vid, url=f"https://youtu.be/{vid}",
                 publish_at=when.isoformat(timespec="seconds") if when else None, thumbnail_warning=warn,
                 privacy=res.get("status", {}).get("privacyStatus"))
    return Outcome(item.id, "youtube", status, f"https://youtu.be/{vid}", {"video_id": vid})


def _to_uploadpost(item, video, providers, state, when, public) -> Outcome:
    if providers.uploadpost is None:
        raise UploadPostError("Upload-Post is not configured (UPLOADPOST_API_KEY).")
    res = providers.uploadpost.upload(video, title=item.title, description=item.description, when=when, public=public)
    status = "scheduled" if when else "uploaded"
    state.record(item.id, "uploadpost", status, response=res,
                 publish_at=when.isoformat(timespec="seconds") if when else None)
    return Outcome(item.id, "uploadpost", status, json.dumps(res)[:200])


def due_items(settings: Settings, project: Project) -> list[queue.Item]:
    """Approved items that have a schedule but have not been uploaded to every target yet."""
    state = PublishState(project)
    out = []
    for it in queue.discover(settings, project):
        if it.status == "approved" and state.get(it.id).get("schedule") and \
                not all(state.done(it.id, t) for t in targets_for(settings, it)):
            out.append(it)
    return out


def run_due(settings: Settings, projects: list[Project], providers: Providers, *, dry_run: bool = False) -> list[Outcome]:
    """Upload every approved, scheduled item (the platforms hold them until the scheduled time)."""
    results: list[Outcome] = []
    for project in projects:
        for it in due_items(settings, project):
            try:
                results += publish_item(settings, project, it.id, providers, dry_run=dry_run)
            except (PublishError, queue.ReviewError) as exc:
                results.append(Outcome(it.id, "-", "failed", str(exc)))
    return results
