import io
import json
import subprocess

import httpx
import pytest
from conftest import FakeLlm, make_script
from helpers import FakeVoiceStudio, tone
from PIL import Image

from cinekahani.assemble import music as music_lib
from cinekahani.assemble import subtitles
from cinekahani.assemble.build import build_video
from cinekahani.assemble.render import RenderError, encoder_args, fades, motion_exprs, probe_video
from cinekahani.core.cache import Decision
from cinekahani.core.guardrails import check_trailer_plan
from cinekahani.core.variety import Variety
from cinekahani.meta.models import Image as Img
from cinekahani.script.generate import approve_script, generate_script
from cinekahani.visuals import trailer
from cinekahani.visuals.plan import GuardrailError, Plan, Shot, enforce_guardrails, plan_video
from cinekahani.visuals.styles import STYLES, get_style
from cinekahani.voice.client import VoiceStudioClient
from cinekahani.voice.narrate import load_timeline, narrate


def jpeg(color=(200, 80, 40), size=(640, 360)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def art_client():
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=jpeg())))


def with_art(meta):
    return meta.model_copy(update={"sample": False,
                                   "posters": [Img(url="https://image.tmdb.org/t/p/original/p1.jpg")],
                                   "backdrops": [Img(url=f"https://image.tmdb.org/t/p/original/b{i}.jpg") for i in range(4)]})


def make_trailer(project, seconds=30):
    dest = trailer.source_path(project)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size=640x360:rate=15:duration={seconds}",
                    "-pix_fmt", "yuv420p", str(dest)], check=True)
    return dest


# ---------- pure pieces ----------

def test_wrap_and_subtitle_events():
    assert subtitles.wrap("one two three four five", 9) == ["one two", "three", "four five"]
    tl = {"sections": [{"paragraphs": [{"sentences": [
        {"text": "short one.", "start": 0.0, "end": 0.2}, {"text": "next sentence here.", "start": 2.0, "end": 3.0}]}]}]}
    ev = subtitles.events(tl, 40, 2.0)
    assert ev[0][0] == 2.0 and ev[0][1] == pytest.approx(2.0 + subtitles.MIN_SHOW) and ev[0][1] <= ev[1][0]
    assert ev[1][2] == "next sentence here."


def test_long_sentence_splits_into_timed_two_line_events():
    text = " ".join(["word"] * 40)
    ev = subtitles.events({"sections": [{"paragraphs": [{"sentences": [{"text": text, "start": 0, "end": 10}]}]}]}, 20, 0)
    assert len(ev) > 1 and ev[0][0] == 0 and ev[-1][1] == pytest.approx(10)
    assert all(e[2].count("\\N") <= 1 for e in ev)
    assert all(ev[i][1] <= ev[i + 1][0] + 1e-6 for i in range(len(ev) - 1))


def test_ass_file_has_styles_and_dialogue(small):
    tl = {"sections": [{"paragraphs": [{"sentences": [{"text": "Hello {world}.", "start": 0, "end": 2}]}]}]}
    ass = subtitles.build_ass(tl, small, get_style("clean"), "en", 1.0)
    assert "PlayResX: 320" in ass and "Style: Default,Arial" in ass
    assert "Dialogue: 0,0:00:01.00,0:00:03.00" in ass and "{world}" not in ass
    assert "Kohinoor Devanagari" in subtitles.build_ass(tl, small, get_style("pop"), "hi", 0)


def test_motion_fades_and_encoder_choice(small):
    z, x, y = motion_exprs("in", 100)
    assert "on/100" in z and "iw/2" in x
    assert fades(4, "cut") == "" and "fade=t=out:st=3.800" in fades(4, "dip")
    class R:                      # pretend ffmpeg lists the Mac hardware encoder
        stdout = " V..... h264_videotoolbox H.264"
    assert "h264_videotoolbox" in encoder_args(small, runner=lambda *a, **k: R(), platform="darwin")
    assert "libx264" in encoder_args(small, runner=lambda *a, **k: R(), platform="linux")
    small.video.encoder = "libx264"
    assert "-crf" in encoder_args(small)


