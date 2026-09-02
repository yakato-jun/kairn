"""extract の入口（MCP / CLI / UI）のテスト。subprocess はモック（実 CLI は起動しない）。"""
from __future__ import annotations

import json

import anyio
import pytest
from mcp.client import Client

from kairn import server as srv
from kairn.extract import adapters

from test_extract import FakeRun, _claude_stdout, _seed, good_card


# ---------- MCP ----------

def test_mcp_extract_card_returns_failure_as_result(conf, monkeypatch):
    st = _seed(conf)
    mcp = srv.create_server(conf)
    results = {}

    async def main():
        async with Client(mcp, raise_exceptions=True) as c:
            monkeypatch.setattr(adapters.subprocess, "run", FakeRun(stdout="boom", returncode=1))
            r = await c.call_tool("extract_card", {"case": "CASE-123"})
            assert not r.is_error
            results["fail"] = r.structured_content
            monkeypatch.setattr(adapters.subprocess, "run", FakeRun(stdout=_claude_stdout(good_card())))
            r = await c.call_tool("extract_card", {"case": "CASE-123", "workspace": "acme"})
            assert not r.is_error
            results["ok"] = r.structured_content
            r = await c.call_tool("extract_card", {"case": "CASE-404"})
            assert r.is_error and "CASE-404" in r.content[0].text
    anyio.run(main)
    assert results["fail"]["ok"] is False and results["fail"]["error"] == "exit code 1" and results["fail"]["agent"] == "claude"
    assert results["ok"]["ok"] is True and results["ok"]["card"] == {**good_card(), "related_unknown": []}
    assert st.load_case("CASE-123")["title"] == "起動時に driver が初期化されない"  # MCP は書かない
    assert [e["action"] for e in st.events("CASE-123")[-2:]] == ["extract", "extract"]


# ---------- CLI ----------

def test_cli_extract(conf, monkeypatch, capsys):
    from kairn import cli, config as cfg
    _seed(conf)
    monkeypatch.setattr(cfg, "load", lambda path=None: conf)
    monkeypatch.setattr(cfg, "assert_data_not_tracked", lambda data_root=None: None)
    monkeypatch.setattr(adapters.subprocess, "run", FakeRun(stdout=_claude_stdout(good_card())))
    monkeypatch.setattr("sys.argv", ["kairn", "extract", "CASE-123", "--ws", "acme", "--json"])
    cli.main()
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["card"] == {**good_card(), "related_unknown": []} and out["agent"] == "claude"
    monkeypatch.setattr(adapters.subprocess, "run", FakeRun(stdout="x", returncode=1))
    monkeypatch.setattr("sys.argv", ["kairn", "extract", "CASE-123", "--ws", "acme", "--agent", "antigravity"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 1
    cap = capsys.readouterr()
    assert "agent=antigravity ok=False" in cap.out and "exit code 1" in cap.err
    monkeypatch.setattr("sys.argv", ["kairn", "extract", "CASE-404", "--ws", "acme"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert "CASE-404" in str(e.value)


def test_cli_close_suspend_reopen(conf, monkeypatch, capsys):
    """kairn close | suspend | reopen <ws> <case> [--note]: actor=human の status event（from / to / note）を書き、結果を 1 行出す。
    既にそのステータスなら "already …" で終了コード 0（event 無し）。未知の案件／ワークスペースは SystemExit。"""
    from kairn import cli, config as cfg
    st = _seed(conf)
    st.new_plan_version("CASE-123", "o", [{"title": "a"}], reason="r", actor="ai")
    monkeypatch.setattr(cfg, "load", lambda path=None: conf)
    monkeypatch.setattr(cfg, "assert_data_not_tracked", lambda data_root=None: None)

    def run(*args):
        monkeypatch.setattr("sys.argv", ["kairn", *args])
        cli.main()
        return capsys.readouterr().out.strip()

    n = len(st.events("CASE-123"))
    assert run("close", "acme", "CASE-123", "--note", "対応完了") == "CASE-123: open -> closed (1 open task(s) remain)"
    ev = st.events("CASE-123")[-1]
    assert st.load_case("CASE-123")["status"] == "closed" and len(st.events("CASE-123")) == n + 1
    assert ev["action"] == "status" and ev["actor"] == "human" and ev["from"] == "open" and ev["to"] == "closed" and ev["note"] == "対応完了"
    assert run("close", "acme", "CASE-123") == "CASE-123 already closed" and len(st.events("CASE-123")) == n + 1
    assert run("suspend", "acme", "CASE-123") == "CASE-123: closed -> suspended"
    assert st.events("CASE-123")[-1]["note"] == "" and st.load_case("CASE-123")["status"] == "suspended"
    assert run("suspend", "acme", "CASE-123", "--note", "x") == "CASE-123 already suspended"
    assert run("reopen", "acme", "CASE-123", "--note", "再開") == "CASE-123: suspended -> open"
    assert st.load_case("CASE-123")["status"] == "open" and st.events("CASE-123")[-1]["note"] == "再開"
    assert run("reopen", "acme", "CASE-123") == "CASE-123 already open"
    st.set_task_status("CASE-123", "T001", "dropped", [], "", actor="ai")
    assert run("close", "acme", "CASE-123") == "CASE-123: open -> closed"      # open タスクが無ければ件数を出さない
    assert [(e["from"], e["to"]) for e in st.events("CASE-123") if e["action"] == "status"] == [("open", "closed"), ("closed", "suspended"), ("suspended", "open"), ("open", "closed")]
    for args, msg in [(("close", "acme", "CASE-404"), "CASE-404"), (("close", "nowhere", "CASE-123"), "nowhere"), (("reopen", "acme", "../x"), "invalid case id")]:
        monkeypatch.setattr("sys.argv", ["kairn", *args])
        with pytest.raises(SystemExit) as e:
            cli.main()
        assert msg in str(e.value), args
