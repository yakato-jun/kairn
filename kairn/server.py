"""MCP サーバー（docs/mcp-tools.md）と UI を同じプロセスで提供する。

- MCP: streamable HTTP  /mcp   （mcp 2.x: mcp.server.mcpserver.MCPServer）
- UI : /ui                      （kairn/ui.py。Mount ではなく同じ Starlette にルートを直接載せる）
規則の実体は store（証拠必須・superseded 自動化）。ここでは引数を検証して委譲する。
ツール内の失敗は ToolError で返す（呼び出し元のエージェントに理由が文章で届く）。
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.responses import RedirectResponse
from starlette.routing import Mount, Route

from . import config as cfg
from .index import Index
from .store import CaseNotFound, CaseStore

INSTRUCTIONS = (
    "kairn: 案件（case）単位の作業ログ。案件を開くときは open_case（無ければ find_cases / list_cases で選ぶ。選ぶのは人）。"
    "作業したら log_event / update_task（done は証拠必須）。方針が変わったら plan で計画を出し直す（載せなかった open タスクは superseded になる）。"
    "終わったら checkin。ワークスペースをまたぐ参照はしない。"
)
MCP_PATH = "/mcp"
UI_PATH = "/ui"


def _fail(e: Exception) -> ToolError:
    if isinstance(e, CaseNotFound):
        return ToolError(f"unknown case {e.args[0]!r}")
    return ToolError(f"{type(e).__name__}: {e}")


def create_server(conf: cfg.Config, default_agent: str = "unknown") -> MCPServer:
    """設定に閉じた MCP サーバーを作る（テストでは in-process の Client から直接繋ぐ）。"""
    mcp = MCPServer("kairn", instructions=INSTRUCTIONS, version="0.0.1")

    def _ws(workspace: str | None, case: str | None = None) -> cfg.Workspace:
        if workspace:
            if workspace not in conf.workspaces:
                raise ToolError(f"unknown workspace {workspace!r} (known: {list(conf.workspaces)})")
            return conf.workspaces[workspace]
        if case:  # 案件 ID から一意に決まるならそれを使う
            hits = [w for w in conf.workspaces.values() if (w.cases_dir / case / "case.json").exists()]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise ToolError(f"case {case!r} exists in several workspaces {[w.name for w in hits]}; pass workspace=")
        if len(conf.workspaces) == 1:
            return next(iter(conf.workspaces.values()))
        raise ToolError(f"workspace is required (registered: {list(conf.workspaces)})")

    def _store(ws: cfg.Workspace) -> CaseStore:
        return CaseStore(ws.cases_dir)

    def _index(ws: cfg.Workspace) -> Index:
        ix = Index(ws.index_dir, ws.cases_dir)
        ix.rebuild()  # 差分のみ
        return ix

    def _agent(agent: str) -> str:
        return agent or default_agent

    @mcp.tool()
    def open_case(case: str, workspace: str | None = None, agent: str = "") -> dict[str, Any]:
        """案件を開く: case / 最新計画 / open タスク / 直近イベント / 人からの差し戻し・コメント / 関連案件 / worklog 末尾 を 1 回で返す。"""
        ws = _ws(workspace, case); st = _store(ws)
        try:
            st.case_dir(case)  # ID の検証（Drive 取り寄せの前）
            # 順序: ワークスペース解決 → checkout（--update）→ 読み込み。返り値はすべて取り寄せ後のディスクから読む
            fetched = _fetch_from_drive(conf, ws, st, case)
            try:
                c = st.load_case(case)
            except CaseNotFound:
                raise ToolError(f"unknown case {case!r} in workspace {ws.name!r} (drive: {fetched})") from None
            plan = st.current_plan(case)
            all_events = st.events(case)
            feedback = [e for e in all_events if e.get("actor") == "human" and e.get("action") in ("sendback", "comment")][-5:]
            wl = ws.cases_dir / case / "worklog.md"
            tail = wl.read_text(encoding="utf-8", errors="replace")[-3000:] if wl.exists() else ""
            st.append_event(case, {"actor": "ai", "agent": _agent(agent), "action": "checkout", "note": "open_case"})
            return {"case": c, "plan": plan, "open_tasks": st.open_tasks(case), "recent_events": all_events[-20:],
                    "human_feedback": feedback, "related": c.get("related", []), "worklog_tail": tail,
                    "drive": fetched, "paths": {"case_dir": str(ws.cases_dir / case), "worklog": str(wl)}}
        except ToolError:
            raise
        except Exception as e:
            raise _fail(e) from e

    @mcp.tool()
    def list_cases(workspace: str | None = None, status: str = "open", query: str = "") -> list[dict[str, Any]]:
        """案件一覧（進捗 done/全・最終イベント付き）。status: open|closed|suspended|all。query は id/title の部分一致。"""
        ws = _ws(workspace); st = _store(ws)
        out = []
        for cid in st.list_case_ids():
            c = st.load_case(cid)
            if status != "all" and c["status"] != status:
                continue
            if query and query.lower() not in (cid + " " + c.get("title", "")).lower():
                continue
            le = st.last_event(cid)
            out.append({"case": cid, "title": c.get("title"), "status": c["status"], "progress": st.progress(cid),
                        "last_event": {k: le.get(k) for k in ("t", "actor", "agent", "action", "note")} if le else None})
        return out

    @mcp.tool()
    def plan(case: str, objective: str, tasks: list[dict[str, Any]], reason: str,
             workspace: str | None = None, agent: str = "") -> dict[str, Any]:
        """計画の新版を作る。tasks: [{title, owner?: ai|human, carried_from?: "T012"}]。新版に無い open タスクは superseded になる。版番号は自動。"""
        ws = _ws(workspace, case)
        try:
            return _store(ws).new_plan_version(case, objective, tasks, reason, actor="ai", agent=_agent(agent))
        except Exception as e:
            raise _fail(e) from e

    @mcp.tool()
    def update_task(case: str, task: str, status: str, evidence: list[dict[str, Any]] | None = None, note: str = "",
                    workspace: str | None = None, agent: str = "") -> dict[str, Any]:
        """タスク状態の更新＋event 追記。status: open|doing|blocked|done|dropped。done は evidence 必須: [{type: commit|pr|file|test|url, ...}]。存在しない task は拒否。"""
        ws = _ws(workspace, case)
        try:
            return _store(ws).set_task_status(case, task, status, evidence, note, actor="ai", agent=_agent(agent))
        except Exception as e:
            raise _fail(e) from e

    @mcp.tool()
    def log_event(case: str, action: str, note: str, evidence: list[dict[str, Any]] | None = None,
                  workspace: str | None = None, agent: str = "") -> dict[str, Any]:
        """進捗・決定・コメントを追記する（actor=ai と agent を自動付与）。action: progress|decision|comment"""
        if action not in ("progress", "decision", "comment"):
            raise ToolError("action must be progress | decision | comment")
        ws = _ws(workspace, case)
        try:
            _store(ws).load_case(case)
            return _store(ws).append_event(case, {"actor": "ai", "agent": _agent(agent), "action": action, "note": note,
                                                  "evidence": evidence or []})
        except Exception as e:
            raise _fail(e) from e

    @mcp.tool()
    def search(query: str, cases: list[str] | None = None, workspace: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        """worklog 等の `## ` 節単位の全文検索（ワークスペース内のみ）。結果の file/heading で本文を特定できる。"""
        try:
            return _index(_ws(workspace)).search_sections(query, cases, limit)
        except Exception as e:
            raise _fail(e) from e

    @mcp.tool()
    def find_cases(query: str, workspace: str | None = None, k: int = 5) -> list[dict[str, Any]]:
        """問いに関係する案件を理由付きで上位 k 件（案件カード＋全文の複合）。案件を選ぶのは人。"""
        try:
            return _index(_ws(workspace)).find_cases(query, k)
        except Exception as e:
            raise _fail(e) from e

    @mcp.tool()
    def checkin(case: str, workspace: str | None = None, agent: str = "") -> dict[str, Any]:
        """ローカルの案件を Drive（設定済み remote）に戻し、checkin event を記録する。"""
        from . import sync
        ws = _ws(workspace, case); st = _store(ws)
        try:
            st.load_case(case)
            msg = sync.checkin(conf, ws, case)  # 成功時に case.json.last_checkin_at を更新する
        except Exception as e:
            raise _fail(e) from e
        st.append_event(case, {"actor": "ai", "agent": _agent(agent), "action": "checkin", "note": msg[-200:]})
        return {"ok": True, "rclone": msg, "last_checkin_at": st.load_case(case).get("last_checkin_at")}

    @mcp.tool()
    def drive_index(pattern: str, workspace: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Drive 上のファイル一覧（drive-index.txt）を正規表現で検索する（生データの所在）。"""
        from . import sync
        try:
            return sync.grep_drive_index(_ws(workspace), pattern, limit)
        except Exception as e:
            raise _fail(e) from e

    return mcp


