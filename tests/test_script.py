import subprocess
import sys
import textwrap

import pytest
from conftest import EN_SECTIONS, FakeLlm, make_script

from cinekahani.core.cache import Decision
from cinekahani.core.config import Llm
from cinekahani.script import parse
from cinekahani.script.formats import FORMATS, get_format
from cinekahani.script.generate import (ScriptEditedError, ScriptNotReady, approve_script, clean_output,
                                        generate_script, require_approved, script_path)
from cinekahani.script.llm import ClaudeCli, LlmError
from cinekahani.script.prompts import build_prompt


def gen(settings, project, meta, llm, lang="en", **kw):
    return generate_script(settings, project, meta, "review", lang, llm, **kw)


def test_parse_sections_cues_and_verify():
    body = "## Hook\n[VISUAL: poster]\nIt is great. [VERIFY: grossed a lot] Really.\n\n## End\nBye."
    secs = parse.sections(body)
    assert [s.heading for s in secs] == ["Hook", "End"]
    assert secs[0].cues == ["poster"] and "VISUAL" not in secs[0].text
    assert "grossed a lot" in secs[0].text and "VERIFY" not in secs[0].text
    assert parse.verify_tags(body) == ["grossed a lot"]
    assert parse.word_count(body) == 8


def test_front_matter_roundtrip():
    text = parse.render({"a": 1, "t": "नमस्ते"}, "## X\nhi")
    front, body = parse.split_front_matter(text)
    assert front == {"a": 1, "t": "नमस्ते"} and body.strip() == "## X\nhi"


def test_clean_output_strips_fences_and_preface():
    assert clean_output("```markdown\nTitle\n## A\ntext\n```") == "## A\ntext"


def test_generate_writes_editable_file(settings, sample):
    project, meta = sample
    llm = FakeLlm(make_script(50))
    res = gen(settings, project, meta, llm)
    assert res.decision is Decision.RUN and res.path.name == "review.en.md" and not res.warnings
    front, body = parse.read_script(res.path)
    assert front["status"] == "draft" and front["format"] == "review" and front["words"] == 300
    assert [s.heading for s in parse.sections(body)] == EN_SECTIONS
    assert "Inception" in llm.prompts[0] and "ORIGINAL" in llm.prompts[0] and "Cine Kahani" in llm.prompts[0]


def test_second_run_is_cached_and_edits_are_protected(settings, sample):
    project, meta = sample
    llm = FakeLlm(make_script(50))
    res = gen(settings, project, meta, llm)
    assert gen(settings, project, meta, llm).decision is Decision.CACHED
    assert len(llm.prompts) == 1                                  # no second LLM call
    res.path.write_text(res.path.read_text().replace("word word", "my own words", 1))
    assert gen(settings, project, meta, llm).decision is Decision.EDITED_KEPT
    settings.hindi_script = "hinglish"                            # change an input
    with pytest.raises(ScriptEditedError, match="hand edits"):
        gen(settings, project, meta, llm)
    assert "my own words" in res.path.read_text()                # untouched
    forced = gen(settings, project, meta, llm, force=True)
    assert forced.decision is Decision.RUN and len(llm.prompts) == 2


def test_changed_inputs_regenerate_unedited_script(settings, sample):
    project, meta = sample
    llm = FakeLlm(make_script(50))
    gen(settings, project, meta, llm)
    meta2 = meta.model_copy(update={"rating": 1.0})
    assert gen(settings, project, meta2, llm).decision is Decision.RUN


def test_short_script_triggers_one_corrective_retry(settings, sample):
    project, meta = sample
    llm = FakeLlm(make_script(5), make_script(50))
    res = gen(settings, project, meta, llm)
    assert not res.warnings and len(llm.prompts) == 2
    assert "too short" in llm.prompts[1] and "FIX THIS" in llm.prompts[1]


def test_too_long_is_a_warning_without_retry(settings, sample):
    project, meta = sample
    llm = FakeLlm(make_script(120))
    res = gen(settings, project, meta, llm)
    assert len(llm.prompts) == 1 and any("too long" in w for w in res.warnings)


def test_still_bad_after_retry_is_saved_with_warnings(settings, sample):
    project, meta = sample
    res = gen(settings, project, meta, FakeLlm(make_script(5)))
    front, _ = parse.read_script(res.path)
    assert res.warnings and front["warnings"]


def test_verify_tags_block_approval_until_resolved(settings, sample):
    project, meta = sample
    res = gen(settings, project, meta, FakeLlm(make_script(50, "\n\nIt earned [VERIFY: a billion dollars].")))
    assert parse.read_script(res.path)[0]["verify_open"] == 1
    with pytest.raises(ScriptNotReady, match="a billion dollars"):
        approve_script(project, "review", "en")
    with pytest.raises(ScriptNotReady, match="draft"):
        require_approved(project, "review", "en")
    res.path.write_text(res.path.read_text().replace("[VERIFY: a billion dollars]", "a lot"))
    approve_script(project, "review", "en")
    assert require_approved(project, "review", "en") == res.path
    assert parse.read_script(res.path)[0]["status"] == "approved"
    assert gen(settings, project, meta, FakeLlm(make_script(50))).decision is Decision.EDITED_KEPT  # approve != edit


