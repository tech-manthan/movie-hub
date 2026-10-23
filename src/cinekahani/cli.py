from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

import typer

from .core.cache import Decision
from .core.config import Settings, load_settings, save_local
from .core.logs import setup_logging
from .core.project import Project
from .meta.fetch import fetch_meta, load_meta
from .meta.tmdb import TmdbError
from .script import parse
from .script.formats import FORMATS
from .script.generate import (ScriptEditedError, ScriptNotReady, approve_script, generate_script,
                              script_path)
from .script.llm import ClaudeCli, LlmError
from .assemble import music as music_lib
from .assemble.build import build_video
from .assemble.render import RenderError
from .core.variety import Variety
from .visuals import trailer as trailer_mod
from .visuals.plan import GuardrailError
from .visuals.styles import STYLES
from .shorts import service as shorts_service
from .shorts.client import OpenShortsClient, ShortsError
from .shorts.make import make_shorts
from .voice.client import VoiceError, VoiceStudioClient
from .voice.narrate import narrate as run_narrate

app = typer.Typer(help="Cine Kahani: movie and series video pipeline (Phase 1: metadata + scripts).",
                  no_args_is_help=True, add_completion=False)


def _settings(ctx: typer.Context) -> Settings:
    return ctx.obj


@app.callback()
def main(ctx: typer.Context,
         root: Path = typer.Option(Path("."), "--root", envvar="CINEKAHANI_ROOT",
                                   help="Folder holding config/, .env and projects/."),
         verbose: bool = typer.Option(False, "--verbose", "-v")):
    settings = load_settings(root)
    setup_logging(settings.path("logs"), verbose)
    ctx.obj = settings


