"""Drive 同期（rclone）。ワークスペース単位。remote は設定済みのものだけ使う。

- checkout(ws[, case]):  <remote>:<root>/<ws>/cases[/<case>] -> local（テキスト層のみ、削除は追従しない）
- checkin(ws[, case]):   local -> remote（テキスト層）。案件単位は rclone sync（削除・Drive 側の新しい版は _deleted/<日付>/ へ退避）、
                         ワークスペース全体（daily）は rclone copy（ローカルに無い案件ディレクトリを Drive から消さない。
                         上書きされる Drive 側の版は同じく _deleted/ へ）。成功時に各案件の case.json.last_checkin_at を更新
                         （open_case の checkout skip 判定に使う）
- drive_index(ws):       remote 上の全ファイル一覧を index/drive-index.txt に保存
- bag2zst(ws[, case]):   *.bag / *.bag.active を zstd 圧縮（<name>.zst、mtime 引き継ぎ、元は削除）
- raw_move(ws[, case]):  生データ（rules.raw_data）を rclone move で Drive へ移動し、所在を case.json / worklog に記録
- daily(ws):             bag2zst -> checkin -> raw_move -> drive_index -> index rebuild（失敗しても次段へ。index/daily.log）

生データ判定は既存の _filters（テキスト層の除外）と同じ規則: (拡張子が raw_data.extensions に含まれる OR
サイズが min_size 超) AND 更新から min_age 超。rclone には include パスとサイズパスの 2 回に分けて渡す
（1 回の呼び出しでは --include と --min-size が AND になるため）。
"""
from __future__ import annotations

import datetime as _dt
import fnmatch
import json
import os
import re
import shutil
import subprocess
import time
import traceback
from pathlib import Path

from .config import Config, Workspace
from .store import CaseStore, now_iso


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


def _bw(conf: Config) -> list[str]:
    """帯域制限（任意）。rules.bwlimit をそのまま rclone の --bwlimit に渡す。"""
    bw = conf.rules.get("bwlimit")
    return ["--bwlimit", str(bw)] if bw else []


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
    # --update: 宛先（ローカル）の方が新しいファイルは上書きしない（open_case が毎回 checkout するため）
    r = _run(["rclone", "copy", src, str(dst), "--update", "--fast-list", "--transfers", "8", "--stats-one-line", "-v",
              *_filters(conf), *_bw(conf)], dry)
    return (r.stderr or r.stdout).strip()[-400:]


def checkin(conf: Config, ws: Workspace, case: str | None = None, dry: bool = False) -> str:
    """ローカル → Drive。case 指定は `rclone sync`（案件内の削除を追従）、ワークスペース全体は `rclone copy`
    （ローカルに無い案件ディレクトリは消してよい＝Drive から削除しない。README 原則 2）。どちらも上書きされる Drive 側の版は
    `_deleted/<日付>/` に退避する（--backup-dir）。"""
    src = ws.cases_dir / case if case else ws.cases_dir
    if not src.exists():
        raise RcloneError(f"nothing to check in: {src} does not exist")
    dst = conf.drive_path(ws.name, "cases", *( [case] if case else [] ))
    backup = conf.drive_path(ws.name, "_deleted", _dt.date.today().isoformat())
    verb = "sync" if case else "copy"
    r = _run(["rclone", verb, str(src), dst, "--backup-dir", backup, "--fast-list", "--transfers", "8",
              "--stats-one-line", "-v", *_filters(conf), *_bw(conf)], dry)
    if not dry:
        store = CaseStore(ws.cases_dir)
        for cid in ([case] if case else store.list_case_ids()):
            store.mark_checkin(cid)
    return (r.stderr or r.stdout).strip()[-400:]


def drive_index(conf: Config, ws: Workspace, dry: bool = False) -> Path:
    """remote 上の全ファイル一覧を index/drive-index.txt に保存。dry では一覧を取得するだけで書き換えない。"""
    out = ws.index_dir / "drive-index.txt"
    r = _run(["rclone", "lsf", "-R", "--files-only", "--format", "pst", "--separator", "\t", "--fast-list", conf.drive_path(ws.name)])
    if not dry:
        out.parent.mkdir(parents=True, exist_ok=True)
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


