"""MCP サーバー（docs/mcp-tools.md）。streamable HTTP、localhost / Tailscale 内のみ。"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import config as cfg

INSTRUCTIONS = (
    "kairn: 案件（case）単位の作業ログ。案件を開くときは open_case。作業したら log_event / update_task"
    "（done は証拠必須）。方針が変わったら plan で計画を出し直す。終わったら checkin。"
    "ワークスペースをまたぐ参照はしない。"
)

mcp = FastMCP("kairn", instructions=INSTRUCTIONS)


@mcp.tool()
def open_case(case: str, workspace: str | None = None) -> dict:
    """案件を開く: 取り寄せ → case / 最新計画 / open タスク / 直近イベント / 関連案件 を 1 回で返す。"""
    raise NotImplementedError


@mcp.tool()
def list_cases(workspace: str | None = None, status: str = "open", query: str = "") -> list[dict]:
    """案件一覧（進捗・最終イベント付き）。"""
    raise NotImplementedError


@mcp.tool()
def plan(case: str, objective: str, tasks: list[dict], reason: str) -> dict:
    """計画の新版。新版に無い open タスクは superseded になる。"""
    raise NotImplementedError


@mcp.tool()
def update_task(case: str, task: str, status: str, evidence: list[dict] | None = None, note: str = "") -> dict:
    """タスク状態の更新。done は evidence 必須。"""
    raise NotImplementedError


@mcp.tool()
def log_event(case: str, action: str, note: str, evidence: list[dict] | None = None) -> dict:
    """進捗・決定・コメントを追記する。"""
    raise NotImplementedError


@mcp.tool()
def search(query: str, cases: list[str] | None = None, workspace: str | None = None) -> list[dict]:
    """worklog の節単位の全文検索（ワークスペース内）。"""
    raise NotImplementedError


@mcp.tool()
def find_cases(query: str, workspace: str | None = None, k: int = 5) -> list[dict]:
    """問いに関係する案件を理由付きで返す。"""
    raise NotImplementedError


@mcp.tool()
def checkin(case: str) -> dict:
    """ローカルの案件を Drive に戻す。"""
    raise NotImplementedError


@mcp.tool()
def drive_index(pattern: str, workspace: str | None = None) -> list[dict]:
    """Drive 上のファイル一覧を検索（生データの所在）。"""
    raise NotImplementedError


def main() -> None:
    cfg.assert_data_not_tracked()
    cfg.load()
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
