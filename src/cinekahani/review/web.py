"""Local review dashboard. Binds to 127.0.0.1; every change needs a per-run token and a local Origin."""
from __future__ import annotations

import html
import json
import secrets
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from ..core.config import Settings
from ..core.project import Project
from ..publish.run import targets_for
from ..publish.state import PublishState, ScheduleError, parse_when
from . import queue

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
MEDIA_OK = {".mp4", ".jpg", ".jpeg", ".png"}
CSS = """
:root{--bg:#f6f5f2;--fg:#1c1b19;--mut:#6b6860;--card:#fff;--line:#e2dfd8;--ok:#1f7a45;--bad:#b3261e;--warn:#9a6a00;--acc:#c0392b}
@media (prefers-color-scheme:dark){:root{--bg:#161513;--fg:#efede8;--mut:#a09c92;--card:#1f1e1b;--line:#33312c;--ok:#5cc88a;--bad:#ff8a80;--warn:#e0b341;--acc:#ff7a6b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:1000px;margin:0 auto;padding:16px}h1{font-size:1.4rem}a{color:var(--acc)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:12px 0}
.row{display:flex;gap:14px;align-items:center;flex-wrap:wrap}.grow{flex:1;min-width:200px}
.badge{padding:2px 9px;border-radius:99px;font-size:.8rem;border:1px solid var(--line)}
.approved{color:var(--ok);border-color:var(--ok)}.rejected{color:var(--bad);border-color:var(--bad)}.changed{color:var(--warn);border-color:var(--warn)}
img.th{width:160px;border-radius:6px}video{width:100%;max-height:70vh;background:#000;border-radius:8px}
input,textarea,select{width:100%;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
textarea{min-height:220px}button{padding:9px 16px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer;font:inherit}
button.go{background:var(--ok);color:#fff;border-color:var(--ok)}button.no{color:var(--bad);border-color:var(--bad)}
.ok{color:var(--ok)}.fail{color:var(--bad)}.soft{color:var(--warn)}.mut{color:var(--mut)}small{color:var(--mut)}
"""


def host_name(header: str | None) -> str:
    h = header or ""
    if h.startswith("["):
        return h.split("]")[0] + "]"
    return h.rsplit(":", 1)[0] if ":" in h else h


def local_time(at_utc: str, tz: str) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        return datetime.fromisoformat(at_utc).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return at_utc


