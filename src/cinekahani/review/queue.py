"""Review queue: every output waits here for your approval. Approvals are tied to file hashes,
so editing a video, thumbnail or text afterwards withdraws the approval."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..assemble.build import render_paths
from ..core.config import Settings
from ..core.guardrails import check_trailer_plan
from ..core.hashing import hash_file, hash_json
from ..core.project import Project
from ..script import parse
from ..script.generate import script_path
from ..visuals.plan import Plan

_memo: dict[tuple, str] = {}


class ReviewError(RuntimeError):
    pass


def file_hash(path: Path) -> str:
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key not in _memo:
        _memo[key] = hash_file(path)
    return _memo[key]


@dataclass
class Check:
    name: str
    ok: bool
    message: str = ""
    blocking: bool = True


@dataclass
class Item:
    id: str
    kind: str                       # long | short
    slug: str
    fmt: str
    lang: str
    video: str                      # relative to project root
    thumbnail: str | None
    package: str                    # relative path of package.json
    title: str
    description: str
    tags: list[str]
    hashes: dict[str, str] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    status: str = "pending"         # pending | approved | rejected | changed
    note: str = ""
    short_index: int | None = None

    @property
    def blocked(self) -> list[Check]:
        return [c for c in self.checks if c.blocking and not c.ok]


def review_file(project: Project) -> Path:
    return project.root / "review.json"


def _load(project: Project) -> dict:
    p = review_file(project)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {"decisions": {}}


def _save(project: Project, data: dict) -> None:
    p = review_file(project)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _script_checks(settings: Settings, project: Project, fmt: str, lang: str) -> list[Check]:
    sp = script_path(project, fmt, lang)
    if not sp.is_file():
        return [Check("script approved", False, f"{sp.name} is missing")]
    front, body = parse.read_script(sp)
    out = [Check("script approved", front.get("status") == "approved" or not settings.script.require_approval,
                 f"status is '{front.get('status')}'")]
    tags = parse.verify_tags(body)
    out.append(Check("no unchecked [VERIFY] claims", not tags, f"{len(tags)} left"))
    return out


def _package_checks(settings: Settings, pkg: dict, thumb: Path | None) -> list[Check]:
    title = pkg.get("title", "")
    out = [Check("title length", 0 < len(title) <= 100, f"{len(title)} characters (max 100)"),
           Check("description length", len(pkg.get("description", "")) <= 5000, "over 5000 characters"),
           Check("tags length", sum(len(t) + 1 for t in pkg.get("tags", [])) <= 500, "over 500 characters")]
    label = {"hi": "(Hindi)", "en": "(English)"}.get(pkg.get("language", ""), "")
    if label:
        out.append(Check("language label in title", label.lower() in title.lower(), f"add {label} to the title"))
    if thumb is not None:
        out.append(Check("thumbnail present", thumb.is_file(), "missing"))
        if thumb.is_file():
            out.append(Check("thumbnail under 2 MB", thumb.stat().st_size <= 2_000_000, "too large"))
    return out


def discover(settings: Settings, project: Project) -> list[Item]:
    """Everything that could be published for this project, with current checks and decisions."""
    items: list[Item] = []
    pkg_root = project.root / "package"
    if not pkg_root.is_dir():
        return items
    state = _load(project)["decisions"]
    for pdir in sorted(p for p in pkg_root.iterdir() if p.is_dir()):
        pkg_file = pdir / "package.json"
        if not pkg_file.is_file() or "." not in pdir.name:
            continue
        fmt, lang = pdir.name.rsplit(".", 1)
        pkg = json.loads(pkg_file.read_text(encoding="utf-8"))
        video, plan_path, report_path = render_paths(project, fmt, lang)
        thumb = pdir / "thumbnail.jpg"
        rel = lambda p: str(p.relative_to(project.root))
        long = Item(f"long:{fmt}.{lang}", "long", project.slug, fmt, lang, rel(video),
                    rel(thumb) if thumb.is_file() else None, rel(pkg_file), pkg.get("title", ""),
                    pkg.get("description", ""), pkg.get("tags", []))
        c = _script_checks(settings, project, fmt, lang)
        c.append(Check("video rendered", video.is_file(), "run `cinekahani build`"))
        if report_path.is_file():
            rep = json.loads(report_path.read_text(encoding="utf-8"))
            c.append(Check("render checks passed", rep.get("checks") == "passed", "see report"))
            if plan_path.is_file():
                plan = Plan.load(plan_path)
                problems = check_trailer_plan(plan.snippets(), plan.total_s, settings.guardrails.trailer,
                                              narration_seconds=plan.narration_s)
                c.append(Check("trailer guardrails", not problems, "; ".join(problems)))
                if plan.music:
                    from ..assemble.music import eligible
                    ok = plan.music in {t.file for t in eligible(settings.path("music"), settings.video.music)}
                    c.append(Check("music license ok", ok, f"{plan.music} is not in the license log"))
        else:
            c.append(Check("render report", False, "missing"))
        c += _package_checks(settings, pkg, thumb)
        long.checks = c
        if video.is_file():
            long.hashes = {"video": file_hash(video), "package": hash_json({k: pkg.get(k) for k in
                                                                              ("title", "description", "tags", "chapters")})}
            if thumb.is_file():
                long.hashes["thumbnail"] = file_hash(thumb)
        items.append(long)
        for i, sh in enumerate(pkg.get("shorts", []), 1):
            f = project.root / "clips" / f"{fmt}.{lang}" / sh["file"]
            it = Item(f"short:{fmt}.{lang}:{i}", "short", project.slug, fmt, lang, rel(f), None, rel(pkg_file),
                      sh.get("title", ""), sh.get("description", ""), pkg.get("tags", [])[:10], short_index=i - 1)
            it.checks = _script_checks(settings, project, fmt, lang) + [
                Check("Short file present", f.is_file(), "missing"),
                Check("title length", 0 < len(sh.get("title", "")) <= 100, "1-100 characters"),
                Check("trailer share verified", sh.get("trailer_check") == "ok",
                      sh.get("trailer_check") or "not checked", blocking=False)]
            if f.is_file():
                it.hashes = {"video": file_hash(f), "package": hash_json(sh)}
            items.append(it)
    for it in items:
        d = state.get(it.id)
        if d:
            it.note = d.get("note", "")
            if d["status"] == "approved" and d.get("hashes") != it.hashes:
                it.status, it.note = "changed", "edited after approval; approve again"
            else:
                it.status = d["status"]
    return items


def find(settings: Settings, project: Project, item_id: str) -> Item:
    for it in discover(settings, project):
        if it.id == item_id:
            return it
    raise ReviewError(f"No item '{item_id}' in {project.slug}. Run `cinekahani review list`.")


def decide(settings: Settings, project: Project, item_id: str, status: str, note: str = "") -> Item:
    it = find(settings, project, item_id)
    if status == "approved":
        if it.blocked:
            raise ReviewError("Cannot approve; fix first:\n  - " + "\n  - ".join(
                f"{c.name}: {c.message}" for c in it.blocked))
        if not it.hashes.get("video"):
            raise ReviewError("The video file is missing")
    data = _load(project)
    data["decisions"][item_id] = {"status": status, "note": note, "hashes": it.hashes,
                                  "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _save(project, data)
    it.status, it.note = status, note
    return it


def save_edit(settings: Settings, project: Project, item_id: str, *, title: str | None = None,
              description: str | None = None, tags: list[str] | None = None) -> Item:
    """Edit the text of an item. This changes its hash, so an earlier approval is withdrawn."""
    it = find(settings, project, item_id)
    path = project.root / it.package
    pkg = json.loads(path.read_text(encoding="utf-8"))
    target = pkg if it.kind == "long" else pkg["shorts"][it.short_index]
    if title is not None:
        target["title"] = title.strip()
    if description is not None:
        target["description"] = description
    if tags is not None and it.kind == "long":
        pkg["tags"] = [t.strip() for t in tags if t.strip()]
    path.write_text(json.dumps(pkg, indent=2, ensure_ascii=False), encoding="utf-8")
    return find(settings, project, item_id)


def require_approved(settings: Settings, project: Project, item_id: str) -> Item:
    """The gate publishing goes through. Raises unless the item is approved and unchanged since."""
    it = find(settings, project, item_id)
    if it.status == "changed":
        raise ReviewError(f"{item_id} was edited after you approved it. Review and approve it again.")
    if it.status != "approved":
        raise ReviewError(f"{item_id} is '{it.status}'. Nothing is published without your approval "
                          f"(`cinekahani review approve {project.slug} {item_id}`).")
    if it.blocked:
        raise ReviewError(f"{item_id} no longer passes its checks: " + "; ".join(c.name for c in it.blocked))
    return it
