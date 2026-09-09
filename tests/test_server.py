"""MCP サーバー（mcp 2.x）: in-process の Client で 14 ツールを呼ぶ。rclone は monkeypatch、Drive の版マーカー（cases/<case>/.rev/）はメモリ内の偽物（fake_drive）。
checkin と open_case の取り寄せはジョブ（スレッド）なので、結果を見る前に job.wait() で完了を待つ（_checkin / _open）。
open_case は Drive のマーカーの rev がローカルと違うときだけ取り寄せる: 取り寄せを起こしたいテストは fake_drive.set_rev(case, "…") で rev をずらす。"""
from __future__ import annotations

import threading
import time

import anyio
import pytest
from mcp.client import Client

from kairn import server as srv
from kairn import sync
from kairn.jobs import JobTable
from kairn.store import CaseStore
from tests.conftest import bump_mtime

TOOLS = {"open_case", "list_cases", "create_case", "plan", "update_task", "log_event", "set_case_status", "search", "find_cases", "checkin", "drive_index", "extract_card", "job_status", "link_case"}


@pytest.fixture
def mocked_rclone(monkeypatch, conf):
    calls = []
    monkeypatch.setattr(sync, "checkout", lambda c, ws, case=None, dry=False, **kw: calls.append(("checkout", ws.name, case)) or "fake checkout")
    monkeypatch.setattr(sync, "checkin", lambda c, ws, case=None, dry=False, **kw: calls.append(("checkin", ws.name, case)) or "fake checkin")
    return calls


@pytest.fixture
def jobs():
    return JobTable()


def run(coro_fn):
    return anyio.run(coro_fn)


def _wait(jobs: JobTable, job_id: str, timeout: float = 5.0):
    job = jobs.get(job_id)
    assert job is not None and job.wait(timeout), f"job {job_id} did not finish"
    return job


async def _checkin(c, jobs, case, **kw):
    """checkin を呼び、ジョブの完了を待って job_status の結果（dict）を返す。"""
    r = await c.call_tool("checkin", {"case": case, **kw})
    assert not r.is_error, r.content
    job_id = r.structured_content["job_id"]
    _wait(jobs, job_id)
    s = await c.call_tool("job_status", {"job_id": job_id})
    assert not s.is_error, s.content
    return s.structured_content


async def _open(c, jobs, case, **kw):
    """open_case を呼び、取り寄せジョブが起動していればその完了を待つ（返り値は open_case の結果そのまま＝取り寄せ前のローカル内容）。"""
    r = await c.call_tool("open_case", {"case": case, **kw})
    if not r.is_error and r.structured_content["drive"].get("job_id"):
        _wait(jobs, r.structured_content["drive"]["job_id"])
    return r


def test_tools_listed_with_instructions(conf):
    mcp = srv.create_server(conf)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            tools = await c.list_tools()
            assert {t.name for t in tools.tools} == TOOLS
            assert c.instructions == srv.INSTRUCTIONS
            assert c.server_info.name == "kairn"
    run(main)


def test_full_flow(conf, mocked_rclone, jobs, fake_drive):
    ws = conf.workspaces["acme"]
    fake_drive.set_rev("CASE-123", "on-drive")   # ローカル（rev 無し）と違う → open_case は取り寄せる
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-123", "起動時に driver が初期化されない", "acme", actor="human", elements={"machine": ["unit-2"]}, related=["CASE-100"])
    (ws.cases_dir / "CASE-123" / "worklog.md").write_text("# t\n## Objective\n起動時に widget driver の init が終わらない\n## Notes\nUART 460800 で送信量が超過する\n", encoding="utf-8")
    mcp = srv.create_server(conf, default_agent="test-agent", jobs=jobs)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            # plan
            r = await c.call_tool("plan", {"case": "CASE-123", "objective": "boot works", "reason": "initial",
                                           "tasks": [{"title": "調査"}, {"title": "修正", "owner": "ai"}]})
            assert not r.is_error and r.structured_content["version"] == 1
            assert [t["id"] for t in r.structured_content["tasks"]] == ["T001", "T002"]
            # done without evidence -> refused (is_error, message)
            r = await c.call_tool("update_task", {"case": "CASE-123", "task": "T001", "status": "done", "note": "x"})
            assert r.is_error and "evidence" in r.content[0].text
            # unknown task -> refused
            r = await c.call_tool("update_task", {"case": "CASE-123", "task": "T999", "status": "doing"})
            assert r.is_error and "T999" in r.content[0].text
            # done with evidence -> ok
            r = await c.call_tool("update_task", {"case": "CASE-123", "task": "T001", "status": "done", "note": "調査済み",
                                                  "evidence": [{"type": "commit", "repo": "acme-robot", "id": "abc1234"}]})
            assert not r.is_error and r.structured_content["status"] == "done"
            # log_event
            r = await c.call_tool("log_event", {"case": "CASE-123", "action": "decision", "note": "1 バイト送信をやめる"})
            assert not r.is_error and r.structured_content["actor"] == "ai" and r.structured_content["agent"] == "test-agent"
            r = await c.call_tool("log_event", {"case": "CASE-123", "action": "opened", "note": "bad"})
            assert r.is_error
            # search / find_cases
            r = await c.call_tool("search", {"query": "UART 460800"})
            hits = r.structured_content["results"]
            assert not r.is_error and hits[0]["case"] == "CASE-123" and hits[0]["heading"] == "Notes"
            assert hits[0]["workspace"] == "acme" and hits[0]["cross_workspace"] is False
            assert r.structured_content["workspace"] == "acme" and r.structured_content["scope"] == "auto" and r.structured_content["searched"] == ["acme"]
            r = await c.call_tool("search", {"query": "送信量（超過）", "cases": ["CASE-123"]})
            assert not r.is_error and r.structured_content["results"]
            r = await c.call_tool("find_cases", {"query": "unit-2 driver init"})
            assert not r.is_error and r.structured_content["results"][0]["case"] == "CASE-123" and r.structured_content["searched"] == ["acme"]
            # list_cases
            r = await c.call_tool("list_cases", {})
            lc = r.structured_content["result"]
            assert lc[0]["case"] == "CASE-123" and lc[0]["progress"] == {"total": 2, "done": 1, "open": 1, "plan": 1}
            assert lc[0]["drive"]["state"] == "unknown" and lc[0]["drive"]["checked"] is None   # Drive の版は未取得
            # human sendback via store (UI と同じ書き込み) -> open_case の human_feedback に出る
            st.append_event("CASE-123", {"actor": "human", "action": "sendback", "task": "T001", "note": "unit-6 でも確認"})
            r = await _open(c, jobs, "CASE-123")
            oc = r.structured_content
            assert not r.is_error and oc["case"]["id"] == "CASE-123" and oc["plan"]["version"] == 1
            assert [t["id"] for t in oc["open_tasks"]] == ["T002"]
            assert oc["human_feedback"][-1]["note"] == "unit-6 でも確認" and oc["cross_workspace"] is False
            assert oc["related"] == [{"ref": "CASE-100", "workspace": "acme", "case": "CASE-100", "cross_workspace": False, "exists": False, "title": None, "status": None}]
            assert "460800" in oc["worklog_tail"] and oc["drive"]["fetched"] is True and oc["drive"]["job_id"] and oc["drive"]["drive_rev"] == "on-drive"
            assert jobs.get(oc["drive"]["job_id"]).kind == "checkout" and jobs.get(oc["drive"]["job_id"]).result == "fake checkout"
            assert st.events("CASE-123")[-1]["action"] == "sendback"  # open_case は events.jsonl に書かない
            assert (ws.index_dir / "access.log").read_text().splitlines()[-1].split("\t")[1:] == ["CASE-123", "test-agent"]
            # re-plan without carrying T002 -> superseded
            r = await c.call_tool("plan", {"case": "CASE-123", "objective": "unit-6 も", "reason": "sendback", "tasks": [{"title": "unit-6 で確認"}]})
            assert r.structured_content["superseded"] == ["T002"]
            # checkin（ジョブ）/ drive_index
            js = await _checkin(c, jobs, "CASE-123")
            assert js["status"] == "done" and js["kind"] == "checkin" and js["result"]["ok"] and js["result"]["rclone"] == "fake checkin"
            assert st.events("CASE-123")[-1]["action"] == "checkin" and st.events("CASE-123")[-1]["agent"] == "test-agent"
            (ws.index_dir / "drive-index.txt").write_text("acme/cases/CASE-123/0901_1200_run.bag.zst\t123456\t2026-09-01T00:00:00\nacme/cases/other.txt\t1\t\n")
            r = await c.call_tool("drive_index", {"pattern": r"CASE-123.*\.zst$"})
            assert r.structured_content["result"] == [{"path": "acme/cases/CASE-123/0901_1200_run.bag.zst", "size": "123456", "mtime": "2026-09-01T00:00:00"}]
            # unknown case（取り寄せジョブがまだ走っていれば available=false / fetching、終わって無ければ unknown case）/ workspace
            r = await c.call_tool("open_case", {"case": "CASE-404"})
            if r.is_error:
                assert "CASE-404" in r.content[0].text
            else:
                assert r.structured_content["available"] is False and r.structured_content["status"] == "fetching"
            r = await c.call_tool("list_cases", {"workspace": "nowhere"})
            assert r.is_error and "nowhere" in r.content[0].text
    run(main)
    assert ("checkout", "acme", "CASE-123") in mocked_rclone and ("checkin", "acme", "CASE-123") in mocked_rclone


def test_open_case_continues_when_drive_fails(conf, monkeypatch, jobs, fake_drive):
    """rclone の失敗は open_case を止めず（ローカル写しを返す）、取り寄せジョブの failed / error に残る（drive にも status / error）。"""
    ws = conf.workspaces["acme"]
    CaseStore(ws.cases_dir).create_case("CASE-1", "t", "acme", actor="human")
    fake_drive.set_rev("CASE-1", "on-drive")

    def boom(*a, **k):
        raise sync.RcloneError("remote unreachable")
    monkeypatch.setattr(sync, "checkout", boom)
    mcp = srv.create_server(conf, jobs=jobs)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            r = await _open(c, jobs, "CASE-1")
            assert not r.is_error and r.structured_content["case"]["id"] == "CASE-1"
            d = r.structured_content["drive"]
            assert d["fetched"] is False and d["job_id"] and "local copy" in d["note"] and d["status"] == "failed" and d["error"] == "RcloneError: remote unreachable"
            s = await c.call_tool("job_status", {"job_id": d["job_id"]})
            assert s.structured_content["status"] == "failed" and s.structured_content["error"] == "RcloneError: remote unreachable"
            assert s.structured_content["kind"] == "checkout" and s.structured_content["case"] == "CASE-1"
    run(main)


