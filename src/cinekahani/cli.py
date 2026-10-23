from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

import typer

from .core.cache import Decision
from .core.config import Settings, load_settings
from .core.logs import setup_logging
from .core.project import Project
from .meta.fetch import fetch_meta, load_meta
from .meta.tmdb import TmdbError
from .script import parse
from .script.formats import FORMATS
from .script.generate import (ScriptEditedError, ScriptNotReady, approve_script, generate_script,
                              script_path)
from .script.llm import ClaudeCli, LlmError

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