def test_variety_rotation_and_format_suggestion(tmp_path):
    v = Variety(tmp_path)
    first = v.pick(list(STYLES), "seed", "style", 1)
    v.record(slug="a", format="review", lang="en", style=first)
    assert Variety(tmp_path).pick(list(STYLES), "seed", "style", 1) != first      # never twice in a row
    assert v.suggest_format(["review", "cast_facts"]) == "cast_facts"
    for f in ["review", "cast_facts", "review"]:
        v.record(slug=f + "x", format=f, lang="en")
    assert v.suggest_format(["review", "cast_facts"]) == "cast_facts"             # used longest ago
    with pytest.raises(ValueError):
        v.pick([], "s", "style")


def test_music_rules(tmp_path, small):
    f = tmp_path / "a.wav"
    f.write_bytes(tone(1))
    lib = tmp_path / "lib"
    cfg = small.video.music
    with pytest.raises(music_lib.MusicError, match="NonCommercial"):
        music_lib.add_track(lib, f, cfg, license="CC-BY-NC", source="x")
    with pytest.raises(music_lib.MusicError, match="credit"):
        music_lib.add_track(lib, f, cfg, license="cc-by", source="x")
    with pytest.raises(music_lib.MusicError, match="where"):
        music_lib.add_track(lib, f, cfg, license="cc0")
    with pytest.raises(music_lib.MusicError, match="audio file"):
        music_lib.add_track(lib, tmp_path / "x.txt", cfg, license="cc0", source="x")
    t = music_lib.add_track(lib, f, cfg, license="CC0", source="https://example.org", mood="Calm")
    assert t.license == "cc0" and t.mood == "calm" and (lib / "a.wav").is_file()
    g = tmp_path / "b.wav"
    g.write_bytes(tone(1, freq=300))
    music_lib.add_track(lib, g, cfg, license="cc-by", source="y", artist="Z", title="Song", credit="Music: Song by Z")
    v = Variety(tmp_path)
    first = music_lib.choose_track(lib, cfg, v, "s", "review")
    v.record(slug="p", format="review", lang="en", music=first.file)
    assert music_lib.choose_track(lib, cfg, v, "s", "review").file != first.file
    assert music_lib.credit_line(music_lib.load_tracks(lib)[1]) == "Music: Song by Z"
    assert music_lib.choose_track(tmp_path / "none", cfg, v, "s", "review") is None
    log = lib / "licenses.yaml"
    log.write_text(log.read_text().replace("license: cc0", "license: cc-by-nc"))
    assert [t.file for t in music_lib.eligible(lib, cfg)] == ["b.wav"]            # a bad log entry is ignored


def test_pick_moments():
    assert trailer.pick_moments(0, 60, 3) == []
    m = trailer.pick_moments(3, 100, 3)
    assert m == sorted(m) and 8 <= m[0] and m[-1] <= 90
    assert trailer.pick_moments(2, 100, 3, [10.0, 500.0])[:2] == [10.0, 97.0]
    assert trailer.pick_moments(3, 100, 3, [10.0])[0] == 10.0 and len(trailer.pick_moments(3, 100, 3, [10.0])) == 3
    assert trailer.pick_moments(2, 2, 3) == [0.0, 0.0]


def test_fetch_trailer_rules(small, sample, tmp_path):
    project, meta = sample
    with pytest.raises(trailer.TrailerError, match="sample"):
        trailer.fetch_trailer(small, project, meta.trailer_youtube_id)
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        open(cmd[-2], "wb").write(b"x")

        class P:
            returncode, stderr = 0, ""
        return P()

    p = trailer.fetch_trailer(small, project, "abc123", runner=runner)
    assert p.is_file() and calls[0][-1].endswith("abc123")
    assert trailer.fetch_trailer(small, project, "abc123", runner=runner) == p and len(calls) == 1
    small.guardrails.trailer.enabled = False
    p.unlink()
    with pytest.raises(trailer.TrailerError, match="switched off"):
        trailer.fetch_trailer(small, project, "abc123", runner=runner)


# ---------- planning ----------

def plan_for(small, project, meta, **kw):
    tl = load_timeline(project, "review", "en")
    art = {"backdrops": [f"/b{i}.jpg" for i in range(4)], "posters": ["/p.jpg"]}
    return plan_video(small, project.slug, meta, tl, get_style("clean"), art, fmt="review", lang="en", **kw), tl


def test_plan_covers_timeline_without_gaps(small, narrated):
    project, meta = narrated
    plan, tl = plan_for(small, project, meta)
    assert plan.shots[0].kind == "card" and plan.shots[-1].src == "outro"
    t = 0.0
    for s in plan.shots:
        assert s.start == pytest.approx(t, abs=0.01)
        t += s.dur
    assert t == pytest.approx(plan.total_s, abs=0.02) and plan.total_s == pytest.approx(1 + tl["duration"] + 1.5)
    assert plan.snippets() == []                                   # no trailer available -> none planned
    assert {s.kind for s in plan.shots} >= {"image", "card"}


