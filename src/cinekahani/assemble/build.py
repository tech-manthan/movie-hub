"""Phase 3 step: narration + artwork (+ optional trailer snippets, music, subtitles) -> 16:9 video."""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..core.cache import Decision
from ..core.config import Settings
from ..core.hashing import hash_file, hash_json
from ..core.logs import get_logger
from ..core.project import Project
from ..core.variety import Variety
from ..meta.models import TitleMeta
from ..visuals import images, trailer
from ..visuals.plan import Plan, plan_video
from ..visuals.styles import STYLES, get_style
from ..voice.narrate import audio_paths, load_timeline
from . import music as music_lib
from .render import RenderError, probe_video, render_plan
from .subtitles import write_ass

log = get_logger("build")


@dataclass
class BuildResult:
    video: Path
    plan: Plan
    report: dict
    decision: Decision


def render_paths(project: Project, fmt: str, lang: str):
    d = project.dir("renders")
    return d / f"{fmt}.{lang}.mp4", d / f"plan.{fmt}.{lang}.json", d / f"report.{fmt}.{lang}.json"


def build_video(settings: Settings, project: Project, meta: TitleMeta, fmt: str, lang: str, *,
                style_key: str | None = None, force: bool = False, use_trailer: bool = True,
                http=None, runner=subprocess.run, sleep=time.sleep, probe=None) -> BuildResult:
    wav, tl_path = audio_paths(project, fmt, lang)
    if not wav.is_file():
        raise RenderError(f"No narration yet. Run `cinekahani narrate {project.slug} --format {fmt} --lang {lang}`.")
    timeline = load_timeline(project, fmt, lang)
    out, plan_path, report_path = render_paths(project, fmt, lang)
    projects_dir = project.root.parent
    variety = Variety(projects_dir)
    seed = f"{project.slug}:{fmt}:{lang}"
    if style_key is None:
        keys = list(STYLES)
        style_key = (variety.pick(keys, seed, "style", 1) if settings.guardrails.vary_formats
                     else variety.pick(keys, seed, "style", 0))
    style = get_style(style_key)

    art = images.collect_artwork(project, meta, client=http)
    tsrc = trailer.source_path(project)
    tlen, moments = None, trailer.load_overrides(project)
    if use_trailer and settings.guardrails.trailer.enabled and tsrc.is_file():
        tlen = trailer.trailer_length(tsrc)
    elif use_trailer and settings.guardrails.trailer.enabled:
        log.info("no trailer source downloaded; using artwork only (see `cinekahani trailer fetch`)")
    plan = plan_video(settings, project.slug, meta, timeline, style, art, fmt=fmt, lang=lang,
                      trailer_length=tlen, trailer_source=str(tsrc) if tlen else "", moments=moments)

    music_dir = settings.path("music")
    track = music_lib.choose_track(music_dir, settings.video.music, variety, seed, fmt)
    if track is None:
        log.warning("no usable music in %s (add tracks with `cinekahani music add`); rendering without music", music_dir)
    music_path = music_dir / track.file if track else None
    plan.music = track.file if track else ""
    plan.music_credit = music_lib.credit_line(track)
    plan.save(plan_path)

    ass = None
    if settings.video.subtitles.enabled:
        ass = write_ass(out.parent / f"subs.{fmt}.{lang}.ass", timeline, settings, style, lang, plan.intro_s)

    input_hash = hash_json({"plan": json.loads(plan_path.read_text(encoding="utf-8")), "audio": hash_file(wav),
                            "ass": hash_file(ass) if ass else None,
                            "music": hash_file(music_path) if music_path else None,
                            "size": [settings.video.width, settings.video.height, settings.video.fps], "v": 1})
    key = f"video:{fmt}:{lang}"
    manifest = project.manifest
    decision = manifest.decide(key, input_hash, hash_file(out) if out.is_file() else None, force=force)
    if decision in (Decision.CACHED, Decision.EDITED_KEPT):
        log.info("%s is up to date; skipping", out.name)
        return BuildResult(out, plan, json.loads(report_path.read_text(encoding="utf-8")), decision)

    kw = {"probe": probe} if probe else {}
    render_plan(settings, project, plan, style, art, wav, out, ass=ass, music=music_path,
                runner=runner, sleep=sleep, **kw)
    info = probe_video(out, runner=runner)
    problems = []
    if not (info["video"] and info["audio"]):
        problems.append("output is missing a video or audio stream")
    if abs(info["duration"] - plan.total_s) > 1.0:
        problems.append(f"length {info['duration']:.1f}s differs from plan {plan.total_s:.1f}s")
    if (info["width"], info["height"]) != (settings.video.width, settings.video.height):
        problems.append(f"size {info['width']}x{info['height']} is not {settings.video.width}x{settings.video.height}")
    if problems:
        out.unlink(missing_ok=True)
        raise RenderError("Render check failed: " + "; ".join(problems))
    report = {"style": style.key, "duration": round(info["duration"], 2), "narration_s": plan.narration_s,
              "shots": len(plan.shots), "trailer_snippets": len(plan.snippets()),
              "trailer_share": round(plan.trailer_share(), 4), "music": plan.music, "music_credit": plan.music_credit,
              "subtitles": bool(ass), "artwork": {k: len(v) for k, v in art.items()}, "checks": "passed"}
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    manifest.record(key, input_hash, hash_file(out), [out.name, plan_path.name])
    variety.record(slug=project.slug, format=fmt, lang=lang, style=style.key, music=plan.music)
    log.info("built %s (%.0fs, style %s, %d shots, trailer %.1f%%)", out.name, info["duration"], style.key,
             len(plan.shots), plan.trailer_share() * 100)
    return BuildResult(out, plan, report, Decision.RUN)