def test_search_survives_broken_symlink(conf, monkeypatch, requires_symlinks):
    """再現した不具合: cases 配下の壊れたリンク（*.md）で索引再構築が例外 → search がエラー文字列を返していた。"""
    import os
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir); st.create_case("CASE-1", "t", "acme", actor="human")
    os.symlink("/nonexistent/x.md", ws.cases_dir / "CASE-1" / "broken.md")
    os.symlink(ws.cases_dir, ws.cases_dir / "CASE-1" / "loop")
    mcp = srv.create_server(conf)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            r = await c.call_tool("search", {"query": "Objective"})
            assert not r.is_error, r.content
            assert r.structured_content["results"][0]["case"] == "CASE-1"
    run(main)


# ---------- open_case の順序・checkout skip（項目 1） ----------

def _fake_checkout_creating_case(conf, calls, gate: threading.Event | None = None):
    """Drive にしか無い案件を取り寄せる偽 checkout: 案件ディレクトリを作って成功を返す（2 回目以降は何もしない）。
    gate を渡すと、それが set されるまで転送を始めない（open_case が「取り寄せ中」を返す状況を作る）。"""
    def checkout(c, ws, case=None, dry=False, **kw):
        calls.append(("checkout", ws.name, case))
        if gate is not None:
            assert gate.wait(5)
        st = CaseStore(ws.cases_dir)
        if (ws.cases_dir / case / "case.json").exists():
            return "fake checkout (already local)"
        st.create_case(case, "from drive", ws.name, actor="human")
        st.new_plan_version(case, "obj", [{"title": "t1"}], reason="on drive", actor="ai")
        (ws.cases_dir / case / "worklog.md").write_text("# from drive\n## Notes\nfetched text\n", encoding="utf-8")
        return "fake checkout created case"
    return checkout


def test_open_case_fetches_in_background_then_reads(conf, monkeypatch, jobs, fake_drive):
    """Drive にしか無い案件: 1 回目の open_case は取り寄せジョブを起動し（待たない）、エラーではなく通常の結果
    {available: false, status: fetching, job_id, case: null, note} を返す。Drive にマーカーが無くてもローカルに無い案件は取り寄せる。
    ジョブ完了後の 2 回目は取り寄せ後のディスクを反映する（available: true。Drive のマーカーの rev と違うので再度取り寄せ、fetched: true）。"""
    calls = []
    gate = threading.Event()
    monkeypatch.setattr(sync, "checkout", _fake_checkout_creating_case(conf, calls, gate))
    mcp = srv.create_server(conf, jobs=jobs)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            r = await c.call_tool("open_case", {"case": "CASE-9"})
            assert not r.is_error, r.content
            f = r.structured_content
            assert f["available"] is False and f["status"] == "fetching" and f["case"] is None and f["job_id"]
            assert "取り寄せ中" in f["note"] and "job_status" in f["note"] and "open_case" in f["note"]
            assert jobs.get(f["job_id"]).kind == "checkout" and jobs.get(f["job_id"]).active
            # 取り寄せ中にもう一度開いても同じジョブ（新しく起動しない）
            r = await c.call_tool("open_case", {"case": "CASE-9"})
            assert r.structured_content["status"] == "fetching" and r.structured_content["job_id"] == f["job_id"]
            gate.set()
            _wait(jobs, f["job_id"])
            job = jobs.get(f["job_id"])
            assert job.status == "done" and job.result == "fake checkout created case"
            s = (await c.call_tool("job_status", {"job_id": f["job_id"]})).structured_content
            assert s["status"] == "done"
            fake_drive.set_rev("CASE-9", "d9")
            r = await _open(c, jobs, "CASE-9")
            assert not r.is_error, r.content
            oc = r.structured_content
            assert oc["available"] is True and oc["drive"]["job_id"] and oc["drive"]["fetched"] is True and oc["case"]["title"] == "from drive"
            assert oc["plan"]["version"] == 1 and [t["id"] for t in oc["open_tasks"]] == ["T001"]
            assert "fetched text" in oc["worklog_tail"] and oc["recent_events"][0]["action"] == "opened"
            # 不正な ID は取り寄せる前に拒否
            r = await c.call_tool("open_case", {"case": "../etc"})
            assert r.is_error and "invalid case id" in r.content[0].text
    run(main)
    assert calls == [("checkout", "acme", "CASE-9"), ("checkout", "acme", "CASE-9")]
    ev = CaseStore(conf.workspaces["acme"].cases_dir).events("CASE-9")
    assert [e["action"] for e in ev] == ["opened", "plan"]  # 閲覧では events.jsonl に何も足さない


def test_invalid_case_id_is_rejected_before_touching_filesystem(conf, monkeypatch):
    """L-9: 案件 ID の検証はワークスペース解決（cases_dir の存在確認）より前。cases_dir に触れたら AssertionError になる。"""
    from kairn import config as cfg
    from kairn.store import validate_case_id
    for bad in ("../x", "..", ".hidden", "a/b", ""):
        with pytest.raises(ValueError):
            validate_case_id(bad)
    validate_case_id("CASE-123")
    mcp = srv.create_server(conf)

    def boom(self):
        raise AssertionError("filesystem touched with an invalid case id")
    monkeypatch.setattr(cfg.Workspace, "cases_dir", property(boom))

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            for tool, args in [("open_case", {}), ("plan", {"objective": "o", "reason": "r", "tasks": []}),
                               ("update_task", {"task": "T001", "status": "doing"}), ("log_event", {"action": "progress", "note": "n"}),
                               ("checkin", {}), ("extract_card", {}), ("set_case_status", {"status": "closed", "instruction": "close it"})]:
                r = await c.call_tool(tool, {"case": "../x", **args})
                assert r.is_error and "invalid case id" in r.content[0].text, (tool, r.content)
    run(main)


@pytest.fixture
def inline_jobs(monkeypatch):
    """ジョブのスレッドを起動せず submit の中で同期実行する（ジョブが open_case の読み取りより先に終わる状況を決定的に作る）。"""
    monkeypatch.setattr(JobTable, "_start", lambda self, job, fn: self._run(job, fn))


def test_open_case_unknown_case_when_fetch_finished_without_it(conf, mocked_rclone, jobs, inline_jobs, fake_drive):
    """取り寄せジョブが done でも案件が無い（Drive にも無い）なら従来どおり unknown case のエラー。"""
    mcp = srv.create_server(conf, jobs=jobs)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            r = await c.call_tool("open_case", {"case": "CASE-404"})
            assert r.is_error and "CASE-404" in r.content[0].text and "fetched" in r.content[0].text and "job_id" in r.content[0].text
    run(main)
    assert jobs.latest("checkout", "acme", "CASE-404").status == "done"
    assert ("checkout", "acme", "CASE-404") in mocked_rclone  # 取り寄せは試みた（ジョブ）


def test_open_case_reports_failed_fetch_and_retries(conf, monkeypatch, jobs, fake_drive):
    """ローカルに無い案件の取り寄せが失敗: 走っている間は fetching、失敗後の open_case は
    {available: false, status: failed, error} を返し、再試行のジョブを起動する（job_id）。"""
    gate = threading.Event(); calls = []

    def failing_checkout(c, ws, case=None, dry=False, **kw):
        calls.append(case)
        assert gate.wait(5)
        raise sync.RcloneError("directory not found")
    monkeypatch.setattr(sync, "checkout", failing_checkout)
    mcp = srv.create_server(conf, jobs=jobs)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            r = await c.call_tool("open_case", {"case": "CASE-9"})
            f = r.structured_content
            assert not r.is_error and f["available"] is False and f["status"] == "fetching" and "error" not in f
            gate.set(); _wait(jobs, f["job_id"])
            assert jobs.get(f["job_id"]).status == "failed"
            gate.clear()
            r = await c.call_tool("open_case", {"case": "CASE-9"})
            f2 = r.structured_content
            assert not r.is_error and f2["available"] is False and f2["status"] == "failed" and f2["case"] is None
            assert f2["error"] == "RcloneError: directory not found" and "人に伝える" in f2["note"]
            assert f2["job_id"] != f["job_id"] and jobs.get(f2["job_id"]).active      # 再試行のジョブ
            r = await c.call_tool("open_case", {"case": "CASE-9"})                     # 再試行中は fetching（同じジョブ）
            assert r.structured_content["status"] == "fetching" and r.structured_content["job_id"] == f2["job_id"]
            gate.set(); _wait(jobs, f2["job_id"])
    run(main)
    assert calls == ["CASE-9", "CASE-9"]


def test_open_case_reports_failed_fetch_when_job_fails_immediately(conf, monkeypatch, jobs, inline_jobs, fake_drive):
    """取り寄せジョブが open_case の読み取りより先に失敗しても failed / error を返す（エラーにしない）。"""
    def boom(*a, **k):
        raise sync.RcloneError("remote unreachable")
    monkeypatch.setattr(sync, "checkout", boom)
    mcp = srv.create_server(conf, jobs=jobs)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            r = await c.call_tool("open_case", {"case": "CASE-9"})
            f = r.structured_content
            assert not r.is_error and f["available"] is False and f["status"] == "failed" and f["error"] == "RcloneError: remote unreachable"
            assert f["job_id"] and jobs.get(f["job_id"]).status == "failed"
    run(main)


