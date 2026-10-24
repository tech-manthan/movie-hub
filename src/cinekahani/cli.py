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
from .publish import run as publish_run
from .publish.state import PublishState, ScheduleError, parse_when
from .publish.instagram import InstagramClient, InstagramError
from .publish.youtube import YoutubeAuth, YoutubeError, YoutubeUploader
from .review import queue as review_queue
from .package.pack import PackageError, build_package, render_for
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


@voice_app.command("use")
def voice_use(ctx: typer.Context, voice: str = typer.Argument(..., help="Name or id of a voice already saved in VoiceStudio"),
              langs: str = typer.Option("en,hi", help="Languages that should use this voice")):
    """Use a voice you already cloned inside VoiceStudio (no new recording needed)."""
    s = _settings(ctx)
    try:
        profiles = VoiceStudioClient(s.voice, s.env).profiles()
    except VoiceError as exc:
        _fail(str(exc))
    want = voice.strip().casefold()
    hits = [p for p in profiles if str(p.get("id", "")).casefold() == want or str(p.get("name", "")).casefold() == want]
    if not hits:
        names = ", ".join(f"{p.get('name')} ({p.get('id')})" for p in profiles) or "none saved yet"
        _fail(f"No saved voice called '{voice}'. VoiceStudio has: {names}")
    if len(hits) > 1:
        _fail("More than one voice matches; use the id: " + ", ".join(str(p.get("id")) for p in hits))
    pick = hits[0]
    path = save_local(s.root, {"voice": {"profile": {lg.strip(): str(pick["id"]) for lg in langs.split(",") if lg.strip()}}})
    typer.echo(f"using '{pick.get('name')}' ({pick['id']}) for {langs}; saved in {path}")


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
def package(ctx: typer.Context, slug: str, format: str = typer.Option("review", "--format", "-f"),
            lang: str = typer.Option("both", "--lang", "-l", help="en, hi or both"),
            force: bool = typer.Option(False)):
    """Phase 5: title options, description, tags, chapters, Shorts copy and thumbnail (edit package.json freely)."""
    s = _settings(ctx)
    try:
        project = Project.open(s.path("projects"), slug)
        meta_ = load_meta(project)
        llm = ClaudeCli(s.llm)
        for lg in (["en", "hi"] if lang == "both" else [lang]):
            r = build_package(s, project, meta_, format, lg, llm, force=force)
            typer.echo(f"{r.path} [{r.decision.value}]")
            typer.echo(f"  title: {r.data['title']}")
            typer.echo(f"  thumbnail: {r.thumbnail}")
    except (FileNotFoundError, KeyError, ScriptNotReady, PackageError, RenderError, LlmError) as exc:
        _fail(str(exc.args[0] if isinstance(exc, KeyError) else exc))


@app.command()
def thumbnail(ctx: typer.Context, slug: str, format: str = typer.Option("review", "--format", "-f"),
              lang: str = typer.Option(..., "--lang", "-l")):
    """Re-draw the thumbnail after you edited thumbnail_text in package.json."""
    import json as _json
    s = _settings(ctx)
    try:
        project = Project.open(s.path("projects"), slug)
        pkg = project.root / "package" / f"{format}.{lang}" / "package.json"
        if not pkg.is_file():
            _fail("Run `cinekahani package` first.")
        out = render_for(s, project, load_meta(project), _json.loads(pkg.read_text(encoding="utf-8")), format, lang)
    except (FileNotFoundError, RenderError) as exc:
        _fail(str(exc))
    typer.echo(out)


def _projects(s: Settings, slug: Optional[str]) -> list[Project]:
    root = s.path("projects")
    if slug:
        return [Project.open(root, slug)]
    return [Project(p) for p in sorted(root.iterdir()) if p.is_dir() and not p.name.startswith(".")] if root.is_dir() else []


review_app = typer.Typer(help="Phase 6: approve every output before it can be published.", no_args_is_help=True)
app.add_typer(review_app, name="review")


@review_app.command("list")
def review_list(ctx: typer.Context, slug: Optional[str] = typer.Argument(None)):
    """Everything waiting for your decision."""
    s = _settings(ctx)
    try:
        projects = _projects(s, slug)
    except FileNotFoundError as exc:
        _fail(str(exc))
    shown = 0
    for p in projects:
        for it in review_queue.discover(s, p):
            shown += 1
            fix = f"  [{len(it.blocked)} to fix]" if it.blocked else ""
            typer.echo(f"{it.status:9} {p.slug} {it.id}  {it.title}{fix}")
    if not shown:
        typer.echo("Nothing packaged yet. Run `cinekahani package` first.")


