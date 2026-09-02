"""kairn CLI（人が使う入口。設定ファイルはこのコマンドが書く）

  kairn setup --remote <rclone remote> [--agent claude|codex|opencode|antigravity] [--extract-timeout <sec>]
  kairn ws list | ws create <name>
  kairn attach <ws> [<repo path>...]     # 省略時は cwd。リポジトリの所属を設定に記録するだけ（リポジトリ側には何も作らない）
  kairn detach [<repo path>]             # 省略時は cwd。所属を外す。glob: 由来なら exclude: を書いて展開から外す
  kairn status
  kairn cases [<ws>] [--all]
  kairn new <case id> "<title>" [--ws <ws>]
  kairn checkout <ws> [<case>] [--dry-run] | checkin <ws> [<case>] [--dry-run] | index <ws> [--full] | drive-index <ws>
                                         # checkout / checkin は同期実行（終わるまで待つ。タイムアウト無し）。大きな初回投入は MCP ではなくここで行う
                                         # checkout <ws>（案件指定なし）は Drive の manifest.json と比べて rev が違う案件だけ取り寄せる
  kairn bag2zst <ws> [<case>] [--dry-run]   # *.bag / *.bag.active を zstd 圧縮（30 分以上更新のないもの）
  kairn raw-move <ws> [<case>] [--dry-run]  # 生データ（rules.raw_data）を Drive へ移動し、所在を案件に記録
  kairn daily <ws> [--dry-run]              # bag2zst -> checkin -> raw-move -> drive-index -> index（systemd timer 用）
  kairn manifest rebuild <ws> [--dry-run]   # 既存 Drive データの移行: cases/*/case.json に rev を付与し manifest.json を作り直す（新方式導入時に一度）
  kairn extract <case> [--ws <ws>] [--agent claude|codex|opencode|antigravity] [--json]
                                         # 子エージェントで case.json の下書きを作る（書き込まない。適用は UI）
  kairn rules show                       # 同期・退避規則（rules）の現在値
  kairn rules set <key> <value>          # raw_data.min_size | raw_data.min_age | bag_to_zst | bwlimit | rclone_flags（値を検証。不正なら拒否）
                                         # rclone_flags は空白区切り（"--transfers 8 --checkers 16"。-- で始まるオプションと値だけ。"" で空に戻す）
  kairn rules add-exclude <pattern> | remove-exclude <pattern>   # 同期しないパターン（rclone のフィルタ規則）
  kairn rules add-raw-ext <ext> | remove-raw-ext <ext>           # 生データ扱いの拡張子
  kairn serve [--port 8765]              # MCP + UI
  kairn install-service [--yes] [--print]  # systemd user unit（kairn-serve.service / kairn-daily@<ws>.timer）を生成して登録（対話式。--yes は既定値、--print は内容表示のみ）
  kairn ensure [--timeout 15]            # 設定の serve.host/port の /mcp が応答しなければ kairn serve を切り離して起動し、応答まで待つ（service が止まっていた時の保険）
  kairn install-skill [--home <dir>]     # <home>/.agents/skills/kairn と <home>/.claude/skills/kairn を skills/kairn へのリンクにする（既存は上書きしない）。
                                         # 最後に、各エージェントがデータ領域（workspaces/）を読み書きするための許可設定手順を表示する
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
    conf = cfg.create(a.remote, a.agent, extract_timeout=a.extract_timeout)
    print(f"config written: {conf.path}\n  remote={conf.remote} root={conf.drive_root} extract.agent={conf.extract_agent} extract.timeout={conf.extract_timeout}s")
    _warn_shared_client_id(conf.remote)
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
        ws.add_repo(repo)
        print(f"{repo} -> {ws.name}")
    ws.cases_dir.mkdir(parents=True, exist_ok=True)
    conf.save()
    print(f"attached {len(repos)} repo(s) to {ws.name}. config: {conf.path}\ncases: {ws.cases_dir} (agents need permission for {ws.data_dir.parent}; see: kairn install-skill)")


def cmd_detach(a):
    conf = cfg.load()
    repo = Path(os.path.expanduser(a.repo or os.getcwd())).resolve()
    ws = conf.workspace_for_path(repo)
    if not ws or repo not in [r.resolve() for r in ws.repos]:
        raise SystemExit(f"kairn: {repo} is not attached")
    ws.remove_repo(repo)
    conf.save(); print(f"detached {repo} from {ws.name}")


def _warn_shared_client_id(remote: str) -> None:
    """remote が Google Drive で自前の client_id が無ければ stderr に 1 行（config.shared_client_id_warning）。それ以外は何も出さない。"""
    w = cfg.shared_client_id_warning(remote)
    if w:
        print(w, file=sys.stderr)


def cmd_status(a):
    conf = cfg.load()
    from .store import CaseStore
    print(f"config: {conf.path}\nremote: {conf.remote}  root: {conf.drive_root}  extract.agent: {conf.extract_agent}  extract.timeout: {conf.extract_timeout}s")
    _warn_shared_client_id(conf.remote)
    here = conf.workspace_for_path(Path.cwd())
    print(f"cwd: {Path.cwd()} -> workspace: {here.name if here else '(not attached)'}")
    for ws in conf.workspaces.values():
        st = CaseStore(ws.cases_dir)
        ids = st.list_case_ids(); raw = st.list_dirs_without_case()
        print(f"\n[{ws.name}] {ws.description}\n  data: {ws.data_dir} (exists={ws.data_dir.exists()})\n  cases: {len(ids)} with case.json, {len(raw)} dirs without (legacy)\n  repos:")
        for r in ws.repos:
            print(f"    {r}")


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
        if a.dry_run:
            print("(dry-run: index not rebuilt)")
        else:
            print(Index(ws.index_dir, ws.cases_dir).rebuild())
    elif a.cmd == "checkin":
        print(sync.checkin(conf, ws, a.case, dry=a.dry_run))
    elif a.cmd == "index":
        print(Index(ws.index_dir, ws.cases_dir).rebuild(full=a.full))
    elif a.cmd == "drive-index":
        print(sync.drive_index(conf, ws))
    elif a.cmd == "bag2zst":
        r = sync.bag2zst(conf, ws, a.case, dry=a.dry_run)
        if not r["enabled"]:
            print("bag_to_zst is disabled (rules.bag_to_zst: false)"); return
        for x in r["done"]:
            print(f"{'[dry] ' if a.dry_run else ''}{x['src']} -> {x['dst']} ({x['bytes']} bytes)")
        for x in r["errors"]:
            print(f"ERROR {x['src']}: {x['error']}", file=sys.stderr)
        print(f"{len(r['done'])} compressed, {len(r['errors'])} errors")
        if r["errors"]:
            sys.exit(1)
    elif a.cmd == "raw-move":
        r = sync.raw_move(conf, ws, a.case, dry=a.dry_run)
        errors = 0
        for cid, c in r["cases"].items():
            if c.get("error"):
                errors += 1; print(f"ERROR {cid}: {c['error']}", file=sys.stderr); continue
            if not c["planned"]:
                continue
            if a.dry_run:
                print(f"[dry] {cid}: {len(c['planned'])} file(s) -> {c['drive']}")
                for rel in c["planned"]:
                    print(f"  {rel}")
            else:
                print(f"{cid}: moved {len(c['moved'])}/{len(c['planned'])} file(s), {c['bytes']} bytes -> {c['drive']}")
        print(f"total: {r['files']} file(s), {r['bytes']} bytes{' (dry-run)' if a.dry_run else ''}")
        if errors:
            sys.exit(1)
    elif a.cmd == "daily":
        r = sync.daily(conf, ws, dry=a.dry_run)
        for name, st in r["steps"].items():
            print(f"{name}: {'ok' if st['ok'] else 'ERROR ' + st['error']}")
        print(f"daily {'ok' if r['ok'] else 'FAILED'} ({r['started']} .. {r['finished']}), log: {ws.index_dir / 'daily.log'}")
        if not r["ok"]:
            sys.exit(1)


def cmd_manifest(a):
    conf = cfg.load(); ws = _ws(conf, a.ws)
    from . import sync
    try:
        r = sync.manifest_rebuild(conf, ws, dry=a.dry_run)
    except sync.RcloneError as e:
        raise SystemExit(f"kairn: {e}") from None
    tag = "[dry] " if a.dry_run else ""
    for cid, c in r["cases"].items():
        remote = "rev assigned" if c["rev_assigned"] else "rev kept"
        if c["checked_in_at_assigned"]:
            remote += ", last_checkin_at from drive mtime"
        local = c["local"] + (f" ({c['local_reason']})" if c.get("local_reason") else "")
        print(f"{tag}{cid:<40} drive: {remote:<45} local: {local}")
    for cid, err in r["errors"].items():
        print(f"ERROR {cid}: {err}", file=sys.stderr)
    print(f"{tag}manifest {r['manifest']}: {len(r['cases'])} case(s), {len(r['errors'])} error(s)"
          + (f", local skipped: {', '.join(r['local_skipped'])}" if r["local_skipped"] else "") + (" (dry-run: nothing written)" if a.dry_run else ""))
    if r["errors"]:
        sys.exit(1)


def cmd_extract(a):
    conf = cfg.load(); ws = _ws(conf, a.ws)
    import json
    from . import extract
    from .store import CaseNotFound
    try:
        r = extract.extract_card(conf, ws, a.case, agent=a.agent)
    except CaseNotFound:
        raise SystemExit(f"kairn: unknown case {a.case!r} in workspace {ws.name!r}") from None
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print(f"agent={r['agent']} ok={r['ok']} elapsed={r['elapsed_sec']}s")
        if r["ok"]:
            print(json.dumps(r["card"], ensure_ascii=False, indent=1))
        else:
            print(f"error: {r['error']}", file=sys.stderr)
            if r["raw_excerpt"]:
                print(r["raw_excerpt"], file=sys.stderr)
    if not r["ok"]:
        sys.exit(1)


def rules_lines(conf: cfg.Config) -> list[str]:
    """`kairn rules show` の表示（1 行ずつ）。"""
    v = cfg.rules_view(conf)
    return [f"config: {conf.path}",
            f"raw_data.min_size:   {v['raw_data.min_size'] or '(none)'}",
            f"raw_data.min_age:    {v['raw_data.min_age'] or '(none)'}",
            f"bag_to_zst:          {'true' if v['bag_to_zst'] else 'false'}",
            f"bwlimit:             {v['bwlimit'] or '(none)'}",
            f"rclone_flags:        {' '.join(v['rclone_flags']) or '(none)'}",
            f"raw_data.extensions: {' '.join(v['raw_data.extensions']) or '(none)'}",
            "exclude:"] + [f"  {pat}" for pat in v["exclude"]]


def _shown(out: object) -> str:
    """set_rule の返り値の表示: None / 空リスト → (none)、bool → true/false、リスト → 空白区切り。"""
    if out is None or out == []:
        return "(none)"
    if isinstance(out, bool):
        return str(out).lower()
    return " ".join(out) if isinstance(out, list) else str(out)


def cmd_rules(a):
    conf = cfg.load()
    try:
        if a.sub == "show":
            print("\n".join(rules_lines(conf))); return
        if a.sub == "set":
            print(f"{a.key} = {_shown(cfg.set_rule(conf, a.key, a.value))}")
        elif a.sub == "add-exclude":
            print(f"exclude += {a.pattern}" if cfg.add_exclude(conf, a.pattern) else f"exclude already has {a.pattern}")
        elif a.sub == "remove-exclude":
            cfg.remove_exclude(conf, a.pattern); print(f"exclude -= {a.pattern}")
        elif a.sub == "add-raw-ext":
            print(f"raw_data.extensions += {a.ext}" if cfg.add_raw_ext(conf, a.ext) else f"raw_data.extensions already has {a.ext}")
        elif a.sub == "remove-raw-ext":
            cfg.remove_raw_ext(conf, a.ext); print(f"raw_data.extensions -= {a.ext}")
    except ValueError as e:
        raise SystemExit(f"kairn: {e}") from None
    print(f"saved: {conf.path} (a running kairn serve keeps its loaded rules until restarted)")


def cmd_serve(a):
    conf = cfg.load()
    from .server import serve
    serve(conf, host=a.host, port=a.port)


def install_skill(home: Path) -> list[str]:
    """skills/kairn を <home>/.agents/skills/kairn と <home>/.claude/skills/kairn からのシンボリックリンクにする。
    既存（リンク・ディレクトリ・ファイル）は上書きせず報告する。戻り値は 1 行ずつの報告。"""
    src = cfg.ROOT / "skills" / "kairn"
    out = []
    for base in (home / ".agents" / "skills", home / ".claude" / "skills"):
        base.mkdir(parents=True, exist_ok=True)
        dst = base / "kairn"
        if dst.is_symlink():
            target = Path(os.readlink(dst))
            state = "already linked" if dst.resolve() == src.resolve() else f"symlink to {target}, not {src}; fix by hand"
            out.append(f"exists: {dst} ({state})")
        elif dst.exists():
            out.append(f"exists: {dst} ({'dir' if dst.is_dir() else 'file'}; not overwritten)")
        else:
            dst.symlink_to(src, target_is_directory=True)
            out.append(f"linked {dst} -> {src}")
    return out


def permission_notes(data_root: Path | None = None) -> str:
    """各エージェントがデータ領域（workspaces/）を読み書きするための許可設定の手順。kairn は設定を書き換えず、表示するだけ。
    README「各エージェントへの適用」の 6 と同じ内容（tests/test_install_skill.py が照合する）。"""
    d = str((data_root or cfg.DATA_ROOT).resolve())
    return "\n".join([
        f"案件は {d}/<ws>/cases/ にある（リポジトリの外。open_case が返す paths.case_dir の絶対パスで読み書きする）。",
        "各エージェントがここを読み書きできるよう、追加ディレクトリの許可を設定する（kairn は自動では書き換えない）:",
        f"  Claude Code : claude --add-dir {d}",
        f"                または ~/.claude/settings.json の permissions.additionalDirectories に \"{d}\" を追加（ユーザー設定。フォルダを trust した後に有効）",
        f"  Codex       : codex --add-dir {d}   （workspace に加えて書き込み可。読み取り範囲も広げるなら -c 'sandbox_permissions=[\"disk-full-read-access\"]'）",
        f"  OpenCode    : opencode.json の permission.external_directory に {{\"{d}/**\": \"allow\"}}（該当エージェントの permission でも可）",
        f"  Antigravity : agy --add-dir {d}   （複数指定可）",
    ])


def cmd_install_skill(a):
    for line in install_skill(Path(os.path.expanduser(a.home)).resolve()):
        print(line)
    print()
    print(permission_notes())


def cmd_install_service(a):
    conf = cfg.load()
    from . import service
    if a.print:
        opts = service.interview(conf, yes=True)
        for name, body in service.render_units(opts, service.self_command()).items():
            print(f"# ==== {service.unit_dir() / name}\n{body}")
        return
    opts = service.interview(conf, yes=a.yes)
    sys.exit(service.install(conf, opts, yes=a.yes))


def cmd_ensure(a):
    conf = cfg.load()
    from . import service
    sys.exit(service.ensure(conf, timeout=a.timeout))


def _argv(argv: list[str]) -> list[str]:
    """`kairn rules set rclone_flags --fast-list` のように値が `-` で始まる（空白を含まない）と argparse がオプションと誤読するので、
    `rules set rclone_flags` の直後に `--` を補う（既にあれば何もしない）。"""
    if argv[:3] == ["rules", "set", "rclone_flags"] and "--" not in argv[3:]:
        return argv[:3] + ["--"] + argv[3:]
    return argv


def main() -> None:
    ap = argparse.ArgumentParser(prog="kairn", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup"); s.add_argument("--remote", required=True); s.add_argument("--agent", default=None, choices=["claude", "codex", "opencode", "antigravity"], help="抽出エージェント（省略時は既存値を保つ。初回は claude）"); s.add_argument("--extract-timeout", type=int, default=None, metavar="SEC", help="extract の子エージェントのタイムアウト秒（既定 600。省略時は既存値を保つ）"); s.set_defaults(f=cmd_setup)
    s = sub.add_parser("ws"); ss = s.add_subparsers(dest="sub", required=True); ss.add_parser("list"); c = ss.add_parser("create"); c.add_argument("name"); c.add_argument("--description"); s.set_defaults(f=cmd_ws)
    s = sub.add_parser("attach"); s.add_argument("ws"); s.add_argument("repos", nargs="*"); s.set_defaults(f=cmd_attach)
    s = sub.add_parser("detach"); s.add_argument("repo", nargs="?"); s.set_defaults(f=cmd_detach)
    s = sub.add_parser("status"); s.set_defaults(f=cmd_status)
    s = sub.add_parser("cases"); s.add_argument("ws", nargs="?"); s.add_argument("--all", action="store_true"); s.set_defaults(f=cmd_cases)
    s = sub.add_parser("new"); s.add_argument("id"); s.add_argument("title"); s.add_argument("--ws"); s.set_defaults(f=cmd_new)
    for name in ("checkout", "checkin", "bag2zst", "raw-move"):
        s = sub.add_parser(name); s.add_argument("ws", nargs="?"); s.add_argument("case", nargs="?"); s.add_argument("--dry-run", action="store_true"); s.set_defaults(f=cmd_sync)
    s = sub.add_parser("index", help="rebuild the local search index (changed files only; --full for everything)"); s.add_argument("ws", nargs="?"); s.add_argument("--full", action="store_true"); s.set_defaults(f=cmd_sync)
    s = sub.add_parser("drive-index", help="list all files on the drive into index/drive-index.txt"); s.add_argument("ws", nargs="?"); s.set_defaults(f=cmd_sync)
    s = sub.add_parser("daily", help="bag2zst -> checkin -> raw-move -> drive-index -> index"); s.add_argument("ws", nargs="?"); s.add_argument("--dry-run", action="store_true"); s.set_defaults(f=cmd_sync)
    s = sub.add_parser("manifest", help="rebuild the drive's manifest.json from cases/*/case.json (assign rev where missing; run once when adopting the manifest)"); ss = s.add_subparsers(dest="sub", required=True)
    c = ss.add_parser("rebuild"); c.add_argument("ws", nargs="?"); c.add_argument("--dry-run", action="store_true"); s.set_defaults(f=cmd_manifest)
    s = sub.add_parser("extract", help="draft case.json with an isolated child agent (read-only; apply in the UI)"); s.add_argument("case"); s.add_argument("--ws"); s.add_argument("--agent", choices=["claude", "codex", "opencode", "antigravity"]); s.add_argument("--json", action="store_true"); s.set_defaults(f=cmd_extract)
    s = sub.add_parser("rules", help="show / edit the sync rules (rules: in the config; never edit the file by hand)"); ss = s.add_subparsers(dest="sub", required=True)
    ss.add_parser("show")
    c = ss.add_parser("set"); c.add_argument("key", choices=list(cfg.RULE_KEYS)); c.add_argument("value")
    for name in ("add-exclude", "remove-exclude"):
        c = ss.add_parser(name); c.add_argument("pattern")
    for name in ("add-raw-ext", "remove-raw-ext"):
        c = ss.add_parser(name); c.add_argument("ext")
    s.set_defaults(f=cmd_rules)
    s = sub.add_parser("serve"); s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8765); s.set_defaults(f=cmd_serve)
    s = sub.add_parser("install-skill", help="symlink skills/kairn into ~/.agents/skills and ~/.claude/skills (existing entries are kept)"); s.add_argument("--home", default="~", help="HOME to install into (default: ~)"); s.set_defaults(f=cmd_install_skill)
    s = sub.add_parser("install-service", help="generate systemd user units (kairn-serve.service, kairn-daily@<ws>.timer) and enable them"); s.add_argument("--yes", action="store_true", help="非対話（既定値: 127.0.0.1:8765、設定の全ワークスペースを 12:30、enable --now、linger なし。既存 unit は上書き）"); s.add_argument("--print", action="store_true", help="書き込む unit の内容を表示するだけ（ファイルもコマンドも実行しない）"); s.set_defaults(f=cmd_install_service)
    s = sub.add_parser("ensure", help="start kairn serve (detached) if /mcp does not answer, and wait for it"); s.add_argument("--timeout", type=float, default=15.0, metavar="SEC"); s.set_defaults(f=cmd_ensure)
    a = ap.parse_args(_argv(sys.argv[1:]))
    cfg.assert_data_not_tracked()
    a.f(a)


if __name__ == "__main__":
    main()