def test_open_case_skips_checkout_when_local_changes_newer_than_checkin(conf, monkeypatch, jobs, fake_drive):
    """rclone は _run の層で偽装し、sync.checkout / sync.checkin 本体（版マーカー・.rev/ の作り直しを含む）を通す。
    checkin 直後（rev 一致）は取り寄せない。Drive のマーカーの rev が違えば取り寄せる。未 checkin のローカル変更があれば rev が違っても skip。"""
    import os, subprocess, time
    mocked_rclone = []

    def fake_run(cmd, dry=False, progress=None):
        if cmd[1] == "copyto":  # events.jsonl の取り寄せ: Drive に無い → 失敗（マージは飛ばす）
            raise sync.RcloneError("object not found")
        mocked_rclone.append(({"copy": "checkout", "sync": "checkin"}[cmd[1]], "acme", cmd[2].rsplit("/", 1)[-1]))
        return subprocess.CompletedProcess(cmd, 0, "fake", "")
    monkeypatch.setattr(sync, "_run", fake_run)
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "t", "acme", actor="human")
    wl = ws.cases_dir / "CASE-1" / "worklog.md"
    mcp = srv.create_server(conf, jobs=jobs)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            # last_checkin_at 未記録・Drive が読めない（オフライン）→ 取り寄せず、ローカル写し（up_to_date: None）
            fake_drive.unavailable = True
            r = await _open(c, jobs, "CASE-1")
            d = r.structured_content["drive"]
            assert d["up_to_date"] is None and "drive unavailable" in d["note"] and "job_id" not in d and mocked_rclone == []
            fake_drive.unavailable = False
            # Drive のマーカーが別の rev → 取り寄せ（ジョブ完了まで待って fetched: true）
            fake_drive.set_rev("CASE-1", "d1")
            r = await _open(c, jobs, "CASE-1")
            d = r.structured_content["drive"]
            assert d["fetched"] is True and d["up_to_date"] is True and d["job_id"] and d["drive_rev"] == "d1" and "skipped" not in d
            assert mocked_rclone.count(("checkout", "acme", "CASE-1")) == 1
            # checkin → rev / last_checkin_at が記録され .rev/<rev> が置かれる（rclone sync が Drive へ運ぶ: 偽 Drive に反映）。直後の open_case は取り寄せない（up_to_date）
            js = await _checkin(c, jobs, "CASE-1")
            assert js["status"] == "done" and js["result"]["last_checkin_at"]
            c1 = st.load_case("CASE-1")
            assert c1["last_checkin_at"] == js["result"]["last_checkin_at"] and st.rev_markers("CASE-1") == [c1["rev"]]
            fake_drive.sync_from_local(ws, "CASE-1")
            assert fake_drive.rev("CASE-1") == c1["rev"]
            r = await _open(c, jobs, "CASE-1")
            d = r.structured_content["drive"]
            assert d == {"fetched": False, "up_to_date": True, "checked": d["checked"], "rev": c1["rev"]} and d["checked"]
            assert mocked_rclone.count(("checkout", "acme", "CASE-1")) == 1
            # 別環境が checkin した（Drive のマーカーの rev が変わった）→ 取り寄せる
            fake_drive.set_rev("CASE-1", "from-another-host")
            r = await _open(c, jobs, "CASE-1")
            assert r.structured_content["drive"]["fetched"] is True and mocked_rclone.count(("checkout", "acme", "CASE-1")) == 2
            # checkin より新しいローカル変更（worklog.md の mtime を進める）→ rev が違っても checkout を skip（ジョブも作らず Drive も見ない）
            t = time.time() + 30
            os.utime(wl, (t, t))
            n = fake_drive.lookups
            r = await _open(c, jobs, "CASE-1")
            d = r.structured_content["drive"]
            assert d["skipped"] == "local changes newer than last checkin" and d["fetched"] is False and "worklog.md" in d["files"] and "job_id" not in d
            assert mocked_rclone.count(("checkout", "acme", "CASE-1")) == 2 and fake_drive.lookups == n  # 呼ばれていない
            assert st.events("CASE-1")[-1]["action"] == "checkin"             # open_case は event を書かない
            # もう一度 checkin すれば skip は解ける（偽装した未来の mtime は現在に戻す）。rev が一致するので取り寄せない
            os.utime(wl, None)
            await _checkin(c, jobs, "CASE-1")
            fake_drive.sync_from_local(ws, "CASE-1")
            r = await _open(c, jobs, "CASE-1")
            assert r.structured_content["drive"]["up_to_date"] is True and "job_id" not in r.structured_content["drive"]
            assert mocked_rclone.count(("checkout", "acme", "CASE-1")) == 2
    run(main)


def test_open_case_repeated_after_checkin_does_not_block_next_checkout(conf, monkeypatch, jobs, fake_drive):
    """checkin 後に open_case を繰り返しても（events.jsonl の mtime が CHECKIN_SLACK_SEC を超えて進んでも）checkout は skip されない。
    人／AI の実質的な変更（log_event / update_task / UI 操作）があれば skip する。checkin ジョブ自身の checkin event は変更に数えない。"""
    import os, subprocess, time
    calls = []
    monkeypatch.setattr(sync, "_run", lambda cmd, dry=False, progress=None: calls.append(cmd[1]) or subprocess.CompletedProcess(cmd, 0, "fake", ""))
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "t", "acme", actor="human")
    st.new_plan_version("CASE-1", "o", [{"title": "a"}], reason="r", actor="ai")
    ev = ws.cases_dir / "CASE-1" / "events.jsonl"

    def bump(p):  # slack（2 秒）に隠れないよう mtime を +5s
        t = time.time() + 5
        os.utime(p, (t, t))

    mcp = srv.create_server(conf, jobs=jobs)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            await _checkin(c, jobs, "CASE-1")
            for i in range(3):
                bump(ev)
                fake_drive.set_rev("CASE-1", f"other-{i}")      # 取り寄せの理由を作る（rev が違う）
                r = await _open(c, jobs, "CASE-1")
                assert r.structured_content["drive"].get("job_id") and r.structured_content["drive"]["fetched"] is True, (i, r.structured_content["drive"])
            assert calls.count("copy") == 3 and st.events("CASE-1")[-1]["action"] == "checkin"
            # AI の実質的な変更 → skip
            await c.call_tool("log_event", {"case": "CASE-1", "action": "progress", "note": "worked"})
            bump(ev)
            r = await _open(c, jobs, "CASE-1")
            d = r.structured_content["drive"]
            assert d["fetched"] is False and d["skipped"] == "local changes newer than last checkin" and d["files"] == ["events.jsonl"]
            # checkin で解け、UI 操作（人の comment）で再び skip、update_task でも skip
            await _checkin(c, jobs, "CASE-1")
            bump(ev)
            fake_drive.set_rev("CASE-1", "other-3")
            assert (await _open(c, jobs, "CASE-1")).structured_content["drive"].get("job_id")
            st.append_event("CASE-1", {"actor": "human", "action": "comment", "note": "check unit-6"})
            bump(ev)
            assert (await _open(c, jobs, "CASE-1")).structured_content["drive"].get("skipped")
            await _checkin(c, jobs, "CASE-1")
            await c.call_tool("update_task", {"case": "CASE-1", "task": "T001", "status": "doing"})
            bump(ev); bump(ws.cases_dir / "CASE-1" / "plan" / "v0001.json")
            r = await _open(c, jobs, "CASE-1")
            assert r.structured_content["drive"]["fetched"] is False and "plan/v0001.json" in r.structured_content["drive"]["files"]
    run(main)


def test_open_case_drive_rev_decides_fetch(conf, monkeypatch, jobs, fake_drive):
    """Drive のマーカーの rev 一致 → 取り寄せ省略（rclone lsf 1 回だけ。checkout は呼ばれない）。不一致 → 取り寄せ、20 秒以内に完了すれば fetched: true。
    完了しなければ job_id / status。マーカーが無い／2 個以上（不定）の案件は未知として取り寄せる。Drive が読めない → up_to_date: None でローカル。"""
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "t", "acme", actor="human")
    st.mark_checkin("CASE-1")
    c1 = st.load_case("CASE-1"); rev = c1["rev"]
    calls = []; release = threading.Event()

    def checkout(c, w, case=None, dry=False, progress=None):
        calls.append(case)
        assert release.wait(5)
        return "fake checkout"
    monkeypatch.setattr(sync, "checkout", checkout)
    mcp = srv.create_server(conf, jobs=jobs, fetch_wait_sec=0.3)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            fake_drive.set_rev("CASE-1", rev)
            r = await c.call_tool("open_case", {"case": "CASE-1"})
            d = r.structured_content["drive"]
            assert d["up_to_date"] is True and d["fetched"] is False and d["rev"] == rev and "job_id" not in d
            assert fake_drive.lookups == 1 and fake_drive.listings == 0 and calls == [] and jobs.all() == []
            cache = sync.load_drive_revs_cache(ws)                                                 # 1 案件の照会もキャッシュに反映（fetched_at は全件取得の時刻なので null）
            assert cache == {"revs": {"CASE-1": rev}, "fetched_at": None}
            lc = (await c.call_tool("list_cases", {})).structured_content["result"]                 # 一覧の印はキャッシュとの比較
            assert lc[0]["drive"] == {"state": "synced", "rev": rev, "drive_rev": rev, "checked_in_at": c1["last_checkin_at"], "from": c1["checked_in_from"], "checked": None}
            # rev 不一致・取り寄せが間に合わない → job_id と status、ローカル写し
            fake_drive.set_rev("CASE-1", "newer")
            r = await c.call_tool("open_case", {"case": "CASE-1"})
            d = r.structured_content["drive"]
            assert d["fetched"] is False and d["up_to_date"] is False and d["status"] in ("queued", "running") and d["drive_rev"] == "newer"
            assert "not finished within 0.3s" in d["note"] and "open_case again" in d["note"] and jobs.get(d["job_id"]).active
            r2 = await c.call_tool("open_case", {"case": "CASE-1"})
            assert r2.structured_content["drive"]["job_id"] == d["job_id"] and "already running" in r2.structured_content["drive"]["note"]
            lc = (await c.call_tool("list_cases", {})).structured_content["result"]
            assert lc[0]["drive"]["state"] == "drive_newer" and lc[0]["drive"]["drive_rev"] == "newer"
            release.set(); _wait(jobs, d["job_id"])
            # 間に合う → fetched: true（偽 checkout は rev を変えないので Drive とはまだ違う）
            r = await c.call_tool("open_case", {"case": "CASE-1"})
            d = r.structured_content["drive"]
            assert d["fetched"] is True and d["up_to_date"] is True and jobs.get(d["job_id"]).status == "done" and d["rev"] == rev
            assert calls == ["CASE-1", "CASE-1"]
            # マーカーが無い → 未知として取り寄せる（安全側）。キャッシュのエントリは消える
            fake_drive.markers["CASE-1"] = []
            r = await c.call_tool("open_case", {"case": "CASE-1"})
            assert r.structured_content["drive"]["fetched"] is True and r.structured_content["drive"]["drive_rev"] is None and len(calls) == 3
            assert sync.load_drive_revs_cache(ws)["revs"] == {}
            lc = (await c.call_tool("list_cases", {})).structured_content["result"]
            assert lc[0]["drive"]["state"] == "unknown"
            # マーカーが 2 個以上（不定）→ 取り寄せる。一覧では Drive の方が新しい（ambiguous）
            fake_drive.markers["CASE-1"] = ["a", "b"]
            r = await c.call_tool("open_case", {"case": "CASE-1"})
            assert r.structured_content["drive"]["fetched"] is True and r.structured_content["drive"]["drive_rev"] is None and len(calls) == 4
            lc = (await c.call_tool("list_cases", {})).structured_content["result"]
            assert lc[0]["drive"]["state"] == "drive_newer" and lc[0]["drive"]["ambiguous"] is True and lc[0]["drive"]["drive_rev"] is None
            # Drive が読めない（オフライン）→ 取り寄せずローカル、up_to_date: None
            fake_drive.unavailable = True
            r = await c.call_tool("open_case", {"case": "CASE-1"})
            d = r.structured_content["drive"]
            assert d["up_to_date"] is None and d["fetched"] is False and "drive unavailable" in d["note"] and "job_id" not in d and len(calls) == 4
            assert r.structured_content["available"] is True and r.structured_content["case"]["id"] == "CASE-1"
    run(main)


