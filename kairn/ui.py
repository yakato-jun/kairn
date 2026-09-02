"""ローカル Web UI（人が触る唯一の入口。docs/ui.md）。Starlette + 素の HTML。データは store と同じファイル。

ルートは Mount ではなく `ui_routes(conf, prefix)` で外側のアプリに直接載せる
（Mount 配下の "/" は末尾スラッシュ無しの /ui で 404 になるため）。
人の操作はすべて event として記録され、AI は次の open_case で human_feedback として受け取る。
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from urllib.parse import quote, urlsplit

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from . import config as cfg
from . import sync
from .jobs import JobTable
from .store import JST, TASK_OWNERS, CaseStore

STALE_DAYS = 7  # これを超えて動きの無い open タスクを目立たせる（自動では消さない）
LIVE = ("open", "doing", "blocked")
LIST_STATUSES = ("open", "closed", "suspended", "all")
# 一覧の Drive 列（sync.drive_state。直近に読んだ Drive の版マーカーのキャッシュとの比較）: state → (表示, CSS クラス)
DRIVE_MARKS = {"synced": ("同期済み", "ok"), "drive_newer": ("Drive の方が新しい", "stale"),
               "local_changes": ("ローカル未 checkin", "warn"), "unknown": ("不明", "muted")}

CSS = """
body{font-family:system-ui,sans-serif;margin:0;background:#f5f6f8;color:#222}header{background:#22313f;color:#fff;padding:.6em 1em}
header a{color:#fff;text-decoration:none;margin-right:1em}header a.right{float:right;margin:0;font-size:.9em}main{padding:1em;max-width:1200px;margin:auto}
table{border-collapse:collapse;width:100%;background:#fff}th,td{border-bottom:1px solid #e3e5e8;padding:.4em .6em;text-align:left;font-size:.92em;vertical-align:top}
.bar{background:#dde;height:8px;border-radius:4px;overflow:hidden;width:120px;display:inline-block;vertical-align:middle}.bar i{display:block;height:100%;background:#3a8}
.kanban{display:grid;grid-template-columns:repeat(4,1fr);gap:.6em}.col{background:#e9ecf0;border-radius:6px;padding:.5em;min-height:120px}
.col h3{margin:.2em 0 .5em;font-size:.9em;color:#555}.card{background:#fff;border-radius:4px;padding:.5em;margin-bottom:.5em;box-shadow:0 1px 2px #0002;font-size:.9em}
.card small,.muted{color:#777}.ev{font-size:.88em;border-left:3px solid #ccc;padding:.2em .6em;margin:.3em 0}.ev.human{border-color:#e69}.ev.ai{border-color:#69c}
form.inline{display:inline}input,textarea,select{font:inherit}button{font:inherit;padding:.2em .6em}
.stale{color:#b00;font-weight:bold}.card.stale{border-left:4px solid #c33}.age{font-size:.85em}
details{margin:.4em 0}pre{background:#fff;padding:.6em;overflow-x:auto;font-size:.85em;white-space:pre-wrap}
.tag{display:inline-block;background:#e3e8f0;border-radius:3px;padding:0 .4em;margin:0 .2em;font-size:.85em}
.ok{color:#3a8;font-weight:bold}.warn{color:#c80;font-weight:bold}.drive{font-size:.85em;white-space:nowrap}
.jobs{background:#fff7e0;border-left:4px solid #e9a825;padding:.4em .8em;margin:.5em 0;font-size:.9em}.jobs ul{margin:.3em 0}.job code{font-size:.85em}
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


def ui_routes(conf: cfg.Config, prefix: str = "/ui", jobs: JobTable | None = None) -> list[Route]:
    """jobs: MCP と共有するジョブ表（進行中の checkin / 取り寄せを案件ページに出す。None なら表示しない）。"""
    P = prefix.rstrip("/")

    def _page(title: str, body: str) -> HTMLResponse:
        return HTMLResponse(f"<!doctype html><meta charset='utf-8'><title>kairn – {_esc(title)}</title><style>{CSS}</style>"
                            f"<header><a href='{P}'>kairn</a> {_esc(title)} <a class='right' href='{P}/settings'>設定</a></header><main>{body}</main>")

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
        checked: dict[str, str | None] = {}   # ws → 版マーカーのキャッシュで全案件を読んだ時刻（無ければ None）
        for ws in conf.workspaces.values():
            if want_ws and ws.name != want_ws:
                continue
            st = CaseStore(ws.cases_dir)
            cache = sync.load_drive_revs_cache(ws)
            revs = cache["revs"] if cache else None
            checked[ws.name] = (cache or {}).get("fetched_at")
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
                    f"<td>{_age(fr)}</td><td>{_drive_mark(sync.drive_state(st, revs, cid))}</td>"
                    f"<td><small>{_esc((le or {}).get('t', '')[:16])} {_esc((le or {}).get('actor', ''))} {_esc((le or {}).get('action', ''))}</small></td>"
                    f"<td><small>{_esc((ai_last or {}).get('t', '')[:16])} {_esc((ai_last or {}).get('agent', ''))} {_esc((ai_last or {}).get('action', ''))}</small></td></tr>")
            legacy = st.list_dirs_without_case()
            if legacy and not element:
                rows.append(f"<tr><td>{_esc(ws.name)}</td><td colspan=6><small>{len(legacy)} directories without case.json (legacy)</small></td></tr>")
        filt = (f"<p><small>status: " + " ".join(f"<a href='{P}?status={s}{'&element=' + quote(element, safe='') if element else ''}'>{s}</a>" for s in LIST_STATUSES)
                + (f" · element: <b>{_esc(element)}</b> <a href='{P}?status={status}'>✕</a>" if element else "") + "</small></p>")
        refreshed = req.query_params.get("refreshed") or ""
        drive = (f"<p><small>Drive: " + " · ".join(f"{_esc(w)}: 版 {_esc(t[:16]) + ' 取得' if t else '未取得'}" for w, t in checked.items())
                 + f" <form class='inline' method=post action='{P}/refresh' accept-charset='utf-8'><input type=hidden name=ws value='{_esc(want_ws)}'>"
                 f"<button>更新確認</button></form>"
                 + (f" <span class='ok'>{_esc(refreshed)}</span>" if refreshed else "")
                 + "<br><span class='muted'>Drive 列は直近に読んだ Drive の版マーカー（cases/&lt;case&gt;/.rev/）との比較（同期済み = rev 一致 / Drive の方が新しい = rev が違う・マーカーが不定 / ローカル未 checkin / 不明 = 未取得・マーカー無し・未 checkin）。"
                   "更新確認は全案件の版を 1 回で読み、違う案件だけ取り寄せる（未 checkin のローカル変更がある案件は取り寄せない）</span></small></p>")
        return _page("cases", filt + drive + f"<table><tr><th>ws</th><th>case</th><th>status</th><th>progress</th><th>鮮度</th><th>Drive</th><th>last event</th><th>AI last</th></tr>{''.join(rows)}</table>")

    async def refresh(req: Request) -> Response:
        """一覧の「更新確認」: Drive の版マーカーを rclone lsf 1 回で読んでキャッシュを更新し、rev が違う案件だけ取り寄せる
        （sync.checkout_workspace。未 checkin のローカル変更がある案件は取り寄せない）。ws が空なら全ワークスペース。
        rclone は threadpool で実行し、同じプロセスの MCP を止めない。"""
        if not same_origin(req):
            return PlainTextResponse("forbidden: cross-site request", status_code=403)
        form = await req.form()
        want = str(form.get("ws", ""))
        targets = [w for w in conf.workspaces.values() if not want or w.name == want]
        if want and not targets:
            return PlainTextResponse("not found", status_code=404)
        results = []
        for w in targets:
            try:
                results.append(f"{w.name}: {await run_in_threadpool(sync.checkout_workspace, conf, w)}")
            except (sync.RcloneError, OSError) as e:
                results.append(f"{w.name}: {str(e)[:300]}")
        return RedirectResponse(f"{P}?{'ws=' + quote(want, safe='') + '&' if want else ''}refreshed={quote(', '.join(results), safe='')}", status_code=303)

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
        summary = f"<p><small>summary: {_esc(c['summary'])}</small></p>" if c.get("summary") else ""
        active = jobs.active(ws.name, cid) if jobs is not None else []
        running = ("<div class='jobs'><b>進行中のジョブ</b><ul>" + "".join(
            f"<li class='job'><b>{_esc(j.kind)}</b> <span class='muted'>{_esc(j.status)} · {_esc(j.elapsed_sec())}s · {_esc(j.id)}</span>"
            f"<br><code>{_esc(j.progress or ('(waiting for the previous job of this case)' if j.status == 'queued' else '(no output yet)'))}</code></li>" for j in active)
            + "</ul><small class='muted'>checkin / open_case の取り寄せ（MCP のジョブ）。同じ案件のジョブは 1 つずつ実行（queued は先行ジョブ待ち）。"
              "完了すると時系列に checkin event が出る（取り寄せは出ない）。再読み込みで更新</small></div>"
            if active else "")
        causal = "".join(f"<li>{_esc(x.get('symptom', ''))} → {_esc(x.get('component', ''))} → {_esc(x.get('cause', ''))}"
                         f" <small class='muted'>({_esc(x.get('evidence', ''))})</small></li>" for x in c.get("causal", []) if isinstance(x, dict))
        wl = ws.cases_dir / cid / "worklog.md"
        worklog = _esc(wl.read_text(encoding="utf-8", errors="replace")) if wl.exists() else "(no worklog.md)"
        forms = (f"<h3>操作</h3>"
                 f"<form method=post action='{_url(ws, cid, 'task')}' accept-charset='utf-8'><input name=title placeholder='新しいタスク（計画の新版として追加）' size=50>"
                 f"<select name=owner><option>ai</option><option>human</option></select><button>追加</button></form>"
                 f"<form method=post action='{_url(ws, cid, 'comment')}' accept-charset='utf-8'><input name=note placeholder='コメント／指示' size=50><button>記録</button></form>"
                 f"<form method=post action='{_url(ws, cid, 'status')}' accept-charset='utf-8'><select name=status>"
                 + "".join(f"<option{' selected' if s == c.get('status') else ''}>{s}</option>" for s in ("open", "closed", "suspended"))
                 + f"</select><input name=note placeholder='理由' size=30><button>案件の状態を変更</button></form>"
                 f"<form method=post action='{_url(ws, cid, 'extract')}'><button>下書きを取得</button> "
                 f"<small>extract.agent={_esc(conf.extract_agent)} の子エージェントが案件ディレクトリを読んで case.json の下書きを返す（書き込まない。適用は次の画面で）</small></form>")
        body = (f"<h2>{_esc(cid)} <small>{_esc(c.get('title', ''))}</small></h2><p><small>{meta}</small></p>{running}"
                f"<p><small>objective: {_esc((plan or {}).get('objective', ''))}</small></p>{summary}"
                f"<div class='kanban'>{kanban}</div>{forms}"
                f"<h3>計画の版履歴</h3>{plans or '<small>(no plan)</small>'}"
                f"<h3>データ所在</h3><ul>{data or '<li><small>(none)</small></li>'}</ul>"
                + (f"<h3>症状 → 部品 → 原因</h3><ul>{causal}</ul>" if causal else "")
                +
                f"<details><summary>worklog.md</summary><pre>{worklog}</pre></details>"
                f"<h3>時系列</h3>{evs}")
        return _page(cid, body)

    async def settings(req: Request) -> Response:
        """rules の現在値と編集フォーム（docs/ui.md「設定」）。保存後は ?saved=<メッセージ> で戻ってくる。"""
        v = cfg.rules_view(conf)
        message = req.query_params.get("saved") or ""
        note = f"<p class='ok'>{_esc(message)}</p>" if message else ""

        def _set_form(key: str, current: object, hint: str) -> str:
            cur = "" if current is None else ("true" if current is True else "false" if current is False else str(current))
            return (f"<tr><th>{_esc(key)}</th><td><code>{_esc(cur) or '(none)'}</code></td><td>"
                    f"<form class='inline' method=post action='{P}/settings/set' accept-charset='utf-8'><input type=hidden name=key value='{_esc(key)}'>"
                    f"<input name=value value='{_esc(cur)}' size=24><button>保存</button></form> <small class='muted'>{_esc(hint)}</small></td></tr>")
        rows = (_set_form("raw_data.min_size", v["raw_data.min_size"], "これを超えるファイルはテキスト層の同期から外れ、min_age 後に raw-move の対象（rclone の表記: 10M, 1G）")
                + _set_form("raw_data.min_age", v["raw_data.min_age"], "更新からこの期間を過ぎた生データだけ raw-move で Drive へ移動（14d, 12h, 2w）")
                + _set_form("bag_to_zst", v["bag_to_zst"], "*.bag / *.bag.active を zstd 圧縮してから扱う（true / false）")
                + _set_form("bwlimit", v["bwlimit"], "rclone の --bwlimit にそのまま渡す（4M、\"08:00,4M 20:00,off\"。off で制限なし）")
                + _set_form("rclone_flags", " ".join(v["rclone_flags"]) or None,
                            "rclone を呼ぶすべての箇所に付ける追加引数（空白区切り。-- で始まるオプションと値だけ。空で既定に戻す）。"
                            "例: --transfers 8 --checkers 16 --drive-pacer-min-sleep 10ms --drive-pacer-burst 200（自前の OAuth client_id が前提。README「専用 OAuth クライアント」）")),

        def _list(title: str, items: list[str], add: str, remove: str, name: str, hint: str) -> str:
            lis = "".join(f"<li><code>{_esc(x)}</code> <form class='inline' method=post action='{P}/settings/{remove}' accept-charset='utf-8'>"
                          f"<input type=hidden name={name} value='{_esc(x)}'><button>削除</button></form></li>" for x in items)
            return (f"<h3>{title}</h3><ul>{lis or '<li><small>(none)</small></li>'}</ul>"
                    f"<form method=post action='{P}/settings/{add}' accept-charset='utf-8'><input name={name} size=24 placeholder='{_esc(hint)}'><button>追加</button></form>")
        body = (f"<h2>設定 <small>rules（同期・退避規則）</small></h2>{note}"
                f"<p><small class='muted'>設定ファイル: <code>{_esc(conf.path)}</code>（kairn が書く。手で編集しない）。CLI の <code>kairn rules …</code> と同じ操作。"
                f"変更後は <code>kairn checkin &lt;ws&gt; &lt;case&gt; --dry-run</code> で転送対象を確認できる</small></p>"
                f"<table><tr><th>key</th><th>現在値</th><th></th></tr>{rows}</table>"
                + _list("exclude（同期・移動しないパターン。rclone のフィルタ規則）", v["exclude"], "add-exclude", "remove-exclude", "pattern", "logs/** や *.csv")
                + _list("raw_data.extensions（生データ扱いの拡張子）", v["raw_data.extensions"], "add-raw-ext", "remove-raw-ext", "ext", "bag"))
        return _page("settings", body)

    async def settings_act(req: Request) -> Response:
        if not same_origin(req):
            return PlainTextResponse("forbidden: cross-site request", status_code=403)
        op = req.path_params["op"]
        form = await req.form()
        try:
            if op == "set":
                key = str(form.get("key", "")); out = cfg.set_rule(conf, key, str(form.get("value", "")))
                msg = f"{key} = {'(none)' if out is None or out == [] else ' '.join(out) if isinstance(out, list) else out}"
            elif op == "add-exclude":
                pat = str(form.get("pattern", "")); msg = f"exclude += {pat}" if cfg.add_exclude(conf, pat) else f"exclude already has {pat}"
            elif op == "remove-exclude":
                pat = str(form.get("pattern", "")); cfg.remove_exclude(conf, pat); msg = f"exclude -= {pat}"
            elif op == "add-raw-ext":
                ext = str(form.get("ext", "")); msg = f"raw_data.extensions += {ext}" if cfg.add_raw_ext(conf, ext) else f"raw_data.extensions already has {ext}"
            elif op == "remove-raw-ext":
                ext = str(form.get("ext", "")); cfg.remove_raw_ext(conf, ext); msg = f"raw_data.extensions -= {ext}"
            else:
                return PlainTextResponse("unknown action", status_code=404)
        except ValueError as e:  # 検証エラー: 保存しない（他の UI 操作と同じく 400）
            return PlainTextResponse(f"invalid: {e}", status_code=400)
        return RedirectResponse(f"{P}/settings?saved={quote(msg, safe='')}", status_code=303)

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
        elif kind == "extract":  # 子エージェントで下書きを作り、差分と適用ボタンを表示する（case.json は書かない）
            from . import extract
            # 子プロセスは最長 extract.timeout 秒ブロックする。同じプロセスの MCP（/mcp）を止めないようスレッドで実行する
            r = await run_in_threadpool(extract.extract_card, conf, ws, cid)
            return _page(f"{cid} draft", _draft_view(ws, cid, st.load_case(cid), r))
        elif kind == "apply":  # 人が確認した下書きを case.json に適用（title / summary / elements / related / causal）
            from . import extract
            try:
                card = json.loads(str(form.get("card", "")))
                extract.apply_card(st, cid, card)
            except (ValueError, TypeError) as e:
                return PlainTextResponse(f"invalid draft: {e}", status_code=400)
        else:
            return PlainTextResponse("unknown action", status_code=404)
        return RedirectResponse(_url(ws, cid), status_code=303)

    def _draft_view(ws: cfg.Workspace, cid: str, c: dict, r: dict) -> str:
        head = (f"<h2><a href='{_url(ws, cid)}'>{_esc(cid)}</a> <small>下書き</small></h2>"
                f"<p>agent: <b>{_esc(r.get('agent'))}</b> · {_esc(r.get('elapsed_sec'))}s · "
                + (f"<b style='color:#3a8'>ok</b>" if r.get("ok") else f"<b class='stale'>失敗</b>: {_esc(r.get('error'))}") + "</p>")
        if not r.get("ok"):
            return head + f"<details open><summary>出力の抜粋</summary><pre>{_esc(r.get('raw_excerpt') or '(empty)')}</pre></details>"
        card = r["card"]

        def _v(x: object) -> str:
            if isinstance(x, dict):
                return "; ".join(f"{k}: {', '.join(map(str, v))}" for k, v in x.items() if v)
            if isinstance(x, list):
                return ", ".join(map(str, x))
            return str(x or "")
        unknown = card.get("related_unknown") or []
        mark = (f" <span class='stale' title='ワークスペースに実在しない案件 ID（適用しても related に入らない）'>⚠ 実在しない: {_esc(', '.join(map(str, unknown)))}</span>"
                if unknown else "")
        rows = "".join(f"<tr><th>{k}</th><td>{_esc(_v(c.get(k)))}</td><td>{_esc(_v(card.get(k)))}{mark if k == 'related' else ''}</td></tr>"
                       for k in ("title", "summary", "elements", "related"))
        causal = "".join(f"<li>{_esc(x['symptom'])} → {_esc(x['component'])} → {_esc(x['cause'])} <small class='muted'>({_esc(x['evidence'])})</small></li>"
                         for x in card.get("causal", []))
        return (head + f"<p><small>confidence: {_esc(card.get('confidence'))}</small></p>"
                f"<table><tr><th></th><th>現在の case.json</th><th>下書き</th></tr>{rows}</table>"
                f"<h3>症状 → 部品 → 原因（下書き）</h3><ul>{causal or '<li><small>(none)</small></li>'}</ul>"
                f"<form method=post action='{_url(ws, cid, 'apply')}' accept-charset='utf-8'>"
                f"<input type=hidden name=card value='{_esc(json.dumps(card, ensure_ascii=False))}'>"
                f"<button>この下書きを case.json に適用</button> <small>title / summary / elements / related / causal を置き換え、decision として記録する</small></form>"
                f"<details><summary>下書き JSON</summary><pre>{_esc(json.dumps(card, ensure_ascii=False, indent=1))}</pre></details>")

    return [Route(P, index), Route(P + "/", index), Route(P + "/refresh", refresh, methods=["POST"]),
            Route(P + "/settings", settings), Route(P + "/settings/{op}", settings_act, methods=["POST"]),
            Route(P + "/{ws}/{case}", case_page), Route(P + "/{ws}/{case}/{kind}", act, methods=["POST"])]


def _drive_mark(d: dict) -> str:
    """一覧の Drive 列: sync.drive_state の結果を印にする（title に rev / Drive 側の rev・ローカル case.json の checkin 元と時刻）。"""
    label, cls = DRIVE_MARKS.get(d.get("state", "unknown"), DRIVE_MARKS["unknown"])
    if d.get("state") == "local_changes" and d.get("drive_differs"):
        label += "（Drive も更新あり）"
    title = (f"rev: {d.get('rev') or '-'} / drive: {'(ambiguous)' if d.get('ambiguous') else d.get('drive_rev') or '-'}"
             + (f" (checked in from {d['from']}, {d.get('checked_in_at') or ''})" if d.get("from") else ""))
    files = f"<br><small class='muted'>{_esc(', '.join(d['files']))}</small>" if d.get("files") else ""
    return f"<span class='drive {cls}' title='{_esc(title)}'>{_esc(label)}</span>{files}"


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


def build_ui(conf: cfg.Config, prefix: str = "/ui", jobs: JobTable | None = None) -> Starlette:
    """UI 単体のアプリ（テスト用）。本番は server.build_app が同じルートを /mcp と同居させる。"""
    return Starlette(routes=ui_routes(conf, prefix, jobs))
