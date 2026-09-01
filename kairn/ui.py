"""ローカル Web UI（人が触る唯一の入口。docs/ui.md）。Starlette + 素の HTML。データは store と同じファイル。

ルートは Mount ではなく `ui_routes(conf, prefix)` で外側のアプリに直接載せる
（Mount 配下の "/" は末尾スラッシュ無しの /ui で 404 になるため）。
人の操作はすべて event として記録され、AI は次の open_case で human_feedback として受け取る。
"""
from __future__ import annotations

import html
from datetime import datetime
from urllib.parse import quote, urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from . import config as cfg
from .store import JST, TASK_OWNERS, CaseStore

STALE_DAYS = 7  # これを超えて動きの無い open タスクを目立たせる（自動では消さない）
LIVE = ("open", "doing", "blocked")
LIST_STATUSES = ("open", "closed", "suspended", "all")

CSS = """
body{font-family:system-ui,sans-serif;margin:0;background:#f5f6f8;color:#222}header{background:#22313f;color:#fff;padding:.6em 1em}
header a{color:#fff;text-decoration:none;margin-right:1em}main{padding:1em;max-width:1200px;margin:auto}
table{border-collapse:collapse;width:100%;background:#fff}th,td{border-bottom:1px solid #e3e5e8;padding:.4em .6em;text-align:left;font-size:.92em;vertical-align:top}
.bar{background:#dde;height:8px;border-radius:4px;overflow:hidden;width:120px;display:inline-block;vertical-align:middle}.bar i{display:block;height:100%;background:#3a8}
.kanban{display:grid;grid-template-columns:repeat(4,1fr);gap:.6em}.col{background:#e9ecf0;border-radius:6px;padding:.5em;min-height:120px}
.col h3{margin:.2em 0 .5em;font-size:.9em;color:#555}.card{background:#fff;border-radius:4px;padding:.5em;margin-bottom:.5em;box-shadow:0 1px 2px #0002;font-size:.9em}
.card small,.muted{color:#777}.ev{font-size:.88em;border-left:3px solid #ccc;padding:.2em .6em;margin:.3em 0}.ev.human{border-color:#e69}.ev.ai{border-color:#69c}
form.inline{display:inline}input,textarea,select{font:inherit}button{font:inherit;padding:.2em .6em}
.stale{color:#b00;font-weight:bold}.card.stale{border-left:4px solid #c33}.age{font-size:.85em}
details{margin:.4em 0}pre{background:#fff;padding:.6em;overflow-x:auto;font-size:.85em;white-space:pre-wrap}
.tag{display:inline-block;background:#e3e8f0;border-radius:3px;padding:0 .4em;margin:0 .2em;font-size:.85em}
"""


def _esc(x: object) -> str:
    return html.escape(str(x if x is not None else ""))


def _days_since(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=JST)
    return max(0, (datetime.now(JST) - t).days)


def task_freshness(events: list[dict], task: dict) -> dict:
    """open タスクの鮮度: そのタスクの最終イベント（無ければタスクの作成/更新時刻）からの経過日数。"""
    last = None
    for e in events:
        if e.get("task") == task["id"]:
            last = e.get("t")
    last = last or task.get("updated_at") or task.get("created_at")
    days = _days_since(last)
    return {"last": last, "days": days, "stale": days is not None and days > STALE_DAYS}


def case_freshness(st: CaseStore, cid: str) -> dict | None:
    """案件の open タスクのうち最も古いものの鮮度（open タスクが無ければ None）。"""
    events = st.events(cid)
    fr = [task_freshness(events, t) for t in st.open_tasks(cid)]
    fr = [f for f in fr if f["days"] is not None]
    return max(fr, key=lambda f: f["days"]) if fr else None


def _age(f: dict | None) -> str:
    if not f or f["days"] is None:
        return ""
    cls = "age stale" if f["stale"] else "age muted"
    return f"<span class='{cls}'>{f['days']}d{' ⚠' if f['stale'] else ''}</span>"