def test_plan_places_capped_trailer_snippets(small, narrated):
    project, meta = narrated
    tl = load_timeline(project, "review", "en")
    for sec in tl["sections"]:
        sec["cues"] = ["trailer moment, something"]
    art = {"backdrops": ["/b.jpg"], "posters": []}
    plan = plan_video(small, project.slug, meta, tl, get_style("clean"), art, fmt="review", lang="en",
                      trailer_length=60, trailer_source="/t.mp4")
    snips = [s for s in plan.shots if s.kind == "trailer"]
    assert snips and all(s.dur <= 3 for s in snips)
    assert plan.trailer_share() <= 0.20 and len({s.trailer_start for s in snips}) == len(snips)
    starts = sorted(s.start for s in snips)
    assert all(b - a >= 8 for a, b in zip(starts, starts[1:]))
    small.guardrails.trailer.enabled = False
    off = plan_video(small, project.slug, meta, tl, get_style("clean"), art, fmt="review", lang="en",
                     trailer_length=60, trailer_source="/t.mp4")
    assert off.snippets() == []


def test_hand_edited_plan_is_stopped_by_guardrails(small, narrated):
    project, meta = narrated
    plan, _ = plan_for(small, project, meta)
    plan.shots[1] = Shot("trailer", plan.shots[1].start, 9.0, "/t.mp4")
    with pytest.raises(GuardrailError, match="max 3s"):
        enforce_guardrails(plan, small)
    path = project.root / "renders" / "p.json"
    plan.save(path)
    assert Plan.load(path).shots[1].kind == "trailer"
    assert check_trailer_plan(plan.snippets(), plan.total_s, small.guardrails.trailer, plan.narration_s)


# ---------- real ffmpeg ----------

def test_build_with_title_cards_only_and_cache(small, narrated):
    project, meta = narrated
    r = build_video(small, project, meta, "review", "en", style_key="noir", sleep=lambda s: None)
    assert r.decision is Decision.RUN and r.video.is_file()
    info = probe_video(r.video)
    assert info["video"] and info["audio"] and (info["width"], info["height"]) == (320, 180)
    assert abs(info["duration"] - r.plan.total_s) < 1.0
    assert r.report["checks"] == "passed" and r.report["trailer_share"] == 0 and r.report["subtitles"]
    assert build_video(small, project, meta, "review", "en", style_key="noir").decision is Decision.CACHED
    assert (project.root / "renders" / "subs.review.en.ass").read_text().count("Dialogue:") > 5


def test_build_with_artwork_trailer_and_music(small, narrated, tmp_path):
    project, meta = narrated
    meta = with_art(meta)
    make_trailer(project)
    f = tmp_path / "m.wav"
    f.write_bytes(tone(20, rate=22050, freq=220))
    music_lib.add_track(small.path("music"), f, small.video.music, license="cc0", source="https://example.org")
    tl_path = project.root / "audio" / "timeline.review.en.json"
    tl = json.loads(tl_path.read_text())
    for sec in tl["sections"]:
        sec["cues"] = ["trailer moment, x"]
    tl_path.write_text(json.dumps(tl))
    shots_before = len(list((project.root / "visuals").glob("shots/*"))) if (project.root / "visuals" / "shots").exists() else 0
    r = build_video(small, project, meta, "review", "en", style_key="pop", http=art_client(), sleep=lambda s: None)
    assert r.report["trailer_snippets"] >= 1 and 0 < r.report["trailer_share"] <= 0.20
    assert r.report["music"] == "m.wav" and r.report["artwork"] == {"posters": 1, "backdrops": 4}
    kinds = {s.kind for s in r.plan.shots}
    assert {"image", "trailer", "card"} <= kinds
    info = probe_video(r.video)
    assert info["audio"] and abs(info["duration"] - r.plan.total_s) < 1.0
    # shots are cached: a forced re-assembly renders no new shot files
    n = len(list((project.root / "visuals" / "shots").glob("*.mp4")))
    build_video(small, project, meta, "review", "en", style_key="pop", http=art_client(), force=True)
    assert len(list((project.root / "visuals" / "shots").glob("*.mp4"))) == n