def _fail(msg: str) -> None:
    typer.secho(f"Error: {msg}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


@app.command()
def meta(ctx: typer.Context, title: str = typer.Argument(..., help="Movie or series title"),
         kind: str = typer.Option("movie", help="movie or tv"),
         year: Optional[int] = typer.Option(None, help="Release year, to pick the right match"),
         sample: bool = typer.Option(False, help="Use built-in sample data (no TMDB key needed)"),
         force: bool = typer.Option(False, help="Fetch again even if cached")):
    """Fetch metadata from TMDB into projects/<slug>/metadata/."""
    s = _settings(ctx)
    try:
        r = fetch_meta(s, title, kind=kind, year=year, sample=sample, force=force)
    except (TmdbError, ValueError) as exc:
        _fail(str(exc))
    m = r.meta
    typer.echo(f"{m.title} ({m.year}) | {', '.join(m.genres)} | rating {m.rating} | "
               f"trailer: {m.trailer_youtube_id or 'none'}")
    typer.echo(f"project: {r.project.slug}")


@app.command()
def script(ctx: typer.Context, slug: str = typer.Argument(..., help="Project name from `cinekahani meta`"),
           format: str = typer.Option("review", "--format", "-f", help=" | ".join(FORMATS)),
           lang: str = typer.Option("both", "--lang", "-l", help="en, hi or both"),
           with_title: Optional[str] = typer.Option(None, "--with", help="Second title (comparison)"),
           sample: bool = typer.Option(False, help="Second title: use sample data"),
           force: bool = typer.Option(False, help="Rewrite even if you edited the script")):
    """Write a script with Claude (`claude -p`). Edit the .md file, then approve it."""
    s = _settings(ctx)
    langs = ["en", "hi"] if lang == "both" else [lang]
    try:
        project = Project.open(s.path("projects"), slug)
        title = load_meta(project)
        other = fetch_meta(s, with_title, sample=sample).meta if with_title else None
        llm = ClaudeCli(s.llm)
        for lg in langs:
            res = generate_script(s, project, title, format, lg, llm, other=other, force=force,
                                  model_name=llm.model_name)
            tag = {Decision.RUN: "written", Decision.CACHED: "up to date",
                   Decision.EDITED_KEPT: "kept (your edits)"}[res.decision]
            typer.echo(f"{res.path} [{tag}]")
            for w in res.warnings:
                typer.secho(f"  warning: {w}", fg=typer.colors.YELLOW)
    except (FileNotFoundError, KeyError, ValueError, TmdbError) as exc:
        _fail(str(exc.args[0] if isinstance(exc, KeyError) else exc))
    except (LlmError, ScriptEditedError) as exc:
        _fail(str(exc))


@app.command()
def approve(ctx: typer.Context, slug: str, format: str = typer.Option("review", "--format", "-f"),
            lang: str = typer.Option(..., "--lang", "-l", help="en or hi")):
    """Mark a script as approved. Blocked while [VERIFY: ...] claims remain."""
    s = _settings(ctx)
    try:
        p = approve_script(Project.open(s.path("projects"), slug), format, lang)
    except (FileNotFoundError, ScriptNotReady) as exc:
        _fail(str(exc))
    typer.echo(f"approved: {p}")


voice_app = typer.Typer(help="VoiceStudio: set up and check your cloned voice.", no_args_is_help=True)
app.add_typer(voice_app, name="voice")


@voice_app.command("check")
def voice_check(ctx: typer.Context):
    """Is VoiceStudio running, and which voices and engine will be used?"""
    s = _settings(ctx)
    try:
        c = VoiceStudioClient(s.voice, s.env)
        profiles = c.profiles()
    except VoiceError as exc:
        _fail(str(exc))
    typer.echo(f"VoiceStudio reachable at {s.voice.base_url}; engine setting: {s.voice.engine}")
    typer.echo(f"saved voices: {', '.join(p.get('name', '?') + ' (' + str(p.get('id')) + ')' for p in profiles) or 'none'}")
    typer.echo(f"profiles in config: {s.voice.profile or 'none yet - run `cinekahani voice setup`'}")
    if s.voice.engine in ("omnivoice", "", None):
        typer.secho("warning: OmniVoice weights are non-commercial; do not use them on a monetized channel.",
                    fg=typer.colors.YELLOW)


@voice_app.command("setup")
def voice_setup(ctx: typer.Context, wav: Path = typer.Option(..., exists=True, help="10-15 s clean recording of your voice"),
                text: str = typer.Option("", help="Exactly what you said in the recording (improves cloning)"),
                name: str = typer.Option("cinekahani", help="Name for the saved voice"),
                langs: str = typer.Option("en,hi", help="Languages that should use this voice")):
    """Clone your voice once and remember the profile id in config/studio.local.yaml."""
    s = _settings(ctx)
    try:
        pid = VoiceStudioClient(s.voice, s.env).create_clone(name, wav, text)
    except VoiceError as exc:
        _fail(str(exc))
    patch = {"voice": {"profile": {lg.strip(): pid for lg in langs.split(",") if lg.strip()}}}
    path = save_local(s.root, patch)
    typer.echo(f"voice '{name}' saved as {pid}; recorded in {path}")


@app.command()
def narrate(ctx: typer.Context, slug: str, format: str = typer.Option("review", "--format", "-f"),
            lang: str = typer.Option("both", "--lang", "-l", help="en, hi or both"),
            force: bool = typer.Option(False, help="Speak everything again")):
    """Phase 2: speak an approved script in your cloned voice (chunked, cached)."""
    s = _settings(ctx)
    try:
        project = Project.open(s.path("projects"), slug)
        client = VoiceStudioClient(s.voice, s.env)
        for lg in (["en", "hi"] if lang == "both" else [lang]):
            r = run_narrate(s, project, format, lg, client, force=force)
            typer.echo(f"{r.wav} [{r.decision.value}] {r.duration:.0f}s "
                       f"({r.generated} spoken, {r.cached} reused)")
    except (FileNotFoundError, ScriptNotReady, VoiceError) as exc:
        _fail(str(exc))


music_app = typer.Typer(help="Royalty-free music library with a license log.", no_args_is_help=True)
app.add_typer(music_app, name="music")


@music_app.command("add")
def music_add(ctx: typer.Context, file: Path, license: str = typer.Option(..., help="cc0, pixabay, royalty-free, cc-by, public-domain, own"),
              source: str = typer.Option("", help="Where you got it (URL)"), title: str = "", artist: str = "",
              credit: str = typer.Option("", help="Credit line (required for cc-by)"), mood: str = typer.Option("", help="calm, upbeat, tense, dramatic, mysterious")):
    """Copy a track into music/ and log its license. NC/ND licenses are refused."""
    s = _settings(ctx)
    try:
        t = music_lib.add_track(s.path("music"), file, s.video.music, license=license, source=source,
                                title=title, artist=artist, credit=credit, mood=mood)
    except music_lib.MusicError as exc:
        _fail(str(exc))
    typer.echo(f"added {t.file} ({t.license})")


@music_app.command("list")
def music_list(ctx: typer.Context):
    """Show the tracks the pipeline may use."""
    s = _settings(ctx)
    ok = {t.file for t in music_lib.eligible(s.path("music"), s.video.music)}
    tracks = music_lib.load_tracks(s.path("music"))
    for t in tracks:
        typer.echo(f"{'usable ' if t.file in ok else 'BLOCKED'} {t.file}  [{t.license}] {t.mood or '-'}  {t.source}")
    if not tracks:
        typer.echo("No music yet. Add tracks with `cinekahani music add FILE --license cc0 --source URL`.")


trailer_app = typer.Typer(help="Optional trailer snippets (supporting footage only).", no_args_is_help=True)
app.add_typer(trailer_app, name="trailer")


@trailer_app.command("fetch")
def trailer_fetch(ctx: typer.Context, slug: str, yes: bool = typer.Option(False, "--yes", help="I accept the risk in the notice")):
    """Download the official trailer once, to cut 2-3 second snippets from."""
    s = _settings(ctx)
    typer.secho(trailer_mod.NOTICE, fg=typer.colors.YELLOW)
    if not yes:
        _fail("Re-run with --yes to confirm.")
    try:
        project = Project.open(s.path("projects"), slug)
        path = trailer_mod.fetch_trailer(s, project, load_meta(project).trailer_youtube_id)
    except (FileNotFoundError, trailer_mod.TrailerError) as exc:
        _fail(str(exc))
    typer.echo(f"trailer source: {path}")


@app.command()
def build(ctx: typer.Context, slug: str, format: str = typer.Option("review", "--format", "-f"),
          lang: str = typer.Option("both", "--lang", "-l", help="en, hi or both"),
          style: Optional[str] = typer.Option(None, help=" | ".join(STYLES) + " (default: rotates)"),
          no_trailer: bool = typer.Option(False, help="Artwork only"),
          force: bool = typer.Option(False)):
    """Phase 3: assemble the 16:9 video (artwork, optional snippets, subtitles, music, intro/outro)."""
    s = _settings(ctx)
    try:
        project = Project.open(s.path("projects"), slug)
        meta_ = load_meta(project)
        for lg in (["en", "hi"] if lang == "both" else [lang]):
            r = build_video(s, project, meta_, format, lg, style_key=style, force=force, use_trailer=not no_trailer)
            typer.echo(f"{r.video} [{r.decision.value}] style={r.report['style']} {r.report['duration']}s "
                       f"trailer={r.report['trailer_share']:.1%}")
    except (FileNotFoundError, KeyError, RenderError, GuardrailError, trailer_mod.TrailerError) as exc:
        _fail(str(exc.args[0] if isinstance(exc, KeyError) else exc))


@app.command()
def suggest(ctx: typer.Context):
    """Which script format to make next, so videos stay varied."""
    s = _settings(ctx)
    typer.echo(Variety(s.path("projects")).suggest_format(list(FORMATS)))


shorts_app = typer.Typer(help="Phase 4: Shorts/Reels with OpenShorts (runs in Docker).", no_args_is_help=True)
app.add_typer(shorts_app, name="shorts")


@shorts_app.command("up")
def shorts_up(ctx: typer.Context):
    """Start the OpenShorts container."""
    s = _settings(ctx)
    try:
        started = shorts_service.start(s, OpenShortsClient(s.shorts, s.env))
    except ShortsError as exc:
        _fail(str(exc))
    typer.echo("OpenShorts started" if started else "OpenShorts was already running")


@shorts_app.command("down")
def shorts_down(ctx: typer.Context):
    """Stop the OpenShorts container (frees RAM for the other steps)."""
    try:
        shorts_service.stop(_settings(ctx))
    except ShortsError as exc:
        _fail(str(exc))


@shorts_app.command("make")
def shorts_make(ctx: typer.Context, slug: str, format: str = typer.Option("review", "--format", "-f"),
                lang: str = typer.Option("both", "--lang", "-l", help="en, hi or both"),
                keep_running: bool = typer.Option(False, help="Do not start/stop Docker for me"),
                force: bool = typer.Option(False)):
    """Cut the finished long video into vertical clips with captions and hooks."""
    s = _settings(ctx)
    try:
        project = Project.open(s.path("projects"), slug)
        client = OpenShortsClient(s.shorts, s.env)
        for lg in (["en", "hi"] if lang == "both" else [lang]):
            r = make_shorts(s, project, format, lg, client, force=force, manage_service=not keep_running)
            typer.echo(f"{r.folder}: {len(r.clips)} Short(s), {len(r.rejected)} rejected [{r.decision.value}]")
            for rej in r.rejected:
                typer.secho(f"  rejected clip {rej['index']}: {rej['rejected']}", fg=typer.colors.YELLOW)
    except (FileNotFoundError, ShortsError) as exc:
        _fail(str(exc))


@app.command()
def status(ctx: typer.Context, slug: Optional[str] = typer.Argument(None)):
    """List projects and the state of their scripts."""
    s = _settings(ctx)
    root = s.path("projects")
    slugs = [slug] if slug else sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    if not slugs:
        typer.echo("No projects yet. Try: cinekahani meta \"Inception\" --sample")
    for sl in slugs:
        try:
            proj = Project.open(root, sl)
        except FileNotFoundError as exc:
            _fail(str(exc))
        typer.echo(sl)
        for f in sorted((proj.root / "script").glob("*.md")) if (proj.root / "script").is_dir() else []:
            front, body = parse.read_script(f)
            typer.echo(f"  {f.name}: {front.get('status', '?')}, {front.get('words', '?')} words, "
                       f"{len(parse.verify_tags(body))} unchecked claims")


@app.command()
def formats():
    """List script formats."""
    for f in FORMATS.values():
        typer.echo(f"{f.key:18} {f.label}{'  (spoilers)' if f.spoilers else ''}: {f.goal}")


@app.command()
def doctor(ctx: typer.Context):
    """Check what this Mac has ready."""
    s = _settings(ctx)
    ok = True

    def line(name: str, good: bool, hint: str = "", needed: bool = True):
        nonlocal ok
        mark = "ok " if good else ("MISSING" if needed else "later")
        typer.echo(f"[{mark}] {name}{'' if good else '  -> ' + hint}")
        ok = ok and (good or not needed)

    line("claude CLI", shutil.which(s.llm.command[0]) is not None,
         "install Claude Code and log in by running `claude` once")
    line("TMDB key in .env", bool(s.env.get("TMDB_API_KEY") or s.env.get("TMDB_READ_TOKEN")),
         "add TMDB_API_KEY to .env (use --sample until then)", needed=False)
    line("ffmpeg", shutil.which("ffmpeg") is not None, "brew install ffmpeg (Phase 3)", needed=False)
    line("docker", shutil.which("docker") is not None, "install Docker Desktop (Phase 4)", needed=False)
    raise typer.Exit(0 if ok else 1)


if __name__ == "__main__":
    app()
