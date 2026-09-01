"""kairn CLI: link / checkout / checkin / sync / serve / ui。"""
from __future__ import annotations

import argparse

from . import config as cfg


def main() -> None:
    ap = argparse.ArgumentParser(prog="kairn")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("config", help="workspaces.yaml を検証して表示")
    sub.add_parser("link", help="登録リポジトリの tmp/ を cases/ へのリンクにする")
    sub.add_parser("serve", help="MCP サーバー起動")
    args = ap.parse_args()
    cfg.assert_data_not_tracked()
    conf = cfg.load()
    if args.cmd == "config":
        print(f"remote={conf.remote} root={conf.drive_root}")
        for ws in conf.workspaces.values():
            print(f"- {ws.name}: repos={[str(r) for r in ws.repos]} data={ws.data_dir}")
    elif args.cmd == "serve":
        from .server import main as serve
        serve()
    else:
        raise SystemExit(f"{args.cmd}: not implemented yet (see docs/roadmap.md)")