# ---------------------------------------------------------------------------
# 生データ層（raw）: 判定・圧縮・移動
# ---------------------------------------------------------------------------

_SIZE_UNITS = {"b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4, "p": 1024 ** 5}
_AGE_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400, "M": 30 * 86400, "y": 365 * 86400}
BAG_MIN_AGE_SEC = 30 * 60  # 更新から 30 分未満の bag は書き込み中とみなして圧縮しない


def parse_size(v) -> int:
    """rclone の SizeSuffix 表記（'50M', '1.5G', '100k'）→ bytes。単位なしは rclone と同じく KiB。"""
    if isinstance(v, (int, float)):
        return int(v * 1024)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([bBkKmMgGtTpP])?(?:i?[bB])?\s*", str(v))
    if not m:
        raise ValueError(f"invalid size: {v!r} (expected e.g. 50M, 1G, 100k)")
    n, unit = float(m.group(1)), (m.group(2) or "k").lower()
    return int(n * _SIZE_UNITS[unit])


def parse_age(v) -> float:
    """rclone の Duration 表記（'14d', '12h', '2w', '1M', '1y'）→ 秒。単位なしは秒。"""
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d|w|M|y)?\s*", str(v))
    if not m:
        raise ValueError(f"invalid age: {v!r} (expected e.g. 14d, 12h, 2w)")
    return float(m.group(1)) * _AGE_UNITS[m.group(2) or "s"]


def raw_rules(conf: Config) -> dict:
    """rules.raw_data を解釈した形: {extensions: [...], min_size: bytes, min_age: sec, min_size_str, min_age_str}"""
    raw = conf.rules.get("raw_data") or {}
    exts = [str(e).lstrip(".").lower() for e in raw.get("extensions", [])]
    return {"extensions": exts,
            "min_size": parse_size(raw["min_size"]) if raw.get("min_size") else None, "min_size_str": str(raw.get("min_size") or ""),
            "min_age": parse_age(raw["min_age"]) if raw.get("min_age") else 0.0, "min_age_str": str(raw.get("min_age") or "")}


def is_raw(path: Path, rules: dict, now: float | None = None) -> bool:
    """生データか: (拡張子が対象 OR サイズが min_size 超) AND 更新から min_age 超。シンボリックリンクは対象外。"""
    if path.is_symlink() or not path.is_file():
        return False
    st = path.stat()
    now = time.time() if now is None else now
    if now - st.st_mtime <= rules["min_age"]:
        return False
    ext = path.name.rsplit(".", 1)[-1].lower() if "." in path.name else ""
    by_ext = ext in rules["extensions"]
    by_size = rules["min_size"] is not None and st.st_size > rules["min_size"]
    return by_ext or by_size


def _case_dirs(ws: Workspace, case: str | None) -> list[Path]:
    if case:
        d = ws.cases_dir / case
        if not d.is_dir():
            raise RcloneError(f"no such case directory: {d}")
        return [d]
    if not ws.cases_dir.exists():
        return []
    return sorted(p for p in ws.cases_dir.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith("."))


def excluded_dir(name: str, exclude: list[str]) -> bool:
    """ディレクトリ名が rules.exclude の `<dir>/**` 形（target/** 等。先頭に / の無いものは任意の階層）に当たるか。"""
    return any(p.endswith("/**") and "/" not in p[:-3] and fnmatch.fnmatch(name, p[:-3]) for p in exclude)


def excluded_file(name: str, exclude: list[str]) -> bool:
    """ファイル名が rules.exclude のファイルパターン（*.o 等。`/**` で終わらないもの）に当たるか。"""
    return any(not p.endswith("/**") and fnmatch.fnmatch(name, p) for p in exclude)


