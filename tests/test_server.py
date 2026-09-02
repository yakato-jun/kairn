"""MCP サーバー（mcp 2.x）: in-process の Client で 11 ツールを呼ぶ。rclone は monkeypatch、Drive の manifest.json はメモリ内の偽物（drive_manifest）。
checkin と open_case の取り寄せはジョブ（スレッド）なので、結果を見る前に job.wait() で完了を待つ（_checkin / _open）。
open_case は manifest の rev がローカルと違うときだけ取り寄せる: 取り寄せを起こしたいテストは drive_manifest.set_rev(case, "…") で rev をずらす。"""
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

TOOLS = {"open_case", "list_cases", "plan", "update_task", "log_event", "search", "find_cases", "checkin", "drive_index", "extract_card", "job_status"}


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


def test_full_flow(conf, mocked_rclone, jobs, drive_manifest):
    ws = conf.workspaces["acme"]
    drive_manifest.set_rev("CASE-123", "on-drive")   # ローカル（rev 無し）と違う → open_case は取り寄せる
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
            hits = r.structured_content["result"]
            assert not r.is_error and hits[0]["case"] == "CASE-123" and hits[0]["heading"] == "Notes"
            r = await c.call_tool("search", {"query": "送信量（超過）", "cases": ["CASE-123"]})
            assert not r.is_error and r.structured_content["result"]
            r = await c.call_tool("find_cases", {"query": "unit-2 driver init"})
            assert not r.is_error and r.structured_content["result"][0]["case"] == "CASE-123"
            # list_cases
            r = await c.call_tool("list_cases", {})
            lc = r.structured_content["result"]
            assert lc[0]["case"] == "CASE-123" and lc[0]["progress"] == {"total": 2, "done": 1, "open": 1, "plan": 1}
            assert lc[0]["drive"]["state"] == "unknown" and lc[0]["drive"]["checked"] is None   # manifest 未取得
            # human sendback via store (UI と同じ書き込み) -> open_case の human_feedback に出る
            st.append_event("CASE-123", {"actor": "human", "action": "sendback", "task": "T001", "note": "unit-6 でも確認"})
            r = await _open(c, jobs, "CASE-123")
            oc = r.structured_content
            assert not r.is_error and oc["case"]["id"] == "CASE-123" and oc["plan"]["version"] == 1
            assert [t["id"] for t in oc["open_tasks"]] == ["T002"]
            assert oc["human_feedback"][-1]["note"] == "unit-6 でも確認" and oc["related"] == ["CASE-100"]
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


def test_open_case_continues_when_drive_fails(conf, monkeypatch, jobs, drive_manifest):
    """rclone の失敗は open_case を止めず（ローカル写しを返す）、取り寄せジョブの failed / error に残る（drive にも status / error）。"""
    ws = conf.workspaces["acme"]
    CaseStore(ws.cases_dir).create_case("CASE-1", "t", "acme", actor="human")
    drive_manifest.set_rev("CASE-1", "on-drive")

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


def test_search_survives_broken_symlink(conf, monkeypatch):
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
            assert r.structured_content["result"][0]["case"] == "CASE-1"
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


def test_open_case_fetches_in_background_then_reads(conf, monkeypatch, jobs, drive_manifest):
    """Drive にしか無い案件: 1 回目の open_case は取り寄せジョブを起動し（待たない）、エラーではなく通常の結果
    {available: false, status: fetching, job_id, case: null, note} を返す。manifest が無くても（未作成）ローカルに無い案件は取り寄せる。
    ジョブ完了後の 2 回目は取り寄せ後のディスクを反映する（available: true。manifest の rev と違うので再度取り寄せ、fetched: true）。"""
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
            drive_manifest.set_rev("CASE-9", "d9")
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
                               ("checkin", {}), ("extract_card", {})]:
                r = await c.call_tool(tool, {"case": "../x", **args})
                assert r.is_error and "invalid case id" in r.content[0].text, (tool, r.content)
    run(main)


@pytest.fixture
def inline_jobs(monkeypatch):
    """ジョブのスレッドを起動せず submit の中で同期実行する（ジョブが open_case の読み取りより先に終わる状況を決定的に作る）。"""
    monkeypatch.setattr(JobTable, "_start", lambda self, job, fn: self._run(job, fn))


def test_open_case_unknown_case_when_fetch_finished_without_it(conf, mocked_rclone, jobs, inline_jobs, drive_manifest):
    """取り寄せジョブが done でも案件が無い（Drive にも無い）なら従来どおり unknown case のエラー。"""
    mcp = srv.create_server(conf, jobs=jobs)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            r = await c.call_tool("open_case", {"case": "CASE-404"})
            assert r.is_error and "CASE-404" in r.content[0].text and "fetched" in r.content[0].text and "job_id" in r.content[0].text
    run(main)
    assert jobs.latest("checkout", "acme", "CASE-404").status == "done"
    assert ("checkout", "acme", "CASE-404") in mocked_rclone  # 取り寄せは試みた（ジョブ）


