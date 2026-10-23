import httpx
import pytest

from cinekahani.core.cache import Decision
from cinekahani.meta.fetch import fetch_meta
from cinekahani.meta.tmdb import TmdbClient, TmdbError, choose_result, parse_details, sample_raw


def test_parse_sample():
    m = parse_details(sample_raw(), "movie", sample=True)
    assert (m.title, m.year, m.runtime_minutes) == ("Inception", 2010, 148)
    assert m.directors[0].name == "Christopher Nolan"
    assert m.trailer_youtube_id == "SAMPLE_KEY_1"          # trailer beats teaser
    assert m.backdrops[0].url.startswith("https://image.tmdb.org/t/p/original/")
    assert m.cast[0].role == "Cobb" and m.sample


def test_tv_uses_creators_and_episode_runtime():
    raw = {"id": 1, "name": "Show", "first_air_date": "2020-01-02", "episode_run_time": [45],
           "created_by": [{"name": "Jane Maker"}], "number_of_seasons": 3, "number_of_episodes": 24}
    m = parse_details(raw, "tv")
    assert (m.year, m.runtime_minutes, m.seasons) == (2020, 45, 3)
    assert m.directors[0].name == "Jane Maker" and m.trailer_youtube_id is None


def test_choose_result():
    res = [{"title": "Inception 2", "popularity": 99}, {"title": "Inception", "popularity": 5},
           {"title": "Inception", "popularity": 50}]
    assert choose_result(res, "inception", None)["popularity"] == 50
    with pytest.raises(TmdbError, match="no match"):
        choose_result([], "zzz", None)


def _client(handler, sleeps=None):
    return TmdbClient({"TMDB_API_KEY": "k"}, transport=httpx.MockTransport(handler),
                      sleep=(sleeps.append if sleeps is not None else lambda s: None))


def test_client_requires_credentials():
    with pytest.raises(TmdbError, match="--sample"):
        TmdbClient({})


def test_client_retries_429_and_sends_key():
    seen, sleeps = [], []

    def handler(req):
        seen.append(req)
        if len(seen) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"results": [{"id": 1, "title": "X"}]})

    res = _client(handler, sleeps).search("x")
    assert res[0]["id"] == 1 and len(seen) == 2
    assert seen[1].url.params["api_key"] == "k" and 2.0 in sleeps


def test_client_bearer_token_and_errors():
    def handler(req):
        assert req.headers["authorization"] == "Bearer tok"
        assert "api_key" not in req.url.params
        return httpx.Response(401)

    c = TmdbClient({"TMDB_READ_TOKEN": "tok"}, transport=httpx.MockTransport(handler))
    with pytest.raises(TmdbError, match="401"):
        c.search("x")
    with pytest.raises(TmdbError, match="404"):
        _client(lambda r: httpx.Response(404)).details("movie", 1)


def test_client_gives_up_after_repeated_500():
    n = []
    with pytest.raises(TmdbError):
        _client(lambda r: (n.append(1), httpx.Response(503))[1]).search("x")
    assert len(n) == 4


def test_fetch_meta_sample_caches(settings):
    first = fetch_meta(settings, "Inception", sample=True)
    assert first.decision is Decision.RUN and first.project.slug == "inception-2010"
    for f in ("title.json", "tmdb_raw.json"):
        assert (first.project.root / "metadata" / f).is_file()
    again = fetch_meta(settings, "inception", sample=True)       # case-insensitive, no network
    assert again.decision is Decision.CACHED
    forced = fetch_meta(settings, "Inception", sample=True, force=True)
    assert forced.decision is Decision.RUN


def test_fetch_meta_real_path_uses_client_once(settings):
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path.startswith("/3/search"):
            return httpx.Response(200, json={"results": [{"id": 27205, "title": "Inception", "popularity": 1}]})
        return httpx.Response(200, json=sample_raw())

    client = _client(handler)
    r = fetch_meta(settings, "Inception", client=client)
    assert not r.meta.sample and calls == ["/3/search/movie", "/3/movie/27205"]
    assert fetch_meta(settings, "Inception", client=client).decision is Decision.CACHED
    assert len(calls) == 2
    with pytest.raises(ValueError):
        fetch_meta(settings, "x", kind="book")
