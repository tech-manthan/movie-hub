from __future__ import annotations

from pydantic import BaseModel

IMG_BASE = "https://image.tmdb.org/t/p/original"


class Person(BaseModel):
    name: str
    role: str = ""          # character for cast, job for crew


class Image(BaseModel):
    url: str
    width: int = 0
    height: int = 0


class TitleMeta(BaseModel):
    kind: str                       # "movie" or "tv"
    tmdb_id: int
    title: str
    original_title: str = ""
    year: int | None = None
    release_date: str = ""
    tagline: str = ""
    overview: str = ""
    genres: list[str] = []
    runtime_minutes: int | None = None
    seasons: int | None = None
    episodes: int | None = None
    rating: float | None = None
    vote_count: int = 0
    original_language: str = ""
    cast: list[Person] = []
    directors: list[Person] = []
    writers: list[Person] = []
    composers: list[Person] = []
    studios: list[str] = []
    posters: list[Image] = []
    backdrops: list[Image] = []
    trailer_youtube_id: str | None = None
    locale: str = "en-US"
    sample: bool = False