def test_open_case_reports_failed_fetch_and_retries(conf, monkeypatch, jobs, drive_manifest):
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


def test_open_case_reports_failed_fetch_when_job_fails_immediately(conf, monkeypatch, jobs, inline_jobs, drive_manifest):
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


def test_open_case_skips_checkout_when_local_changes_newer_than_checkin(conf, monkeypatch, jobs, drive_manifest):
    """rclone は _run の層で偽装し、sync.checkout / sync.checkin 本体（版マーカー・manifest 更新を含む）を通す。
    checkin 直後（rev 一致）は取り寄せない。manifest の rev が違えば取り寄せる。未 checkin のローカル変更があれば rev が違っても skip。"""
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
            # last_checkin_at 未記録・manifest 未作成 → 取り寄せず、ローカル写し（up_to_date: None）
            r = await _open(c, jobs, "CASE-1")
            d = r.structured_content["drive"]
            assert d["up_to_date"] is None and "manifest unavailable" in d["note"] and "job_id" not in d and mocked_rclone == []
            # manifest に別の rev → 取り寄せ（ジョブ完了まで待って fetched: true）
            drive_manifest.set_rev("CASE-1", "d1")
            r = await _open(c, jobs, "CASE-1")
            d = r.structured_content["drive"]
            assert d["fetched"] is True and d["up_to_date"] is True and d["job_id"] and d["drive_rev"] == "d1" and "skipped" not in d
            assert mocked_rclone.count(("checkout", "acme", "CASE-1")) == 1
            # checkin → rev / last_checkin_at が記録され manifest も同じ rev になる。直後の open_case は取り寄せない（up_to_date）
            js = await _checkin(c, jobs, "CASE-1")
            assert js["status"] == "done" and js["result"]["last_checkin_at"]
            c1 = st.load_case("CASE-1")
            assert c1["last_checkin_at"] == js["result"]["last_checkin_at"] and drive_manifest.rev("CASE-1") == c1["rev"]
            r = await _open(c, jobs, "CASE-1")
            d = r.structured_content["drive"]
            assert d == {"fetched": False, "up_to_date": True, "checked": d["checked"], "rev": c1["rev"]} and d["checked"]
            assert mocked_rclone.count(("checkout", "acme", "CASE-1")) == 1
            # 別環境が checkin した（manifest の rev が変わった）→ 取り寄せる
            drive_manifest.set_rev("CASE-1", "from-another-host")
            r = await _open(c, jobs, "CASE-1")
            assert r.structured_content["drive"]["fetched"] is True and mocked_rclone.count(("checkout", "acme", "CASE-1")) == 2
            # checkin より新しいローカル変更（worklog.md の mtime を進める）→ rev が違っても checkout を skip（ジョブも作らず manifest も見ない）
            t = time.time() + 30
            os.utime(wl, (t, t))
            n = drive_manifest.fetches
            r = await _open(c, jobs, "CASE-1")
            d = r.structured_content["drive"]
            assert d["skipped"] == "local changes newer than last checkin" and d["fetched"] is False and "worklog.md" in d["files"] and "job_id" not in d
            assert mocked_rclone.count(("checkout", "acme", "CASE-1")) == 2 and drive_manifest.fetches == n  # 呼ばれていない
            assert st.events("CASE-1")[-1]["action"] == "checkin"             # open_case は event を書かない
            # もう一度 checkin すれば skip は解ける（偽装した未来の mtime は現在に戻す）。rev が一致するので取り寄せない
            os.utime(wl, None)
            await _checkin(c, jobs, "CASE-1")
            r = await _open(c, jobs, "CASE-1")
            assert r.structured_content["drive"]["up_to_date"] is True and "job_id" not in r.structured_content["drive"]
            assert mocked_rclone.count(("checkout", "acme", "CASE-1")) == 2
    run(main)


def test_open_case_repeated_after_checkin_does_not_block_next_checkout(conf, monkeypatch, jobs, drive_manifest):
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
                drive_manifest.set_rev("CASE-1", f"other-{i}")      # 取り寄せの理由を作る（rev が違う）
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
            drive_manifest.set_rev("CASE-1", "other-3")
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