# ---------- 証拠の型検証（項目 2）・計画の検証（項目 5）: MCP 経由で is_error ----------

def test_evidence_type_validation_via_mcp(conf):
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "t", "acme", actor="human")
    st.new_plan_version("CASE-1", "o", [{"title": "a"}], reason="r", actor="ai")
    mcp = srv.create_server(conf)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            bad = [
                ([{"type": "note", "text": "done, trust me"}], "human only"),
                ([{"type": "commit"}], "requires 'id'"),
                ([{"type": "pr", "repo": "acme-robot"}], "requires 'id'"),
                ([{"type": "file"}], "requires 'path'"),
                ([{"type": "test", "result": "pass"}], "requires 'cmd'"),
                ([{"type": "url"}], "requires 'url'"),
                ([{"type": "screenshot", "path": "x.png"}], "unknown evidence type"),
                ([{"id": "abc1234"}], "objects with a 'type'"),
            ]
            for ev, msg in bad:
                r = await c.call_tool("update_task", {"case": "CASE-1", "task": "T001", "status": "done", "evidence": ev})
                assert r.is_error and msg in r.content[0].text, (ev, r.content)
                assert st.current_plan("CASE-1")["tasks"][0]["status"] == "open"
                r = await c.call_tool("log_event", {"case": "CASE-1", "action": "progress", "note": "x", "evidence": ev})
                assert r.is_error and msg in r.content[0].text, (ev, r.content)
            assert not any(e["action"] == "progress" for e in st.events("CASE-1"))
            ok = [{"type": "commit", "repo": "acme-robot", "id": "abc1234"}, {"type": "pr", "id": 42}, {"type": "file", "path": "a.md"},
                  {"type": "test", "cmd": "pytest", "result": "pass"}, {"type": "url", "url": "https://example.com/x"}]
            r = await c.call_tool("log_event", {"case": "CASE-1", "action": "progress", "note": "x", "evidence": ok})
            assert not r.is_error
            r = await c.call_tool("update_task", {"case": "CASE-1", "task": "T001", "status": "done", "evidence": ok})
            assert not r.is_error and r.structured_content["status"] == "done"
    run(main)


def test_plan_validation_via_mcp(conf):
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "t", "acme", actor="human")
    st.new_plan_version("CASE-1", "o", [{"title": "a"}, {"title": "b"}], reason="r", actor="ai")
    mcp = srv.create_server(conf)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            for tasks, msg in [
                ([{"carried_from": "T002"}, {"carried_from": "T002"}], "T002 carried twice"),
                ([{"owner": "ai"}], "task needs title or carried_from"),
                ([{"title": "x", "owner": "robot"}], "owner must be ai | human"),
                ([{"carried_from": "T001", "owner": "bot"}], "owner must be ai | human"),
            ]:
                r = await c.call_tool("plan", {"case": "CASE-1", "objective": "o", "reason": "r", "tasks": tasks})
                assert r.is_error and msg in r.content[0].text, (tasks, r.content)
            assert st.current_plan("CASE-1")["version"] == 1 and st.progress("CASE-1")["open"] == 2  # 何も変わっていない
    run(main)


def test_open_case_writes_access_log_not_events(conf, mocked_rclone, jobs, fake_drive):
    """open_case を繰り返しても events.jsonl は変わらず（内容も mtime も）、index/access.log（ローカル）に 1 行ずつ増える。"""
    ws = conf.workspaces["acme"]
    fake_drive.set_rev("CASE-1", "on-drive")
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "t", "acme", actor="human")
    ev = ws.cases_dir / "CASE-1" / "events.jsonl"
    before = (ev.read_text(encoding="utf-8"), ev.stat().st_mtime_ns)
    log = ws.index_dir / "access.log"
    mcp = srv.create_server(conf, default_agent="test-agent", jobs=jobs)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            for i in range(3):
                r = await _open(c, jobs, "CASE-1", agent=f"agent-{i}")
                assert not r.is_error and r.structured_content["drive"]["job_id"]
                assert (ev.read_text(encoding="utf-8"), ev.stat().st_mtime_ns) == before
                lines = log.read_text(encoding="utf-8").splitlines()
                assert len(lines) == i + 1 and lines[-1].split("\t")[1:] == ["CASE-1", f"agent-{i}"]
    run(main)
    assert [e["action"] for e in st.events("CASE-1")] == ["opened"]
    assert mocked_rclone.count(("checkout", "acme", "CASE-1")) == 3


# ---------- ジョブ化した checkin / 取り寄せ（docs/mcp-tools.md「ジョブ」） ----------

def test_checkin_returns_job_immediately_and_job_status_follows(conf, monkeypatch, jobs):
    """rclone が長く走っても checkin はすぐ返る（job_id、queued/running）。job_status が running → done（result に従来の結果と
    last_checkin_at）と進む。同じ案件の 2 回目は既存の job_id。mark_checkin と checkin event はジョブ側が完了時に書く。"""
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "t", "acme", actor="human")
    started = threading.Event(); release = threading.Event()

    def slow_checkin(c, w, case=None, dry=False, progress=None):
        started.set()
        progress("Transferred: 1 MiB / 700 MiB, 0%, ETA 10m")
        assert release.wait(5)
        progress("Transferred: 700 MiB / 700 MiB, 100%, ETA 0s")
        st.mark_checkin(case)
        return "fake sync done"
    monkeypatch.setattr(sync, "checkin", slow_checkin)
    mcp = srv.create_server(conf, default_agent="test-agent", jobs=jobs)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            r = await c.call_tool("checkin", {"case": "CASE-1"})
            assert not r.is_error and r.structured_content["status"] in ("queued", "running") and "job_status" in r.structured_content["note"]
            job_id = r.structured_content["job_id"]
            assert started.wait(5)
            r2 = await c.call_tool("checkin", {"case": "CASE-1"})
            assert r2.structured_content["job_id"] == job_id and "already running" in r2.structured_content["note"]
            s = (await c.call_tool("job_status", {"job_id": job_id})).structured_content
            assert s["status"] == "running" and s["kind"] == "checkin" and s["case"] == "CASE-1" and s["workspace"] == "acme"
            assert s["progress"].startswith("Transferred: 1 MiB") and s["result"] is None and s["error"] is None and s["started_at"]
            assert "last_checkin_at" not in st.load_case("CASE-1") and st.events("CASE-1")[-1]["action"] != "checkin"
            release.set()
            _wait(jobs, job_id)
            s = (await c.call_tool("job_status", {"job_id": job_id})).structured_content
            assert s["status"] == "done" and s["finished_at"] and s["progress"].endswith("ETA 0s")
            assert s["result"] == {"ok": True, "rclone": "fake sync done", "last_checkin_at": st.load_case("CASE-1")["last_checkin_at"]}
            ev = st.events("CASE-1")[-1]
            assert ev["action"] == "checkin" and ev["agent"] == "test-agent" and ev["note"] == "fake sync done"
            # 完了後は同じ案件で新しいジョブになる
            release.set()
            r3 = await c.call_tool("checkin", {"case": "CASE-1"})
            assert r3.structured_content["job_id"] != job_id
            _wait(jobs, r3.structured_content["job_id"])
            # 未知の案件・ワークスペースはジョブを作らずに拒否
            r = await c.call_tool("checkin", {"case": "CASE-404"})
            assert r.is_error and "CASE-404" in r.content[0].text
            assert len(jobs.all()) == 2
    run(main)


def test_job_status_failed_and_unknown(conf, monkeypatch, jobs):
    ws = conf.workspaces["acme"]
    CaseStore(ws.cases_dir).create_case("CASE-1", "t", "acme", actor="human")

    def boom(*a, **k):
        raise sync.RcloneError("Failed to sync: quota exceeded")
    monkeypatch.setattr(sync, "checkin", boom)
    mcp = srv.create_server(conf, jobs=jobs)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            r = await c.call_tool("checkin", {"case": "CASE-1"})
            job_id = r.structured_content["job_id"]
            _wait(jobs, job_id)
            s = (await c.call_tool("job_status", {"job_id": job_id})).structured_content
            assert s["status"] == "failed" and s["error"] == "RcloneError: Failed to sync: quota exceeded" and s["result"] is None
            assert "last_checkin_at" not in CaseStore(ws.cases_dir).load_case("CASE-1")
            assert not any(e["action"] == "checkin" for e in CaseStore(ws.cases_dir).events("CASE-1"))
            r = await c.call_tool("job_status", {"job_id": "nope"})
            assert r.is_error and "unknown job 'nope'" in r.content[0].text and "restart" in r.content[0].text
    run(main)


