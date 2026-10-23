import pytest

from cinekahani.core.cache import Decision, Manifest
from cinekahani.core.config import TrailerGuard, load_settings
from cinekahani.core.guardrails import Snippet, check_trailer_plan
from cinekahani.core.project import Project, slugify
from cinekahani.core.retry import retry_call


def test_retry_backs_off_then_succeeds():
    calls, sleeps = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ValueError("boom")
        return "ok"

    assert retry_call(flaky, attempts=4, base_delay=2, retry_on=(ValueError,), sleep=sleeps.append) == "ok"
    assert sleeps == [2, 4]


def test_retry_gives_up_and_raises():
    with pytest.raises(ValueError):
        retry_call(lambda: (_ for _ in ()).throw(ValueError("x")), attempts=2,
                   retry_on=(ValueError,), sleep=lambda s: None)


def test_manifest_decisions(tmp_path):
    m = Manifest(tmp_path / "m.json")
    assert m.decide("s", "in1", None) is Decision.RUN
    m.record("s", "in1", "out1", ["f"])
    m2 = Manifest(tmp_path / "m.json")                       # persisted
    assert m2.decide("s", "in1", "out1") is Decision.CACHED
    assert m2.decide("s", "in1", "edited") is Decision.EDITED_KEPT
    assert m2.decide("s", "in2", "out1") is Decision.RUN
    assert m2.decide("s", "in2", "edited") is Decision.EDITED_BLOCKED
    assert m2.decide("s", "in1", "out1", force=True) is Decision.RUN
    assert m2.decide("s", "in1", None) is Decision.RUN        # output deleted


def test_manifest_expires(tmp_path):
    m = Manifest(tmp_path / "m.json")
    m.record("s", "i", "o", [])
    m.data["steps"]["s"]["at"] = "2000-01-01T00:00:00+00:00"
    assert m.decide("s", "i", "o", max_age_days=30) is Decision.RUN
    assert m.decide("s", "i", "o") is Decision.CACHED


def test_slugify_and_project_layout(tmp_path):
    assert slugify("Amélie: The Movie!") == "amelie-the-movie"
    p = Project.create(tmp_path, "x-2020")
    assert {d.name for d in p.root.iterdir()} >= {"metadata", "script", "audio", "visuals", "renders", "clips"}
    with pytest.raises(FileNotFoundError, match="Known projects: x-2020"):
        Project.open(tmp_path, "nope")


def test_guardrails():
    g = TrailerGuard()
    assert check_trailer_plan([], 600, g) == []
    ok = [Snippet("t", 10, 3), Snippet("t", 100, 2)]
    assert check_trailer_plan(ok, 600, g, narration_seconds=550) == []
    long = check_trailer_plan([Snippet("t", 10, 6)], 600, g)
    assert any("max 3s" in p for p in long)
    share = check_trailer_plan([Snippet("t", i * 10, 3) for i in range(50)], 600, g)
    assert any("share" in p for p in share)
    chain = check_trailer_plan([Snippet("t", 10, 3), Snippet("t", 13, 3)], 600, g)
    assert any("continuous" in p for p in chain)
    assert any("narration" in p for p in check_trailer_plan(ok, 600, g, narration_seconds=300))
    off = TrailerGuard(enabled=False)
    assert check_trailer_plan(ok, 600, off)


def test_config_defaults_yaml_and_env(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "studio.yaml").write_text(
        "guardrails:\n  trailer:\n    max_snippet_seconds: 2\nllm:\n  model: sonnet\n")
    (tmp_path / ".env").write_text("# c\nTMDB_API_KEY='abc'\n")
    monkeypatch.setenv("TMDB_READ_TOKEN", "tok")
    s = load_settings(tmp_path)
    assert s.guardrails.trailer.max_snippet_seconds == 2
    assert s.guardrails.trailer.max_share_of_runtime == 0.20      # untouched default
    assert s.llm.model == "sonnet"
    assert s.env["TMDB_API_KEY"] == "abc" and s.env["TMDB_READ_TOKEN"] == "tok"
    assert "TMDB" in s.credit_line and "Cine Kahani" in s.credit_line


def test_repo_config_file_is_valid():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    s = load_settings(root)
    assert s.languages == ["hi", "en"] and s.guardrails.trailer.max_snippet_seconds == 3
