import json

import pytest
from helpers import FakeVoiceStudio, tone
from conftest import FakeLlm, make_script

from cinekahani.core import wavutil
from cinekahani.core.cache import Decision
from cinekahani.core.config import Cooldown
from cinekahani.core.thermal import cool_down
from cinekahani.script.generate import ScriptNotReady, approve_script, generate_script
from cinekahani.voice.chunker import chunk_text, split_sentences
from cinekahani.voice.client import VoiceError, VoiceStudioClient
from cinekahani.voice.narrate import load_timeline, narrate


def test_split_sentences_handles_hindi_danda():
    assert split_sentences("यह पहला है। यह दूसरा है? हाँ! ok.") == ["यह पहला है।", "यह दूसरा है?", "हाँ!", "ok."]


def test_chunking_packs_sentences_and_splits_long_ones():
    text = "One sentence here. Another sentence here. A third one."
    assert chunk_text(text, 25) == ["One sentence here.", "Another sentence here.", "A third one."]
    assert chunk_text(text, 100) == [text]
    long = "word " * 50
    parts = chunk_text(long, 60)
    assert all(len(p) <= 60 for p in parts) and " ".join(parts).split() == long.split()


def test_cool_down_waits_while_throttled():
    sleeps, limits = [], iter([80, 90, 100])
    total = cool_down(Cooldown(between_chunks_s=5, check_thermal=True), sleep=sleeps.append,
                      probe=lambda: next(limits))
    assert sleeps == [5, 20, 20] and total == 45
    assert cool_down(Cooldown(between_chunks_s=0, check_thermal=False), sleep=sleeps.append) == 0
    assert cool_down(Cooldown(between_chunks_s=0), sleep=sleeps.append, probe=lambda: None) == 0


def make_client(settings, fake):
    return VoiceStudioClient(settings.voice, {}, transport=fake.transport, sleep=lambda s: None)


def test_client_generate_and_errors(settings):
    fake = FakeVoiceStudio()
    c = make_client(settings, fake)
    sp = c.generate("hello there", language="en", profile_id="p1")
    assert sp.audio_id == "a1" and wavutil.read.__name__
    call = fake.calls[0]
    assert call["engine"] == "voxcpm2" and call["profile_id"] == "p1" and call["wav_bits"] == "16"
    assert c.profiles()[0]["id"] == "p1"


def test_client_reports_unreachable_and_rejections(settings):
    import httpx

    def refuse(req):
        raise httpx.ConnectError("nope")

    c = VoiceStudioClient(settings.voice, {}, transport=httpx.MockTransport(refuse), sleep=lambda s: None)
    with pytest.raises(VoiceError, match="Open the VoiceStudio"):
        c.profiles()
    calls = []
    c = VoiceStudioClient(settings.voice, {}, sleep=lambda s: None, transport=httpx.MockTransport(
        lambda r: (calls.append(1), httpx.Response(500, text="oops"))[1]))
    with pytest.raises(VoiceError, match="500"):
        c.profiles()
    assert len(calls) == settings.voice.retries + 1
    c = VoiceStudioClient(settings.voice, {}, transport=httpx.MockTransport(lambda r: httpx.Response(422, text="bad")))
    with pytest.raises(VoiceError, match="422"):
        c.generate("x", language="en", profile_id="p")


def test_clone_profile_upload(settings, tmp_path):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(tone(1))
    assert make_client(settings, FakeVoiceStudio()).create_clone("me", ref, "hello") == "p-new"


@pytest.fixture
def approved(settings, sample):
    project, meta = sample
    generate_script(settings, project, meta, "review", "en", FakeLlm(make_script(50, unique=True)))
    settings.voice.max_chunk_chars = 200
    settings.cooldown.between_chunks_s = 7
    settings.cooldown.check_thermal = False
    settings.voice.profile = {"en": "p1"}
    return project


