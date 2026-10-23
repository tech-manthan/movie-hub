"""Phase 4: cut the finished long video into 9:16 Shorts/Reels with OpenShorts."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..assemble.build import render_paths
from ..core.cache import Decision
from ..core.config import Settings
from ..core.hashing import hash_file, hash_json
from ..core.logs import get_logger
from ..core.project import Project
from ..visuals.plan import Plan
from . import service
from .client import OpenShortsClient, ShortsError

log = get_logger("shorts")


@dataclass
class ShortsResult:
    clips: list[dict]
    rejected: list[dict]
    decision: Decision
    folder: Path


def clips_dir(project: Project, fmt: str, lang: str) -> Path:
    return project.dir("clips") / f"{fmt}.{lang}"


def trailer_share(clip: dict, plan: Plan) -> float | None:
    """Share of a Short that is trailer footage. None when OpenShorts gave no start/end to check."""
    try:
        start, end = float(clip["start"]), float(clip["end"])
    except (KeyError, TypeError, ValueError):
        return None
    if end <= start:
        return None
    over = sum(max(0.0, min(end, s.start + s.length) - max(start, s.start)) for s in plan.snippets())
    return over / (end - start)


def make_shorts(settings: Settings, project: Project, fmt: str, lang: str, client: OpenShortsClient, *,
                force: bool = False, runner=subprocess.run, sleep=time.sleep,
                manage_service: bool = True) -> ShortsResult:
    video, plan_path, _ = render_paths(project, fmt, lang)
    if not video.is_file():
        raise ShortsError(f"No long video yet. Run `cinekahani build {project.slug} --format {fmt} --lang {lang}`.")
    plan = Plan.load(plan_path)
    folder = clips_dir(project, fmt, lang)
    meta_file = folder / "clips.json"
    cfg = settings.shorts
    input_hash = hash_json({"video": hash_file(video), "n": cfg.target_clips, "min": cfg.min_seconds,
                            "max": cfg.max_seconds, "hook": cfg.auto_hook, "cap": cfg.captions,
                            "share": cfg.max_trailer_share})
    key = f"shorts:{fmt}:{lang}"
    manifest = project.manifest
    cur = hash_file(meta_file) if meta_file.is_file() else None
    decision = manifest.decide(key, input_hash, cur, force=force)
    if decision in (Decision.CACHED, Decision.EDITED_KEPT):
        data = json.loads(meta_file.read_text(encoding="utf-8"))
        log.info("Shorts for %s.%s are up to date; skipping", fmt, lang)
        return ShortsResult(data["clips"], data["rejected"], decision, folder)

    started = False
    try:
        if manage_service:
            started = service.start(settings, client, runner=runner, sleep=sleep)
        elif not client.healthy():
            raise ShortsError(f"OpenShorts is not running at {cfg.base_url}")
        log.info("uploading %s to OpenShorts", video.name)
        upload_id = client.upload(video)
        job = client.process(upload_id)
        log.info("OpenShorts job %s running; this can take several minutes", job)
        raw = client.wait(job)
    finally:
        if started:
            service.stop(settings, runner=runner)

    if folder.exists():
        shutil.rmtree(folder)
    kept: list[dict] = []
    rejected: list[dict] = []
    for i, clip in enumerate(raw, 1):
        url = clip.get("video_url")
        if not url:
            continue
        share = trailer_share(clip, plan)
        entry = {"index": i, "title": clip.get("title") or clip.get("video_title_for_youtube_short") or "",
                 "start": clip.get("start"), "end": clip.get("end"), "trailer_share": share,
                 "raw": {k: v for k, v in clip.items() if k != "video_url"}}
        if share is not None and share > cfg.max_trailer_share:
            entry["rejected"] = f"{share:.0%} trailer footage (limit {cfg.max_trailer_share:.0%})"
            rejected.append(entry)
            continue
        entry["file"] = client.download(url, folder / f"short-{i:02d}.mp4").name
        entry["trailer_check"] = "ok" if share is not None else "unverified (OpenShorts gave no start/end)"
        kept.append(entry)
    if not kept:
        raise ShortsError("OpenShorts returned no usable clips" + (f" ({len(rejected)} rejected for trailer share)"
                                                                  if rejected else ""))
    folder.mkdir(parents=True, exist_ok=True)
    meta_file.write_text(json.dumps({"source": video.name, "clips": kept, "rejected": rejected}, indent=2,
                                    ensure_ascii=False), encoding="utf-8")
    manifest.record(key, input_hash, hash_file(meta_file), [meta_file.name])
    log.info("%d Short(s) saved in %s (%d rejected)", len(kept), folder, len(rejected))
    return ShortsResult(kept, rejected, Decision.RUN, folder)
