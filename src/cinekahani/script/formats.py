"""Script formats. Each has beats the LLM must hit and a spoiler policy."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Format:
    key: str
    label: str
    spoilers: bool
    goal: str
    beats: tuple[str, ...]
    needs_second_title: bool = False


FORMATS: dict[str, Format] = {f.key: f for f in [
    Format("review", "Review", False,
           "A personal, opinionated review that takes a clear stance.",
           ("Hook: your verdict in one sharp line, no plot recap",
            "What it is: premise in two sentences, spoiler free",
            "What works: two or three specific strengths with concrete examples",
            "What does not: honest weaknesses, not hedged",
            "Craft: direction, performances, sound, pacing",
            "Verdict: who it is for, a score out of 10 with reasons")),
    Format("ending_explained", "Ending explained", True,
           "Explain the ending and argue for the most convincing reading.",
           ("Spoiler warning, then the hook question the ending leaves",
            "Quick setup of what the viewer needs to remember",
            "What literally happens in the final scenes",
            "The main readings of the ending, each fairly stated",
            "Clues in the film that support each reading",
            "Your pick and why, and what the ending says about the theme")),
    Format("things_you_missed", "Things you missed", True,
           "Details, foreshadowing and craft choices most viewers miss on a first watch.",
           ("Spoiler warning, then the hook",
            "Five to seven details, each with where it appears, what it means and why it matters",
            "One detail that changes how the whole film reads",
            "Closing take: is the film better on a second watch?")),
    Format("cast_facts", "Cast facts", False,
           "The people behind the film: cast, crew and what they brought to it.",
           ("Hook: a surprising angle on the cast or crew",
            "The lead and what the role asked of them",
            "Supporting cast worth noticing and why",
            "The director and key crew, and how their style shows",
            "Your take on the casting as a whole")),
    Format("should_you_watch", "Should you watch it", False,
           "A short, decisive recommendation for a viewer deciding tonight.",
           ("Hook: yes, no or it depends, in one line",
            "Who will love it and who will not",
            "What to expect: tone, pace, length, content to know about",
            "Watch it if / skip it if",
            "Final call")),
    Format("comparison", "Comparison", False,
           "Compare two titles on specific points and pick a winner.",
           ("Hook: the question the comparison answers",
            "Quick intro to both, spoiler free",
            "Round by round: story, performances, craft, rewatch value",
            "Where each one wins",
            "Winner, and who should watch which"),
           needs_second_title=True),
]}


def get_format(key: str) -> Format:
    if key not in FORMATS:
        raise KeyError(f"Unknown format '{key}'. Choose one of: {', '.join(FORMATS)}")
    return FORMATS[key]
