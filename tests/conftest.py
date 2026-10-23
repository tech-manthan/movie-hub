import pytest

from cinekahani.core.config import load_settings
from cinekahani.core.project import Project
from cinekahani.meta.fetch import fetch_meta
from cinekahani.script.generate import approve_script, generate_script
from cinekahani.voice.client import VoiceStudioClient
from cinekahani.voice.narrate import narrate
from helpers import FakeVoiceStudio

EN_SECTIONS = ["Hook", "What it is", "What works", "What does not", "Craft", "Verdict"]


def make_script(words_per_section=60, extra="", sections=EN_SECTIONS, unique=False):
    out = []
    for i, name in enumerate(sections):
        w = f"word{i}" if unique else "word"
        out.append(f"## {name}\n[VISUAL: backdrop]\n" + " ".join([w] * words_per_section))
    return "\n\n".join(out) + extra


class FakeLlm:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "config").mkdir()
    s = load_settings(tmp_path)
    s.script.target_minutes["review"] = 2          # 300 words in en
    return s


@pytest.fixture
def sample(settings):
    r = fetch_meta(settings, "Inception", sample=True)
    return r.project, r.meta


@pytest.fixture
def small(settings):
    v = settings.video
    v.width, v.height, v.fps = 320, 180, 15
    v.shot_seconds, v.intro_seconds, v.outro_seconds, v.batch_size = (3, 5), 1, 1.5, 100
    settings.voice.profile = {"en": "p1"}
    settings.voice.max_chunk_chars = 400
    settings.cooldown.between_chunks_s = 0
    settings.cooldown.check_thermal = False
    return settings


@pytest.fixture
def narrated(small, sample):
    project, meta = sample
    generate_script(small, project, meta, "review", "en", FakeLlm(make_script(
        30, unique=True, sections=["Hook", "What it is", "What works", "What does not", "Craft", "Verdict"])))
    approve_script(project, "review", "en")
    client = VoiceStudioClient(small.voice, {}, transport=FakeVoiceStudio().transport, sleep=lambda s: None)
    narrate(small, project, "review", "en", client, sleep=lambda s: None, probe=lambda: None)
    return project, meta
