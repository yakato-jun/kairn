"""kairn CLI（人が使う入口。設定ファイルはこのコマンドが書く）

  kairn setup --remote <rclone remote> [--agent claude|codex|opencode|antigravity]
  kairn ws list | ws create <name>
  kairn attach <ws> [<repo path>...]     # 省略時は cwd。<repo>/<link_name> を cases/ へのリンクにする
  kairn detach [<repo path>]
  kairn status
  kairn cases [<ws>] [--all]
  kairn new <case id> "<title>" [--ws <ws>]
  kairn checkout <ws> [<case>] | checkin <ws> [<case>] | index <ws> | drive-index <ws>
  kairn serve [--port 8765]              # MCP + UI
  kairn install-skill                    # skills/kairn を ~/.agents/skills に置き ~/.claude/skills からリンク
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import config as cfg


def _ws(conf: cfg.Config, name: str | None, path: Path | None = None) -> cfg.Workspace:
    if name:
        if name not in conf.workspaces:
            raise SystemExit(f"kairn: unknown workspace {name!r} (known: {list(conf.workspaces)})")
        return conf.workspaces[name]
    ws = conf.workspace_for_path(path or Path.cwd())
    if not ws:
        raise SystemExit("kairn: this directory is not attached to any workspace (run: kairn attach <ws>)")
    return ws


def cmd_setup(a):
    remotes = cfg.rclone_remotes()
    if a.remote not in remotes:
        raise SystemExit(f"kairn: rclone remote {a.remote!r} not found (have: {remotes}).\n"
                         f"create it first:  rclone config create {a.remote} drive scope=drive")
    conf = cfg.create(a.remote, a.agent)
    print(f"config written: {conf.path}\n  remote={conf.remote} root={conf.drive_root} extract.agent={conf.extract_agent}")
    from . import sync
    names = sync.list_ws_on_drive(conf)
    print(f"workspaces on drive: {names or '(none)'}")
    for n in names:
        conf.workspaces.setdefault(n, cfg.Workspace(name=n))
    conf.save()


def cmd_ws(a):
    conf = cfg.load()
    from . import sync
    if a.sub == "list":
        remote = set(sync.list_ws_on_drive(conf))
        for n, w in conf.workspaces.items():
            print(f"{n:<16} drive={'yes' if n in remote else 'no '} repos={len(w.repos)} local={w.data_dir if w.data_dir.exists() else '-'}")
        for n in sorted(remote - set(conf.workspaces)):
            print(f"{n:<16} drive=yes (not registered locally; run: kairn attach {n})")
    elif a.sub == "create":
        if a.name in conf.workspaces or sync.ws_exists_on_drive(conf, a.name):
            raise SystemExit(f"kairn: workspace {a.name!r} already exists")
        sync.create_ws_on_drive(conf, a.name)
        conf.workspaces[a.name] = cfg.Workspace(name=a.name, description=a.description or "")
        conf.workspaces[a.name].cases_dir.mkdir(parents=True, exist_ok=True)
        conf.save()
        print(f"created workspace {a.name}: {conf.drive_path(a.name)} and {conf.workspaces[a.name].data_dir}")


def _link(repo: Path, ws: cfg.Workspace) -> str:
    link = repo / ws.link_name
    target = ws.cases_dir
    target.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if link.resolve() == target.resolve():
            return "already linked"
        raise SystemExit(f"kairn: {link} is a symlink to {link.resolve()} (not {target}); fix by hand")
    if link.exists():
        n = sum(1 for _ in link.iterdir())
        raise SystemExit(f"kairn: {link} exists with {n} entries. move its contents into {target} first (migration), then re-run attach")
    link.symlink_to(target, target_is_directory=True)
    return f"linked {link} -> {target}"


def cmd_attach(a):
    conf = cfg.load()
    ws = conf.workspaces.get(a.ws)
    if not ws:
        from . import sync
        if not sync.ws_exists_on_drive(conf, a.ws):
            raise SystemExit(f"kairn: workspace {a.ws!r} does not exist (kairn ws create {a.ws})")
        ws = conf.workspaces[a.ws] = cfg.Workspace(name=a.ws)
    repos = [Path(os.path.expanduser(p)).resolve() for p in (a.repos or [os.getcwd()])]
    for repo in repos:
        if not repo.is_dir():
            raise SystemExit(f"kairn: {repo} is not a directory")
        other = conf.workspace_for_path(repo)
        if other and other.name != ws.name:
            raise SystemExit(f"kairn: {repo} is already attached to workspace {other.name!r} (detach first)")
        if repo not in ws.repos:
            ws.repos.append(repo)
        print(f"{repo}: {_link(repo, ws)}")
    conf.save()
    print(f"attached {len(repos)} repo(s) to {ws.name}. config: {conf.path}")


def cmd_detach(a):
    conf = cfg.load()
    repo = Path(os.path.expanduser(a.repo or os.getcwd())).resolve()
    ws = conf.workspace_for_path(repo)
    if not ws or repo not in [r.resolve() for r in ws.repos]:
        raise SystemExit(f"kairn: {repo} is not attached")
    ws.repos = [r for r in ws.repos if r.resolve() != repo]
    link = repo / ws.link_name
    if link.is_symlink():
        link.unlink(); print(f"removed link {link}")
    conf.save(); print(f"detached {repo} from {ws.name}")


def cmd_status(a):
    conf = cfg.load()
    from .store import CaseStore
    print(f"config: {conf.path}\nremote: {conf.remote}  root: {conf.drive_root}  extract.agent: {conf.extract_agent}")
    here = conf.workspace_for_path(Path.cwd())
    print(f"cwd: {Path.cwd()} -> workspace: {here.name if here else '(not attached)'}")
    for ws in conf.workspaces.values():
        st = CaseStore(ws.cases_dir)
        ids = st.list_case_ids(); raw = st.list_dirs_without_case()
        print(f"\n[{ws.name}] {ws.description}\n  data: {ws.data_dir} (exists={ws.data_dir.exists()})\n  cases: {len(ids)} with case.json, {len(raw)} dirs without (legacy)\n  repos:")
        for r in ws.repos:
            link = r / ws.link_name
            state = "linked" if link.is_symlink() and link.resolve() == ws.cases_dir.resolve() else ("EXISTS(not link)" if link.exists() else "no link")
            print(f"    {r}  [{state}]")


def cmd_cases(a):
    conf = cfg.load(); ws = _ws(conf, a.ws)
    from .store import CaseStore
    st = CaseStore(ws.cases_dir)
    for cid in st.list_case_ids():
        c = st.load_case(cid)
        if not a.all and c["status"] != "open":
            continue
        p = st.progress(cid); le = st.last_event(cid)
        print(f"{cid:<40} {c['status']:<9} plan v{p['plan']} done {p['done']}/{p['total']}  last: {le['t'][:16] if le else '-'} {le['actor'] if le else ''} {le['action'] if le else ''}")
    legacy = st.list_dirs_without_case()
    if legacy:
        print(f"(+{len(legacy)} directories without case.json)")


def cmd_new(a):
    conf = cfg.load(); ws = _ws(conf, a.ws)
    from .store import CaseStore
    c = CaseStore(ws.cases_dir).create_case(a.id, a.title, ws.name, actor="human")
    print(f"created {ws.cases_dir / c['id']}")


def cmd_sync(a):
    conf = cfg.load(); ws = _ws(conf, a.ws)
    from . import sync
    from .index import Index
    if a.cmd == "checkout":
        print(sync.checkout(conf, ws, a.case, dry=a.dry_run))
        print(Index(ws.index_dir, ws.cases_dir).rebuild())
    elif a.cmd == "checkin":
        print(sync.checkin(conf, ws, a.case, dry=a.dry_run))
    elif a.cmd == "index":
        print(Index(ws.index_dir, ws.cases_dir).rebuild(full=a.full))
    elif a.cmd == "drive-index":
        print(sync.drive_index(conf, ws))


def cmd_serve(a):
    conf = cfg.load()
    from .server import serve
    serve(conf, host=a.host, port=a.port)


def cmd_install_skill(a):
    src = cfg.ROOT / "skills" / "kairn"
    agents = Path(os.path.expanduser("~/.agents/skills")); claude = Path(os.path.expanduser("~/.claude/skills"))
    agents.mkdir(parents=True, exist_ok=True); claude.mkdir(parents=True, exist_ok=True)
    for base in (agents, claude):
        dst = base / "kairn"
        if dst.is_symlink() or dst.exists():
            print(f"exists: {dst} ({'symlink' if dst.is_symlink() else 'dir'})"); continue
        dst.symlink_to(src, target_is_directory=True); print(f"linked {dst} -> {src}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="kairn", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup"); s.add_argument("--remote", required=True); s.add_argument("--agent", default="claude", choices=["claude", "codex", "opencode", "antigravity"]); s.set_defaults(f=cmd_setup)
    s = sub.add_parser("ws"); ss = s.add_subparsers(dest="sub", required=True); ss.add_parser("list"); c = ss.add_parser("create"); c.add_argument("name"); c.add_argument("--description"); s.set_defaults(f=cmd_ws)
    s = sub.add_parser("attach"); s.add_argument("ws"); s.add_argument("repos", nargs="*"); s.set_defaults(f=cmd_attach)
    s = sub.add_parser("detach"); s.add_argument("repo", nargs="?"); s.set_defaults(f=cmd_detach)
    s = sub.add_parser("status"); s.set_defaults(f=cmd_status)
    s = sub.add_parser("cases"); s.add_argument("ws", nargs="?"); s.add_argument("--all", action="store_true"); s.set_defaults(f=cmd_cases)
    s = sub.add_parser("new"); s.add_argument("id"); s.add_argument("title"); s.add_argument("--ws"); s.set_defaults(f=cmd_new)
    for name in ("checkout", "checkin", "index", "drive-index"):
        s = sub.add_parser(name); s.add_argument("ws", nargs="?"); s.add_argument("case", nargs="?"); s.add_argument("--dry-run", action="store_true"); s.add_argument("--full", action="store_true"); s.set_defaults(f=cmd_sync)
    s = sub.add_parser("serve"); s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8765); s.set_defaults(f=cmd_serve)
    s = sub.add_parser("install-skill"); s.set_defaults(f=cmd_install_skill)
    a = ap.parse_args()
    cfg.assert_data_not_tracked()
    a.f(a)


if __name__ == "__main__":
    main()
