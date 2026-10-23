"""Phase 5: titles, description, tags, chapters, Shorts copy and a thumbnail, written by `claude -p`."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..assemble.build import render_paths
from ..core.cache import Decision
from ..core.config import Settings
from ..core.hashing import hash_file, hash_json, sha
from ..core.logs import get_logger
from ..core.project import Project
from ..core.variety import Variety
from ..meta.models import TitleMeta
from ..script import parse
from ..script.formats import get_format
from ..script.generate import require_approved, script_path
from ..script.llm import LlmError
from ..visuals import images
from ..visuals.plan import Plan
from ..visuals.styles import get_style
from ..voice.narrate import load_timeline
from .thumbnail import render_thumbnail

log = get_logger("package")
PV = "1"
LABEL = {"hi": "Hindi", "en": "English"}
TITLE_MAX = 100


class PackageError(RuntimeError):
    pass


@dataclass
class PackageResult:
    path: Path
    thumbnail: Path
    decision: Decision
    data: dict


def package_dir(project: Project, fmt: str, lang: str) -> Path:
    d = project.dir("package") / f"{fmt}.{lang}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def timestamp(sec: float) -> str:
    s = int(round(sec))
    h, m, s = s // 3600, s // 60 % 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def chapters(timeline: dict, intro_s: float) -> list[tuple[str, str]]:
    """YouTube chapters: first at 0:00, at least three, each at least 10 s. Returns [] if that cannot hold."""
    marks: list[tuple[float, str]] = []
    for i, sec in enumerate(timeline["sections"]):
        start = 0.0 if i == 0 else sec["start"] + intro_s
        if marks and start - marks[-1][0] < 10:
            continue                                   # too short: folds into the previous chapter
        marks.append((start, sec["heading"]))
    if len(marks) >= 3 and (timeline["duration"] + intro_s - marks[-1][0]) < 10:
        marks.pop()
    return [(timestamp(t), name) for t, name in marks] if len(marks) >= 3 else []


def with_language_label(title: str, lang: str) -> str:
    label = f"({LABEL[lang]})"
    title = title.strip().strip('"')
    if label.lower() in title.lower():
        return title[:TITLE_MAX]
    return title[: TITLE_MAX - len(label) - 1].rstrip() + " " + label


def clean_tags(tags: list[str], base: list[str], limit: int = 450) -> list[str]:
    out, seen, used = [], set(), 0
    for t in base + tags:
        t = re.sub(r"\s+", " ", str(t)).strip().lstrip("#")
        if not t or len(t) > 30 or t.casefold() in seen:
            continue
        cost = len(t) + (2 if " " in t else 0) + 1
        if used + cost > limit:
            continue
        seen.add(t.casefold())
        out.append(t)
        used += cost
    return out


def parse_json_reply(text: str) -> dict:
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise PackageError("Claude's reply had no JSON object")
    try:
        return json.loads(text[a:b + 1])
    except json.JSONDecodeError as exc:
        raise PackageError(f"Claude's reply was not valid JSON: {exc}") from exc


def validate_reply(d: dict, n_titles: int) -> list[str]:
    problems = []
    titles = d.get("titles")
    if not isinstance(titles, list) or len(titles) < max(2, n_titles - 2) or not all(isinstance(t, str) and t.strip() for t in titles):
        problems.append(f"'titles' must be a list of about {n_titles} non-empty strings")
    if not isinstance(d.get("description_intro"), str) or len(d.get("description_intro", "").strip()) < 40:
        problems.append("'description_intro' must be 2-3 sentences")
    if not isinstance(d.get("tags"), list) or len(d.get("tags", [])) < 5:
        problems.append("'tags' must be a list of at least 5 short tags")
    if not isinstance(d.get("thumbnail_text"), str) or not 0 < len(d.get("thumbnail_text", "").split()) <= 5:
        problems.append("'thumbnail_text' must be 1-5 words")
    return problems


def build_prompt(meta: TitleMeta, fmt_key: str, lang: str, script_text: str, shorts: list[dict],
                 settings: Settings, n_titles: int) -> str:
    fmt = get_format(fmt_key)
    lang_rule = ("Write the titles and description in natural Hindi (Devanagari). Keep film names and common English "
                 "film terms in Latin letters." if lang == "hi" else "Write in natural English.")
    short_block = ""
    if shorts:
        lst = "\n".join(f"{i}. {s['title'] or '(untitled)'}" for i, s in enumerate(shorts, 1))
        short_block = f'\nSHORTS (rewrite each title, max 60 characters, same order):\n{lst}\n'
    return f"""You write YouTube packaging for the channel "{settings.channel.name}" about movies and series.
