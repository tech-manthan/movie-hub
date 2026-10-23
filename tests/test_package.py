import json

import pytest
from conftest import FakeLlm
from PIL import Image

from cinekahani.assemble.build import render_paths
from cinekahani.core.cache import Decision
from cinekahani.package import pack
from cinekahani.package.thumbnail import MAX_BYTES, render_thumbnail
from cinekahani.visuals.plan import Plan
from cinekahani.visuals.styles import get_style

REPLY = {"titles": [f"Inception explained, take {i}" for i in range(5)],
         "description_intro": "A deep look at how Inception builds its dream rules and why the ending still divides people.",
         "tags": ["inception", "nolan", "dreams", "movie review", "sci fi", "ending", "explained", "film"],
         "thumbnail_text": "WAKE UP?", "thumbnail_text_native": "जागो?", "hashtags": ["Inception", "Nolan", "Movies"],
         "short_titles": ["A better short title"]}


def reply(**over):
    return json.dumps({**REPLY, **over})


@pytest.fixture
def ready(small, narrated):
    project, meta = narrated
    plan = Plan(project.slug, "review", "en", "clean", 1, 1.5, 58, 60.5, meta.title, meta.year,
                music="m.wav", music_credit="Music: Song by Z")
    plan.save(render_paths(project, "review", "en")[1])
    return project, meta


def test_timestamp_and_chapter_rules():
    assert pack.timestamp(0) == "0:00" and pack.timestamp(75) == "1:15" and pack.timestamp(3725) == "1:02:05"
    tl = {"duration": 120, "sections": [{"heading": h, "start": s} for h, s in
                                         [("A", 0), ("B", 5), ("C", 30), ("D", 70), ("E", 118)]]}
    ch = pack.chapters(tl, 2)
    assert ch[0] == ("0:00", "A")                              # first chapter always at 0:00
    assert [n for _, n in ch] == ["A", "C", "D"]               # B folds into A (under 10 s); E is under 10 s from the end
    assert pack.chapters({"duration": 40, "sections": tl["sections"][:2]}, 0) == []    # fewer than three


def test_title_label_tags_and_json_reply():
    assert pack.with_language_label("Great film", "hi") == "Great film (Hindi)"
    assert pack.with_language_label("Great film (English)", "en") == "Great film (English)"
    assert len(pack.with_language_label("x" * 200, "en")) <= 100
    tags = pack.clean_tags(["#Nolan", "nolan", "a" * 40, "dream"], ["Inception"], limit=20)
    assert tags == ["Inception", "Nolan"]               # dupes, over-long tags and the size budget are dropped
    assert sum(len(t) + 1 for t in pack.clean_tags([f"tag{i}" for i in range(500)], [])) <= 450
    assert pack.parse_json_reply('Sure!\n{"a": 1}\nbye') == {"a": 1}
    with pytest.raises(pack.PackageError, match="no JSON"):
        pack.parse_json_reply("nothing")
    with pytest.raises(pack.PackageError, match="not valid"):
        pack.parse_json_reply("{oops}")
    assert pack.validate_reply(REPLY, 5) == []
    assert len(pack.validate_reply({"titles": ["a"], "tags": []}, 5)) == 4


def test_build_package_writes_everything(small, ready):
    project, meta = ready
    llm = FakeLlm(reply())
    r = pack.build_package(small, project, meta, "review", "en", llm)
    d = r.data
    assert d["title"] == "Inception explained, take 0 (English)" and len(d["title_options"]) == 5
    assert "Cine Kahani" in d["tags"] and "सिने कहानी" in d["tags"] and "Inception" in d["tags"]
    desc = d["description"]
    assert desc.index("A deep look") < desc.index("0:00 Hook") < desc.index("Narration: my own voice")
    assert "Music: Song by Z" in desc and "TMDB" in desc and desc.rstrip().endswith("#Inception #Nolan #Movies")
    assert d["chapters"][0][0] == "0:00" and len(d["chapters"]) >= 3 and len(desc) < 5000
    with Image.open(r.thumbnail) as im:
        assert im.size == (1280, 720)
    assert r.thumbnail.stat().st_size <= MAX_BYTES
    assert "narration script" in llm.prompts[0].lower() and "Spoiler free" in llm.prompts[0]
    again = pack.build_package(small, project, meta, "review", "en", llm)
    assert again.decision is Decision.CACHED and len(llm.prompts) == 1