def esc(x) -> str:
    return html.escape(str(x), quote=True)


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="Cine Kahani review", docs_url=None, redoc_url=None, openapi_url=None)
    token = secrets.token_urlsafe(24)
    projects_dir = settings.path("projects")

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if host_name(request.headers.get("host")) not in LOCAL_HOSTS:
            return JSONResponse({"error": "local access only"}, status_code=403)
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and (urlparse(origin).hostname or "") not in {h.strip("[]") for h in LOCAL_HOSTS}:
                return JSONResponse({"error": "bad origin"}, status_code=403)
            if request.headers.get("x-csrf") != token:
                return JSONResponse({"error": "bad token"}, status_code=403)
        return await call_next(request)

    def project_for(slug: str) -> Project:
        if not slug or slug != Path(slug).name or slug.startswith("."):
            raise HTTPException(400, "bad project name")
        try:
            return Project.open(projects_dir, slug)
        except FileNotFoundError:
            raise HTTPException(404, "no such project")

    def all_items():
        out = []
        if projects_dir.is_dir():
            for d in sorted(p for p in projects_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
                out += queue.discover(settings, Project(d))
        return out

    def link(it) -> str:
        return f"/item?slug={quote(it.slug)}&id={quote(it.id)}"

    def page(title: str, body: str) -> HTMLResponse:
        return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="csrf" content="{token}">
<title>{esc(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>""")

    @app.get("/", response_class=HTMLResponse)
    def index():
        items = all_items()
        rows = []
        for it in items:
            st = PublishState(Project(projects_dir / it.slug)).get(it.id)
            pub = ", ".join(f"{t}: {r['status']}" for t, r in st["results"].items()) or (
                f"scheduled {st['schedule']['at']}" if st.get("schedule") else "")
            th = f'<img class="th" src="/media?slug={quote(it.slug)}&path={quote(it.thumbnail)}" alt="">' if it.thumbnail else ""
            rows.append(f'<div class="card row">{th}<div class="grow"><a href="{link(it)}"><b>{esc(it.title or it.id)}</b></a><br>'
                        f'<small>{esc(it.slug)} · {esc(it.kind)} · {esc(it.fmt)} · {esc(it.lang)}</small><br>'
                        f'<small>{esc(pub)}</small></div>'
                        f'<span class="badge {esc(it.status)}">{esc(it.status)}</span>'
                        f'{"<span class=badge>" + str(len(it.blocked)) + " to fix</span>" if it.blocked else ""}</div>')
        body = "<h1>Review queue</h1><p class='mut'>Nothing is published until you approve it here and give it a schedule.</p>" + \
               ("".join(rows) or "<p>No packaged videos yet. Run <code>cinekahani package</code>.</p>")
        return page("Review queue", body)

    @app.get("/item", response_class=HTMLResponse)
    def item_page(slug: str, id: str):
        project = project_for(slug)
        try:
            it = queue.find(settings, project, id)
        except queue.ReviewError:
            raise HTTPException(404, "no such item")
        pkg = json.loads((project.root / it.package).read_text(encoding="utf-8"))
        st = PublishState(project).get(it.id)
        m = lambda p: f"/media?slug={quote(slug)}&path={quote(p)}"
        checks = "".join(
            f'<li class="{"ok" if c.ok else ("fail" if c.blocking else "soft")}">{"✓" if c.ok else ("✗" if c.blocking else "!")} '
            f'{esc(c.name)}{"" if c.ok else " <small>" + esc(c.message) + "</small>"}</li>' for c in it.checks)
        opts = ""
        if it.kind == "long" and pkg.get("title_options"):
            opts = "<label>Title options</label><select id='opts'>" + "".join(
                f"<option>{esc(t)}</option>" for t in pkg["title_options"]) + "</select>"
        sched = st.get("schedule", {})
        results = "".join(f"<li>{esc(t)}: {esc(r['status'])} {esc(r.get('url') or r.get('error') or '')}</li>"
                          for t, r in st["results"].items())
        thumb = f'<img style="width:100%;border-radius:8px" src="{m(it.thumbnail)}" alt="thumbnail">' if it.thumbnail else ""
        tags = ", ".join(it.tags) if it.kind == "long" else ""
        body = f"""<p><a href="/">&larr; queue</a></p><h1>{esc(it.title)}</h1>
<p><span class="badge {esc(it.status)}">{esc(it.status)}</span> <small>{esc(it.note)}</small></p>
<div class="card"><video controls preload="metadata" src="{m(it.video)}"></video></div>
<div class="row"><div class="grow card">{thumb}<h3>Checks</h3><ul style="list-style:none;padding:0">{checks}</ul></div>
<div class="grow card"><label>Title</label><input id="title" value="{esc(it.title)}">{opts}
<label>Description</label><textarea id="desc">{esc(it.description)}</textarea>
{"<label>Tags (comma separated)</label><input id='tags' value='" + esc(tags) + "'>" if it.kind == "long" else ""}
<p><button id="save">Save edits</button> <small>Saving withdraws any earlier approval.</small></p></div></div>
<div class="card"><label>Note</label><input id="note" value="{esc(it.note if it.status != 'changed' else '')}">
<p class="row"><button class="go" id="approve">Approve</button><button class="no" id="reject">Reject</button></p></div>
<div class="card"><h3>Schedule</h3><div class="row"><div class="grow"><label>Publish at (local time)</label>
<input id="at" placeholder="2026-11-03 18:00" value="{esc(local_time(sched['at'], sched.get('tz', 'UTC')) if sched else '')}"></div>
<div class="grow"><label>Timezone</label><input id="tz" value="{esc(sched.get('tz') or settings.publish.uploadpost.timezone)}"></div></div>
<p><button id="sched">Save schedule</button> <small>Uploads happen with `cinekahani publish run`; the platform then holds the video until this time.
Targets: {esc(", ".join(targets_for(settings, it)))}</small></p><ul>{results}</ul></div>
<p id="msg" role="status"></p>
<script>
const csrf=document.querySelector('meta[name=csrf]').content, q=new URLSearchParams(location.search);
const base={{slug:q.get('slug'),id:q.get('id')}}, $=id=>document.getElementById(id);
async function post(url,extra){{const r=await fetch(url,{{method:'POST',headers:{{'Content-Type':'application/json','X-CSRF':csrf}},body:JSON.stringify({{...base,...extra}})}});
const j=await r.json().catch(()=>({{}}));$('msg').textContent=r.ok?'Done.':(j.error||j.detail||'Failed');$('msg').style.color=r.ok?'var(--ok)':'var(--bad)';if(r.ok)setTimeout(()=>location.reload(),500)}}
$('approve').onclick=()=>post('/api/approve',{{note:$('note').value}});
$('reject').onclick=()=>post('/api/reject',{{note:$('note').value}});
$('save').onclick=()=>post('/api/save',{{title:$('title').value,description:$('desc').value,tags:$('tags')?$('tags').value:null}});
$('sched').onclick=()=>post('/api/schedule',{{at:$('at').value,tz:$('tz').value}});
if($('opts'))$('opts').onchange=e=>$('title').value=e.target.value;
</script>"""
        return page(it.title or it.id, body)

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    @app.get("/media")
    def media(slug: str, path: str):
        project = project_for(slug)
        target = (project.root / path).resolve()
        if project.root.resolve() not in target.parents or target.suffix.lower() not in MEDIA_OK or not target.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(target)

    async def body_of(request: Request) -> dict:
        try:
            return await request.json()
        except Exception:
            raise HTTPException(400, "bad request")

    def err(exc: Exception, code: int = 400):
        return JSONResponse({"error": str(exc)}, status_code=code)

    @app.post("/api/approve")
    async def approve(request: Request):
        b = await body_of(request)
        try:
            queue.decide(settings, project_for(b.get("slug", "")), b.get("id", ""), "approved", b.get("note", ""))
        except queue.ReviewError as exc:
            return err(exc)
        return {"ok": True}

    @app.post("/api/reject")
    async def reject(request: Request):
        b = await body_of(request)
        try:
            queue.decide(settings, project_for(b.get("slug", "")), b.get("id", ""), "rejected", b.get("note", ""))
        except queue.ReviewError as exc:
            return err(exc)
        return {"ok": True}

    @app.post("/api/save")
    async def save(request: Request):
        b = await body_of(request)
        tags = [t for t in str(b["tags"]).split(",")] if b.get("tags") is not None else None
        try:
            queue.save_edit(settings, project_for(b.get("slug", "")), b.get("id", ""), title=b.get("title"),
                            description=b.get("description"), tags=tags)
        except queue.ReviewError as exc:
            return err(exc)
        return {"ok": True}

    @app.post("/api/schedule")
    async def schedule(request: Request):
        b = await body_of(request)
        project = project_for(b.get("slug", ""))
        try:
            it = queue.find(settings, project, b.get("id", ""))
            when = parse_when(b.get("at", ""), b.get("tz") or "UTC", min_lead_minutes=settings.publish.min_lead_minutes)
            PublishState(project).schedule(it.id, when, b.get("tz") or "UTC", targets_for(settings, it))
        except (queue.ReviewError, ScheduleError) as exc:
            return err(exc)
        return {"ok": True, "at_utc": when.isoformat(timespec="seconds")}

    return app