@review_app.command("show")
def review_show(ctx: typer.Context, slug: str, item: str):
    """Title, description, tags and checks for one item."""
    s = _settings(ctx)
    try:
        it = review_queue.find(s, Project.open(s.path("projects"), slug), item)
    except (FileNotFoundError, review_queue.ReviewError) as exc:
        _fail(str(exc))
    typer.echo(f"{it.title}\nvideo: {it.video}\nstatus: {it.status} {it.note}\n\n{it.description}\n\ntags: {', '.join(it.tags)}\n")
    for c in it.checks:
        typer.echo(f"  {'ok  ' if c.ok else ('FAIL' if c.blocking else 'warn')} {c.name} {'' if c.ok else '- ' + c.message}")


@review_app.command("approve")
def review_approve(ctx: typer.Context, slug: str, item: str, note: str = ""):
    """Approve one item (blocked while a check fails). Editing it afterwards withdraws the approval."""
    s = _settings(ctx)
    try:
        review_queue.decide(s, Project.open(s.path("projects"), slug), item, "approved", note)
    except (FileNotFoundError, review_queue.ReviewError) as exc:
        _fail(str(exc))
    typer.echo(f"approved {item}")


@review_app.command("reject")
def review_reject(ctx: typer.Context, slug: str, item: str, note: str = ""):
    s = _settings(ctx)
    try:
        review_queue.decide(s, Project.open(s.path("projects"), slug), item, "rejected", note)
    except (FileNotFoundError, review_queue.ReviewError) as exc:
        _fail(str(exc))
    typer.echo(f"rejected {item}")


@review_app.command("serve")
def review_serve(ctx: typer.Context, port: Optional[int] = typer.Option(None, help="default: publish.review_port")):
    """Open the review dashboard (local only): watch videos, edit text, approve, schedule."""
    import uvicorn
    from .review.web import create_app
    s = _settings(ctx)
    p = port or s.publish.review_port
    typer.echo(f"Review dashboard: http://127.0.0.1:{p}  (Ctrl+C to stop)")
    uvicorn.run(create_app(s), host="127.0.0.1", port=p, log_level="warning")


youtube_app = typer.Typer(help="YouTube sign-in.", no_args_is_help=True)
app.add_typer(youtube_app, name="youtube")


def _yt_auth(s: Settings) -> YoutubeAuth:
    return YoutubeAuth(s.env, s.root / "token.youtube.json")


@youtube_app.command("auth")
def youtube_auth(ctx: typer.Context):
    """Sign in to YouTube once (opens your browser)."""
    s = _settings(ctx)
    try:
        _yt_auth(s).authorize()
    except YoutubeError as exc:
        _fail(str(exc))
    typer.echo("YouTube connected. Note: a Google Cloud project that has not passed Google's audit can only "
               "upload videos as private; schedule or publish them from YouTube Studio if that applies to you.")


publish_app = typer.Typer(help="Phase 6: schedule and upload APPROVED items.", no_args_is_help=True)
app.add_typer(publish_app, name="publish")


def _ig(s: Settings) -> InstagramClient:
    return InstagramClient(s.publish.instagram, s.root / "token.instagram.json")


def _providers(s: Settings, need: set[str]) -> publish_run.Providers:
    p = publish_run.Providers()
    if "youtube" in need:
        p.youtube = YoutubeUploader(_yt_auth(s), s.publish.youtube)
    if "instagram" in need:
        p.instagram = _ig(s)
    return p


instagram_app = typer.Typer(help="Instagram sign-in (Facebook Login flavour, no third party).", no_args_is_help=True)
app.add_typer(instagram_app, name="instagram")


@instagram_app.command("setup")
def instagram_setup(ctx: typer.Context, page: Optional[str] = typer.Option(None, help="Facebook Page name, if you have several")):
    """Turn META_USER_TOKEN (.env) into a non-expiring Page token, stored in token.instagram.json."""
    s = _settings(ctx)
    try:
        info = _ig(s).setup(s.env.get("META_USER_TOKEN", ""), page)
    except InstagramError as exc:
        _fail(str(exc))
    typer.echo(f"connected: @{info['ig_username']} through Page '{info['page_name']}'. You can remove META_USER_TOKEN from .env now.")


@instagram_app.command("whoami")
def instagram_whoami(ctx: typer.Context):
    """Check the saved Instagram connection still works."""
    try:
        info = _ig(_settings(ctx)).whoami()
    except InstagramError as exc:
        _fail(str(exc))
    typer.echo(f"@{info['username']} via Page '{info['page']}', {info['media_count']} posts")