def test_build_requires_narration(small, sample):
    project, meta = sample
    with pytest.raises(RenderError, match="narrate"):
        build_video(small, project, meta, "review", "en")


def test_ffmpeg_failure_is_reported(small, narrated):
    project, meta = narrated

    class Bad:
        returncode, stdout, stderr = 1, "", "boom: bad filter"

    import subprocess

    def runner(cmd, **kw):
        return subprocess.run(cmd, **kw) if "-h" in cmd else Bad()      # real filter check, failing render

    with pytest.raises(RenderError, match="bad filter"):
        build_video(small, project, meta, "review", "en", style_key="clean", runner=runner)


def no_libass(real=None):
    import subprocess

    class P:
        returncode, stdout, stderr = 0, "", "Unknown filter 'subtitles'."

    def runner(cmd, **kw):
        return P() if ("-h" in cmd and "filter=subtitles" in cmd) else subprocess.run(cmd, **kw)
    return runner


def test_has_filter_detection():
    from cinekahani.assemble.render import has_filter

    class P:
        def __init__(self, out, err=""):
            self.stdout, self.stderr, self.returncode = out, err, 0

    assert has_filter("subtitles", runner=lambda *a, **k: P("Filter subtitles\n  Render text subtitles"))
    assert not has_filter("subtitles", runner=lambda *a, **k: P("", "Unknown filter 'subtitles'."))
    assert not has_filter("subtitles", runner=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert has_filter("subtitles")                                  # this machine's ffmpeg has it


def test_overlay_list_covers_the_whole_video(small, narrated, tmp_path):
    from cinekahani.assemble.subtitles import _ass_rgba, write_overlay
    assert _ass_rgba("&H0000E0FF") == (255, 224, 0, 255) and _ass_rgba("&H00000000") == (0, 0, 0, 255)
    project, _ = narrated
    tl = load_timeline(project, "review", "en")
    total = tl["duration"] + 2.5
    lst = write_overlay(tl, small, get_style("clean"), "en", 1.0, total, tmp_path / "ov")
    text = lst.read_text()
    assert text.startswith("ffconcat version 1.0") and text.count("duration") >= 6
    secs = sum(float(l.split()[1]) for l in text.splitlines() if l.startswith("duration"))
    assert secs == pytest.approx(total, abs=0.05)
    pngs = {l.split("'")[1] for l in text.splitlines() if l.startswith("file")}
    assert len(pngs) > 3 and all(__import__("pathlib").Path(p).is_file() for p in pngs)


def test_overlay_needs_raqm_for_hindi(small, monkeypatch):
    from cinekahani.assemble import subtitles
    monkeypatch.setattr("cinekahani.visuals.fonts.find_font", lambda script="latin": "/some/font.ttc")
    monkeypatch.setattr("PIL.features.check", lambda name: False)
    assert "libraqm" in subtitles.overlay_ready("hi") and subtitles.overlay_ready("en") is None
    monkeypatch.setattr("cinekahani.visuals.fonts.find_font", lambda script="latin": None)
    assert "Devanagari font" in subtitles.overlay_ready("hi")


def test_build_without_libass_draws_subtitles_itself(small, narrated):
    import subprocess
    project, meta = narrated

    def frame(path, t):
        return subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1",
                               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout

    with_subs = build_video(small, project, meta, "review", "en", style_key="pop", runner=no_libass())
    assert with_subs.report["subtitle_mode"] == "overlay" and with_subs.report["checks"] == "passed"
    drawn = frame(with_subs.video, 4.0)                              # grab it before the next build overwrites the file
    small.video.subtitles.enabled = False
    plain = build_video(small, project, meta, "review", "en", style_key="pop", force=True)
    assert plain.report["subtitle_mode"] == "off"
    assert drawn != frame(plain.video, 4.0)                          # the text is really drawn
    assert abs(probe_video(plain.video)["duration"] - with_subs.plan.total_s) < 1.0


def test_no_libass_hindi_without_raqm_is_a_clear_error(small, narrated, monkeypatch):
    project, meta = narrated
    from cinekahani.assemble import subtitles
    monkeypatch.setattr(subtitles, "overlay_ready", lambda lang: "Pillow cannot shape Devanagari (libraqm)")
    with pytest.raises(RenderError, match="libraqm"):
        build_video(small, project, meta, "review", "en", style_key="clean", runner=no_libass())
