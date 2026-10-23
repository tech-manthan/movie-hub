"""Step 2: write a script with `claude -p`, as an editable markdown file."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from ..core.cache import Decision
from ..core.config import Settings
from ..core.hashing import hash_json, sha
from ..core.logs import get_logger
from ..core.project import Project
from ..meta.models import TitleMeta
from . import parse
from .formats import get_format
from .prompts import PROMPT_VERSION, build_prompt

log = get_logger("script")
LANGS = ("en", "hi")


class ScriptEditedError(RuntimeError):
    pass


class ScriptNotReady(RuntimeError):
    pass


@dataclass
class ScriptResult:
    path: object
    decision: Decision
    warnings: list[str] = field(default_factory=list)


def script_path(project: Project, fmt: str, lang: str):
    return project.dir("script") / f"{fmt}.{lang}.md"


def _body_hash(path) -> str | None:
    if not path.is_file():
        return None
    return sha(parse.read_script(path)[1].strip())


def clean_output(text: str) -> str:
    text = re.sub(r"^```[a-z]*\n|\n```\s*$", "", text.strip())
    first = text.find("## ")
    if first > 0:
        text = text[first:]          # drop any preface or title line before the first section
    return text.strip()


def validate(body: str, expected_words: int, tolerance: tuple[float, float], min_sections: int) -> list[str]:
    problems = []
    secs = parse.sections(body)
    if len(secs) < min_sections:
        problems.append(f"only {len(secs)} sections with '## ' headings; expected at least {min_sections}")
    words = parse.word_count(body)
    lo, hi = int(expected_words * tolerance[0]), int(expected_words * tolerance[1])
    if words < lo:
        problems.append(f"too short: {words} words, wanted about {expected_words}")
    elif words > hi:
        problems.append(f"too long: {words} words, wanted about {expected_words}")
    return problems


def generate_script(settings: Settings, project: Project, meta: TitleMeta, fmt_key: str, lang: str,
                    llm: Callable[[str], str], *, other: TitleMeta | None = None,
                    force: bool = False, model_name: str = "default") -> ScriptResult:
    fmt = get_format(fmt_key)
    if lang not in LANGS:
        raise ValueError(f"language must be one of {LANGS}")
    if fmt.needs_second_title and other is None:
        raise ValueError(f"'{fmt.key}' compares two titles: pass a second title with --with")
    minutes = settings.script.target_minutes[fmt.key]
    target = minutes * settings.script.words_per_minute[lang]
    path = script_path(project, fmt.key, lang)
    key = f"script:{fmt.key}:{lang}"
    input_hash = hash_json({
        "meta": meta.model_dump(mode="json"), "other": other.model_dump(mode="json") if other else None,
        "fmt": fmt.key, "lang": lang, "pv": PROMPT_VERSION, "model": model_name,
        "hindi": settings.hindi_script, "target": target, "channel": settings.channel.name})

    manifest = project.manifest
    decision = manifest.decide(key, input_hash, _body_hash(path), force=force)
    if decision is Decision.CACHED:
        log.info("%s is up to date; skipping (use --force to rewrite)", path.name)
        return ScriptResult(path, decision)
    if decision is Decision.EDITED_KEPT:
        log.info("%s has your edits and its inputs are unchanged; keeping it", path.name)
        return ScriptResult(path, decision)
    if decision is Decision.EDITED_BLOCKED:
        raise ScriptEditedError(
            f"{path.name} has your hand edits, but its inputs changed (new metadata, settings or prompt). "
            "Rewriting would erase your edits. Copy the file somewhere safe and re-run with --force.")

    feedback, body, problems = "", "", []
    for attempt in (1, 2):
        prompt = build_prompt(meta, fmt, lang, target, channel=settings.channel.name,
                              hindi_script=settings.hindi_script, other=other, feedback=feedback)
        log.info("asking Claude for the %s script in %s (attempt %d, about %d words)",
                 fmt.label, lang, attempt, target)
        body = clean_output(llm(prompt))
        problems = validate(body, target, settings.script.length_tolerance, max(3, len(fmt.beats) - 2))
        if not problems:
            break
        feedback = "; ".join(problems) + ". Follow the beats and the word count."
        log.warning("script check: %s", feedback)
        if not any(p.startswith(("only", "too short")) for p in problems):
            break                    # too long is only a warning: you can trim it by hand

    open_verify = parse.verify_tags(body)
    front = {
        "title": meta.title, "year": meta.year, "format": fmt.key, "language": lang,
        "status": "draft", "words": parse.word_count(body), "target_words": target,
        "model": model_name, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "warnings": problems, "verify_open": len(open_verify),
        "compared_with": other.title if other else None,
    }
    path.write_text(parse.render(front, body), encoding="utf-8")
    manifest.record(key, input_hash, sha(body.strip()), [path.name])
    if open_verify:
        log.warning("%d claim(s) marked [VERIFY: ...] must be checked or removed before approval", len(open_verify))
    return ScriptResult(path, Decision.RUN, problems)


def approve_script(project: Project, fmt: str, lang: str):
    path = script_path(project, fmt, lang)
    if not path.is_file():
        raise ScriptNotReady(f"{path.name} does not exist. Write it with `cinekahani script` first.")
    front, body = parse.read_script(path)
    tags = parse.verify_tags(body)
    if tags:
        raise ScriptNotReady("Cannot approve while [VERIFY: ...] claims remain. Check each one, then "
                             "edit the tag away (keep the claim or delete it):\n  - " + "\n  - ".join(tags))
    front["status"] = "approved"
    front["verify_open"] = 0
    front["approved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.write_text(parse.render(front, body), encoding="utf-8")
    return path


def require_approved(project: Project, fmt: str, lang: str, needed: bool = True):
    """Later phases call this before using a script."""
    path = script_path(project, fmt, lang)
    if not path.is_file():
        raise ScriptNotReady(f"{path.name} does not exist.")
    front, _ = parse.read_script(path)
    if needed and front.get("status") != "approved":
        raise ScriptNotReady(f"{path.name} is '{front.get('status', 'draft')}'. "
                             f"Review it, then run `cinekahani approve {project.slug} --format {fmt} --lang {lang}`.")
    return path
