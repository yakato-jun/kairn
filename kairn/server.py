"""MCP サーバー（docs/mcp-tools.md）と UI を同じプロセスで提供する。

- MCP: streamable HTTP  /mcp   （mcp 2.x: mcp.server.mcpserver.MCPServer）
- UI : /ui                      （kairn/ui.py。Mount ではなく同じ Starlette にルートを直接載せる）
規則の実体は store（証拠必須・superseded 自動化）。ここでは引数を検証して委譲する。
ツール内の失敗は ToolError で返す（呼び出し元のエージェントに理由が文章で届く）。
rclone の転送（checkin、open_case の取り寄せ）はジョブ（kairn/jobs.py、デーモンスレッド）にして job_id を返す
（大きな案件で MCP クライアントの呼び出しタイムアウトに当たらないため）。状態は job_status で見る。
open_case の取り寄せは、先に Drive の manifest.json（rclone cat 1 回、10 秒）で案件の rev を見て、ローカルの case.json.rev と同じなら
省略する（差分なしの取り寄せでも 30 秒かかるため）。違うときだけジョブを起動し、最大 FETCH_WAIT_SEC 秒待って間に合えば取り寄せ後の
内容を返す（_fetch_from_drive）。
停止（SIGTERM / SIGINT）: MCP クライアントが streamable HTTP のセッション（SSE）を張ったままだと uvicorn の graceful shutdown が
接続の終了を待ち続け systemd の停止タイムアウトに当たるので、開いている接続は最大 GRACEFUL_SHUTDOWN_SEC 秒しか待たない。
受信時に running のジョブがあれば一覧をログに 1 行出す（ジョブ表はメモリ内。整合は次回の checkin / checkout に任せる）。
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
from .jobs import JobTable
from .store import CaseNotFound, CaseStore, append_access_log, now_iso, validate_case_id

INSTRUCTIONS = (
    "kairn: 案件（case）単位の作業ログ。案件を開くときは open_case（無ければ find_cases / list_cases で選ぶ。選ぶのは人）。"
    "作業したら log_event / update_task（done は証拠必須）。方針が変わったら plan で計画を出し直す（載せなかった open タスクは superseded になる）。"
    "終わったら checkin（ジョブとして走る。job_status で done を確認する）。ワークスペースをまたぐ参照はしない。"
)
MCP_PATH = "/mcp"
UI_PATH = "/ui"
GRACEFUL_SHUTDOWN_SEC = 5   # SIGTERM 後、開いている接続（MCP の SSE 等）を待つ上限秒。systemd の TimeoutStopSec（15）より短くする
FETCH_WAIT_SEC = 20         # open_case が取り寄せジョブの完了を待つ上限秒（間に合えば取り寄せ後の内容を返す。越えたらローカル写し＋job_id）


def _fail(e: Exception) -> ToolError:
    if isinstance(e, CaseNotFound):
        return ToolError(f"unknown case {e.args[0]!r}")
    return ToolError(f"{type(e).__name__}: {e}")


def create_server(conf: cfg.Config, default_agent: str = "unknown", jobs: JobTable | None = None,
                  fetch_wait_sec: float = FETCH_WAIT_SEC) -> MCPServer:
    """設定に閉じた MCP サーバーを作る（テストでは in-process の Client から直接繋ぐ）。
    jobs は checkin / 取り寄せのジョブ表（UI と共有する。省略時は専用に作る）。fetch_wait_sec は open_case が取り寄せを待つ上限秒。"""
    mcp = MCPServer("kairn", instructions=INSTRUCTIONS, version="0.0.1")
    jobs = jobs if jobs is not None else JobTable()

    def _ws(workspace: str | None, case: str | None = None) -> cfg.Workspace:
        if case is not None:  # ファイルシステムに触れる前に案件 ID を検証する（"../x" 等）
            try:
                validate_case_id(case)
            except ValueError as e:
                raise ToolError(str(e)) from None
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
        """案件を開く: case / 最新計画 / open タスク / 直近イベント / 人からの差し戻し・コメント / 関連案件 / worklog 末尾 を 1 回で返す（available=true）。drive: Drive の manifest と rev を比べ、同じなら up_to_date=true（取り寄せ省略）、違えば取り寄せて fetched=true（20 秒で間に合わなければ job_id）。ローカルに無く Drive から取り寄せ中なら available=false, status=fetching, job_id（エラーではない。job_status が done になってから再実行）。取り寄せが失敗していれば status=failed, error。"""
        ws = _ws(workspace, case); st = _store(ws)
        try:
            st.case_dir(case)  # ID の検証（Drive 取り寄せの前）
            # 順序: ワークスペース解決 → manifest の rev を比較 → 違えば取り寄せジョブ（events.jsonl はマージ、他は --update）を起動して
            # 最大 fetch_wait_sec 待つ → ローカル内容を読む（間に合えば取り寄せ後、間に合わなければ今の写し。job_status が done になってから
            # もう一度 open_case すると最新になる）
            # ローカルに無い案件の直前の取り寄せが failed なら、その error を報告する（新しい取り寄せは下で起動＝再試行）
            prev = None if (ws.cases_dir / case / "case.json").exists() else jobs.latest("checkout", ws.name, case)
            fetched = _fetch_from_drive(conf, ws, st, case, jobs, wait_sec=fetch_wait_sec)
            try:
                c = st.load_case(case)
            except CaseNotFound:
                # ローカルに無い案件はエラーにせず、取り寄せの状態を通常の結果として返す（available=false）:
                #   fetching: 取り寄せジョブが queued / running（job_status が done になってから open_case を再実行）
                #   failed:   取り寄せが失敗した（直前のジョブ、または今起動したジョブが即座に失敗）。job_id は再試行のジョブ
                # ジョブが done なのに無い（Drive にも無い）／取り寄せを起動しなかった（skip）なら従来どおり unknown case
                job = jobs.get(fetched["job_id"]) if fetched.get("job_id") else None
                if prev is not None and prev.status == "failed" and job is not None:
                    return {"available": False, "status": "failed", "case": None, "error": prev.error, "job_id": job.id,
                            "note": "直前の取り寄せが失敗した（error）。再試行のジョブを起動した: job_status で確認し、失敗が続くなら error を人に伝える"}
                if job is not None and job.active:
                    return {"available": False, "status": "fetching", "job_id": job.id, "case": None,
                            "note": "取り寄せ中。job_status で done を確認してから open_case を再実行"}
                if job is not None and job.status == "failed":
                    return {"available": False, "status": "failed", "case": None, "error": job.error, "job_id": job.id,
                            "note": "取り寄せが失敗した（error）。open_case を再実行すると再試行する。失敗が続くなら error を人に伝える"}
                raise ToolError(f"unknown case {case!r} in workspace {ws.name!r} (drive: {fetched})") from None
            plan = st.current_plan(case)
            all_events = st.events(case)
            feedback = [e for e in all_events if e.get("actor") == "human" and e.get("action") in ("sendback", "comment")][-5:]
            wl = ws.cases_dir / case / "worklog.md"
            tail = wl.read_text(encoding="utf-8", errors="replace")[-3000:] if wl.exists() else ""
            # 閲覧記録はローカルの index/access.log へ（events.jsonl には書かない: 閲覧で Drive との差分を作らない）
            append_access_log(ws.index_dir / "access.log", case, _agent(agent))
            return {"available": True, "case": c, "plan": plan, "open_tasks": st.open_tasks(case), "recent_events": all_events[-20:],
                    "human_feedback": feedback, "related": c.get("related", []), "worklog_tail": tail,
                    "drive": fetched, "paths": {"case_dir": str(ws.cases_dir / case), "worklog": str(wl)}}
        except ToolError:
            raise
        except Exception as e:
            raise _fail(e) from e

    @mcp.tool()
    def list_cases(workspace: str | None = None, status: str = "open", query: str = "") -> list[dict[str, Any]]:
        """案件一覧（進捗 done/全・最終イベント・Drive との同期状態付き）。status: open|closed|suspended|all。query は id/title の部分一致。drive.state: synced|drive_newer|local_changes|unknown（直近に取得した manifest のキャッシュとの比較。checked はその取得時刻）。"""
        from . import sync
        ws = _ws(workspace); st = _store(ws)
        cache = sync.load_manifest_cache(ws)
        out = []
        for cid in st.list_case_ids():
            c = st.load_case(cid)
            if status != "all" and c["status"] != status:
                continue
            if query and query.lower() not in (cid + " " + c.get("title", "")).lower():
                continue
            le = st.last_event(cid)
            out.append({"case": cid, "title": c.get("title"), "status": c["status"], "progress": st.progress(cid),
                        "last_event": {k: le.get(k) for k in ("t", "actor", "agent", "action", "note")} if le else None,
                        "drive": {**sync.drive_state(st, cache, cid), "checked": (cache or {}).get("fetched_at")}})
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
        """ローカルの案件を Drive（設定済み remote）に戻すジョブを起動し、即座に {job_id, status, note} を返す。完了は job_status(job_id) が done になったとき（result に従来の結果と last_checkin_at）。同じ案件の checkin が走っていればその job_id を返す。"""
        from . import sync
        ws = _ws(workspace, case); st = _store(ws)
        try:
            st.load_case(case)
        except Exception as e:
            raise _fail(e) from e
        agent_name = _agent(agent)
        job, created = jobs.submit("checkin", ws.name, case, lambda progress: sync.checkin_job(conf, ws, case, agent_name, progress))
        note = (("checkin queued behind another job for this case (jobs for one case run one at a time); poll job_status(job_id) until status is done (or failed: see error)"
                 if job.status == "queued" else "checkin started in the background; poll job_status(job_id) until status is done (or failed: see error)")
                if created else "a checkin for this case is already running; poll job_status(job_id) for that one")
        return {"job_id": job.id, "status": job.status, "note": note}

    @mcp.tool()
    def job_status(job_id: str) -> dict[str, Any]:
        """ジョブ（checkin / open_case の取り寄せ）の状態: {job_id, kind, case, status: queued|running|done|failed, progress, elapsed_sec, result, error}。queued は同じ案件の先行ジョブ待ち（同一案件のジョブは 1 つずつ実行）。done なら result に従来の結果（checkin: ok / rclone / last_checkin_at）。ジョブ表はサーバーのメモリ内（再起動で消える）。"""
        job = jobs.get(job_id)
        if job is None:
            raise ToolError(f"unknown job {job_id!r} (jobs live in the server's memory: finished ones are dropped after 24h / 200 entries, "
                            "and all are lost when kairn serve restarts. the case's last_checkin_at and checkin event still show whether a checkin completed)")
        return job.to_dict()

    @mcp.tool()
    def extract_card(case: str, workspace: str | None = None) -> dict[str, Any]:
        """文脈隔離した子エージェント（設定 extract.agent）で case.json の下書きを作る。読み取り専用・書き込まない。失敗は ok=False と error で返す（is_error にしない）。"""
        from . import extract
        ws = _ws(workspace, case)
        try:
            return extract.extract_card(conf, ws, case)
        except Exception as e:
            raise _fail(e) from e

    @mcp.tool()
    def drive_index(pattern: str, workspace: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Drive 上のファイル一覧（drive-index.txt）を正規表現で検索する（生データの所在）。"""
        from . import sync
        try:
            return sync.grep_drive_index(_ws(workspace), pattern, limit)
        except Exception as e:
            raise _fail(e) from e

    return mcp


