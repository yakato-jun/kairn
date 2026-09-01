"""MCP サーバー（mcp 2.x）: in-process の Client で 9 ツールを呼ぶ。rclone は monkeypatch。"""
from __future__ import annotations

import anyio
import pytest
from mcp.client import Client

from kairn import server as srv
from kairn import sync
from kairn.store import CaseStore

TOOLS = {"open_case", "list_cases", "plan", "update_task", "log_event", "search", "find_cases", "checkin", "drive_index"}


@pytest.fixture
def mocked_rclone(monkeypatch, conf):
    calls = []
    monkeypatch.setattr(sync, "checkout", lambda c, ws, case=None, dry=False: calls.append(("checkout", ws.name, case)) or "fake checkout")
    monkeypatch.setattr(sync, "checkin", lambda c, ws, case=None, dry=False: calls.append(("checkin", ws.name, case)) or "fake checkin")
    return calls


def run(coro_fn):
    return anyio.run(coro_fn)


def test_tools_listed_with_instructions(conf):
    mcp = srv.create_server(conf)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            tools = await c.list_tools()
            assert {t.name for t in tools.tools} == TOOLS
            assert c.instructions == srv.INSTRUCTIONS
            assert c.server_info.name == "kairn"
    run(main)


def test_full_flow(conf, mocked_rclone):
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-123", "起動時に driver が初期化されない", "acme", actor="human", elements={"machine": ["unit-2"]}, related=["CASE-100"])
    (ws.cases_dir / "CASE-123" / "worklog.md").write_text("# t\n## Objective\n起動時に widget driver の init が終わらない\n## Notes\nUART 460800 で送信量が超過する\n", encoding="utf-8")
    mcp = srv.create_server(conf, default_agent="test-agent")

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
            # human sendback via store (UI と同じ書き込み) -> open_case の human_feedback に出る
            st.append_event("CASE-123", {"actor": "human", "action": "sendback", "task": "T001", "note": "unit-6 でも確認"})
            r = await c.call_tool("open_case", {"case": "CASE-123"})
            oc = r.structured_content
            assert not r.is_error and oc["case"]["id"] == "CASE-123" and oc["plan"]["version"] == 1
            assert [t["id"] for t in oc["open_tasks"]] == ["T002"]
            assert oc["human_feedback"][-1]["note"] == "unit-6 でも確認" and oc["related"] == ["CASE-100"]
            assert "460800" in oc["worklog_tail"] and oc["drive"]["fetched"] is True
            assert st.events("CASE-123")[-1]["action"] == "checkout"
            # re-plan without carrying T002 -> superseded
            r = await c.call_tool("plan", {"case": "CASE-123", "objective": "unit-6 も", "reason": "sendback", "tasks": [{"title": "unit-6 で確認"}]})
            assert r.structured_content["superseded"] == ["T002"]
            # checkin / drive_index
            r = await c.call_tool("checkin", {"case": "CASE-123"})
            assert not r.is_error and r.structured_content["ok"] and st.events("CASE-123")[-1]["action"] == "checkin"
            (ws.index_dir / "drive-index.txt").write_text("acme/cases/CASE-123/0901_1200_run.bag.zst\t123456\t2026-09-01T00:00:00\nacme/cases/other.txt\t1\t\n")
            r = await c.call_tool("drive_index", {"pattern": r"CASE-123.*\.zst$"})
            assert r.structured_content["result"] == [{"path": "acme/cases/CASE-123/0901_1200_run.bag.zst", "size": "123456", "mtime": "2026-09-01T00:00:00"}]
            # unknown case / workspace
            r = await c.call_tool("open_case", {"case": "CASE-404"})
            assert r.is_error and "CASE-404" in r.content[0].text
            r = await c.call_tool("list_cases", {"workspace": "nowhere"})
            assert r.is_error and "nowhere" in r.content[0].text
    run(main)
    assert ("checkout", "acme", "CASE-123") in mocked_rclone and ("checkin", "acme", "CASE-123") in mocked_rclone


def test_open_case_continues_when_drive_fails(conf, monkeypatch):
    ws = conf.workspaces["acme"]
    CaseStore(ws.cases_dir).create_case("CASE-1", "t", "acme", actor="human")

    def boom(*a, **k):
        raise sync.RcloneError("remote unreachable")
    monkeypatch.setattr(sync, "checkout", boom)
    mcp = srv.create_server(conf)

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            r = await c.call_tool("open_case", {"case": "CASE-1"})
            assert not r.is_error and r.structured_content["drive"] == {"fetched": False, "error": "remote unreachable", "note": "continuing with local copy"}
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