def test_open_case_drive_job_id_and_dedupe(conf, monkeypatch, jobs, fake_drive):
    """取り寄せが待ち時間（fetch_wait_sec）に間に合わなければ open_case は今のローカル内容を返し、drive に job_id / status を入れる。
    取り寄せ中にもう一度開いても新しいジョブは作らない。"""
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "before fetch", "acme", actor="human")
    fake_drive.set_rev("CASE-1", "on-drive")
    started = threading.Event(); release = threading.Event()

    def slow_checkout(c, w, case=None, dry=False, progress=None):
        started.set()
        assert release.wait(5)
        case_json = st.load_case(case); case_json["title"] = "after fetch"; st.save_case(case_json)
        return "fake copy done"
    monkeypatch.setattr(sync, "checkout", slow_checkout)
    mcp = srv.create_server(conf, jobs=jobs, fetch_wait_sec=0.2)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            r = await c.call_tool("open_case", {"case": "CASE-1"})
            d = r.structured_content["drive"]
            assert d["fetched"] is False and d["status"] in ("queued", "running") and "open_case again" in d["note"]
            assert r.structured_content["case"]["title"] == "before fetch"
            assert started.wait(5)
            r2 = await c.call_tool("open_case", {"case": "CASE-1"})
            assert r2.structured_content["drive"]["job_id"] == d["job_id"] and "already running" in r2.structured_content["drive"]["note"]
            release.set()
            _wait(jobs, d["job_id"])
            s = (await c.call_tool("job_status", {"job_id": d["job_id"]})).structured_content
            assert s["status"] == "done" and s["kind"] == "checkout" and s["result"] == "fake copy done"
            r3 = await _open(c, jobs, "CASE-1")
            assert r3.structured_content["case"]["title"] == "after fetch" and r3.structured_content["drive"]["job_id"] != d["job_id"]
            assert r3.structured_content["drive"]["fetched"] is True
    run(main)


# ---- serve: 停止時の graceful shutdown 上限と running ジョブのログ ---------------------------------

def test_serve_limits_graceful_shutdown_and_logs_running_jobs_on_signal(conf, monkeypatch, capsys):
    """serve は uvicorn.Config に timeout_graceful_shutdown=5 を渡し、SIGTERM のハンドラ（handle_exit）で running ジョブを 1 行出してから
    uvicorn の handle_exit に渡す。uvicorn.Server はモックして実際には listen しない。"""
    import signal

    import uvicorn

    created = {}

    class FakeServer:
        def __init__(self, config):
            created["config"] = config
            self.exits = []

        def handle_exit(self, sig, frame):
            self.exits.append(sig)

        def run(self):
            created["ran"] = True

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    watchdogs = []
    monkeypatch.setattr(srv, "start_shutdown_watchdog", lambda jobs, sig, **kw: watchdogs.append((jobs, sig)))
    server = srv.serve(conf, host="127.0.0.1", port=1)
    config = created["config"]
    assert isinstance(config, uvicorn.Config) and created["ran"] is True
    assert config.timeout_graceful_shutdown == srv.GRACEFUL_SHUTDOWN_SEC == 5
    assert config.host == "127.0.0.1" and config.port == 1
    capsys.readouterr()

    jobs = config.app.state.jobs
    release = threading.Event(); started = threading.Event()

    def blocking(progress):
        started.set(); release.wait(5)
        return "ok"
    job, _ = jobs.submit("checkin", "acme", "CASE-123", blocking)
    assert started.wait(5)
    # 何も走っていない案件のジョブは出ない: queued（同じ案件の後続）は running ではないので一覧に含めない
    queued, _ = jobs.submit("checkout", "acme", "CASE-123", lambda progress: "later")
    assert queued.status == "queued"
    try:
        server.handle_exit(signal.SIGTERM, None)
        out = capsys.readouterr().out
        assert server.exits == [signal.SIGTERM]                       # uvicorn 側の停止処理に渡している
        lines = [ln for ln in out.splitlines() if "running job" in ln]
        assert len(lines) == 1, out
        listed = lines[0].split("running job(s): ", 1)[1].split(". jobs are not persisted", 1)[0]
        assert "1 running job(s)" in lines[0] and "next checkin/checkout" in lines[0]
        assert listed.startswith("checkin acme/CASE-123 (") and job.id in listed and queued.id not in listed
    finally:
        release.set()
    assert job.wait(5) and queued.wait(5)
    # running が無ければ何も出さない。ウォッチドッグは最初のシグナルで 1 回だけ起動する
    server.handle_exit(signal.SIGTERM, None)
    assert "running job" not in capsys.readouterr().out and server.exits == [signal.SIGTERM, signal.SIGTERM]
    assert watchdogs == [(jobs, signal.SIGTERM)]


def test_shutdown_watchdog_forces_exit_after_delay():
    """ウォッチドッグ: delay 秒（既定 GRACEFUL_SHUTDOWN_SEC + 2 = 7）待ってからも呼ばれる＝プロセスが残っていれば、running ジョブを
    1 行出して os._exit(0)（モック）を呼ぶ。待っている間は何もしない。デーモンスレッド。"""
    import signal
    assert srv.SHUTDOWN_WATCHDOG_EXTRA_SEC == 2 and srv.GRACEFUL_SHUTDOWN_SEC + srv.SHUTDOWN_WATCHDOG_EXTRA_SEC == 7
    jobs = JobTable()
    release = threading.Event(); started = threading.Event()

    def blocking(progress):
        started.set(); release.wait(5)
    job, _ = jobs.submit("checkout", "acme", "CASE-123", blocking)
    assert started.wait(5)
    exits: list[int] = []; lines: list[str] = []; slept: list[float] = []
    gate = threading.Event()

    def sleep(sec):
        slept.append(sec); assert gate.wait(5)
    t = srv.start_shutdown_watchdog(jobs, signal.SIGTERM, delay=7, exit_fn=exits.append, out=lambda msg, **kw: lines.append(msg), sleep=sleep)
    assert t.daemon and t.name == "kairn-shutdown-watchdog"
    time.sleep(0.05)
    assert exits == [] and lines == [] and slept == [7]           # 待っている間は落とさない
    gate.set(); t.join(5)
    assert exits == [0] and len(lines) == 1
    assert "still running 7s after signal 15" in lines[0] and "forcing exit" in lines[0] and f"checkout acme/CASE-123 (" in lines[0] and job.id in lines[0]
    release.set(); job.wait(5)
    # running が無ければ none と出して落とす。既定の exit_fn は os._exit
    gate2 = threading.Event(); exits2: list[int] = []; lines2: list[str] = []
    t2 = srv.start_shutdown_watchdog(jobs, signal.SIGINT, delay=0, exit_fn=exits2.append, out=lambda msg, **kw: lines2.append(msg), sleep=lambda s: gate2.wait(5))
    gate2.set(); t2.join(5)
    assert exits2 == [0] and "running job(s): none" in lines2[0]
    import inspect, os
    assert inspect.signature(srv.start_shutdown_watchdog).parameters["exit_fn"].default is os._exit


# ---------- set_case_status（案件を閉じる・保留する・再開するのは人の判断。AI は人の発言を instruction に添えて代行する） ----------

def test_set_case_status_transitions_via_mcp(conf):
    """closed / suspended / open の遷移が event {action: status, from, to, note=instruction, actor: ai, agent} を書き、open_case の recent_events に出る。
    同じステータスは changed=false でイベント無し。instruction が空・空白は ToolError。open タスクの件数は open_tasks。未知の案件／ワークスペース・不正な status は ToolError。"""
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "t", "acme", actor="human")
    st.new_plan_version("CASE-1", "o", [{"title": "a"}, {"title": "b"}], reason="r", actor="ai")
    st.set_task_status("CASE-1", "T001", "done", [{"type": "commit", "id": "abc"}], "", actor="ai")
    mcp = srv.create_server(conf, default_agent="test-agent")

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            tool = next(t for t in (await c.list_tools()).tools if t.name == "set_case_status")
            assert "人の判断" in tool.description and "AI の判断で呼ばない" in tool.description and "instruction" in tool.description
            assert set(tool.input_schema["required"]) == {"case", "status", "instruction"}
            n = len(st.events("CASE-1"))
            # instruction が空・空白 → 拒否（何も書かない）
            for bad in ("", "   ", "\n"):
                r = await c.call_tool("set_case_status", {"case": "CASE-1", "status": "closed", "instruction": bad})
                assert r.is_error and "instruction is required" in r.content[0].text and "human decision" in r.content[0].text, bad
            assert st.load_case("CASE-1")["status"] == "open" and len(st.events("CASE-1")) == n
            # open → closed（open タスク T002 が残っていても拒否しない。open_tasks で知らせる）
            r = await c.call_tool("set_case_status", {"case": "CASE-1", "status": "closed", "instruction": "この案件は閉じて"})
            assert not r.is_error, r.content
            out = r.structured_content
            assert out["case"] == "CASE-1" and out["status"] == "closed" and out["previous_status"] == "open" and out["changed"] is True and out["open_tasks"] == 1
            ev = out["event"]
            assert ev["action"] == "status" and ev["from"] == "open" and ev["to"] == "closed" and ev["note"] == "この案件は閉じて"
            assert ev["actor"] == "ai" and ev["agent"] == "test-agent" and ev["case"] == "CASE-1"
            assert st.load_case("CASE-1")["status"] == "closed" and st.events("CASE-1")[-1] == ev
            # 同じステータス → changed=false、イベント無し
            r = await c.call_tool("set_case_status", {"case": "CASE-1", "status": "closed", "instruction": "閉じて"})
            out = r.structured_content
            assert not r.is_error and out["changed"] is False and out["event"] is None and out["status"] == "closed" and out["previous_status"] == "closed"
            assert len(st.events("CASE-1")) == n + 1
            # closed → suspended → open（agent 指定）
            r = await c.call_tool("set_case_status", {"case": "CASE-1", "status": "suspended", "instruction": "しばらく保留で", "agent": "codex"})
            assert r.structured_content["previous_status"] == "closed" and r.structured_content["event"]["agent"] == "codex"
            r = await c.call_tool("set_case_status", {"case": "CASE-1", "status": "open", "instruction": "再開して", "workspace": "acme"})
            assert r.structured_content["status"] == "open" and r.structured_content["previous_status"] == "suspended" and r.structured_content["changed"] is True
            assert [(e["from"], e["to"]) for e in st.events("CASE-1") if e["action"] == "status"] == [("open", "closed"), ("closed", "suspended"), ("suspended", "open")]
            # open_case の直近イベントに actor / note 付きで出る
            r = await c.call_tool("open_case", {"case": "CASE-1"})
            last = r.structured_content["recent_events"][-1]
            assert last["action"] == "status" and last["actor"] == "ai" and last["note"] == "再開して" and last["to"] == "open"
            # 不正な status・未知の案件・未知のワークスペース → ToolError（何も書かない）
            m = len(st.events("CASE-1"))
            r = await c.call_tool("set_case_status", {"case": "CASE-1", "status": "archived", "instruction": "x"})
            assert r.is_error and "invalid case status" in r.content[0].text
            r = await c.call_tool("set_case_status", {"case": "CASE-404", "status": "closed", "instruction": "x"})
            assert r.is_error and "CASE-404" in r.content[0].text
            r = await c.call_tool("set_case_status", {"case": "CASE-1", "status": "closed", "instruction": "x", "workspace": "nowhere"})
            assert r.is_error and "nowhere" in r.content[0].text
            assert len(st.events("CASE-1")) == m and st.load_case("CASE-1")["status"] == "open"
            # open タスクが無ければ open_tasks は 0
            st.set_task_status("CASE-1", "T002", "dropped", [], "", actor="ai")
            r = await c.call_tool("set_case_status", {"case": "CASE-1", "status": "closed", "instruction": "閉じて"})
            assert r.structured_content["open_tasks"] == 0 and r.structured_content["changed"] is True
    run(main)