def test_package_edits_are_protected(small, ready):
    project, meta = ready
    llm = FakeLlm(reply())
    r = pack.build_package(small, project, meta, "review", "en", llm)
    data = json.loads(r.path.read_text())
    data["title"] = "My own title"
    r.path.write_text(json.dumps(data))
    assert pack.build_package(small, project, meta, "review", "en", llm).decision is Decision.EDITED_KEPT
    small.package.hashtags = 2                                     # an input changes
    with pytest.raises(pack.PackageError, match="your edits"):
        pack.build_package(small, project, meta, "review", "en", llm)
    forced = pack.build_package(small, project, meta, "review", "en", llm, force=True)
    assert forced.data["title"] != "My own title"


def test_bad_reply_is_retried_once_then_reported(small, ready):
    project, meta = ready
    llm = FakeLlm("not json", reply())
    assert pack.build_package(small, project, meta, "review", "en", llm).decision is Decision.RUN
    assert "FIX:" in llm.prompts[1]
    with pytest.raises(pack.PackageError, match="unusable"):
        pack.build_package(small, project, meta, "review", "en", FakeLlm("nope"), force=True)


def test_shorts_copy_and_hindi_labels(small, ready):
    project, meta = ready
    clips = project.root / "clips" / "review.en"
    clips.mkdir(parents=True)
    (clips / "clips.json").write_text(json.dumps({"clips": [
        {"file": "short-01.mp4", "title": "Original A", "trailer_check": "ok"},
        {"file": "short-02.mp4", "title": "", "trailer_check": "unverified"}]}))
    r = pack.build_package(small, project, meta, "review", "en", FakeLlm(reply()))
    s = r.data["shorts"]
    assert [x["title"] for x in s] == ["A better short title", "Inception #2"]
    assert "#Shorts" in s[0]["description"] and "TMDB" in s[0]["description"] and s[1]["trailer_check"] == "unverified"
    assert "SHORTS (rewrite" not in pack.build_prompt(meta, "review", "hi", "text", [], small, 5)
    assert "Hindi" in pack.build_prompt(meta, "ending_explained", "hi", "text", [], small, 5)
    d = pack.make_description(small, Plan("s", "f", "hi", "clean", 1, 1, 1, 3, "T", 2010), "intro text here", [],
                              True, "hi", [], meta)
    assert d.startswith("Spoiler warning:") and "intro text here" in d


@pytest.mark.parametrize("layout", ["left", "right"])
def test_thumbnail_with_and_without_art(tmp_path, layout):
    bd = tmp_path / "b.jpg"
    po = tmp_path / "p.jpg"
    Image.new("RGB", (1600, 900), (30, 90, 160)).save(bd)
    Image.new("RGB", (400, 600), (160, 40, 40)).save(po)
    for art in ({"backdrop": bd, "poster": po}, {"backdrop": None, "poster": None}):
        out = render_thumbnail(tmp_path / f"t-{layout}.jpg", "Ending explained now", style=get_style("pop"),
                               channel="Cine Kahani", subtitle="Inception (2010)", layout=layout, **art)
        with Image.open(out) as im:
            assert im.size == (1280, 720)
        assert out.stat().st_size <= MAX_BYTES


def test_thumbnail_native_falls_back_to_latin_when_unsupported(tmp_path, monkeypatch):
    from cinekahani.package import thumbnail
    monkeypatch.setattr(thumbnail, "can_render_native", lambda: False)
    out = render_thumbnail(tmp_path / "t.jpg", "जागो", style=get_style("noir"), backdrop=None, poster=None,
                           channel="Cine Kahani", native=True)
    assert out.is_file()


def test_package_cli(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from cinekahani import cli
    r = CliRunner()
    (tmp_path / "config").mkdir()
    out = r.invoke(cli.app, ["--root", str(tmp_path), "package", "nothing-1"])
    assert out.exit_code == 1 and "No project" in out.output
    out = r.invoke(cli.app, ["--root", str(tmp_path), "thumbnail", "nothing-1", "-l", "en"])
    assert out.exit_code == 1