@publish_app.command("schedule")
def publish_schedule(ctx: typer.Context, slug: str, item: str,
                     at: str = typer.Option(..., help="e.g. '2026-11-03 18:00'"),
                     tz: str = typer.Option("Asia/Kolkata", help="timezone of --at")):
    """Choose when an item goes live. The platform holds the upload until then."""
    s = _settings(ctx)
    try:
        project = Project.open(s.path("projects"), slug)
        it = review_queue.find(s, project, item)
        when = parse_when(at, tz, min_lead_minutes=s.publish.min_lead_minutes)
        PublishState(project).schedule(it.id, when, tz, publish_run.targets_for(s, it))
    except (FileNotFoundError, review_queue.ReviewError, ScheduleError) as exc:
        _fail(str(exc))
    typer.echo(f"{item} scheduled for {when.isoformat(timespec='minutes')} UTC. Upload it with `cinekahani publish run` "
               "after approving it.")


@publish_app.command("run")
def publish_run_cmd(ctx: typer.Context, slug: Optional[str] = typer.Argument(None),
                    dry_run: bool = typer.Option(False, help="Show what would be uploaded")):
    """Upload what is ready. Safe to run repeatedly: a launchd job runs it every 15 minutes (see README)."""
    s = _settings(ctx)
    try:
        projects = _projects(s, slug)
        need: set[str] = set()
        for p in projects:
            for it in publish_run.due_items(s, p):
                need |= set(publish_run.targets_for(s, it))
        outcomes = publish_run.run_due(s, projects, _providers(s, need), dry_run=dry_run)
    except (FileNotFoundError, YoutubeError, InstagramError) as exc:
        _fail(str(exc))
    if not outcomes:
        typer.echo("Nothing due: approve items and schedule them first.")
    bad = 0
    for o in outcomes:
        bad += o.status == "failed"
        typer.echo(f"{o.status:9} {o.item} -> {o.target} {o.detail}")
    raise typer.Exit(1 if bad else 0)


@publish_app.command("now")
def publish_now(ctx: typer.Context, slug: str, item: str,
                yes: bool = typer.Option(False, "--yes", help="Confirm: upload this approved item right now"),
                public: bool = typer.Option(False, help="Make it public immediately (default: private)")):
    """Upload one approved item immediately, without a schedule."""
    s = _settings(ctx)
    if not yes:
        _fail("Re-run with --yes to confirm you want to upload this item now.")
    try:
        project = Project.open(s.path("projects"), slug)
        it = review_queue.find(s, project, item)
        outcomes = publish_run.publish_item(s, project, item, _providers(s, set(publish_run.targets_for(s, it))),
                                            now=True, public=public)
    except (FileNotFoundError, review_queue.ReviewError, publish_run.PublishError, YoutubeError, InstagramError) as exc:
        _fail(str(exc))
    bad = 0
    for o in outcomes:
        bad += o.status == "failed"
        typer.echo(f"{o.status:9} {o.item} -> {o.target} {o.detail}")
    raise typer.Exit(1 if bad else 0)


STAGES = ("script", "approved", "narration", "video", "shorts", "package", "reviewed", "published")


def stage_report(s: Settings, proj: Project) -> list[tuple[str, str, dict[str, bool]]]:
    """(format, lang, {stage: done}) for every script in the project."""
    from .assemble.build import render_paths
    from .voice.narrate import audio_paths
    out = []
    sdir = proj.root / "script"
    items = {i.id: i for i in review_queue.discover(s, proj)}
    state = PublishState(proj)
    for f in sorted(sdir.glob("*.md")) if sdir.is_dir() else []:
        fmt, lang = f.stem.rsplit(".", 1)
        front, _ = parse.read_script(f)
        long_id = f"long:{fmt}.{lang}"
        out.append((fmt, lang, {
            "script": True, "approved": front.get("status") == "approved",
            "narration": audio_paths(proj, fmt, lang)[0].is_file(),
            "video": render_paths(proj, fmt, lang)[0].is_file(),
            "shorts": (proj.root / "clips" / f"{fmt}.{lang}" / "clips.json").is_file(),
            "package": long_id in items,
            "reviewed": long_id in items and items[long_id].status == "approved",
            "published": state.done(long_id, "youtube")}))
    return out