def _fetch_from_drive(conf: cfg.Config, ws: cfg.Workspace, st: CaseStore, case: str, jobs: JobTable,
                      wait_sec: float = FETCH_WAIT_SEC) -> dict[str, Any]:
    """open_case の取り寄せ判定（docs/mcp-tools.md「open_case の drive」）。順に:
    1. case.json.last_checkin_at より新しいローカル変更があれば skip（未 checkin の変更を Drive で上書きしない。manifest も見ない）。
       open_case 自身は events.jsonl に書かない（閲覧記録は index/access.log）ので、繰り返し開いても skip にならない
    2. Drive の manifest.json を rclone cat で 1 回読む（sync.MANIFEST_TIMEOUT_SEC。キャッシュも更新）。
       ローカルに案件があり、manifest が取れなければ取り寄せず {up_to_date: None, note: "manifest unavailable"}。
       manifest の当該案件の rev がローカルの case.json.rev と同じなら取り寄せず {up_to_date: True, checked}
    3. それ以外（rev が違う／manifest にエントリが無い＝未知として安全側／ローカルに無い）は checkout をジョブとして起動する。
       ローカルに案件があれば最大 wait_sec 秒待ち、done なら {fetched: True}、failed なら {fetched: False, status: failed, error}、
       間に合わなければ {fetched: False, job_id, status}（open_case は今のローカル写しを返す）。ローカルに無い案件は待たない
       （従来の fetching 応答。open_case が available=false で返す）。同じ案件の取り寄せが走っていればその job_id。
    rclone の失敗はジョブの error に残る。"""
    from . import sync
    changed = st.local_changes_since_checkin(case)
    if changed:
        return {"fetched": False, "skipped": "local changes newer than last checkin", "files": changed}
    local = (ws.cases_dir / case / "case.json").exists()
    manifest = sync.refresh_manifest(conf, ws)
    checked = now_iso()
    entry = manifest["cases"].get(case) if manifest else None
    entry = entry if isinstance(entry, dict) else None
    if local:
        if manifest is None:
            return {"fetched": False, "up_to_date": None, "checked": checked,
                    "note": f"manifest unavailable ({sync.manifest_drive_path(conf, ws)}: offline, or not created yet: kairn manifest rebuild {ws.name}); "
                            "the drive was not consulted and this result is the current local copy"}
        local_rev = st.load_case(case).get("rev")
        if entry is not None and local_rev and entry.get("rev") == local_rev:
            return {"fetched": False, "up_to_date": True, "checked": checked, "rev": local_rev}
    job, created = jobs.submit("checkout", ws.name, case, lambda progress: sync.checkout(conf, ws, case, progress=progress))
    base: dict[str, Any] = {"fetched": False, "up_to_date": False, "checked": checked, "job_id": job.id,
                            "drive_rev": entry.get("rev") if entry else None}
    if local and job.wait(wait_sec):
        if job.status == "done":
            return {**base, "fetched": True, "up_to_date": True, "rev": st.load_case(case).get("rev"),
                    "note": "fetched from the drive (rev differed); this result reflects the fetched content"}
        return {**base, "status": "failed", "error": job.error,
                "note": "the fetch from the drive failed (error); this result is the current local copy"}
    return {**base, "status": job.status,
            "note": ((f"fetching from the drive in the background (not finished within {wait_sec:g}s); this result is the current local copy. "
                      "open_case again after job_status(job_id) reports done") if created
                     else "a fetch for this case is already running; this result is the current local copy. open_case again after job_status(job_id) reports done")}