def test_narrate_requires_approval_and_profile(settings, approved):
    fake = FakeVoiceStudio()
    with pytest.raises(ScriptNotReady, match="draft"):
        narrate(settings, approved, "review", "en", make_client(settings, fake))
    approve_script(approved, "review", "en")
    settings.voice.profile = {}
    with pytest.raises(VoiceError, match="voice setup"):
        narrate(settings, approved, "review", "en", make_client(settings, fake))


def test_narrate_builds_audio_timeline_and_caches(settings, approved):
    approve_script(approved, "review", "en")
    fake, sleeps = FakeVoiceStudio(), []
    client = make_client(settings, fake)
    r = narrate(settings, approved, "review", "en", client, sleep=sleeps.append, probe=lambda: None)
    assert r.decision is Decision.RUN and r.generated == len(fake.calls) > 0
    assert sleeps == [7] * (r.generated - 1)                      # pause only while more speaking is needed
    assert abs(wavutil.duration(r.wav) - r.duration) < 0.01
    tl = load_timeline(approved, "review", "en")
    assert [s["heading"] for s in tl["sections"]][:2] == ["Hook", "What it is"]
    prev = 0
    for sec in tl["sections"]:
        assert sec["start"] >= prev and sec["end"] > sec["start"]
        prev = sec["end"]
        for para in sec["paragraphs"]:
            assert para["start"] >= sec["start"] and para["end"] <= sec["end"] + 0.001
            assert para["sentences"][0]["start"] == pytest.approx(para["start"], abs=0.01)
    assert tl["sections"][0]["cues"] == ["backdrop"]
    assert tl["duration"] == r.duration
    n = len(fake.calls)
    again = narrate(settings, approved, "review", "en", client, sleep=sleeps.append)
    assert again.decision is Decision.CACHED and len(fake.calls) == n


def test_edit_only_respeaks_changed_chunks(settings, approved):
    approve_script(approved, "review", "en")
    fake = FakeVoiceStudio()
    client = make_client(settings, fake)
    narrate(settings, approved, "review", "en", client, sleep=lambda s: None, probe=lambda: None)
    first = len(fake.calls)
    path = approved.root / "script" / "review.en.md"
    path.write_text(path.read_text().replace("## Verdict\n[VISUAL: backdrop]\nword5", "## Verdict\n[VISUAL: backdrop]\nchanged", 1))
    r = narrate(settings, approved, "review", "en", client, sleep=lambda s: None, probe=lambda: None)
    assert r.decision is Decision.RUN and len(fake.calls) - first == r.generated and 0 < r.generated < first


def test_mixed_sample_rates_are_conformed(settings, approved):
    approve_script(approved, "review", "en")
    fake = FakeVoiceStudio()
    rates = iter([24000, 16000] * 50)
    orig = fake.handle

    def varying(req):
        fake.rate = next(rates) if req.url.path == "/generate" else fake.rate
        return orig(req)

    import httpx
    fake.transport = httpx.MockTransport(varying)
    r = narrate(settings, approved, "review", "en", make_client(settings, fake), sleep=lambda s: None, probe=lambda: None)
    assert r.wav.is_file() and r.duration > 5


def test_cli_voice_and_narrate(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from cinekahani import cli
    from cinekahani.core.config import load_settings

    fake = FakeVoiceStudio()
    real = cli.VoiceStudioClient
    monkeypatch.setattr(cli, "VoiceStudioClient", lambda cfg, env=None: real(cfg, env, transport=fake.transport, sleep=lambda s: None))
    (tmp_path / "config").mkdir()
    ref = tmp_path / "ref.wav"
    ref.write_bytes(tone(1))
    r = CliRunner()
    base = ["--root", str(tmp_path)]
    assert "me (p1)" in r.invoke(cli.app, base + ["voice", "check"]).output
    out = r.invoke(cli.app, base + ["voice", "setup", "--wav", str(ref), "--text", "hi"])
    assert out.exit_code == 0 and "p-new" in out.output
    assert load_settings(tmp_path).voice.profile == {"en": "p-new", "hi": "p-new"}
    bad = r.invoke(cli.app, base + ["narrate", "nothing-1999"])
    assert bad.exit_code == 1 and "No project" in bad.output