@app.command()
def status(ctx: typer.Context, slug: Optional[str] = typer.Argument(None)):
    """Where every video stands: script > narration > video > shorts > package > review > publish."""
    s = _settings(ctx)
    try:
        projects = _projects(s, slug)
    except FileNotFoundError as exc:
        _fail(str(exc))
    if not projects:
        typer.echo("No projects yet. Try: cinekahani meta \"Inception\" --sample")
    for proj in projects:
        typer.echo(proj.slug)
        rows = stage_report(s, proj)
        if not rows:
            typer.echo("  (no scripts yet: cinekahani script " + proj.slug + ")")
        for fmt, lang, done in rows:
            nxt = next((st for st in STAGES if not done[st]), "done")
            typer.echo(f"  {fmt}.{lang}: " + " ".join(f"{'[x]' if done[st] else '[ ]'}{st}" for st in STAGES)
                       + f"   next: {nxt}")


@app.command()
def formats():
    """List script formats."""
    for f in FORMATS.values():
        typer.echo(f"{f.key:18} {f.label}{'  (spoilers)' if f.spoilers else ''}: {f.goal}")


@app.command()
def doctor(ctx: typer.Context):
    """Check what this Mac has ready for each phase."""
    import subprocess
    from .visuals.fonts import find_font
    s = _settings(ctx)
    ok = True

    def line(name: str, good: bool, hint: str = "", needed: bool = True):
        nonlocal ok
        mark = "ok     " if good else ("MISSING" if needed else "later  ")
        typer.echo(f"[{mark}] {name}{'' if good else '  -> ' + hint}")
        ok = ok and (good or not needed)

    line("claude CLI (scripts, packaging)", shutil.which(s.llm.command[0]) is not None,
         "install Claude Code and log in by running `claude` once")
    line("ffmpeg + ffprobe (video)", bool(shutil.which("ffmpeg") and shutil.which("ffprobe")), "brew install ffmpeg")
    from .assemble.render import SUBTITLES_HELP, has_filter
    where = shutil.which("ffmpeg") or "not found"
    from .assemble.subtitles import overlay_ready
    libass = has_filter("subtitles")
    line(f"subtitles: ffmpeg libass [{where}]", libass or overlay_ready("en") is None,
         "no libass here, so Cine Kahani draws subtitles itself (fine; or install an ffmpeg with libass)", needed=False)
    if not libass:
        problem = overlay_ready("hi")
        line("subtitles: Hindi drawing without libass", problem is None, problem or "", needed=False)
    line("latin font for cards and thumbnails", find_font("latin") is not None, "install Arial or DejaVu Sans")
    line("Devanagari font (Hindi subtitles use the system font)", find_font("devanagari") is not None,
         "macOS has Kohinoor Devanagari built in", needed=False)
    line("TMDB key in .env", bool(s.env.get("TMDB_API_KEY") or s.env.get("TMDB_READ_TOKEN")),
         "add TMDB_API_KEY to .env (use --sample until then)", needed=False)
    try:
        VoiceStudioClient(s.voice, s.env).profiles()
        vs = True
    except VoiceError:
        vs = False
    line(f"VoiceStudio running at {s.voice.base_url}", vs, "open the VoiceStudio app", needed=False)
    line("voice profile saved", bool(s.voice.profile), "cinekahani voice setup --wav ref.wav", needed=False)
    line("music tracks with licenses", bool(music_lib.eligible(s.path("music"), s.video.music)),
         "cinekahani music add FILE --license cc0 --source URL", needed=False)
    line("yt-dlp (optional trailer snippets)", shutil.which("yt-dlp") is not None, "brew install yt-dlp", needed=False)
    line("docker (OpenShorts)", shutil.which("docker") is not None, "install Docker Desktop", needed=False)
    line("OpenShorts set up", (s.root / s.shorts.compose_dir / "docker-compose.yml").is_file(),
         "bash scripts/setup_openshorts.sh", needed=False)
    line("GEMINI_API_KEY (OpenShorts)", bool(s.env.get("GEMINI_API_KEY")), "add to .env", needed=False)
    line("YouTube client id/secret", bool(s.env.get("YOUTUBE_CLIENT_ID") and s.env.get("YOUTUBE_CLIENT_SECRET")),
         "see README, Phase 6", needed=False)
    line("YouTube signed in", (s.root / "token.youtube.json").is_file(), "cinekahani youtube auth", needed=False)
    line("Instagram connected", (s.root / "token.instagram.json").is_file(), "cinekahani instagram setup", needed=False)
    raise typer.Exit(0 if ok else 1)


if __name__ == "__main__":
    app()