# ---------- 設定のホットリロード（ConfigHolder）: serve 起動後の config.yaml の変更が再起動なしで MCP に効く ----------

def test_mcp_sees_workspace_added_to_config_after_start(conf, jobs, fake_drive):
    """serve 起動後に別プロセス（kairn ws create / attach）が config.yaml にワークスペースを足す → list_cases(workspace=新 ws) / open_case が
    unknown workspace にならず認識する。変更が無ければ読み直さない。壊れた設定に書き換わっても直前の設定で動き続ける。"""
    from kairn import config as cfg
    conf.save()
    warnings = []
    holder = cfg.ConfigHolder(conf, warn=warnings.append)
    CaseStore(conf.workspaces["acme"].cases_dir).create_case("CASE-1", "t", "acme", actor="human")
    mcp = srv.create_server(holder, jobs=jobs)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            r = await c.call_tool("list_cases", {"workspace": "beta"})
            assert r.is_error and "unknown workspace 'beta'" in r.content[0].text
            assert (await c.call_tool("list_cases", {})).structured_content["result"][0]["case"] == "CASE-1" and holder.reloads == 0
            # 別プロセスがワークスペースを足す（このプロセスの conf オブジェクトには触れない）
            other = cfg.load(conf.path)
            other.workspaces["beta"] = cfg.Workspace(name="beta", description="second")
            other.save(); bump_mtime(conf.path)
            bst = CaseStore(other.workspaces["beta"].cases_dir)
            bst.create_case("CASE-7", "beta の案件", "beta", actor="human")
            r = await c.call_tool("list_cases", {"workspace": "beta"})
            assert not r.is_error, r.content
            assert [x["case"] for x in r.structured_content["result"]] == ["CASE-7"] and holder.reloads == 1
            # 複数ワークスペースになったので workspace 省略は案件 ID から解決する（CASE-7 は beta にだけある）
            r = await c.call_tool("open_case", {"case": "CASE-7"})
            assert not r.is_error, r.content
            oc = r.structured_content
            assert oc["available"] is True and oc["case"]["title"] == "beta の案件" and oc["paths"]["case_dir"] == str(bst.cases_dir / "CASE-7")
            r = await c.call_tool("list_cases", {})
            assert r.is_error and "workspace is required" in r.content[0].text
            assert holder.reloads == 1 and warnings == []
            # 壊れた設定に書き換わっても直前の設定で動く（警告 1 行）
            conf.path.write_text("drive: {remote: [broken\n", encoding="utf-8"); bump_mtime(conf.path)
            r = await c.call_tool("open_case", {"case": "CASE-7"})
            assert not r.is_error and r.structured_content["case"]["id"] == "CASE-7"
            assert holder.reloads == 1 and len(warnings) == 1 and "could not be reloaded" in warnings[0]
            r = await c.call_tool("list_cases", {"workspace": "beta"})
            assert not r.is_error and len(warnings) == 1
    run(main)


def test_mcp_checkin_uses_rclone_flags_set_after_start(conf, monkeypatch, jobs):
    """serve 起動後に `kairn rules set rclone_flags …` した → 次の checkin ジョブの rclone 引数に付く。ジョブは投入時点の設定を使う。
    rclone は subprocess の層（run / Popen）で偽装し、sync.checkin 本体を通す。"""
    import subprocess
    from kairn import config as cfg
    conf.save()
    holder = cfg.ConfigHolder(conf)
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "t", "acme", actor="human")
    cmds = []

    def fake_run(cmd, **kw):   # progress 無し（events.jsonl の copyto、版マーカーの lsf 等）: Drive に無い → 失敗させる（マージは飛ぶ）
        cmds.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 3, "", "object not found")

    class FakePopen:           # progress 付き（転送本体）: 1 行出して成功
        def __init__(self, cmd, **kw):
            cmds.append(list(cmd)); self.returncode = 0
            self.stdout = iter(["Transferred: 1 / 1, 100%\n"])

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(sync.process, "run", fake_run)
    monkeypatch.setattr(sync.process, "popen", FakePopen)
    mcp = srv.create_server(holder, jobs=jobs)

    def transfer_cmds():
        return [c for c in cmds if c[:2] == ["rclone", "sync"]]

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            js = await _checkin(c, jobs, "CASE-1")
            assert js["status"] == "done", js
            assert len(transfer_cmds()) == 1 and "--transfers" in transfer_cmds()[0] and "--checkers" not in transfer_cmds()[0]
            # 別プロセスの `kairn rules set rclone_flags "--checkers 16 --drive-pacer-burst 200"` 相当
            cfg.set_rule(cfg.load(conf.path), "rclone_flags", "--checkers 16 --drive-pacer-burst 200"); bump_mtime(conf.path)
            js = await _checkin(c, jobs, "CASE-1")
            assert js["status"] == "done", js
            cmd = transfer_cmds()[1]
            assert cmd[-4:] == ["--checkers", "16", "--drive-pacer-burst", "200"] and holder.reloads == 1
            # 空に戻す → 付かない
            cfg.set_rule(cfg.load(conf.path), "rclone_flags", ""); bump_mtime(conf.path)
            js = await _checkin(c, jobs, "CASE-1")
            assert js["status"] == "done" and "--checkers" not in transfer_cmds()[2] and holder.reloads == 2
    run(main)


# ---------- 跨ぎ参照（docs/mcp-tools.md「跨ぎ参照」）: scope / from_case / related の展開 ----------

def _seed_two_workspaces(conf2):
    """acme: CASE-1（worklog に alphaword）、beta: CASE-9（worklog に betaword。related に acme/CASE-1）。"""
    a = CaseStore(conf2.workspaces["acme"].cases_dir); b = CaseStore(conf2.workspaces["beta"].cases_dir)
    a.create_case("CASE-1", "acme の案件 alpha", "acme", actor="human", elements={"machine": ["unit-2"]})
    (conf2.workspaces["acme"].cases_dir / "CASE-1" / "worklog.md").write_text("# a\n## Notes\nalphaword only here\n", encoding="utf-8")
    b.create_case("CASE-9", "beta の案件 beta", "beta", actor="human", related=["acme/CASE-1", "CASE-8"])
    (conf2.workspaces["beta"].cases_dir / "CASE-9" / "worklog.md").write_text("# b\n## Notes\nbetaword only here\n## Decision Log\nbetaword again\n", encoding="utf-8")
    return a, b


def test_search_and_find_cases_scope(conf2):
    """scope=auto: 自 ws にヒットがあれば他 ws を見ない、0 件なら全 ws を検索し cross_workspace / searched が付く。scope=workspace: 自 ws のみ。scope=all: 両方。
    自 ws は workspace= → from_case の ws → 登録が 1 つならそれ。決まらなければ scope=all 以外は ToolError。不正な scope は ToolError。"""
    _seed_two_workspaces(conf2)
    mcp = srv.create_server(conf2)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            # auto: 自 ws にヒット → 他 ws は検索しない
            r = await c.call_tool("search", {"query": "alphaword", "workspace": "acme"})
            out = r.structured_content
            assert not r.is_error and out["workspace"] == "acme" and out["scope"] == "auto" and out["searched"] == ["acme"]
            assert [(h["case"], h["workspace"], h["cross_workspace"]) for h in out["results"]] == [("CASE-1", "acme", False)]
            # auto: 自 ws に 0 件 → 全 ws を検索し、他 ws のヒットは cross_workspace
            r = await c.call_tool("search", {"query": "betaword", "workspace": "acme"})
            out = r.structured_content
            assert out["searched"] == ["acme", "beta"] and len(out["results"]) == 2
            assert all(h["case"] == "CASE-9" and h["workspace"] == "beta" and h["cross_workspace"] is True for h in out["results"])
            assert {h["heading"] for h in out["results"]} == {"Notes", "Decision Log"}
            # workspace: 自 ws のみ（0 件でも他を見ない）
            r = await c.call_tool("search", {"query": "betaword", "workspace": "acme", "scope": "workspace"})
            assert r.structured_content["searched"] == ["acme"] and r.structured_content["results"] == []
            # all: 両方（自 ws が先。limit は全体に効く）
            r = await c.call_tool("search", {"query": "only here", "workspace": "acme", "scope": "all"})
            out = r.structured_content
            assert out["searched"] == ["acme", "beta"] and {(h["case"], h["cross_workspace"]) for h in out["results"]} == {("CASE-1", False), ("CASE-9", True)}
            r = await c.call_tool("search", {"query": "only here", "workspace": "beta", "scope": "all", "limit": 1})
            assert r.structured_content["searched"] == ["beta", "acme"] and len(r.structured_content["results"]) == 1
            # find_cases も同じ規則
            r = await c.call_tool("find_cases", {"query": "unit-2", "workspace": "beta"})
            out = r.structured_content
            assert not r.is_error and out["searched"] == ["beta", "acme"] and [(h["case"], h["workspace"], h["cross_workspace"]) for h in out["results"]] == [("CASE-1", "acme", True)]
            assert out["results"][0]["reasons"]
            r = await c.call_tool("find_cases", {"query": "unit-2", "workspace": "acme"})
            assert r.structured_content["searched"] == ["acme"] and r.structured_content["results"][0]["cross_workspace"] is False
            r = await c.call_tool("find_cases", {"query": "unit-2", "workspace": "beta", "scope": "workspace"})
            assert r.structured_content["searched"] == ["beta"] and r.structured_content["results"] == []
            r = await c.call_tool("find_cases", {"query": "案件", "scope": "all"})     # 自 ws が決まらなくても all なら全 ws（workspace: null、cross_workspace は付かない）
            out = r.structured_content
            assert out["workspace"] is None and out["searched"] == ["acme", "beta"] and {h["case"] for h in out["results"]} == {"CASE-1", "CASE-9"}
            assert all(h["cross_workspace"] is False for h in out["results"])
            # 自 ws が決まらない（2 ws・workspace / from_case 無し）→ auto / workspace は ToolError
            for scope in ("auto", "workspace"):
                r = await c.call_tool("search", {"query": "alphaword", "scope": scope})
                assert r.is_error and "workspace is required" in r.content[0].text and "from_case" in r.content[0].text, scope
            # from_case の ws が自 ws になる
            r = await c.call_tool("search", {"query": "betaword", "from_case": "beta/CASE-9"})
            assert r.structured_content["workspace"] == "beta" and r.structured_content["searched"] == ["beta"]
            # 不正な scope
            r = await c.call_tool("search", {"query": "x", "workspace": "acme", "scope": "everything"})
            assert r.is_error and "scope must be one of" in r.content[0].text
    run(main)


