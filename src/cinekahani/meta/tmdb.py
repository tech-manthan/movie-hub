"""TMDB client: search, details, credits, images and the official trailer id."""
from __future__ import annotations

import json
import time
from importlib import resources

import httpx

from ..core.logs import get_logger
from ..core.retry import retry_call
from .models import IMG_BASE, Image, Person, TitleMeta

log = get_logger("tmdb")
API = "https://api.themoviedb.org/3"


class TmdbError(RuntimeError):
    pass


class TmdbRetryable(TmdbError):
    pass


class TmdbClient:
    def __init__(self, env: dict[str, str], *, transport: httpx.BaseTransport | None = None,
                 sleep=time.sleep):
        self.key = env.get("TMDB_API_KEY", "")
        self.token = env.get("TMDB_READ_TOKEN", "")
        if not (self.key or self.token):
            raise TmdbError("No TMDB credentials. Put TMDB_API_KEY (v3) or TMDB_READ_TOKEN (v4) "
                            "in .env, or run with --sample to try the pipeline offline.")
        self.sleep = sleep
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        self.http = httpx.Client(base_url=API, headers=headers, timeout=20, transport=transport)

    def _get(self, path: str, **params) -> dict:
        if not self.token:
            params["api_key"] = self.key

        def once() -> dict:
            try:
                r = self.http.get(path, params=params)
            except httpx.TransportError as exc:
                raise TmdbRetryable(f"network error: {exc}") from exc
            if r.status_code == 429 or r.status_code >= 500:
                wait = float(r.headers.get("Retry-After", 0) or 0)
                if wait:
                    self.sleep(min(wait, 30))
                raise TmdbRetryable(f"TMDB returned {r.status_code}")
            if r.status_code == 401:
                raise TmdbError("TMDB rejected the credentials (401). Check the key in .env.")
            if r.status_code == 404:
                raise TmdbError(f"TMDB has nothing at {path} (404).")
            r.raise_for_status()
            return r.json()

        return retry_call(once, attempts=4, base_delay=1.0, retry_on=(TmdbRetryable,),
                          sleep=self.sleep, what=f"TMDB {path}")

    def search(self, query: str, kind: str = "movie", year: int | None = None,
               language: str = "en-US") -> list[dict]:
        params = {"query": query, "language": language, "include_adult": "false"}
        if year:
            params["year" if kind == "movie" else "first_air_date_year"] = year
        return self._get(f"/search/{kind}", **params).get("results", [])

    def details(self, kind: str, tmdb_id: int, language: str = "en-US") -> dict:
        return self._get(f"/{kind}/{tmdb_id}", language=language,
                         append_to_response="credits,videos,images",
                         include_image_language="en,hi,null",
                         include_video_language="en,hi,null")


def sample_raw() -> dict:
    text = resources.files("cinekahani").joinpath("data/sample_movie.json").read_text(encoding="utf-8")
    return json.loads(text)


def pick_trailer(raw: dict) -> str | None:
    vids = [v for v in raw.get("videos", {}).get("results", [])
            if v.get("site") == "YouTube" and v.get("type") == "Trailer" and v.get("key")]
    if not vids:
        return None
    vids.sort(key=lambda v: (bool(v.get("official")), v.get("published_at", "")), reverse=True)
    return vids[0]["key"]


def _images(items: list[dict], limit: int) -> list[Image]:
    items = sorted(items, key=lambda i: i.get("vote_average", 0), reverse=True)[:limit]
    return [Image(url=IMG_BASE + i["file_path"], width=i.get("width", 0), height=i.get("height", 0))
            for i in items if i.get("file_path")]


def parse_details(raw: dict, kind: str, locale: str = "en-US", sample: bool = False) -> TitleMeta:
    credits = raw.get("credits", {})
    crew = credits.get("crew", [])

    def jobs(*names: str) -> list[Person]:
        seen, out = set(), []
        for c in crew:
            if c.get("job") in names and c["name"] not in seen:
                seen.add(c["name"])
                out.append(Person(name=c["name"], role=c["job"]))
        return out

    date = raw.get("release_date") or raw.get("first_air_date") or ""
    runtime = raw.get("runtime")
    if runtime is None and raw.get("episode_run_time"):
        runtime = raw["episode_run_time"][0]
    directors = jobs("Director")
    if kind == "tv" and not directors:
        directors = [Person(name=c["name"], role="Creator") for c in raw.get("created_by", [])]
    images = raw.get("images", {})
    return TitleMeta(
        kind=kind, tmdb_id=raw["id"],
        title=raw.get("title") or raw.get("name") or "",
        original_title=raw.get("original_title") or raw.get("original_name") or "",
        year=int(date[:4]) if date[:4].isdigit() else None, release_date=date,
        tagline=raw.get("tagline", ""), overview=raw.get("overview", ""),
        genres=[g["name"] for g in raw.get("genres", [])],
        runtime_minutes=runtime, seasons=raw.get("number_of_seasons"),
        episodes=raw.get("number_of_episodes"),
        rating=round(raw["vote_average"], 1) if raw.get("vote_average") else None,
        vote_count=raw.get("vote_count", 0), original_language=raw.get("original_language", ""),
        cast=[Person(name=c["name"], role=c.get("character", "")) for c in credits.get("cast", [])[:12]],
        directors=directors, writers=jobs("Writer", "Screenplay", "Story"),
        composers=jobs("Original Music Composer", "Composer"),
        studios=[c["name"] for c in raw.get("production_companies", [])],
        posters=_images(images.get("posters", []), 8),
        backdrops=_images(images.get("backdrops", []), 12),
        trailer_youtube_id=pick_trailer(raw), locale=locale, sample=sample,
    )


def choose_result(results: list[dict], query: str, year: int | None) -> dict:
    if not results:
        raise TmdbError(f"TMDB found no match for '{query}'. Try the exact title or add --year.")
    q = query.casefold()
    exact = [r for r in results if (r.get("title") or r.get("name") or "").casefold() == q]
    pool = exact or results
    return max(pool, key=lambda r: r.get("popularity", 0)) if year is None else pool[0]
