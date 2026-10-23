"""Content guardrails. Limits come from config; this checks a planned timeline."""
from __future__ import annotations

from dataclasses import dataclass

from .config import TrailerGuard


@dataclass
class Snippet:
    source: str       # e.g. trailer id
    start: float      # position in the final video, seconds
    length: float


def check_trailer_plan(snippets: list[Snippet], runtime: float, guard: TrailerGuard,
                       narration_seconds: float | None = None) -> list[str]:
    """Return a list of problems; an empty list means the plan is within limits."""
    problems: list[str] = []
    if not snippets:
        return problems
    if not guard.enabled:
        return ["trailer footage is switched off in config but the plan uses some"]
    for s in snippets:
        if s.length > guard.max_snippet_seconds + 1e-6:
            problems.append(f"snippet at {s.start:.1f}s is {s.length:.1f}s "
                            f"(max {guard.max_snippet_seconds:g}s)")
    total = sum(s.length for s in snippets)
    if runtime > 0 and total / runtime > guard.max_share_of_runtime + 1e-9:
        problems.append(f"trailer share {total / runtime:.0%} is over the {guard.max_share_of_runtime:.0%} limit")
    ordered = sorted(snippets, key=lambda s: s.start)
    run = 0.0
    prev_end: float | None = None
    for s in ordered:
        run = run + s.length if prev_end is not None and abs(s.start - prev_end) < 0.05 else s.length
        if run > guard.max_continuous_seconds + 1e-6:
            problems.append(f"continuous trailer footage reaches {run:.1f}s at {s.start:.1f}s "
                            f"(max {guard.max_continuous_seconds:g}s)")
        prev_end = s.start + s.length
    if narration_seconds is not None and runtime > 0:
        share = narration_seconds / runtime
        if share < guard.min_narration_share - 1e-9:
            problems.append(f"narration covers {share:.0%} of runtime (min {guard.min_narration_share:.0%})")
    return problems
