"""Publishing. Only approved, unchanged items go out, and only with a schedule or an explicit --now.

YouTube holds a scheduled upload until its time (publishAt). Instagram cannot, so a scheduled Reel is
posted by `cinekahani publish run` once its time has come; run that every few minutes (see README).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..core.config import Settings
from ..core.logs import get_logger
from ..core.project import Project
from ..review import queue
from .instagram import InstagramClient, InstagramError, normalize_for_upload
from .state import PublishState
from .youtube import YoutubeError, YoutubeUploader

log = get_logger("publish")
ERRORS = (YoutubeError, InstagramError)


class PublishError(RuntimeError):
    pass


@dataclass
class Providers:
    youtube: YoutubeUploader | None = None
    instagram: InstagramClient | None = None


@dataclass
class Outcome:
    item: str
    target: str
    status: str                 # uploaded | scheduled | waiting | skipped | failed | dry-run
    detail: str = ""
    info: dict = field(default_factory=dict)


def targets_for(settings: Settings, item: queue.Item) -> list[str]:
    if item.kind == "long":
        return ["youtube"]
    return [t for t in settings.publish.shorts_targets if t in ("youtube", "instagram")]


def publish_item(settings: Settings, project: Project, item_id: str, providers: Providers, *,
                 now: bool = False, public: bool = False, dry_run: bool = False,
                 clock=lambda: datetime.now(timezone.utc)) -> list[Outcome]:
    item = queue.require_approved(settings, project, item_id)          # the approval gate
    state = PublishState(project)
    sched = state.get(item_id).get("schedule")
    if not sched and not now:
        raise PublishError(f"{item_id} has no schedule. Set one with `cinekahani publish schedule`, "
                           "or publish immediately with --now.")
    when = datetime.fromisoformat(sched["at"]) if sched else None
    due = when is None or when <= clock()
    video = project.root / item.video
    outcomes: list[Outcome] = []
    for target in targets_for(settings, item):
        if state.done(item_id, target):
            outcomes.append(Outcome(item_id, target, "skipped", "already uploaded"))
            continue
        if target == "instagram" and not due:
            outcomes.append(Outcome(item_id, target, "waiting", f"posts at {when.isoformat(timespec='minutes')} UTC "
                                                                 "(run `publish run` again then)"))
            continue
        if target == "instagram" and not sched and not public:
            outcomes.append(Outcome(item_id, target, "skipped", "Instagram has no private mode; add --public to post now"))
            continue
        if dry_run:
            outcomes.append(Outcome(item_id, target, "dry-run", f"would upload {item.title!r} at {when or 'now'}"))
            continue
        try:
            if target == "youtube":
                outcomes.append(_to_youtube(project, item, video, providers, state, when if not due else None,
                                            public or bool(sched)))
            else:
                outcomes.append(_to_instagram(project, item, video, providers, state))
        except ERRORS as exc:
            state.record(item_id, target, "failed", error=str(exc))
            outcomes.append(Outcome(item_id, target, "failed", str(exc)))
    return outcomes


def _to_youtube(project, item, video, providers, state, publish_at, public) -> Outcome:
    if providers.youtube is None:
        raise YoutubeError("YouTube is not configured (run `cinekahani youtube auth`).")
    res = providers.youtube.upload(video, title=item.title, description=item.description, tags=item.tags,
                                   language=item.lang, publish_at=publish_at, public=public)
    vid = res.get("id", "")
    warn = ""
    if item.thumbnail and vid:
        try:
            providers.youtube.set_thumbnail(vid, project.root / item.thumbnail)
        except YoutubeError as exc:
            warn = str(exc)
            log.warning(warn)
    status = "scheduled" if publish_at else "uploaded"
    state.record(item.id, "youtube", status, video_id=vid, url=f"https://youtu.be/{vid}",
                 publish_at=publish_at.isoformat(timespec="seconds") if publish_at else None, thumbnail_warning=warn,
                 privacy=res.get("status", {}).get("privacyStatus"))
    return Outcome(item.id, "youtube", status, f"https://youtu.be/{vid}", {"video_id": vid})


def instagram_caption(item: queue.Item) -> str:
    return item.description.replace("#Shorts", "").replace("  ", " ").strip()


def _to_instagram(project, item, video, providers, state) -> Outcome:
    if providers.instagram is None:
        raise InstagramError("Instagram is not configured (run `cinekahani instagram setup`).")
    ready = normalize_for_upload(video, video.parent / "upload" / video.name)
    res = providers.instagram.publish_reel(ready, instagram_caption(item))
    state.record(item.id, "instagram", "uploaded", media_id=res["media_id"], url=res.get("permalink"))
    return Outcome(item.id, "instagram", "uploaded", res.get("permalink") or res["media_id"], res)


def due_items(settings: Settings, project: Project) -> list[queue.Item]:
    """Approved items with a schedule that are not yet up on every target."""
    state = PublishState(project)
    out = []
    for it in queue.discover(settings, project):
        if it.status == "approved" and state.get(it.id).get("schedule") and \
                not all(state.done(it.id, t) for t in targets_for(settings, it)):
            out.append(it)
    return out


def run_due(settings: Settings, projects: list[Project], providers: Providers, *, dry_run: bool = False,
            clock=lambda: datetime.now(timezone.utc)) -> list[Outcome]:
    """Upload what is ready: YouTube items (held by YouTube until their time) and Instagram items whose time has come."""
    results: list[Outcome] = []
    for project in projects:
        for it in due_items(settings, project):
            try:
                results += publish_item(settings, project, it.id, providers, dry_run=dry_run, clock=clock)
            except (PublishError, queue.ReviewError) as exc:
                results.append(Outcome(it.id, "-", "failed", str(exc)))
    return results