def build_app(conf: cfg.Config, host: str = "127.0.0.1", default_agent: str = "unknown") -> Starlette:
    """UI（/ui…）と MCP（/mcp）を 1 つの Starlette に載せる。/ は /ui へ。"""
    from .ui import ui_routes
    jobs = JobTable()  # MCP と UI で共有（UI は案件ページに進行中のジョブを出す）
    mcp = create_server(conf, default_agent, jobs)
    mcp_app = mcp.streamable_http_app(streamable_http_path=MCP_PATH, host=host)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield

    routes = [Route("/", lambda r: RedirectResponse(UI_PATH)), *ui_routes(conf, UI_PATH, jobs),
              Mount("/", app=mcp_app)]  # Mount は残り全部を受けるので最後
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.mcp = mcp
    app.state.jobs = jobs
    return app


def log_running_jobs(jobs: JobTable, sig: int, out=print) -> None:
    """停止シグナル受信時: running のジョブ（kind / workspace / case / 経過秒）を 1 行で出す。無ければ何も出さない。
    シグナルハンドラから呼ぶのでジョブ表のロックは取らない（JobTable.running_snapshot）。"""
    running = jobs.running_snapshot()
    if not running:
        return
    items = ", ".join(f"{j.kind} {j.workspace}/{j.case} ({j.elapsed_sec():.0f}s, {j.id})" for j in running)
    out(f"kairn: signal {sig}: shutting down with {len(running)} running job(s): {items}. "
        "jobs are not persisted; a checkin/checkout cut short here is reconciled by the next checkin/checkout of that case",
        flush=True)


def serve(conf: cfg.Config, host: str = "127.0.0.1", port: int = 8765):
    """uvicorn で app を動かす（ブロックする）。停止シグナルでは開いている接続を最大 GRACEFUL_SHUTDOWN_SEC 秒しか待たず、
    running のジョブがあればログに出す。uvicorn.run は内部で Server を作りハンドラを差し込めないので Config + Server を直接使う。
    返り値は uvicorn.Server（テストが引数と handle_exit を確かめる用）。"""
    import uvicorn
    app = build_app(conf, host)
    print(f"kairn: MCP http://{host}:{port}{MCP_PATH}   UI http://{host}:{port}{UI_PATH}", flush=True)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning",
                                           timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SEC))
    uvicorn_exit = server.handle_exit

    def handle_exit(sig, frame) -> None:
        log_running_jobs(app.state.jobs, sig)
        uvicorn_exit(sig, frame)

    server.handle_exit = handle_exit   # capture_signals は signal.signal(sig, self.handle_exit) なのでインスタンス属性で差し替わる
    server.run()
    return server
