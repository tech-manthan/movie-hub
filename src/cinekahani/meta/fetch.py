"""Step 1: fetch title metadata into projects/<slug>/metadata/ (cached)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..core.cache import Decision
from ..core.config import Settings
from ..core.hashing import hash_file, hash_json
from ..core.logs import get_logger
from ..core.project import Project, slugify
from .models import TitleMeta
from .tmdb import TmdbClient, TmdbError, choose_result, parse_details, sample_raw

log = get_logger("meta")


@dataclass
class MetaResult:
    project: Project
    meta: TitleMeta
    decision: Decision


def _index_path(settings: Settings) -> Path:
    return settings.path("projects") / ".index.json"


def _load_index(settings: Settings) -> dict:
    p = _index_path(settings)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def _slug_for(meta: TitleMeta) -> str:
    base = slugify(meta.title)
    if meta.year:
        base += f"-{meta.year}"
    return base + ("-tv" if meta.kind == "tv" else "")


def load_meta(project: Project) -> TitleMeta:
    path = project.root / "metadata" / "title.json"
    if not path.is_file():
        raise FileNotFoundError(f"{path} is missing. Run `cinekahani meta` for this title first.")
    return TitleMeta.model_validate_json(path.read_text(encoding="utf-8"))


def fetch_meta(settings: Settings, query: str, *, kind: str = "movie", year: int | None = None,
               sample: bool = False, force: bool = False,
               client: TmdbClient | None = None) -> MetaResult:
    if kind not in ("movie", "tv"):
        raise ValueError("kind must be 'movie' or 'tv'")
    locale = settings.tmdb.locale.get("en", "en-US")
    projects = settings.path("projects")
    projects.mkdir(parents=True, exist_ok=True)
    key = f"{'sample' if sample else kind}:{query.casefold()}:{year or ''}"
    input_hash = hash_json({"q": query.casefold(), "kind": kind, "year": year, "locale": locale,
                            "sample": sample})
    index = _load_index(settings)
    slug = index.get(key)

    if slug and (projects / slug).is_dir():
        project = Project(projects / slug)
        out = project.root / "metadata" / "title.json"
        cur = hash_file(out) if out.is_file() else None
        decision = project.manifest.decide("meta", input_hash, cur, force=force,
                                           max_age_days=settings.tmdb.max_age_days)
        if decision in (Decision.CACHED, Decision.EDITED_KEPT):
            log.info("metadata for %s is cached; skipping TMDB", project.slug)
            return MetaResult(project, load_meta(project), decision)

    if sample:
        raw, kind_used = sample_raw(), "movie"
    else:
        client = client or TmdbClient(settings.env)
        results = client.search(query, kind, year, locale)
        chosen = choose_result(results, query, year)
        log.info("matched '%s' (%s)", chosen.get("title") or chosen.get("name"),
                 (chosen.get("release_date") or chosen.get("first_air_date") or "")[:4])
        raw, kind_used = client.details(kind, chosen["id"], locale), kind

    meta = parse_details(raw, kind_used, locale, sample=sample)
    project = Project.create(projects, _slug_for(meta))
    (project.root / "metadata" / "tmdb_raw.json").write_text(
        json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    out = project.root / "metadata" / "title.json"
    out.write_text(meta.model_dump_json(indent=2), encoding="utf-8")
    project.manifest.record("meta", input_hash, hash_file(out), [str(out.relative_to(project.root))])
    index[key] = project.slug
    _index_path(settings).write_text(json.dumps(index, indent=2), encoding="utf-8")
    if not meta.trailer_youtube_id:
        log.warning("TMDB lists no official trailer for %s", meta.title)
    log.info("saved metadata for %s (%s) in %s", meta.title, meta.year, project.root)
    return MetaResult(project, meta, Decision.RUN)
