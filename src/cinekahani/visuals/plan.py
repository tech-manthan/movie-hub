"""Turn a narration timeline into a shot list (and check the trailer guardrails)."""
from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..core.config import Settings
from ..core.guardrails import Snippet, check_trailer_plan
from ..core.hashing import sha
from ..meta.models import TitleMeta
from .styles import Style


class GuardrailError(RuntimeError):
    pass


@dataclass
class Shot:
    kind: str                 # image | poster_card | card | trailer
    start: float              # seconds in the final video
    dur: float
    src: str = ""             # image path, or the card key (intro/outro/title)
    motion: str = "in"        # in | out | left | right
    label: str = ""           # script section this shot belongs to
    trailer_start: float = 0.0


@dataclass
class Plan:
    slug: str
    format: str
    lang: str
    style: str
    intro_s: float
    outro_s: float
    narration_s: float
    total_s: float
    title: str
    year: int | None
    shots: list[Shot] = field(default_factory=list)
    trailer_id: str | None = None
    trailer_source: str = ""
    music: str = ""
    music_credit: str = ""

    def snippets(self) -> list[Snippet]:
        return [Snippet(self.trailer_id or "trailer", s.start, s.dur) for s in self.shots if s.kind == "trailer"]

    def trailer_share(self) -> float:
        return sum(s.dur for s in self.shots if s.kind == "trailer") / self.total_s if self.total_s else 0.0

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Plan":
        d = json.loads(path.read_text(encoding="utf-8"))
        d["shots"] = [Shot(**s) for s in d["shots"]]
        return cls(**d)


def enforce_guardrails(plan: Plan, settings: Settings) -> None:
    problems = check_trailer_plan(plan.snippets(), plan.total_s, settings.guardrails.trailer,
                                  narration_seconds=plan.narration_s)
    if problems:
        raise GuardrailError("Trailer guardrails failed:\n  - " + "\n  - ".join(problems) +
                             "\nEdit the plan or the limits in config/studio.yaml (guardrails.trailer).")


def _split_span(start: float, end: float, lo: float, hi: float, rng: random.Random) -> list[tuple[float, float]]:
    out, t = [], start
    while end - t > 1e-6:
        d = rng.uniform(lo, hi)
        if end - (t + d) < lo * 0.6:          # do not leave a stub shot
            d = end - t
        out.append((round(t, 3), round(min(d, end - t), 3)))
        t = round(t + d, 3)
    return out


def plan_video(settings: Settings, slug: str, meta: TitleMeta, timeline: dict, style: Style, art: dict,
               *, fmt: str, lang: str, trailer_length: float | None = None, trailer_source: str = "",
               moments: list[float] | None = None) -> Plan:
    from .trailer import pick_moments
    v = settings.video
    g = settings.guardrails.trailer
    rng = random.Random(int(sha(f"{slug}:{fmt}:{lang}:{style.key}")[:8], 16))
    narration = float(timeline["duration"])
    total = v.intro_seconds + narration + v.outro_seconds
    plan = Plan(slug, fmt, lang, style.key, v.intro_seconds, v.outro_seconds, narration, total,
                meta.title, meta.year, trailer_id=meta.trailer_youtube_id, trailer_source=trailer_source)
    plan.shots.append(Shot("card", 0.0, v.intro_seconds, "intro", "in", "intro"))

    backdrops, posters = [str(p) for p in art.get("backdrops", [])], [str(p) for p in art.get("posters", [])]
    use_trailer = bool(trailer_length) and g.enabled
    max_snips = int(g.max_share_of_runtime * total / max(g.max_snippet_seconds, 0.1)) if use_trailer else 0
    snips = 0
    last_trailer_end = -999.0
    order: list[str] = []
    n = 0
    lo, hi = v.shot_seconds[0] * style.shot_scale, v.shot_seconds[1] * style.shot_scale
    secs = timeline["sections"]
    for i, sec in enumerate(secs):
        s0 = 0.0 if i == 0 else sec["start"]
        s1 = secs[i + 1]["start"] if i + 1 < len(secs) else narration
        spans = _split_span(s0, s1, lo, hi, rng)
        cue = (sec.get("cues") or [""])[0].lower()
        for j, (st, du) in enumerate(spans):
            at = round(v.intro_seconds + st, 3)
            n += 1
            if (j == 0 and cue.startswith("trailer") and snips < max_snips and at - last_trailer_end >= 8
                    and du > g.max_snippet_seconds + 0.5):
                dur = min(g.max_snippet_seconds, du)
                plan.shots.append(Shot("trailer", at, dur, trailer_source, "in", sec["heading"]))
                snips += 1
                last_trailer_end = at + dur
                # the rest of this span continues with artwork
                rest = du - dur
                if rest > 0.1:
                    plan.shots.append(_art_shot(rng, order, backdrops, posters, style, at + dur, rest, n, sec["heading"],
                                                meta, cue=""))
                continue
            plan.shots.append(_art_shot(rng, order, backdrops, posters, style, at, du, n, sec["heading"], meta,
                                        cue=cue if j == 0 else ""))
    plan.shots.append(Shot("card", round(v.intro_seconds + narration, 3), v.outro_seconds, "outro", "out", "outro"))
    if use_trailer and snips:
        for shot, t in zip([s for s in plan.shots if s.kind == "trailer"],
                           pick_moments(snips, trailer_length, g.max_snippet_seconds, moments)):
            shot.trailer_start = t
    enforce_guardrails(plan, settings)
    return plan


def _art_shot(rng, order, backdrops, posters, style, at, dur, n, label, meta, cue) -> Shot:
    motion = rng.choice(style.motions)
    if not backdrops and not posters:
        return Shot("card", at, dur, "title", motion, label)
    if posters and (cue.startswith("poster") or (style.poster_card_every and n % style.poster_card_every == 0)
                    or not backdrops):
        return Shot("poster_card", at, dur, rng.choice(posters), motion, label)
    if not order:
        order[:] = backdrops[:]
        rng.shuffle(order)
    return Shot("image", at, dur, order.pop(), motion, label)