def test_open_case_manifest_decides_fetch(conf, monkeypatch, jobs, drive_manifest):
    """manifest の rev 一致 → 取り寄せ省略（rclone cat 1 回だけ。checkout は呼ばれない）。不一致 → 取り寄せ、20 秒以内に完了すれば fetched: true。
    完了しなければ job_id / status。manifest 取得失敗 → up_to_date: None でローカル。エントリが無い案件は未知として取り寄せる。"""
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "t", "acme", actor="human")
    st.mark_checkin("CASE-1")
    rev = st.load_case("CASE-1")["rev"]
    calls = []; release = threading.Event()

    def checkout(c, w, case=None, dry=False, progress=None):
        calls.append(case)
        assert release.wait(5)
        return "fake checkout"
    monkeypatch.setattr(sync, "checkout", checkout)
    mcp = srv.create_server(conf, jobs=jobs, fetch_wait_sec=0.3)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            drive_manifest.data = {"cases": {"CASE-1": {"rev": rev, "checked_in_at": "2026-09-01T00:00:00+09:00", "from": "host-a"}}}
            r = await c.call_tool("open_case", {"case": "CASE-1"})
            d = r.structured_content["drive"]
            assert d["up_to_date"] is True and d["fetched"] is False and d["rev"] == rev and "job_id" not in d
            assert drive_manifest.fetches == 1 and calls == [] and jobs.all() == []
            cache = sync.load_manifest_cache(ws)
            assert cache["cases"]["CASE-1"]["rev"] == rev and cache["fetched_at"]
            lc = (await c.call_tool("list_cases", {})).structured_content["result"]                 # 一覧の印はキャッシュとの比較
            assert lc[0]["drive"] == {"state": "synced", "rev": rev, "drive_rev": rev, "checked_in_at": "2026-09-01T00:00:00+09:00", "from": "host-a", "checked": cache["fetched_at"]}
            # rev 不一致・取り寄せが間に合わない → job_id と status、ローカル写し
            drive_manifest.set_rev("CASE-1", "newer")
            r = await c.call_tool("open_case", {"case": "CASE-1"})
            d = r.structured_content["drive"]
            assert d["fetched"] is False and d["up_to_date"] is False and d["status"] in ("queued", "running") and d["drive_rev"] == "newer"
            assert "not finished within 0.3s" in d["note"] and "open_case again" in d["note"] and jobs.get(d["job_id"]).active
            r2 = await c.call_tool("open_case", {"case": "CASE-1"})
            assert r2.structured_content["drive"]["job_id"] == d["job_id"] and "already running" in r2.structured_content["drive"]["note"]
            release.set(); _wait(jobs, d["job_id"])
            # 間に合う → fetched: true（案件は rev が違うままなので再度取り寄せる）
            r = await c.call_tool("open_case", {"case": "CASE-1"})
            d = r.structured_content["drive"]
            assert d["fetched"] is True and d["up_to_date"] is True and jobs.get(d["job_id"]).status == "done" and d["rev"] == rev
            assert calls == ["CASE-1", "CASE-1"]
            lc = (await c.call_tool("list_cases", {})).structured_content["result"]
            assert lc[0]["drive"]["state"] == "drive_newer" and lc[0]["drive"]["drive_rev"] == "newer"
            # manifest にエントリが無い → 未知として取り寄せる（安全側）
            drive_manifest.data = {"cases": {}}
            r = await c.call_tool("open_case", {"case": "CASE-1"})
            assert r.structured_content["drive"]["fetched"] is True and r.structured_content["drive"]["drive_rev"] is None and len(calls) == 3
            # manifest が取れない（オフライン）→ 取り寄せずローカル、up_to_date: None
            drive_manifest.unavailable = True
            r = await c.call_tool("open_case", {"case": "CASE-1"})
            d = r.structured_content["drive"]
            assert d["up_to_date"] is None and d["fetched"] is False and "manifest unavailable" in d["note"] and "job_id" not in d and len(calls) == 3
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


def test_open_case_writes_access_log_not_events(conf, mocked_rclone, jobs, drive_manifest):
    """open_case を繰り返しても events.jsonl は変わらず（内容も mtime も）、index/access.log（ローカル）に 1 行ずつ増える。"""
    ws = conf.workspaces["acme"]
    drive_manifest.set_rev("CASE-1", "on-drive")
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


def test_open_case_drive_job_id_and_dedupe(conf, monkeypatch, jobs, drive_manifest):
    """取り寄せが待ち時間（fetch_wait_sec）に間に合わなければ open_case は今のローカル内容を返し、drive に job_id / status を入れる。
    取り寄せ中にもう一度開いても新しいジョブは作らない。"""
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "before fetch", "acme", actor="human")
    drive_manifest.set_rev("CASE-1", "on-drive")
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
