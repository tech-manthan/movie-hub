import pytest

from cinekahani.core.config import load_settings
from cinekahani.core.project import Project
from cinekahani.meta.fetch import fetch_meta

EN_SECTIONS = ["Hook", "What it is", "What works", "What does not", "Craft", "Verdict"]


def make_script(words_per_section=60, extra="", sections=EN_SECTIONS):
    out = []
    for name in sections:
        out.append(f"## {name}\n[VISUAL: backdrop]\n" + " ".join(["word"] * words_per_section))
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
