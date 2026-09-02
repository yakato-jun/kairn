"""共通フィクスチャ。実設定（~/.config/kairn）・実データ（workspaces/）・rclone には触れない。"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from kairn import config as cfg
from kairn import sync


@pytest.fixture(autouse=True)
def _state_home(tmp_path: Path, monkeypatch):
    """ワークスペース非依存の状態置き場（serve.log 等）を一時ディレクトリに向ける（実 ~/.local/state/kairn に触れない）。"""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.fixture
def conf(tmp_path: Path, monkeypatch) -> cfg.Config:
    """一時ディレクトリに閉じた設定: ワークスペース acme、remote my-drive（架空）。
    DATA_ROOT も同じ一時ディレクトリに向ける: ConfigHolder が conf.path から読み直した Config の Workspace は data_root を持たず
    DATA_ROOT を見るので、読み直し後も実データ（workspaces/）に触れない。"""
    data_root = tmp_path / "data"
    monkeypatch.setattr(cfg, "DATA_ROOT", data_root)
    ws = cfg.Workspace(name="acme", description="fixture", data_root=data_root)
    ws.cases_dir.mkdir(parents=True)
    return cfg.Config(remote="my-drive", drive_root="ws", extract_agent="claude", rules=dict(cfg.DEFAULT_RULES),
                      workspaces={"acme": ws}, path=tmp_path / "config.yaml")


def bump_mtime(path: Path) -> None:
    """ファイルの mtime を +1 秒進める（ConfigHolder の更新検知をテストで決定的にする: 連続した書き込みが同じ mtime 粒度に収まっても変更と分かる）。"""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


class FakeDrive:
    """sync.drive_rev / sync.drive_revs の代役（メモリ内。rclone を呼ばない）。markers: {case: [rev, …]} が Drive の cases/<case>/.rev/ の中身。
    unavailable なら照会失敗（drive_rev は available=False、drive_revs は None）。"""

    def __init__(self):
        self.markers: dict[str, list[str]] = {}
        self.unavailable = False
        self.lookups = 0    # drive_rev（1 案件）の回数
        self.listings = 0   # drive_revs（全案件）の回数

    def lookup(self, conf, ws, case, timeout=sync.REV_LSF_TIMEOUT_SEC):
        self.lookups += 1
        if self.unavailable:
            return {"available": False, "rev": None, "markers": [], "error": "fake drive: offline"}
        m = sorted(self.markers.get(case, []))
        return {"available": True, "rev": m[0] if len(m) == 1 else None, "markers": m}

    def listing(self, conf, ws, timeout=sync.DRIVE_REVS_TIMEOUT_SEC):
        self.listings += 1
        if self.unavailable:
            return None
        return {c: (m[0] if len(m) == 1 else None) for c, m in self.markers.items() if m}

    def rev(self, case: str):
        m = self.markers.get(case, [])
        return m[0] if len(m) == 1 else None

    def set_rev(self, case: str, rev: str):
        self.markers[case] = [rev]

    def sync_from_local(self, ws, case: str):
        """checkin の rclone sync が案件フォルダの .rev/ を Drive へ運んだことにする（rclone を偽装したテストで使う）。"""
        from kairn.store import CaseStore
        self.markers[case] = CaseStore(ws.cases_dir).rev_markers(case)


@pytest.fixture
def fake_drive(monkeypatch) -> FakeDrive:
    """Drive の版マーカーをメモリ内で偽装する（sync.drive_rev / sync.drive_revs を差し替え）。"""
    fd = FakeDrive()
    monkeypatch.setattr(sync, "drive_rev", fd.lookup)
    monkeypatch.setattr(sync, "drive_revs", fd.listing)
    return fd


FAKE_RCLONE = r'''#!/usr/bin/env python3
"""偽 rclone（テスト用）: 呼ばれた引数を LOG に記録し、remote（<name>:<path>）を STORE/<path> に写像してローカルで真似る。
lsf（-R / --files-only / --include）・copy / sync（--update、--include、.rev/ 限定の --filter）・copyto・cat・rcat・deletefile。
それ以外は成功を返すだけ。クラウドには接続しない。"""
import fnmatch, os, re, shutil, sys
STORE = %(store)r
LOG = %(log)r
VALUED = {"--include", "--exclude", "--filter", "--format", "--separator", "--max-depth", "--transfers", "--checkers", "--stats",
          "--bwlimit", "--backup-dir", "--max-size", "--min-size", "--min-age", "--drive-pacer-min-sleep", "--drive-pacer-burst"}
args = sys.argv[1:]
with open(LOG, "a") as fh:
    fh.write(" ".join(args) + "\n")
sub, rest = args[0], args[1:]
opts, pos = {}, []
i = 0
while i < len(rest):
    a = rest[i]
    if a in VALUED:
        opts.setdefault(a, []).append(rest[i + 1]); i += 2
    elif a.startswith("-"):
        opts.setdefault(a, []).append(True); i += 1
    else:
        pos.append(a); i += 1


def path(p):
    m = re.match(r"^([A-Za-z0-9_-]+):(.*)$", p)
    return os.path.join(STORE, m.group(2)) if m else p


def rx(pat):
    """rclone の include パターン（/ 始まりは root 固定、* は / を含まない、** は何でも）→ 正規表現。"""
    anchored = pat.startswith("/")
    body = re.escape(pat.lstrip("/")).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.compile(("^" if anchored else "(^|/)") + body + "$")


def wanted(rel):
    inc = opts.get("--include", [])
    return not inc or any(rx(p).search(rel) for p in inc)


def walk(root):
    for r, dirs, files in os.walk(root):
        for f in files:
            full = os.path.join(r, f)
            yield os.path.relpath(full, root).replace(os.sep, "/"), full


def copy_tree(src, dst, update=False):
    for rel, full in walk(src):
        if not wanted(rel):
            continue
        d = os.path.join(dst, rel)
        if update and os.path.exists(d) and os.path.getmtime(d) >= os.path.getmtime(full):
            continue
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copy2(full, d)


if sub == "lsf":
    root = path(pos[0]).rstrip("/")
    if not os.path.isdir(root):
        print("error listing: directory not found", file=sys.stderr); sys.exit(3)
    if "-R" in opts:
        for rel, _ in sorted(walk(root)):
            if wanted(rel):
                print(rel)
    else:
        for name in sorted(os.listdir(root)):
            full = os.path.join(root, name)
            if os.path.isdir(full):
                if "--files-only" not in opts:
                    print(name + "/")
            else:
                print(name)
elif sub in ("copy", "sync"):
    src, dst = path(pos[0]), path(pos[1])
    if "--dry-run" in opts:
        sys.exit(0)
    filters = opts.get("--filter", [])
    if sub == "sync" and filters and filters[-1] == "- **":   # .rev/ 限定の sync（sync_rev_markers）: + /<case>/.rev/** ごとにその下だけ置き換える
        for pat in [f[2:] for f in filters if f.startswith("+ ")]:
            sub_rel = pat.strip("/").replace("/**", "")
            s, d = os.path.join(src, sub_rel), os.path.join(dst, sub_rel)
            shutil.rmtree(d, ignore_errors=True)
            if os.path.isdir(s):
                shutil.copytree(s, d)
    else:
        if sub == "sync":
            shutil.rmtree(dst, ignore_errors=True)
        copy_tree(src, dst, update="--update" in opts)
    print("fake rclone: %%s ok" %% sub, file=sys.stderr)
elif sub == "copyto":
    src, dst = path(pos[0]), path(pos[1])
    if not os.path.isfile(src):
        print("object not found", file=sys.stderr); sys.exit(3)
    os.makedirs(os.path.dirname(dst), exist_ok=True); shutil.copy2(src, dst)
elif sub == "cat":
    f = path(pos[0])
    if not os.path.isfile(f):
        print("object not found", file=sys.stderr); sys.exit(3)
    sys.stdout.write(open(f, encoding="utf-8").read())
elif sub == "rcat":
    f = path(pos[0]); os.makedirs(os.path.dirname(f), exist_ok=True)
    open(f, "w", encoding="utf-8").write(sys.stdin.read())
elif sub == "deletefile":
    f = path(pos[0])
    if not os.path.isfile(f):
        print("object not found", file=sys.stderr); sys.exit(3)
    os.remove(f)
else:
    print("fake rclone: %%s ok" %% sub)
'''


@pytest.fixture
def fake_rclone(tmp_path: Path) -> Path:
    """PATH の先頭に置く偽 rclone（Python スクリプト）。呼ばれた引数を rclone.log に記録し、remote を <tmp>/drive/ に写像して
    lsf / copy / sync / copyto / cat / rcat / deletefile をローカルで真似る（クラウドには接続しない）。"""
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    store = tmp_path / "drive"; store.mkdir()
    script = bin_dir / "rclone"
    script.write_text(FAKE_RCLONE % {"store": str(store), "log": str(tmp_path / "rclone.log")})
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def with_fake_rclone_env(bin_dir: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env.update(extra)
    return env