def _walk_files(root: Path, exclude: list[str] = ()):
    """root 配下の通常ファイル（シンボリックリンクのディレクトリ・ファイルは辿らない。rules.exclude のディレクトリ・ファイルも除く）。"""
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(r, d)) and not excluded_dir(d, exclude)]
        for fn in files:
            p = Path(r) / fn
            if not p.is_symlink() and p.is_file() and not excluded_file(fn, exclude):
                yield p


def bag_candidates(root: Path, now: float | None = None, exclude: list[str] = ()) -> list[Path]:
    """圧縮対象: *.bag / *.bag.active のうち更新から 30 分以上経ったもの（.part / .zst 済みは除く）。
    rules.exclude（target/** 等）配下は対象にしない（同期もされないビルド産物を圧縮しない）。"""
    now = time.time() if now is None else now
    out = []
    for p in _walk_files(root, list(exclude)):
        if not (p.name.endswith(".bag") or p.name.endswith(".bag.active")):
            continue
        if (p.with_name(p.name + ".zst")).exists():
            continue
        if now - p.stat().st_mtime < BAG_MIN_AGE_SEC:
            continue
        out.append(p)
    return sorted(out)


def compress_bag(src: Path) -> Path:
    """zstd -T0 -6 で <name>.zst に圧縮（.part に書き、zstd -t で検証後 rename）。mtime を引き継ぎ、元を削除。"""
    if not shutil.which("zstd"):
        raise RuntimeError("zstd command not found")
    dst = src.with_name(src.name + ".zst")
    part = dst.with_name(dst.name + ".part")
    st = src.stat()
    try:
        r = subprocess.run(["zstd", "-T0", "-6", "-q", "-f", "-o", str(part), str(src)], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"zstd failed for {src}: {(r.stderr or r.stdout).strip()[-400:]}")
        t = subprocess.run(["zstd", "-t", "-q", str(part)], capture_output=True, text=True)
        if t.returncode != 0:
            raise RuntimeError(f"zstd -t failed for {part}: {(t.stderr or t.stdout).strip()[-400:]}")
        os.replace(part, dst)
    except BaseException:
        if part.exists():
            part.unlink()
        raise
    os.utime(dst, (st.st_atime, st.st_mtime))
    src.unlink()
    return dst


def bag2zst(conf: Config, ws: Workspace, case: str | None = None, dry: bool = False) -> dict:
    """ワークスペース（または 1 案件）内の bag を圧縮。rules.bag_to_zst が false なら何もしない。
    返り値: {enabled, dry, done: [{src, dst, bytes}], skipped: N, errors: [{src, error}]}"""
    out: dict = {"enabled": bool(conf.rules.get("bag_to_zst", True)), "dry": dry, "done": [], "errors": []}
    if not out["enabled"]:
        return out
    exclude = [str(x) for x in conf.rules.get("exclude", [])]
    for d in _case_dirs(ws, case):
        for src in bag_candidates(d, exclude=exclude):
            rel = str(src.relative_to(ws.cases_dir))
            if dry:
                out["done"].append({"src": rel, "dst": rel + ".zst", "bytes": src.stat().st_size, "dry": True})
                continue
            try:
                size = src.stat().st_size
                dst = compress_bag(src)
                out["done"].append({"src": rel, "dst": str(dst.relative_to(ws.cases_dir)), "bytes": size, "zst_bytes": dst.stat().st_size})
            except Exception as e:  # 1 件の失敗で残りを止めない
                out["errors"].append({"src": rel, "error": str(e)})
    return out


def _raw_filter_sets(conf: Config, rr: dict) -> list[list[str]]:
    """rclone に渡すフィルタ（OR を 2 回の呼び出しで表現）。両方に rules.exclude と --min-age を付ける。
    --include と --exclude の併用は rclone が「順序不定」と警告する（実測で除外が効かない）ため、
    順序が確定する --filter 規則（'- <exclude>' → '+ *.{ext}' → '- **'）で組む。"""
    excl: list[str] = []
    for pat in conf.rules.get("exclude", []):
        excl += ["--filter", f"- {pat}"]
    age = ["--min-age", rr["min_age_str"]] if rr["min_age_str"] else []
    sets = []
    if rr["extensions"]:
        sets.append(excl + ["--filter", "+ *.{" + ",".join(rr["extensions"]) + "}", "--filter", "- **"] + age)
    if rr["min_size_str"]:
        sets.append(excl + ["--min-size", rr["min_size_str"]] + age)
    return sets