def _fetch_from_drive(conf: cfg.Config, ws: cfg.Workspace, st: CaseStore, case: str) -> dict[str, Any]:
    """open_case の取り寄せ。case.json.last_checkin_at より新しいローカル変更があれば skip（未 checkin の変更を Drive で上書きしない）。
    失敗してもローカル写しで続行し、その旨を返す（docs/mcp-tools.md）。"""
    from . import sync
    changed = st.local_changes_since_checkin(case)
    if changed:
        return {"fetched": False, "skipped": "local changes newer than last checkin", "files": changed}
    try:
        return {"fetched": True, "rclone": sync.checkout(conf, ws, case)}
    except Exception as e:  # rclone 不在・remote 不達など
        return {"fetched": False, "error": str(e)[-300:], "note": "continuing with local copy"}


def build_app(conf: cfg.Config, host: str = "127.0.0.1", default_agent: str = "unknown") -> Starlette:
    """UI（/ui…）と MCP（/mcp）を 1 つの Starlette に載せる。/ は /ui へ。"""
    from .ui import ui_routes
    mcp = create_server(conf, default_agent)
    mcp_app = mcp.streamable_http_app(streamable_http_path=MCP_PATH, host=host)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield

    routes = [Route("/", lambda r: RedirectResponse(UI_PATH)), *ui_routes(conf, UI_PATH),
              Mount("/", app=mcp_app)]  # Mount は残り全部を受けるので最後
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.mcp = mcp
    return app


def serve(conf: cfg.Config, host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn
    app = build_app(conf, host)
    print(f"kairn: MCP http://{host}:{port}{MCP_PATH}   UI http://{host}:{port}{UI_PATH}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")