FILM: {meta.title} ({meta.year}), {', '.join(meta.genres)}. Video format: {fmt.label}. {'Contains spoilers.' if fmt.spoilers else 'Spoiler free.'}
{lang_rule}

THE NARRATION SCRIPT (what the video says):
{script_text[:6000]}
{short_block}
Return ONE JSON object and nothing else, with these keys:
- "titles": {n_titles} different title options, each under 70 characters, honest to what the video says, no clickbait
  that the video does not deliver, no ALL CAPS sentences. {'Do not reveal the ending in a spoiler-free format.' if not fmt.spoilers else ''}
- "description_intro": 2-3 sentences that hook a viewer and say what the video covers. No hashtags, no links.
- "tags": 12 to 18 search tags (single words or short phrases, under 30 characters each).
- "thumbnail_text": 2-4 words in LATIN letters (English or Hinglish) that create curiosity, e.g. "ENDING EXPLAINED".
- "thumbnail_text_native": the same idea in the video's own language/script (may equal thumbnail_text).
- "hashtags": {settings.package.hashtags} hashtags without the # sign.
- "short_titles": list of Short titles in the same order as SHORTS above (empty list if none).
Do not mention being an AI. Do not invent facts about the film."""


def hashtag_line(tags: list[str]) -> str:
    return " ".join("#" + re.sub(r"\W+", "", t) for t in tags if re.sub(r"\W+", "", t))


def make_description(settings: Settings, plan: Plan, intro: str, chapter_list, spoiler: bool,
                     lang: str, hashtags: list[str], meta: TitleMeta) -> str:
    parts = []
    if spoiler:
        parts.append("Spoiler warning: this video discusses the ending." if lang == "en"
                     else "Spoiler warning: इस वीडियो में फ़िल्म के अंत की बात होगी।")
    parts.append(intro.strip())
    if chapter_list:
        parts.append("\n".join(f"{t} {name}" for t, name in chapter_list))
    foot = []
    if settings.package.ai_voice_note.strip():
        foot.append(settings.package.ai_voice_note.strip())
    if plan.music_credit:
        foot.append(plan.music_credit)
    foot.append(settings.credit_line)
    parts.append("\n".join(foot))
    tail = hashtag_line(hashtags)
    if tail:
        parts.append(tail)
    return "\n\n".join(parts)[:4900]


def shorts_info(project: Project, fmt: str, lang: str) -> list[dict]:
    f = project.root / "clips" / f"{fmt}.{lang}" / "clips.json"
    return json.loads(f.read_text(encoding="utf-8"))["clips"] if f.is_file() else []


def render_for(settings: Settings, project: Project, meta: TitleMeta, data: dict, fmt: str, lang: str,
               art: dict | None = None) -> Path:
    plan = Plan.load(render_paths(project, fmt, lang)[1])
    art = art if art is not None else images.collect_artwork(project, meta)
    style = get_style(plan.style)
    layout = Variety(project.root.parent).pick(["left", "right"], f"{project.slug}:{fmt}:{lang}", "layout", 0)
    backdrops = art.get("backdrops") or []
    posters = art.get("posters") or []
    native = settings.package.thumbnail_text_script == "native"
    text = data["thumbnail_text_native"] if native and data.get("thumbnail_text_native") else data["thumbnail_text"]
    out = package_dir(project, fmt, lang) / "thumbnail.jpg"
    return render_thumbnail(out, text, style=style, backdrop=Path(backdrops[0]) if backdrops else None,
                            poster=Path(posters[0]) if posters else None, channel=settings.channel.name,
                            subtitle=f"{meta.title} ({meta.year})" if meta.year else meta.title,
                            layout=layout, native=native)


def build_package(settings: Settings, project: Project, meta: TitleMeta, fmt: str, lang: str,
                  llm: Callable[[str], str], *, force: bool = False, http=None) -> PackageResult:
    sp = require_approved(project, fmt, lang, settings.script.require_approval)
    front, body = parse.read_script(sp)
    video, plan_path, _ = render_paths(project, fmt, lang)
    if not plan_path.is_file():
        raise PackageError(f"No render plan yet. Run `cinekahani build {project.slug} --format {fmt} --lang {lang}`.")
    plan = Plan.load(plan_path)
    timeline = load_timeline(project, fmt, lang)
    shorts = shorts_info(project, fmt, lang)
    narration = "\n\n".join(f"## {s.heading}\n{s.text}" for s in parse.sections(body))
    n = settings.package.title_options
    pkg_path = package_dir(project, fmt, lang) / "package.json"
    input_hash = hash_json({"script": sha(body.strip()), "meta": meta.model_dump(mode="json"), "music": plan.music_credit,
                            "shorts": [s["title"] for s in shorts], "pv": PV, "n": n,
                            "note": settings.package.ai_voice_note, "ch": settings.channel.name,
                            "tags": settings.package.hashtags})
    key = f"package:{fmt}:{lang}"
    manifest = project.manifest
    decision = manifest.decide(key, input_hash, hash_file(pkg_path) if pkg_path.is_file() else None, force=force)
    art = images.collect_artwork(project, meta, client=http)
    if decision in (Decision.CACHED, Decision.EDITED_KEPT):
        data = json.loads(pkg_path.read_text(encoding="utf-8"))
        thumb = package_dir(project, fmt, lang) / "thumbnail.jpg"
        if not thumb.is_file():
            thumb = render_for(settings, project, meta, data, fmt, lang, art)
        log.info("package for %s.%s is up to date; skipping", fmt, lang)
        return PackageResult(pkg_path, thumb, decision, data)
    if decision is Decision.EDITED_BLOCKED:
        raise PackageError(f"{pkg_path.name} has your edits but its inputs changed. Copy it somewhere safe and re-run "
                           "with --force.")

    prompt = build_prompt(meta, fmt, lang, narration, shorts, settings, n)
    reply: dict = {}
    problems: list[str] = []
    for attempt in (1, 2):
        try:
            reply = parse_json_reply(llm(prompt if attempt == 1 else prompt + "\n\nFIX: " + "; ".join(problems)))
            problems = validate_reply(reply, n)
        except PackageError as exc:
            problems = [str(exc)]
        if not problems:
            break
        log.warning("packaging reply problem: %s", "; ".join(problems))
    if problems:
        raise PackageError("Claude's packaging reply was unusable: " + "; ".join(problems))

    titles = [with_language_label(t, lang) for t in reply["titles"] if isinstance(t, str) and t.strip()]
    base_tags = [meta.title, f"{meta.title} {LABEL[lang]}", settings.channel.name, settings.channel.name_hindi,
                 f"{get_format(fmt).label}", "movie review" if lang == "en" else "फिल्म रिव्यू"]
    tags = clean_tags(reply["tags"], base_tags)
    hashtags = [str(h).lstrip("#") for h in reply.get("hashtags", [])][: settings.package.hashtags]
    ch = chapters(timeline, plan.intro_s)
    data = {
        "title": titles[0], "title_options": titles, "language": lang, "format": fmt, "slug": project.slug,
        "description": make_description(settings, plan, reply["description_intro"], ch, get_format(fmt).spoilers,
                                        lang, hashtags, meta),
        "tags": tags, "chapters": ch, "hashtags": hashtags,
        "thumbnail_text": str(reply["thumbnail_text"]).strip(),
        "thumbnail_text_native": str(reply.get("thumbnail_text_native") or reply["thumbnail_text"]).strip(),
        "category": "Entertainment", "video": video.name, "music_credit": plan.music_credit,
        "shorts": [],
    }
    short_titles = reply.get("short_titles") if isinstance(reply.get("short_titles"), list) else []
    for i, s in enumerate(shorts):
        t = (short_titles[i] if i < len(short_titles) and isinstance(short_titles[i], str) and short_titles[i].strip()
             else s["title"] or f"{meta.title} #{i + 1}")
        data["shorts"].append({"file": s["file"], "title": t.strip()[:100],
                               "description": f"{t.strip()}\n\n{meta.title} ({meta.year}) - {settings.channel.name}\n"
                                              f"{settings.credit_line}\n\n#Shorts " + hashtag_line(hashtags),
                               "trailer_check": s.get("trailer_check", "")})
    pkg_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    thumb = render_for(settings, project, meta, data, fmt, lang, art)
    manifest.record(key, input_hash, hash_file(pkg_path), [pkg_path.name, thumb.name])
    log.info("packaged %s.%s: %d title options, %d tags, %d chapters, %d Shorts", fmt, lang, len(titles), len(tags),
             len(ch), len(data["shorts"]))
    return PackageResult(pkg_path, thumb, Decision.RUN, data)
