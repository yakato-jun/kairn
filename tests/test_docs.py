"""docs とコードの整合: README に `kairn --help` の全サブコマンドが載っている、docs/mcp-tools.md の表のツール名と必須引数が server.py と一致する。"""
from __future__ import annotations

import re

import anyio
from mcp.client import Client

from kairn import config as cfg, server as srv

README = (cfg.ROOT / "README.md").read_text(encoding="utf-8")
MCP_DOC = (cfg.ROOT / "docs" / "mcp-tools.md").read_text(encoding="utf-8")


def test_readme_lists_every_cli_subcommand():
    from kairn import cli
    names = _subcommand_names(cli)  # cli.main() の argparse からサブパーサ名を取る（parse_args を差し替えて実行しない）
    assert len(names) >= 17
    missing = [n for n in names if not re.search(rf"`kairn {re.escape(n)}\b", README) and not re.search(rf"^kairn {re.escape(n)}\b", README, re.M)]
    assert not missing, f"README に載っていないサブコマンド: {missing}"


def _subcommand_names(cli) -> list[str]:
    captured = {}
    import argparse
    orig = argparse.ArgumentParser.parse_args

    def fake_parse(self, *a, **k):
        for act in self._actions:
            if isinstance(act, argparse._SubParsersAction):
                captured["names"] = list(act.choices)
        raise SystemExit(0)
    argparse.ArgumentParser.parse_args = fake_parse
    try:
        try:
            cli.main()
        except SystemExit:
            pass
    finally:
        argparse.ArgumentParser.parse_args = orig
    return captured["names"]


def _doc_tools() -> dict[str, set[str]]:
    """docs/mcp-tools.md の表 `| \\`name(a, b?, c=1)\\` |` から {name: 必須引数の集合}。`?` と `=既定値` 付きは任意。"""
    out = {}
    for m in re.finditer(r"^\| `(\w+)\(([^)]*)\)`", MCP_DOC, re.M):
        name, args = m.group(1), m.group(2)
        req = set()
        for a in (x.strip() for x in args.split(",") if x.strip()):
            if a.endswith("?") or "=" in a:
                continue
            req.add(a.rstrip("[]"))
        out[name] = req
    return out


def test_mcp_tools_doc_matches_server(conf):
    doc = _doc_tools()
    got = {}

    async def main():
        async with Client(srv.create_server(conf)) as c:
            for t in (await c.list_tools()).tools:
                got[t.name] = set(t.input_schema.get("required", []))
    anyio.run(main)
    assert set(doc) == set(got), f"doc={sorted(doc)} server={sorted(got)}"
    assert len(got) == 10
    for name in got:
        assert doc[name] == got[name], f"{name}: doc requires {sorted(doc[name])}, server requires {sorted(got[name])}"
    assert "ツールは 10 個" in MCP_DOC