def ui_routes(conf: cfg.Config, prefix: str = "/ui") -> list[Route]:
    P = prefix.rstrip("/")

    def _page(title: str, body: str) -> HTMLResponse:
        return HTMLResponse(f"<!doctype html><meta charset='utf-8'><title>kairn – {_esc(title)}</title><style>{CSS}</style>"
                            f"<header><a href='{P}'>kairn</a> {_esc(title)}</header><main>{body}</main>")

    def _ws(name: str) -> cfg.Workspace:
        if name not in conf.workspaces:
            raise KeyError(name)
        return conf.workspaces[name]

    def _url(ws: cfg.Workspace, cid: str, *more: str) -> str:
        return "/".join([P, quote(ws.name, safe=""), quote(cid, safe=""), *more])

    async def index(req: Request) -> Response:
        want_ws = req.query_params.get("ws") or ""
        element = req.query_params.get("element") or ""   # elements の値で絞り込み
        status = req.query_params.get("status") or "open"  # open|closed|suspended|all（それ以外は open に正規化。値を HTML に反射するため）
        if status not in LIST_STATUSES:
            status = "open"
        rows = []
        for ws in conf.workspaces.values():
            if want_ws and ws.name != want_ws:
                continue
            st = CaseStore(ws.cases_dir)
            for cid in st.list_case_ids():
                c = st.load_case(cid)
                if status != "all" and c.get("status") != status:
                    continue
                el = c.get("elements") or {}
                if element and element not in {v for vs in el.values() for v in vs}:
                    continue
                p = st.progress(cid); le = st.last_event(cid); fr = case_freshness(st, cid)
                pct = int(100 * p["done"] / p["total"]) if p["total"] else 0
                ai_last = next((e for e in reversed(st.events(cid)) if e.get("actor") == "ai"), None)
                tags = "".join(f"<a class='tag' href='{P}?element={quote(v, safe='')}'>{_esc(v)}</a>" for vs in el.values() for v in vs)
                rows.append(
                    f"<tr><td>{_esc(ws.name)}</td><td><a href='{_url(ws, cid)}'>{_esc(cid)}</a><br><small>{_esc(c.get('title', ''))}</small> {tags}</td>"
                    f"<td>{_esc(c.get('status'))}</td><td><span class='bar'><i style='width:{pct}%'></i></span> {p['done']}/{p['total']} (v{p['plan']})</td>"
                    f"<td>{_age(fr)}</td>"
                    f"<td><small>{_esc((le or {}).get('t', '')[:16])} {_esc((le or {}).get('actor', ''))} {_esc((le or {}).get('action', ''))}</small></td>"
                    f"<td><small>{_esc((ai_last or {}).get('t', '')[:16])} {_esc((ai_last or {}).get('agent', ''))} {_esc((ai_last or {}).get('action', ''))}</small></td></tr>")
            legacy = st.list_dirs_without_case()
            if legacy and not element:
                rows.append(f"<tr><td>{_esc(ws.name)}</td><td colspan=6><small>{len(legacy)} directories without case.json (legacy)</small></td></tr>")
        filt = (f"<p><small>status: " + " ".join(f"<a href='{P}?status={s}{'&element=' + quote(element, safe='') if element else ''}'>{s}</a>" for s in LIST_STATUSES)
                + (f" · element: <b>{_esc(element)}</b> <a href='{P}?status={status}'>✕</a>" if element else "") + "</small></p>")
        return _page("cases", filt + f"<table><tr><th>ws</th><th>case</th><th>status</th><th>progress</th><th>鮮度</th><th>last event</th><th>AI last</th></tr>{''.join(rows)}</table>")

    async def case_page(req: Request) -> Response:
        try:
            ws = _ws(req.path_params["ws"])
            cid = req.path_params["case"]; st = CaseStore(ws.cases_dir)
            c = st.load_case(cid)
        except (KeyError, ValueError):  # 未知のワークスペース／案件、不正な案件 ID
            return PlainTextResponse("not found", status_code=404)
        plan = st.current_plan(cid); events = st.events(cid)
        cols: dict[str, list[str]] = {"open": [], "doing": [], "blocked": [], "done": []}
        for t in (plan["tasks"] if plan else []):
            if t["status"] not in cols:
                continue
            fr = task_freshness(events, t) if t["status"] in LIVE else None
            ev = "".join(f"<br><small>ev: {_esc(_evidence(e))}</small>" for e in t.get("evidence", []))
            cols[t["status"]].append(
                f"<div class='card{' stale' if fr and fr['stale'] else ''}'><b>{_esc(t['id'])}</b> {_esc(t['title'])} {_age(fr)}<br>"
                f"<small>@{_esc(t.get('owner', 'ai'))} {_esc((fr or {}).get('last') or t.get('updated_at', t.get('created_at', '')))[:16]}</small>{ev}"
                f"<form class='inline' method=post action='{_url(ws, cid, 'sendback')}' accept-charset='utf-8'><input type=hidden name=task value='{_esc(t['id'])}'>"
                f"<input name=note placeholder='差し戻し理由' size=18><button>差し戻し</button></form></div>")
        kanban = "".join(f"<div class='col'><h3>{k} ({len(v)})</h3>{''.join(v)}</div>" for k, v in cols.items())
        evs = "".join(
            f"<div class='ev {_esc(e.get('actor', 'ai'))}'><small>{_esc(e.get('t', ''))[:16]}</small> <b>{_esc(e.get('actor', ''))}</b>"
            f"{(' <small>' + _esc(e['agent']) + '</small>') if e.get('agent') else ''} {_esc(e.get('action', ''))} {_esc(e.get('task') or '')} — {_esc(e.get('note', ''))}"
            + (f" <small>ev: {_esc('; '.join(_evidence(x) for x in e['evidence']))}</small>" if e.get("evidence") else "") + "</div>"
            for e in reversed(events[-100:]))
        plans = "".join(
            f"<details{' open' if p['version'] == (plan or {}).get('version') else ''}><summary>v{p['version']} {_esc(p.get('created_at', ''))[:16]} @{_esc(p.get('actor', ''))}"
            f" — {_esc(p.get('reason', ''))} ({len(p['tasks'])} tasks{', superseded: ' + ', '.join(p.get('superseded', [])) if p.get('superseded') else ''})</summary>"
            f"<small>objective: {_esc(p.get('objective', ''))}</small><ul>"
            + "".join(f"<li>{_esc(t['id'])} [{_esc(t['status'])}] {_esc(t['title'])}{' ← ' + _esc(t['carried_from']) if t.get('carried_from') else ''}"
                      f"{' → ' + _esc(t['superseded_by']) if t.get('superseded_by') else ''}</li>" for t in p["tasks"]) + "</ul></details>"
            for p in reversed(st.list_plans(cid)))
        related = ", ".join(f"<a href='{_url(ws, r)}'>{_esc(r)}</a>" if (ws.cases_dir / r / "case.json").exists() else _esc(r) for r in c.get("related", []))
        el = c.get("elements") or {}
        elements = " ".join(f"{_esc(k)}: " + "".join(f"<a class='tag' href='{P}?element={quote(v, safe='')}'>{_esc(v)}</a>" for v in vs) for k, vs in el.items())
        meta = " · ".join(x for x in (f"status: <b>{_esc(c.get('status'))}</b>",
                                      f"repos: {_esc(', '.join(c.get('repos', [])))}" if c.get("repos") else "",
                                      f"tickets: {_esc(', '.join(map(str, c.get('tickets', []))))}" if c.get("tickets") else "",
                                      f"prs: {_esc(', '.join(map(str, c.get('prs', []))))}" if c.get("prs") else "",
                                      f"related: {related}" if related else "", elements) if x)
        data = "".join(f"<li><code>{_esc(d.get('drive', ''))}</code> <small>moved {_esc(d.get('moved_at', ''))}</small><br>"
                       f"<small>復元: <code>rclone copy {_esc(conf.remote)}:{_esc(d.get('drive', ''))} {_esc(ws.cases_dir / cid)}/</code></small></li>"
                       for d in c.get("data", []))
        wl = ws.cases_dir / cid / "worklog.md"
        worklog = _esc(wl.read_text(encoding="utf-8", errors="replace")) if wl.exists() else "(no worklog.md)"
        forms = (f"<h3>操作</h3>"
                 f"<form method=post action='{_url(ws, cid, 'task')}' accept-charset='utf-8'><input name=title placeholder='新しいタスク（計画の新版として追加）' size=50>"
                 f"<select name=owner><option>ai</option><option>human</option></select><button>追加</button></form>"
                 f"<form method=post action='{_url(ws, cid, 'comment')}' accept-charset='utf-8'><input name=note placeholder='コメント／指示' size=50><button>記録</button></form>"
                 f"<form method=post action='{_url(ws, cid, 'status')}' accept-charset='utf-8'><select name=status>"
                 + "".join(f"<option{' selected' if s == c.get('status') else ''}>{s}</option>" for s in ("open", "closed", "suspended"))
                 + f"</select><input name=note placeholder='理由' size=30><button>案件の状態を変更</button></form>")
        body = (f"<h2>{_esc(cid)} <small>{_esc(c.get('title', ''))}</small></h2><p><small>{meta}</small></p>"
                f"<p><small>objective: {_esc((plan or {}).get('objective', ''))}</small></p>"
                f"<div class='kanban'>{kanban}</div>{forms}"
                f"<h3>計画の版履歴</h3>{plans or '<small>(no plan)</small>'}"
                f"<h3>データ所在</h3><ul>{data or '<li><small>(none)</small></li>'}</ul>"
                f"<details><summary>worklog.md</summary><pre>{worklog}</pre></details>"
                f"<h3>時系列</h3>{evs}")
        return _page(cid, body)

    async def act(req: Request) -> Response:
        if not same_origin(req):
            return PlainTextResponse("forbidden: cross-site request", status_code=403)
        try:
            ws = _ws(req.path_params["ws"])
            cid = req.path_params["case"]; kind = req.path_params["kind"]
            st = CaseStore(ws.cases_dir); st.load_case(cid)
        except (KeyError, ValueError):
            return PlainTextResponse("not found", status_code=404)
        form = await req.form()  # application/x-www-form-urlencoded, UTF-8（percent-encoding は Starlette が復号）
        note = str(form.get("note", "")).strip()
        if kind == "sendback":
            task = str(form.get("task", ""))
            if task not in {t["id"] for t in (st.current_plan(cid) or {"tasks": []})["tasks"]}:
                return PlainTextResponse(f"unknown task {task}", status_code=400)
            st.append_event(cid, {"actor": "human", "action": "sendback", "task": task, "note": note})
        elif kind == "comment":
            if not note:
                return PlainTextResponse("note is required", status_code=400)
            st.append_event(cid, {"actor": "human", "action": "comment", "note": note})
        elif kind == "task":  # 生きているタスク（done 含む）を全部引き継いだ新版＋追加
            title = str(form.get("title", "")).strip()
            if not title:
                return PlainTextResponse("title is required", status_code=400)
            owner = str(form.get("owner", "ai"))
            if owner not in TASK_OWNERS:
                return PlainTextResponse("owner must be ai or human", status_code=400)
            plan = st.current_plan(cid)
            carried = [{"carried_from": t["id"]} for t in (plan["tasks"] if plan else []) if t["status"] in (*LIVE, "done")]
            st.new_plan_version(cid, (plan or {}).get("objective", ""), carried + [{"title": title, "owner": owner}],
                                reason=f"human added a task via UI: {title}", actor="human")
        elif kind == "status":
            try:
                st.set_case_status(cid, str(form.get("status")), actor="human", note=note)
            except ValueError as e:
                return PlainTextResponse(str(e), status_code=400)
        else:
            return PlainTextResponse("unknown action", status_code=404)
        return RedirectResponse(_url(ws, cid), status_code=303)

    return [Route(P, index), Route(P + "/", index), Route(P + "/{ws}/{case}", case_page),
            Route(P + "/{ws}/{case}/{kind}", act, methods=["POST"])]


def same_origin(req: Request) -> bool:
    """CSRF 対策（POST のみ）: Sec-Fetch-Site が same-origin / none 以外、または Origin / Referer のホストが
    Host と異なれば拒否。どちらのヘッダも無い（curl 等）場合は通す。"""
    sfs = req.headers.get("sec-fetch-site")
    if sfs and sfs not in ("same-origin", "none"):
        return False
    host = req.headers.get("host", "")
    for h in ("origin", "referer"):
        v = req.headers.get(h)
        if v and urlsplit(v).netloc != host:
            return False
    return True


def _evidence(e: object) -> str:
    if isinstance(e, dict):
        return " ".join(f"{k}={v}" for k, v in e.items())
    return str(e)


def build_ui(conf: cfg.Config, prefix: str = "/ui") -> Starlette:
    """UI 単体のアプリ（テスト用）。本番は server.build_app が同じルートを /mcp と同居させる。"""
    return Starlette(routes=ui_routes(conf, prefix))