def _lsf_local(src: Path, filt: list[str]) -> dict[str, int]:
    """rclone lsf（同じフィルタ）でローカル側の移動対象を列挙 → {相対パス: bytes}"""
    r = subprocess.run(["rclone", "lsf", "-R", "--files-only", "--format", "ps", "--separator", "\t", str(src), *filt],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RcloneError((r.stderr or r.stdout).strip()[-800:])
    out: dict[str, int] = {}
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        p, *rest = line.split("\t")
        try:
            out[p] = int(rest[0]) if rest else 0
        except ValueError:
            out[p] = 0
    return out


def _human(n: int) -> str:
    x = float(n)
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if x < 1024 or u == "TiB":
            return f"{x:.0f} {u}" if u == "B" else f"{x:.1f} {u}"
        x /= 1024
    return f"{n} B"


def _append_data_location(path: Path, line: str, title: str) -> None:
    """worklog.md の `## Data location` 節（無ければ末尾に作る）の末尾に 1 行追記。DATA.md（無ければ作る）にも使う。"""
    text = path.read_text(encoding="utf-8") if path.exists() else f"# {title}\n"
    lines = text.splitlines()
    head = next((i for i, l in enumerate(lines) if l.strip() == "## Data location"), None)
    if head is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines += ["## Data location", line]
    else:
        end = next((i for i in range(head + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        while end > head + 1 and not lines[end - 1].strip():
            end -= 1
        lines.insert(end, line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def record_data_location(ws: Workspace, case_dir: Path, drive: str, moved: dict[str, int], list_rel: str) -> dict:
    """移動結果を案件に記録: worklog.md があればその `## Data location` 節、無ければ DATA.md（case.json の有無に関わらず）。
    case.json があれば data[] と progress event にも記録する。"""
    n, b = len(moved), sum(moved.values())
    entry = {"drive": drive, "files": n, "bytes": b, "moved_at": now_iso(), "list": list_rel}
    line = (f"- {entry['moved_at'][:10]}: {n} file(s), {_human(b)} moved to `{drive}` (list: {list_rel}). "
            f"restore: `rclone copy {drive}<file> <local case dir>/`")
    store = CaseStore(ws.cases_dir)
    title = case_dir.name
    if (case_dir / "case.json").exists():
        case = store.load_case(case_dir.name)
        title = case.get("title", title)
        case.setdefault("data", []).append(entry)
        store.save_case(case)
        store.append_event(case_dir.name, {"actor": "kairn", "agent": "sync", "action": "progress",
                                           "note": f"raw_move: {n} file(s), {_human(b)} -> {drive}", "data": entry})
    if (case_dir / "worklog.md").exists():
        _append_data_location(case_dir / "worklog.md", line, title)
    else:
        _append_data_location(case_dir / "DATA.md", line, f"{case_dir.name} — data location")
    return entry


def raw_move(conf: Config, ws: Workspace, case: str | None = None, dry: bool = False) -> dict:
    """生データを Drive へ移動（rclone move。転送後にハッシュ照合してローカルを削除するのは rclone）。
    2 回目の move が失敗しても 1 回目で移動済みのファイルは記録してから error を付ける（記録漏れで所在不明にしない）。
    返り値: {dry, cases: {case: {planned: [...], moved: [...], bytes, drive, error?}}, files, bytes}"""
    rr = raw_rules(conf)
    sets = _raw_filter_sets(conf, rr)
    out: dict = {"dry": dry, "cases": {}, "files": 0, "bytes": 0}
    if not sets:
        return out
    today = _dt.date.today().strftime("%Y%m%d")
    list_rel = f"index/raw-moved-{today}.txt"
    for d in _case_dirs(ws, case):
        drive = conf.drive_path(ws.name, "cases", d.name) + "/"
        summary: dict = {"planned": [], "moved": [], "bytes": 0, "drive": drive}
        out["cases"][d.name] = summary
        try:
            planned: dict[str, int] = {}
            for filt in sets:
                for rel, size in _lsf_local(d, filt).items():
                    p = d / rel
                    if is_raw(p, rr):  # rclone の判定と一致することを確認（不一致は移動しない）
                        planned[rel] = size or p.stat().st_size
            summary["planned"] = sorted(planned)
            if not planned:
                continue
            # 2 回の move（拡張子パス／サイズパス）。途中で失敗しても、それまでに消えた（＝移動済みの）ファイルは必ず記録する
            failure: Exception | None = None
            for filt in sets:
                try:
                    _run(["rclone", "move", str(d), drive, "--fast-list", "--transfers", "4", "--stats-one-line", "-v",
                          *filt, *_bw(conf)], dry)
                except Exception as e:
                    failure = e
                    break
            if not dry:
                moved = {rel: size for rel, size in planned.items() if not (d / rel).exists()}
                summary["moved"] = sorted(moved)
                summary["bytes"] = sum(moved.values())
                if moved:
                    lst = ws.data_dir / list_rel
                    lst.parent.mkdir(parents=True, exist_ok=True)
                    with lst.open("a", encoding="utf-8") as fh:
                        for rel in sorted(moved):
                            fh.write(f"{d.name}/{rel}\t{moved[rel]}\t{drive}{rel}\n")
                    summary["record"] = record_data_location(ws, d, drive, moved, list_rel)
                    out["files"] += len(moved)
                    out["bytes"] += summary["bytes"]
            if failure is not None:
                raise failure
        except Exception as e:
            summary["error"] = str(e)
    return out


# ---------------------------------------------------------------------------
# 日次同期
# ---------------------------------------------------------------------------

def daily(conf: Config, ws: Workspace, dry: bool = False) -> dict:
    """bag2zst -> checkin -> raw_move -> drive_index -> index rebuild。各段の結果と例外を index/daily.log に追記し、
    失敗しても次段へ進む。dry では rclone に --dry-run を渡し、drive-index.txt と索引（kairn.sqlite）を書き換えない。
    返り値: {workspace, started, finished, dry, ok, steps: {name: {ok, result|error}}}"""
    from .index import Index
    ws.index_dir.mkdir(parents=True, exist_ok=True)
    log = ws.index_dir / "daily.log"
    steps = [
        ("bag2zst", lambda: bag2zst(conf, ws, dry=dry)),
        ("checkin", lambda: checkin(conf, ws, dry=dry)),
        ("raw_move", lambda: raw_move(conf, ws, dry=dry)),
        ("drive_index", lambda: str(drive_index(conf, ws, dry=dry)) + (" (dry-run: not written)" if dry else "")),
        ("index", lambda: "skipped (dry-run: index not rebuilt)" if dry else Index(ws.index_dir, ws.cases_dir).rebuild()),
    ]
    out: dict = {"workspace": ws.name, "started": now_iso(), "dry": dry, "ok": True, "steps": {}}

    def _log(msg: str) -> None:
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{now_iso()} {msg}\n")

    _log(f"daily start ws={ws.name} dry={dry}")
    for name, fn in steps:
        try:
            res = fn()
            out["steps"][name] = {"ok": True, "result": res}
            _log(f"{name}: ok {json.dumps(res, ensure_ascii=False, default=str)[:2000]}")
        except Exception as e:
            out["ok"] = False
            out["steps"][name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            _log(f"{name}: ERROR {type(e).__name__}: {e}\n{traceback.format_exc()}")
    out["finished"] = now_iso()
    _log(f"daily end ok={out['ok']}")
    return out
