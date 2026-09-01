"""Drive 同期（rclone）。ワークスペース単位。remote は設定済みのものだけ使う。

- checkout(ws[, case]):  <remote>:<root>/<ws>/cases[/<case>] -> local（テキスト層のみ、削除は追従しない）
- checkin(ws[, case]):   local -> remote（テキスト層 sync。削除は _deleted/<日付>/ へ退避）
- drive_index(ws):       remote 上の全ファイル一覧を index/drive-index.txt に保存
生データ（raw）の移動は別コマンド（roadmap 5）。ここでは扱わない。
"""
from __future__ import annotations

import datetime as _dt
import subprocess
from pathlib import Path

from .config import Config, Workspace


class RcloneError(RuntimeError):
    pass


def _filters(conf: Config) -> list[str]:
    args: list[str] = []
    for pat in conf.rules.get("exclude", []):
        args += ["--exclude", pat]
    raw = conf.rules.get("raw_data", {})
    for ext in raw.get("extensions", []):
        args += ["--exclude", f"*.{ext}"]
    if raw.get("min_size"):
        args += ["--max-size", str(raw["min_size"])]
    return args


def _run(cmd: list[str], dry: bool = False) -> subprocess.CompletedProcess:
    if dry:
        cmd = cmd + ["--dry-run"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode not in (0, 9):  # 9 = nothing transferred with --error-on-no-transfer (not used) / keep simple
        raise RcloneError((r.stderr or r.stdout).strip()[-800:])
    return r


def checkout(conf: Config, ws: Workspace, case: str | None = None, dry: bool = False) -> str:
    src = conf.drive_path(ws.name, "cases", *( [case] if case else [] ))
    dst = ws.cases_dir / case if case else ws.cases_dir
    dst.mkdir(parents=True, exist_ok=True)
    r = _run(["rclone", "copy", src, str(dst), "--fast-list", "--transfers", "8", "--stats-one-line", "-v", *_filters(conf)], dry)
    return (r.stderr or r.stdout).strip()[-400:]


def checkin(conf: Config, ws: Workspace, case: str | None = None, dry: bool = False) -> str:
    src = ws.cases_dir / case if case else ws.cases_dir
    if not src.exists():
        raise RcloneError(f"nothing to check in: {src} does not exist")
    dst = conf.drive_path(ws.name, "cases", *( [case] if case else [] ))
    backup = conf.drive_path(ws.name, "_deleted", _dt.date.today().isoformat())
    r = _run(["rclone", "sync", str(src), dst, "--backup-dir", backup, "--fast-list", "--transfers", "8",
              "--stats-one-line", "-v", *_filters(conf)], dry)
    return (r.stderr or r.stdout).strip()[-400:]


def drive_index(conf: Config, ws: Workspace) -> Path:
    out = ws.index_dir / "drive-index.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    r = _run(["rclone", "lsf", "-R", "--files-only", "--format", "pst", "--separator", "\t", "--fast-list", conf.drive_path(ws.name)])
    out.write_text(r.stdout, encoding="utf-8")
    return out


def grep_drive_index(ws: Workspace, pattern: str, limit: int = 50) -> list[dict]:
    import re
    f = ws.index_dir / "drive-index.txt"
    if not f.exists():
        return []
    rx = re.compile(pattern, re.I)
    rows = []
    for line in f.read_text(encoding="utf-8").splitlines():
        p, *rest = line.split("\t")
        if rx.search(p):
            rows.append({"path": p, "size": rest[0] if rest else "", "mtime": rest[1] if len(rest) > 1 else ""})
            if len(rows) >= limit:
                break
    return rows


def ws_exists_on_drive(conf: Config, ws_name: str) -> bool:
    r = subprocess.run(["rclone", "lsd", conf.drive_path(ws_name)], capture_output=True, text=True)
    return r.returncode == 0


def create_ws_on_drive(conf: Config, ws_name: str) -> None:
    _run(["rclone", "mkdir", conf.drive_path(ws_name, "cases")])


def list_ws_on_drive(conf: Config) -> list[str]:
    r = subprocess.run(["rclone", "lsf", "--dirs-only", f"{conf.remote}:{conf.drive_root}"], capture_output=True, text=True)
    return [x.rstrip("/") for x in r.stdout.split()] if r.returncode == 0 else []