def test_approve_missing_script(settings, sample):
    with pytest.raises(ScriptNotReady, match="does not exist"):
        approve_script(sample[0], "review", "hi")


def test_comparison_needs_second_title(settings, sample):
    project, meta = sample
    settings.script.target_minutes["comparison"] = 1
    with pytest.raises(ValueError, match="--with"):
        generate_script(settings, project, meta, "comparison", "en", FakeLlm("x"))
    other = meta.model_copy(update={"title": "Interstellar", "tmdb_id": 2})
    llm = FakeLlm(make_script(30, sections=["a", "b", "c", "d", "e"]))
    res = generate_script(settings, project, meta, "comparison", "en", llm, other=other)
    assert "Interstellar" in llm.prompts[0]
    assert parse.read_script(res.path)[0]["compared_with"] == "Interstellar"


def test_prompt_language_and_spoiler_rules(sample):
    _, meta = sample
    hi = build_prompt(meta, get_format("ending_explained"), "hi", 1000, channel="Cine Kahani",
                      hindi_script="devanagari")
    assert "Devanagari" in hi and "spoiler warning" in hi.lower()
    hing = build_prompt(meta, get_format("review"), "hi", 1000, channel="C", hindi_script="hinglish")
    assert "Latin letters (Hinglish)" in hing and "Do not reveal twists" in hing
    assert all(f.beats for f in FORMATS.values()) and len(FORMATS) == 6


def test_unknown_format_and_language(settings, sample):
    with pytest.raises(KeyError, match="Choose one of"):
        get_format("nope")
    with pytest.raises(ValueError, match="language"):
        generate_script(settings, sample[0], sample[1], "review", "fr", FakeLlm("x"))


class Proc:
    def __init__(self, out="", err="", code=0):
        self.stdout, self.stderr, self.returncode = out, err, code


def test_claude_cli_sends_prompt_on_stdin_with_model():
    seen = {}

    def runner(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return Proc("  hello  ")

    cli = ClaudeCli(Llm(command=["claude", "-p"], model="sonnet"), runner=runner)
    assert cli("PROMPT") == "hello"
    assert seen["cmd"] == ["claude", "-p", "--model", "sonnet"] and seen["input"] == "PROMPT"


def test_claude_cli_retries_then_reports_error():
    outcomes = [Proc(code=1, err="rate limited"), Proc(out="ok")]
    sleeps = []
    cli = ClaudeCli(Llm(retries=2, retry_delay_s=3), runner=lambda *a, **k: outcomes.pop(0), sleep=sleeps.append)
    assert cli("p") == "ok" and sleeps == [3]
    bad = ClaudeCli(Llm(retries=1), runner=lambda *a, **k: Proc(code=2, err="nope"), sleep=lambda s: None)
    with pytest.raises(LlmError, match="nope"):
        bad("p")
    empty = ClaudeCli(Llm(retries=0), runner=lambda *a, **k: Proc(out=" "))
    with pytest.raises(LlmError, match="no text"):
        empty("p")


def test_claude_cli_missing_binary():
    with pytest.raises(LlmError, match="not found"):
        ClaudeCli(Llm(command=["definitely-not-installed-xyz", "-p"]))("p")


def test_end_to_end_cli_with_real_subprocess(tmp_path):
    """Drives the real CLI; a tiny script stands in for `claude -p`."""
    from typer.testing import CliRunner
    from cinekahani.cli import app

    fake = tmp_path / "fake_claude.py"
    fake.write_text(textwrap.dedent(f"""
        import sys
        prompt = sys.stdin.read()
        names = {EN_SECTIONS!r}
        words = 'dil ' if 'Devanagari' in prompt else 'word '
        print('\\n\\n'.join(f'## {{n}}\\n' + words * 50 for n in names))
    """))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "studio.yaml").write_text(
        f"llm:\n  command: ['{sys.executable}', '{fake}']\nscript:\n  target_minutes: {{review: 2}}\n"
        "  words_per_minute: {en: 150, hi: 150}\n")
    r = CliRunner()
    base = ["--root", str(tmp_path)]
    out = r.invoke(app, base + ["meta", "Inception", "--sample"])
    assert out.exit_code == 0 and "inception-2010" in out.output
    out = r.invoke(app, base + ["script", "inception-2010", "--format", "review", "--lang", "both"])
    assert out.exit_code == 0, out.output
    assert (tmp_path / "projects/inception-2010/script/review.hi.md").is_file()
    assert "up to date" in r.invoke(app, base + ["script", "inception-2010", "-l", "en"]).output
    assert r.invoke(app, base + ["approve", "inception-2010", "-l", "en"]).exit_code == 0
    st = r.invoke(app, base + ["status"]).output
    en = next(l for l in st.splitlines() if "review.en:" in l)
    hi = next(l for l in st.splitlines() if "review.hi:" in l)
    assert "[x]approved" in en and "[ ]approved" in hi and "next: narration" in en and "next: approved" in hi
    bad = r.invoke(app, base + ["script", "nope-1999"])
    assert bad.exit_code == 1 and "No project" in bad.output
    assert r.invoke(app, base + ["formats"]).exit_code == 0
    assert r.invoke(app, base + ["meta", "Inception"]).exit_code == 1      # no TMDB key: clear error