def test_from_case_records_cross_reference_once_per_day(conf2, fake_drive):
    """open_case(from_case=<他 ws の案件>) は対象 ws の access.log に cross_from を添えた 1 行と、from 側の案件の events.jsonl に xref を 1 行書く。
    同じ日の 2 回目は access.log には増えるが xref は増えない。同じ ws（from の ws = 対象の ws）なら access.log は従来の 3 列で xref は無い。
    search / find_cases の他 ws ヒットも案件ごとに記録する。from_case の形が不正・未知・ローカルに無い案件は ToolError（何も書かない）。"""
    a, b = _seed_two_workspaces(conf2)
    acme, beta = conf2.workspaces["acme"], conf2.workspaces["beta"]
    mcp = srv.create_server(conf2, default_agent="test-agent")

    def xrefs(st, case):
        return [e for e in st.events(case) if e["action"] == "xref"]

    def log_lines(ws):
        p = ws.index_dir / "access.log"
        return p.read_text(encoding="utf-8").splitlines() if p.exists() else []

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            # 他 ws の案件を from_case 付きで開く（Drive は照会失敗＝取り寄せず、ローカル写し）
            fake_drive.unavailable = True
            r = await c.call_tool("open_case", {"case": "CASE-1", "workspace": "acme", "from_case": "beta/CASE-9", "agent": "claude"})
            assert not r.is_error, r.content
            oc = r.structured_content
            assert oc["available"] is True and oc["case"]["id"] == "CASE-1" and oc["cross_workspace"] is True
            lines = log_lines(acme)
            assert len(lines) == 1 and lines[0].split("\t")[1:] == ["CASE-1", "claude", "cross_from=beta/CASE-9", "tool=open_case"]
            x = xrefs(b, "CASE-9")
            assert len(x) == 1 and x[0]["actor"] == "ai" and x[0]["agent"] == "claude" and x[0]["workspace"] == "acme" and x[0]["case"] == "CASE-1" and x[0]["tool"] == "open_case"
            assert log_lines(beta) == [] and xrefs(a, "CASE-1") == []          # 対象側の events には書かない
            # 同じ日の 2 回目: access.log は増える、xref は増えない
            r = await c.call_tool("open_case", {"case": "CASE-1", "from_case": "beta/CASE-9"})       # workspace 省略でも案件 ID から一意
            assert not r.is_error and r.structured_content["cross_workspace"] is True
            assert len(log_lines(acme)) == 2 and len(xrefs(b, "CASE-9")) == 1
            # 同じ ws からの from_case: 従来の 3 列、xref 無し
            n = len(b.events("CASE-9"))
            r = await c.call_tool("open_case", {"case": "CASE-9", "from_case": "beta/CASE-9"})
            assert not r.is_error and r.structured_content["cross_workspace"] is False
            assert log_lines(beta)[-1].split("\t")[1:] == ["CASE-9", "test-agent"] and len(b.events("CASE-9")) == n
            # search の他 ws ヒット → 案件ごとに 1 回（2 節ヒットでも access.log 1 行・xref 1 行）
            a.create_case("CASE-2", "second", "acme", actor="human")
            (acme.cases_dir / "CASE-2" / "worklog.md").write_text("# c\n## Notes\nalphaword too\n", encoding="utf-8")
            r = await c.call_tool("search", {"query": "alphaword", "from_case": "beta/CASE-9", "agent": "codex"})
            out = r.structured_content
            assert out["workspace"] == "beta" and out["searched"] == ["beta", "acme"] and {h["case"] for h in out["results"]} == {"CASE-1", "CASE-2"}
            lines = log_lines(acme)
            assert len(lines) == 4 and sorted(l.split("\t")[1] for l in lines[2:]) == ["CASE-1", "CASE-2"]
            assert all(l.split("\t")[2:] == ["codex", "cross_from=beta/CASE-9", "tool=search"] for l in lines[2:])
            x = xrefs(b, "CASE-9")
            assert [(e["case"], e["tool"]) for e in x] == [("CASE-1", "open_case"), ("CASE-2", "search")]   # CASE-1 は同じ日に記録済み
            # find_cases: 自 ws（beta）で当たれば他 ws を見ないので記録なし
            r = await c.call_tool("find_cases", {"query": "betaword", "from_case": "beta/CASE-9"})
            assert r.structured_content["searched"] == ["beta"] and len(log_lines(acme)) == 4 and len(xrefs(b, "CASE-9")) == 2
            # from_case 無し・自 ws 内: 何も記録しない
            r = await c.call_tool("search", {"query": "alphaword", "workspace": "beta"})
            assert r.structured_content["searched"] == ["beta", "acme"] and len(log_lines(acme)) == 4 and len(xrefs(b, "CASE-9")) == 2
            # 不正な from_case
            for bad, msg in [("CASE-9", 'must be "<ws>/<case>"'), ("a/b/c", "invalid related reference"), ("gamma/CASE-9", "unknown workspace 'gamma'"),
                             ("beta/CASE-404", "unknown case 'CASE-404'"), ("beta/../x", "invalid related reference")]:
                r = await c.call_tool("open_case", {"case": "CASE-1", "workspace": "acme", "from_case": bad})
                assert r.is_error and msg in r.content[0].text, (bad, r.content)
                r = await c.call_tool("search", {"query": "alphaword", "from_case": bad})
                assert r.is_error and msg in r.content[0].text, (bad, r.content)
            assert len(log_lines(acme)) == 4 and len(xrefs(b, "CASE-9")) == 2
    run(main)


def test_open_case_expands_related_across_workspaces(conf2, fake_drive):
    """related の "<ws>/<case>" は他 ws の案件の title と status だけ展開する（cross_workspace: true）。同 ws の要素も同じ形。実在しない要素は exists: false。
    create_case / apply_card 相当の不正な形（"a/b/c"）は ValueError（store）。"""
    a, b = _seed_two_workspaces(conf2)
    b.set_case_status("CASE-9", "suspended", actor="human")
    a.set_case_status("CASE-1", "closed", actor="human")
    mcp = srv.create_server(conf2)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            fake_drive.unavailable = True
            r = await c.call_tool("open_case", {"case": "CASE-9", "workspace": "beta"})
            assert not r.is_error, r.content
            rel = r.structured_content["related"]
            assert rel == [{"ref": "acme/CASE-1", "workspace": "acme", "case": "CASE-1", "cross_workspace": True, "exists": True, "title": "acme の案件 alpha", "status": "closed"},
                           {"ref": "CASE-8", "workspace": "beta", "case": "CASE-8", "cross_workspace": False, "exists": False, "title": None, "status": None}]
            assert r.structured_content["case"]["related"] == ["acme/CASE-1", "CASE-8"]     # case.json の値はそのまま
            # 展開だけでは跨ぎ参照を記録しない
            assert not (conf2.workspaces["acme"].index_dir / "access.log").exists() and not any(e["action"] == "xref" for e in b.events("CASE-9"))
            # 同 ws の実在する要素、未知の ws を指す要素、形の壊れた要素（手で書かれた case.json）
            case = b.load_case("CASE-9"); case["related"] = ["CASE-9", "gamma/CASE-1", "bad/../x"]; b.save_case(case)
            r = await c.call_tool("open_case", {"case": "CASE-9", "workspace": "beta"})
            rel = r.structured_content["related"]
            assert rel[0] == {"ref": "CASE-9", "workspace": "beta", "case": "CASE-9", "cross_workspace": False, "exists": True, "title": "beta の案件 beta", "status": "suspended"}
            assert rel[1]["workspace"] == "gamma" and rel[1]["cross_workspace"] is True and rel[1]["exists"] is False
            assert rel[2] == {"ref": "bad/../x", "workspace": "beta", "case": None, "cross_workspace": False, "exists": False, "title": None, "status": None}
    run(main)
    with pytest.raises(ValueError):
        a.create_case("CASE-3", "t", "acme", actor="human", related=["a/b/c"])


