"""Phase 3 renderer. Shots are rendered one by one (cached, with cooldown pauses), then joined and mixed once."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from ..core.config import Settings
from ..core.hashing import hash_file, hash_json, sha
from ..core.logs import get_logger
from ..core.project import Project
from ..core.thermal import cool_down, cpu_speed_limit
from ..visuals import images
from ..visuals.plan import Plan, Shot, enforce_guardrails
from ..visuals.styles import Style

log = get_logger("render")
FADE = 0.2


class RenderError(RuntimeError):
    pass


def run_ffmpeg(args: list[str], *, runner=subprocess.run, cwd: Path | None = None) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args]
    try:
        p = runner(cmd, capture_output=True, text=True, cwd=cwd)
    except FileNotFoundError as exc:
        raise RenderError("ffmpeg is not installed (brew install ffmpeg)") from exc
    if p.returncode != 0:
        raise RenderError(f"ffmpeg failed: {(p.stderr or '').strip()[-600:]}")


def encoder_args(settings: Settings, *, runner=subprocess.run, platform: str = sys.platform) -> list[str]:
    v = settings.video
    want = v.encoder
    if want == "auto":
        want = "libx264"
        if platform == "darwin":
            p = runner(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
            if "h264_videotoolbox" in (p.stdout or ""):
                want = "h264_videotoolbox"
    if want == "h264_videotoolbox":
        base = ["-c:v", "h264_videotoolbox", "-b:v", v.bitrate, "-allow_sw", "1"]
    else:
        base = ["-c:v", "libx264", "-preset", "medium", "-crf", str(v.crf)]
    return base + ["-pix_fmt", "yuv420p", "-r", str(v.fps)]


def motion_exprs(motion: str, frames: int) -> tuple[str, str, str]:
    n = max(frames, 1)
    cy = "ih/2-ih/zoom/2"
    if motion == "out":
        return f"1.12-0.12*on/{n}", "iw/2-iw/zoom/2", cy
    if motion == "left":
        return "1.12", f"(iw-iw/zoom)*(1-on/{n})", cy
    if motion == "right":
        return "1.12", f"(iw-iw/zoom)*on/{n}", cy
    return f"1+0.12*on/{n}", "iw/2-iw/zoom/2", cy


def fades(dur: float, transition: str) -> str:
    if transition != "dip":
        return ""
    return f",fade=t=in:st=0:d={FADE},fade=t=out:st={max(dur - FADE, 0):.3f}:d={FADE}"


def still_args(img: Path, shot: Shot, settings: Settings, style: Style) -> list[str]:
    v = settings.video
    frames = max(1, round(shot.dur * v.fps))
    z, x, y = motion_exprs(shot.motion, frames)
    vf = (f"scale={2 * v.width}:{2 * v.height}:force_original_aspect_ratio=increase,"
          f"crop={2 * v.width}:{2 * v.height},"
          f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={v.width}x{v.height}:fps={v.fps},"
          f"{style.grade}{fades(shot.dur, style.transition)}")
    return ["-i", str(img), "-vf", vf, "-frames:v", str(frames)]


def trailer_args(shot: Shot, settings: Settings, style: Style) -> list[str]:
    v = settings.video
    vf = (f"scale={v.width}:{v.height}:force_original_aspect_ratio=increase,crop={v.width}:{v.height},"
          f"fps={v.fps},{style.grade}{fades(shot.dur, style.transition)}")
    return ["-ss", f"{shot.trailer_start:.2f}", "-t", f"{shot.dur:.3f}", "-i", shot.src, "-an", "-vf", vf]


def _card_lines(key: str, plan: Plan, settings: Settings) -> list[tuple[str, int, str]]:
    name = settings.channel.name
    if key == "intro":
        sub = f"{plan.year}" if plan.year else ""
        return [(name.upper(), 52, "accent"), (plan.title, 118, "fg"), (sub, 52, "accent")]
    if key == "outro":
        return [("Thanks for watching", 78, "fg"), (f"Subscribe to {name}", 100, "accent"),
                ("Movie and series data from TMDB", 38, "fg")]
    return [(plan.title, 120, "fg"), (name.upper(), 48, "accent")]


def render_plan(settings: Settings, project: Project, plan: Plan, style: Style, art: dict, audio: Path,
                out: Path, *, ass: Path | None = None, music: Path | None = None,
                runner=subprocess.run, sleep: Callable[[float], None] = time.sleep, probe=cpu_speed_limit) -> Path:
    v = settings.video
    enforce_guardrails(plan, settings)
    shots_dir = project.dir("visuals") / "shots"
    shots_dir.mkdir(exist_ok=True)
    backdrop = Path(art["backdrops"][0]) if art.get("backdrops") else None
    enc = encoder_args(settings, runner=runner)
    files: list[Path] = []
    fresh = 0
    for i, shot in enumerate(plan.shots):
        src_hash = ""
        img: Path | None = None
        if shot.kind == "card":
            lines = _card_lines(shot.src, plan, settings)
            img = project.dir("visuals") / "cards" / f"{shot.src}-{sha(json.dumps([lines, style.key, str(backdrop)]))[:10]}.jpg"
            if not img.is_file():
                images.title_card(img, v.width, v.height, style, lines, backdrop)
        elif shot.kind == "poster_card":
            img = project.dir("visuals") / "cards" / f"poster-{sha(shot.src + str(backdrop))[:12]}.jpg"
            if not img.is_file():
                images.poster_card(Path(shot.src), backdrop, img, v.width, v.height)
        elif shot.kind == "image":
            img = Path(shot.src)
        if img is not None:
            src_hash = hash_file(img)
        elif shot.kind == "trailer":
            src_hash = f"{hash_file(Path(shot.src))}"
        key = hash_json({"k": shot.kind, "src": src_hash, "dur": shot.dur, "m": shot.motion,
                         "ts": shot.trailer_start, "w": v.width, "h": v.height, "fps": v.fps,
                         "style": style.key, "enc": enc})[:20]
        dest = shots_dir / f"{key}.mp4"
        files.append(dest)
        if dest.is_file():
            continue
        args = trailer_args(shot, settings, style) if shot.kind == "trailer" else still_args(img, shot, settings, style)
        tmp = dest.with_suffix(".part.mp4")
        log.info("rendering shot %d/%d (%s, %.1fs)", i + 1, len(plan.shots), shot.kind, shot.dur)
        run_ffmpeg([*args, *enc, "-t", f"{shot.dur:.3f}", str(tmp)], runner=runner)
        tmp.replace(dest)
        fresh += 1
        if fresh % v.batch_size == 0 and i < len(plan.shots) - 1:
            cool_down(settings.cooldown, sleep=sleep, probe=probe)

    concat = out.parent / f"{out.stem}.concat.txt"
    concat.write_text("".join(f"file '{f.resolve()}'\n" for f in files), encoding="utf-8")
    total = plan.total_s
    inputs = ["-f", "concat", "-safe", "0", "-i", concat.name, "-i", str(audio.resolve())]
    delay = int(plan.intro_s * 1000)
    graph = f"[1:a]adelay={delay}:all=1,apad=whole_dur={total:.3f}[n];"
    if music:
        inputs = ["-f", "concat", "-safe", "0", "-i", concat.name, "-i", str(audio.resolve()),
                  "-stream_loop", "-1", "-i", str(Path(music).resolve())]
        fade = settings.video.music.fade_s
        graph += (f"[2:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,volume={settings.video.music.volume},"
                  f"afade=t=in:st=0:d=1.5,afade=t=out:st={max(total - fade, 0):.3f}:d={fade}[m];"
                  "[n]asplit=2[n1][n2];[m][n1]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=500[md];"
                  "[n2][md]amix=inputs=2:duration=first:normalize=0[mix];[mix]loudnorm=I=-16:TP=-1.5:LRA=11[a]")
    else:
        graph += "[n]loudnorm=I=-16:TP=-1.5:LRA=11[a]"
    vargs = ["-map", "0:v"]
    if ass is not None:
        vargs += ["-vf", f"subtitles={ass.name}", *enc]
    else:
        vargs += ["-c:v", "copy"]
    tmp_out = out.with_suffix(".part.mp4")
    run_ffmpeg([*inputs, "-filter_complex", graph, *vargs, "-map", "[a]", "-c:a", "aac", "-b:a", "192k",
                "-t", f"{total:.3f}", "-movflags", "+faststart", str(tmp_out.name)], runner=runner, cwd=out.parent)
    tmp_out.replace(out)
    concat.unlink(missing_ok=True)
    return out


def probe_video(path: Path, *, runner=subprocess.run) -> dict:
    p = runner(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
                "-of", "json", str(path)], capture_output=True, text=True)
    if p.returncode != 0:
        raise RenderError(f"ffprobe cannot read {path.name}: {p.stderr.strip()[-200:]}")
    info = json.loads(p.stdout)
    kinds = [s["codec_type"] for s in info["streams"]]
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), {})
    return {"duration": float(info["format"]["duration"]), "video": "video" in kinds, "audio": "audio" in kinds,
            "width": video.get("width"), "height": video.get("height")}
