from __future__ import annotations

import json

from ..meta.models import TitleMeta
from .formats import Format

PROMPT_VERSION = "1"

LANG_RULES = {
    "en": "Write in natural spoken English, the way a thoughtful friend talks. Short sentences.",
    "hi": ("Write in natural spoken Hindi, conversational and not bookish. "
           "Keep film titles, people's names and common English film terms in Latin letters."),
}


def hindi_rule(script_style: str) -> str:
    if script_style == "hinglish":
        return "Write Hindi in Latin letters (Hinglish), as people text it."
    return "Write Hindi in Devanagari script."


def facts_block(meta: TitleMeta) -> str:
    d = meta.model_dump(exclude={"posters", "backdrops", "sample", "locale"})
    d["cast"] = [f"{p.name} as {p.role}" if p.role else p.name for p in meta.cast[:10]]
    for key in ("directors", "writers", "composers"):
        d[key] = [p.name for p in getattr(meta, key)]
    return json.dumps({k: v for k, v in d.items() if v not in (None, "", [])},
                      ensure_ascii=False, indent=2)


def build_prompt(meta: TitleMeta, fmt: Format, lang: str, target_words: int, *,
                 channel: str, hindi_script: str, other: TitleMeta | None = None,
                 feedback: str = "") -> str:
    lang_rule = LANG_RULES[lang] + (" " + hindi_rule(hindi_script) if lang == "hi" else "")
    spoiler = ("This format may spoil the film. Open with a clear spoiler warning."
               if fmt.spoilers else "Do not reveal twists or the ending.")
    beats = "\n".join(f"- {b}" for b in fmt.beats)
    other_block = ""
    if other is not None:
        other_block = f"\n\nSECOND TITLE (facts from TMDB):\n{facts_block(other)}"
    fb = f"\n\nFIX THIS FROM YOUR LAST ATTEMPT: {feedback}" if feedback else ""
    return f"""You are the scriptwriter for the YouTube channel "{channel}". Write ONE narration script.

FORMAT: {fmt.label}. {fmt.goal}
{spoiler}
LENGTH: about {target_words} spoken words. Do not go far under or over.
LANGUAGE: {lang_rule}

TITLE FACTS (from TMDB):
{facts_block(meta)}{other_block}

BEATS, in this order, one section each:
{beats}

RULES
- The script must be ORIGINAL. Do not copy or closely paraphrase reviews, Wikipedia or other writers.
- It needs real opinions and analysis. Say what you think and why. Plot summary alone is a failure.
- Use only facts from the data above. For any fact you add from your own knowledge (trivia, box office,
  quotes, production stories), write it as [VERIFY: the claim] so a human can check it. Never invent facts.
- Do not quote dialogue at length. At most a few words at a time.
- Narration will be read aloud: no emojis, no markdown bold or italics, no bullet lists, no stage directions,
  no "as an AI". Spell out numbers the way a person would say them.
- Start each section with a heading line "## <short section name>".
- Add at most one cue per section on its own line, like [VISUAL: backdrop] or [VISUAL: poster]
  or [VISUAL: trailer moment, <what is shown>]. Cues are not read aloud.
- Output only the script: no preface, no title line, no notes after it.{fb}
"""