def test_create_case(conf, fake_drive):
    """create_case: case.json / worklog.md / opened event（actor=ai, agent）を作る。作る前に Drive を 1 回見て、同じ ID の案件フォルダが
    あれば作らない（case.json あり＝別案件、無し＝案件化前のデータ。どちらも ToolError）。Drive を確認できなければ作り、note で知らせる。
    ローカルに既にある案件・空の title・不正な案件 ID は ToolError。作った直後に plan / open_case が使える。"""
    ws = conf.workspaces["acme"]; st = CaseStore(ws.cases_dir)
    mcp = srv.create_server(conf, default_agent="test-agent")

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            r = await c.call_tool("create_case", {"case": "mock-nav-turn", "title": "waypoint で旋回しない", "agent": "fable"})
            assert not r.is_error, r.content
            out = r.structured_content
            assert out["case"] == "mock-nav-turn" and out["workspace"] == "acme" and out["created"] is True and out["related"] == []
            assert out["drive"] == {"available": True, "exists": False, "has_case_json": False, "names": []} and fake_drive.case_lookups == 1
            assert out["paths"]["case_dir"] == str(ws.cases_dir / "mock-nav-turn") and "checkin" in out["note"]
            case = st.load_case("mock-nav-turn")
            assert case["title"] == "waypoint で旋回しない" and case["status"] == "open" and case["workspace"] == "acme" and case["current_plan"] == 0
            assert (ws.cases_dir / "mock-nav-turn" / "worklog.md").read_text(encoding="utf-8").startswith("# waypoint で旋回しない")
            ev = st.events("mock-nav-turn")
            assert len(ev) == 1 and ev[0]["action"] == "opened" and ev[0]["actor"] == "ai" and ev[0]["agent"] == "fable"
            # 作った直後に計画を出せる／開ける
            r = await c.call_tool("plan", {"case": "mock-nav-turn", "objective": "原因を切り分ける", "tasks": [{"title": "ログを見る"}], "reason": "初版"})
            assert not r.is_error, r.content
            fake_drive.set_rev("mock-nav-turn", "x")   # 取り寄せを起こさない（ローカル rev と違うと checkout ジョブが走る）
            fake_drive.unavailable = True
            r = await c.call_tool("open_case", {"case": "mock-nav-turn"})
            assert not r.is_error and r.structured_content["available"] is True and len(r.structured_content["open_tasks"]) == 1
            fake_drive.unavailable = False
            # ローカルに既にある
            r = await c.call_tool("create_case", {"case": "mock-nav-turn", "title": "同じ ID"})
            assert r.is_error and "already exists in workspace" in r.content[0].text and "open_case" in r.content[0].text
            # 空の title・不正な案件 ID
            for args, msg in [({"case": "CASE-NEW", "title": "  "}, "title is required"),
                              ({"case": "../etc", "title": "t"}, "invalid case id")]:
                r = await c.call_tool("create_case", args)
                assert r.is_error and msg in r.content[0].text, (args, r.content)
            assert st.list_case_ids() == ["mock-nav-turn"]
            # Drive に同じ ID の案件がある（case.json あり）→ 作らない
            fake_drive.set_case("CASE-ON-DRIVE")
            r = await c.call_tool("create_case", {"case": "CASE-ON-DRIVE", "title": "t"})
            assert r.is_error and "already exists on the drive" in r.content[0].text and "open_case" in r.content[0].text
            # Drive に案件化前のディレクトリだけある → 作らない（次の checkin で _deleted/ に退避されてしまうため）
            fake_drive.set_case("legacy-dir", ["worklog.md", "data"])
            r = await c.call_tool("create_case", {"case": "legacy-dir", "title": "t"})
            assert r.is_error and "without a case.json" in r.content[0].text and "worklog.md" in r.content[0].text
            assert st.list_case_ids() == ["mock-nav-turn"]
            # Drive を確認できない → 作るが note で知らせる
            fake_drive.unavailable = True
            r = await c.call_tool("create_case", {"case": "CASE-OFFLINE", "title": "オフラインで作る"})
            assert not r.is_error, r.content
            assert r.structured_content["drive"]["available"] is False and "could not be checked" in r.structured_content["note"]
            assert st.load_case("CASE-OFFLINE")["title"] == "オフラインで作る"
    run(main)


def test_create_case_with_related(conf2, fake_drive):
    """create_case の related: 実在を検証して case.json.related に入れ、他 ws の案件なら xref（tool=create_case）を 1 行。
    不正・非実在なら案件を作らない。ワークスペースが決まらなければ ToolError。"""
    a, b = _seed_two_workspaces(conf2)
    acme, beta = conf2.workspaces["acme"], conf2.workspaces["beta"]
    mcp = srv.create_server(conf2, default_agent="test-agent")

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            # 登録 ws が 2 つ＝新しい案件からは決まらない（既存案件と違い ID から引けない）
            r = await c.call_tool("create_case", {"case": "CASE-NEW", "title": "t"})
            assert r.is_error and "workspace is required" in r.content[0].text
            # 不正・非実在の related では作らない
            for bad, msg in [("a/b/c", "invalid related reference"), ("CASE-404", "unknown case 'CASE-404'"),
                             ("gamma/CASE-1", "unknown workspace 'gamma'")]:
                r = await c.call_tool("create_case", {"case": "CASE-NEW", "title": "t", "workspace": "acme", "related": bad})
                assert r.is_error and msg in r.content[0].text, (bad, r.content)
            assert not (acme.cases_dir / "CASE-NEW").exists()
            # 同 ws と他 ws を混ぜる → related に入り、他 ws の分だけ xref
            r = await c.call_tool("create_case", {"case": "CASE-NEW", "title": "新しい案件", "workspace": "acme",
                                                  "related": ["CASE-1", "beta/CASE-9"], "agent": "claude"})
            assert not r.is_error, r.content
            assert r.structured_content["related"] == ["CASE-1", "beta/CASE-9"]
            assert a.load_case("CASE-NEW")["related"] == ["CASE-1", "beta/CASE-9"]
            x = [e for e in a.events("CASE-NEW") if e["action"] == "xref"]
            assert len(x) == 1 and x[0]["workspace"] == "beta" and x[0]["case"] == "CASE-9" and x[0]["tool"] == "create_case" and x[0]["agent"] == "claude"
            assert [e["action"] for e in a.events("CASE-NEW")] == ["opened", "xref"]
            assert not (beta.index_dir / "access.log").exists()          # 閲覧ではない
            assert [e for e in b.events("CASE-9") if e["action"] == "xref"] == []   # 相手側には書かない
    run(main)


def test_link_case_appends_related_and_records_xref(conf2, fake_drive):
    """link_case: 同 ws / 他 ws の案件を related に重複なく追記し、event {action: related, added} を 1 行。他 ws を足したときは xref（tool=link_case）も 1 行。
    全部含まれていれば changed=false で何も書かない。不正な形・存在しない案件・未知の ws は ToolError（1 つでも不正なら何も書かない）。
    open_case の related の展開に反映される。access.log には書かない。"""
    a, b = _seed_two_workspaces(conf2)
    acme, beta = conf2.workspaces["acme"], conf2.workspaces["beta"]
    a.create_case("CASE-2", "second", "acme", actor="human")
    mcp = srv.create_server(conf2, default_agent="test-agent")

    def by_action(st, case, action):
        return [e for e in st.events(case) if e["action"] == action]

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            # 同 ws（文字列 1 つ）
            r = await c.call_tool("link_case", {"case": "CASE-1", "related": "CASE-2", "note": "同種の症状", "agent": "claude"})
            assert not r.is_error, r.content
            assert r.structured_content == {"case": "CASE-1", "related": ["CASE-2"], "added": ["CASE-2"], "changed": True}
            assert a.load_case("CASE-1")["related"] == ["CASE-2"]
            ev = by_action(a, "CASE-1", "related")
            assert len(ev) == 1 and ev[0]["actor"] == "ai" and ev[0]["agent"] == "claude" and ev[0]["added"] == ["CASE-2"] and ev[0]["note"] == "同種の症状"
            assert by_action(a, "CASE-1", "xref") == []                       # 同 ws は xref 無し
            # 他 ws（リスト。既存の CASE-2 は保持、リスト内の重複は 1 回）→ xref（tool=link_case）
            r = await c.call_tool("link_case", {"case": "CASE-1", "related": ["beta/CASE-9", "CASE-2", "beta/CASE-9"]})
            assert not r.is_error, r.content
            assert r.structured_content == {"case": "CASE-1", "related": ["CASE-2", "beta/CASE-9"], "added": ["beta/CASE-9"], "changed": True}
            assert a.load_case("CASE-1")["related"] == ["CASE-2", "beta/CASE-9"]
            x = by_action(a, "CASE-1", "xref")
            assert len(x) == 1 and x[0]["workspace"] == "beta" and x[0]["case"] == "CASE-9" and x[0]["tool"] == "link_case" and x[0]["agent"] == "test-agent"
            assert not (beta.index_dir / "access.log").exists() and not (acme.index_dir / "access.log").exists()   # 閲覧ではない
            assert by_action(b, "CASE-9", "related") == [] and by_action(b, "CASE-9", "xref") == []               # 相手側には書かない
            # 全部含まれている → changed=false、event 無し
            n = len(a.events("CASE-1"))
            r = await c.call_tool("link_case", {"case": "CASE-1", "related": ["CASE-2", "beta/CASE-9"]})
            assert not r.is_error and r.structured_content == {"case": "CASE-1", "related": ["CASE-2", "beta/CASE-9"], "added": [], "changed": False}
            assert len(a.events("CASE-1")) == n
            # open_case の related の展開に反映される
            fake_drive.unavailable = True
            r = await c.call_tool("open_case", {"case": "CASE-1", "workspace": "acme"})
            assert not r.is_error, r.content
            rel = r.structured_content["related"]
            assert [(x["ref"], x["cross_workspace"], x["exists"], x["title"]) for x in rel] == [("CASE-2", False, True, "second"), ("beta/CASE-9", True, True, "beta の案件 beta")]
            # 不正な形・存在しない案件・未知の ws・空 → ToolError、何も書かない
            n = len(a.events("CASE-1"))
            for bad, msg in [("a/b/c", "invalid related reference"), ("../x", "invalid related reference"), ("CASE-404", "unknown case 'CASE-404'"),
                             ("beta/CASE-404", "unknown case 'CASE-404'"), ("gamma/CASE-9", "unknown workspace 'gamma'"),
                             (["CASE-2", "beta/CASE-404"], "unknown case 'CASE-404'"), ([], "related is required")]:
                r = await c.call_tool("link_case", {"case": "CASE-1", "related": bad})
                assert r.is_error and msg in r.content[0].text, (bad, r.content)
            assert a.load_case("CASE-1")["related"] == ["CASE-2", "beta/CASE-9"] and len(a.events("CASE-1")) == n
            # 未知の案件（対象側）
            r = await c.call_tool("link_case", {"case": "CASE-404", "workspace": "acme", "related": "CASE-2"})
            assert r.is_error and "unknown case" in r.content[0].text
    run(main)
