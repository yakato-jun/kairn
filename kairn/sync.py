"""Drive 同期（rclone）。ワークスペース単位。remote は設定済みのものだけ使う。

- checkout(ws[, case]):  <remote>:<root>/<ws>/cases[/<case>] -> local（テキスト層のみ、削除は追従しない）。
                         events.jsonl は「新しい方で上書き」せず、Drive 版を取り寄せてローカル版と行の和集合にマージする
                         （merge_case_events → 他ファイルを rclone copy --update）
- checkin(ws[, case]):   local -> remote（テキスト層）。先に events.jsonl を同じくマージし、各案件の case.json に版マーカー
                         （rev = uuid4 / last_checkin_at / checked_in_from。store.mark_checkin）を書いてから転送する。
                         案件単位は rclone sync（削除・Drive 側の新しい版は _deleted/<日付>/ へ退避）、
                         ワークスペース全体（daily）は rclone copy（ローカルに無い案件ディレクトリを Drive から消さない。
                         上書きされる Drive 側の版は同じく _deleted/ へ）。転送後にワークスペースの manifest.json を更新する
                         （update_manifest: rclone cat → 当該案件のエントリを書き換え → rclone rcat）。転送に失敗したら
                         版マーカーは書く前の内容に戻す
- manifest.json:         <remote>:<root>/<ws>/manifest.json = {"cases": {"<case>": {"rev", "checked_in_at", "from"}}, "updated_at"}。
                         open_case（kairn/server.py）は rclone cat 1 回（MANIFEST_TIMEOUT_SEC）で当該案件の rev を見て、ローカルの
                         case.json.rev と同じなら取り寄せを省略する。直近に取得した内容は index/manifest.cache.json に置き、
                         list_cases / UI 一覧の印（drive_state）に使う。同時 checkin の競合は「後勝ち」（案件ごとの独立エントリなので
                         影響は当該案件のみ）
- checkin_job(ws, case, agent): MCP の checkin ジョブ本体（checkin → checkin event）。kairn/jobs.py のスレッドで走る
- merge_events(local_path, remote_lines): 行の文字列一致で重複除去した和集合を `t` で安定ソートし、内容が変わる時だけ書き戻す
- drive_index(ws):       remote 上の全ファイル一覧を index/drive-index.txt に保存
- bag2zst(ws[, case]):   *.bag / *.bag.active を zstd 圧縮（<name>.zst、mtime 引き継ぎ、元は削除）
- raw_move(ws[, case]):  生データ（rules.raw_data）を rclone move で Drive へ移動し、所在を case.json / worklog に記録
- daily(ws):             bag2zst -> checkin -> raw_move -> drive_index -> index rebuild（失敗しても次段へ。index/daily.log）

生データ判定は既存の _filters（テキスト層の除外）と同じ規則: (拡張子が raw_data.extensions に含まれる OR
サイズが min_size 超) AND 更新から min_age 超。rclone には include パスとサイズパスの 2 回に分けて渡す
（1 回の呼び出しでは --include と --min-size が AND になるため）。

checkout / checkin は progress コールバック（1 行ずつ）を受け取れる。渡すと _run は subprocess.Popen で rclone の出力を
行単位に読む（--stats 5s --stats-one-line の進捗行を含む）。MCP のジョブ（kairn/jobs.py）が最新行を進捗として保持する。
"""
from __future__ import annotations

import datetime as _dt
import fnmatch
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import traceback
from collections.abc import Callable
from pathlib import Path

from .config import Config, Workspace
from .store import CaseStore, _atomic_write, now_iso, parse_iso


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


ProgressFn = Callable[[str], None]
STATS_ARGS = ["--stats", "5s", "--stats-one-line"]  # 進捗行（Transferred: … , ETA …）を 5 秒ごとに stderr へ


def _run(cmd: list[str], dry: bool = False, progress: ProgressFn | None = None) -> subprocess.CompletedProcess:
    """rclone を実行する。progress を渡すと subprocess.Popen で stderr（stdout も合流）を行単位に読み、1 行ずつ progress(line) に
    流す（kairn/jobs.py が最新行を進捗として保持する）。progress 無しは従来どおり subprocess.run。終了コードが 0 / 9 以外なら RcloneError。"""
    if dry:
        cmd = cmd + ["--dry-run"]
    if progress is None:
        r = subprocess.run(cmd, capture_output=True, text=True)
    else:
        lines: list[str] = []
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as p:
            assert p.stdout is not None
            for line in p.stdout:
                lines.append(line)
                progress(line)
        r = subprocess.CompletedProcess(cmd, p.returncode, "", "".join(lines))
    if r.returncode not in (0, 9):  # 9 = nothing transferred with --error-on-no-transfer (not used) / keep simple
        raise RcloneError((r.stderr or r.stdout).strip()[-800:])
    return r


# ---------------------------------------------------------------------------
# events.jsonl のマージ（追記専用ログを複数環境で持ち寄る）
# ---------------------------------------------------------------------------

def _event_sort_key(line: str) -> float:
    """行の `t`（ISO 8601）→ epoch 秒。JSON でない／`t` が読めない行は 0（先頭に寄せる。安定ソートなので相対順は保つ）。"""
    try:
        t = parse_iso(json.loads(line).get("t", ""))
    except (ValueError, AttributeError):
        t = None
    return t.timestamp() if t else 0.0


def merge_events(local_path: Path, remote_lines: list[str]) -> list[str]:
    """ローカル版 events.jsonl と Drive 版の行（remote_lines）の和集合を作る。重複は行の文字列一致で 1 つにし、
    `t` で安定ソート（同時刻はローカルの行 → Drive にしか無い行の順）。内容が変わる時だけ local_path に書き戻す
    （変わらなければ mtime も触らない）。返り値: マージ後の行（改行なし）。"""
    local = [l for l in local_path.read_text(encoding="utf-8").splitlines() if l.strip()] if local_path.exists() else []
    seen = set(local)
    merged = list(local)
    for l in remote_lines:
        l = l.rstrip("\r\n")
        if l.strip() and l not in seen:
            seen.add(l)
            merged.append(l)
    merged.sort(key=_event_sort_key)
    if merged != local:
        _atomic_write(local_path, "".join(l + "\n" for l in merged))
    return merged


def fetch_remote_events(conf: Config, ws: Workspace, case: str) -> list[str] | None:
    """Drive 版 events.jsonl を一時ファイルに取り寄せ（rclone copyto）、行を返す。
    rclone が無い・remote 不達・Drive にその案件／ファイルが無い等で取得できなければ None（呼び出し側はマージを飛ばす）。"""
    src = conf.drive_path(ws.name, "cases", case, "events.jsonl")
    with tempfile.TemporaryDirectory(prefix="kairn-events-") as td:
        tmp = Path(td) / "events.jsonl"
        try:
            _run(["rclone", "copyto", src, str(tmp), *_bw(conf)])
        except (RcloneError, OSError):  # OSError: rclone コマンド不在
            return None
        if not tmp.exists():
            return None
        return [l for l in tmp.read_text(encoding="utf-8").splitlines() if l.strip()]


def merge_case_events(conf: Config, ws: Workspace, case: str) -> bool:
    """1 案件の events.jsonl を Drive 版とマージして書き戻す。取得できなければ何もせず False。"""
    remote = fetch_remote_events(conf, ws, case)
    if remote is None:
        return False
    merge_events(ws.cases_dir / case / "events.jsonl", remote)
    return True


# ---------------------------------------------------------------------------
# manifest.json（ワークスペース直下。案件ごとの版マーカー rev）
# ---------------------------------------------------------------------------

MANIFEST_NAME = "manifest.json"
MANIFEST_CACHE_NAME = "manifest.cache.json"
MANIFEST_TIMEOUT_SEC = 10   # open_case が rclone cat を待つ上限秒（越えたら manifest unavailable としてローカルを返す）


def manifest_drive_path(conf: Config, ws: Workspace) -> str:
    return conf.drive_path(ws.name, MANIFEST_NAME)


def parse_manifest(text: str) -> dict | None:
    """manifest.json の本文 → dict。JSON でない／`cases` が dict でないなら None（壊れた manifest は「無い」と同じ扱い）。"""
    try:
        m = json.loads(text)
    except ValueError:
        return None
    if not isinstance(m, dict) or not isinstance(m.get("cases"), dict):
        return None
    return m


def fetch_manifest(conf: Config, ws: Workspace, timeout: float = MANIFEST_TIMEOUT_SEC) -> dict | None:
    """Drive の manifest.json を rclone cat で 1 回読む。rclone 不在・タイムアウト・非ゼロ終了（未作成・オフライン）・
    JSON でない → None（呼び出し側は「manifest unavailable」として扱う）。"""
    try:
        r = subprocess.run(["rclone", "cat", manifest_drive_path(conf, ws)], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return parse_manifest(r.stdout)


def write_manifest(conf: Config, ws: Workspace, manifest: dict) -> None:
    """manifest.json を rclone rcat（stdin → Drive）で書き戻す。失敗は RcloneError。"""
    text = json.dumps(manifest, ensure_ascii=False, indent=1) + "\n"
    try:
        r = subprocess.run(["rclone", "rcat", manifest_drive_path(conf, ws)], input=text, capture_output=True, text=True)
    except OSError as e:  # rclone コマンド不在
        raise RcloneError(f"rclone rcat failed: {e}") from e
    if r.returncode != 0:
        raise RcloneError(f"rclone rcat {manifest_drive_path(conf, ws)} failed: {(r.stderr or r.stdout).strip()[-400:]}")


def save_manifest_cache(ws: Workspace, manifest: dict) -> Path:
    """直近に取得した manifest を index/manifest.cache.json に置く（fetched_at 付き。ローカルのみ、同期しない）。"""
    p = ws.index_dir / MANIFEST_CACHE_NAME
    _atomic_write(p, json.dumps({**manifest, "fetched_at": now_iso()}, ensure_ascii=False, indent=1) + "\n")
    return p


def load_manifest_cache(ws: Workspace) -> dict | None:
    """index/manifest.cache.json（無ければ None）。"""
    p = ws.index_dir / MANIFEST_CACHE_NAME
    if not p.exists():
        return None
    return parse_manifest(p.read_text(encoding="utf-8"))


def refresh_manifest(conf: Config, ws: Workspace, timeout: float = MANIFEST_TIMEOUT_SEC, cache: bool = True) -> dict | None:
    """manifest を取得し、取れたらキャッシュを更新して返す（取れなければ None。キャッシュは触らない）。"""
    m = fetch_manifest(conf, ws, timeout)
    if m is not None and cache:
        save_manifest_cache(ws, m)
    return m


def update_manifest(conf: Config, ws: Workspace, entries: dict[str, dict]) -> dict:
    """checkin 後: Drive の manifest を読み（取得できなければ新規作成）、渡された案件のエントリを書き換えて rcat で書き戻す。
    同時 checkin は「後勝ち」（他案件のエントリには触れないので影響は当該案件のみ）。キャッシュも更新する。"""
    m = fetch_manifest(conf, ws) or {"cases": {}}
    m["cases"].update(entries)
    m["updated_at"] = now_iso()
    write_manifest(conf, ws, m)
    save_manifest_cache(ws, m)
    return m


DRIVE_STATES = ("synced", "drive_newer", "local_changes", "unknown")


def drive_state(st: CaseStore, manifest: dict | None, case_id: str) -> dict:
    """list_cases / UI 一覧の印。manifest（通常はキャッシュ）と case.json を比べる:
    local_changes（last_checkin_at より新しいローカル変更がある。files に一覧。drive_differs は manifest の rev も違うか）、
    synced（rev が一致）、drive_newer（rev が違う＝Drive に別の版がある）、unknown（manifest が無い／案件のエントリが無い／未 checkin）。"""
    case = st.load_case(case_id)
    entry = (manifest or {}).get("cases", {}).get(case_id) if manifest else None
    entry = entry if isinstance(entry, dict) else None
    out: dict = {"rev": case.get("rev"), "drive_rev": entry.get("rev") if entry else None,
                 "checked_in_at": entry.get("checked_in_at") if entry else None, "from": entry.get("from") if entry else None}
    changed = st.local_changes_since_checkin(case_id)
    if changed:
        out.update(state="local_changes", files=changed, drive_differs=bool(entry) and entry.get("rev") != case.get("rev"))
    elif entry is None or not case.get("rev"):
        out["state"] = "unknown"
    elif entry.get("rev") == case["rev"]:
        out["state"] = "synced"
    else:
        out["state"] = "drive_newer"
    return out


def _local_case_dirs(ws: Workspace) -> list[str]:
    if not ws.cases_dir.exists():
        return []
    return sorted(p.name for p in ws.cases_dir.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith("."))


def checkout(conf: Config, ws: Workspace, case: str | None = None, dry: bool = False, progress: ProgressFn | None = None) -> str:
    """Drive → ローカル。events.jsonl は先に Drive 版を取り寄せてマージし（merge_case_events）、転送から除外する
    （--update の mtime 比較でマージ済みの行を失わないため）。他のファイルは rclone copy --update（ローカルの方が新しいファイルは
    上書きしない）。ワークスペース全体では、ローカルにある案件を先にマージ → 転送 → 転送で新しく現れた案件をマージする。
    取得できない案件（Drive に無い・rclone 不在）はマージを飛ばして従来どおり転送する。dry ではマージしない（ローカルを書かない）。
    progress は転送本体（rclone copy）の出力を行単位に受け取る（MCP のジョブが進捗として表示する。kairn/jobs.py）。"""
    src = conf.drive_path(ws.name, "cases", *( [case] if case else [] ))
    dst = ws.cases_dir / case if case else ws.cases_dir
    dst.mkdir(parents=True, exist_ok=True)
    merged: list[str] = []
    exclude: list[str] = []
    if case:
        if not dry and merge_case_events(conf, ws, case):
            merged.append(case)
            exclude = ["--exclude", "/events.jsonl"]
    elif not dry:
        before = _local_case_dirs(ws)
        merged += [c for c in before if merge_case_events(conf, ws, c)]
        exclude = ["--exclude", "/*/events.jsonl"]
    r = _run(["rclone", "copy", src, str(dst), "--update", "--fast-list", "--transfers", "8", *STATS_ARGS, "-v",
              *exclude, *_filters(conf), *_bw(conf)], dry, progress)
    if not case and not dry:
        merged += [c for c in _local_case_dirs(ws) if c not in before and merge_case_events(conf, ws, c)]
    msg = (r.stderr or r.stdout).strip()[-400:]
    return f"{msg} [events merged: {len(merged)}]" if merged else msg


def checkin(conf: Config, ws: Workspace, case: str | None = None, dry: bool = False, progress: ProgressFn | None = None) -> str:
    """ローカル → Drive。先に各案件の events.jsonl を Drive 版とマージしてから転送する（Drive にしか無い行を消さない）。
    転送の前に case.json へ版マーカー（rev / last_checkin_at / checked_in_from / last_checkin_events。store.mark_checkin）を書く
    （Drive に置く case.json に同じ rev が入る）。案件指定は必ず書く。ワークスペース全体（daily）は last_checkin_at より新しい
    ローカル変更がある案件と未 checkin の案件だけに書く（内容が変わっていない案件の rev を毎日変えて他環境に取り寄せさせない）。
    case 指定は `rclone sync`（案件内の削除を追従）、ワークスペース全体は `rclone copy`
    （ローカルに無い案件ディレクトリは消してよい＝Drive から削除しない。README 原則 2）。どちらも上書きされる Drive 側の版は
    `_deleted/<日付>/` に退避する（--backup-dir）。転送が失敗したら版マーカーは書く前の内容（mtime も）に戻す。
    転送後に manifest.json の当該案件のエントリを更新する（update_manifest。失敗は RcloneError: 転送は済んでいる）。
    dry ではマージも版マーカーも書かない。progress は転送本体の出力を行単位に受け取る（checkout と同じ）。"""
    src = ws.cases_dir / case if case else ws.cases_dir
    if not src.exists():
        raise RcloneError(f"nothing to check in: {src} does not exist")
    dst = conf.drive_path(ws.name, "cases", *( [case] if case else [] ))
    backup = conf.drive_path(ws.name, "_deleted", _dt.date.today().isoformat())
    verb = "sync" if case else "copy"
    merged = [] if dry else [c for c in ([case] if case else _local_case_dirs(ws)) if merge_case_events(conf, ws, c)]
    store = CaseStore(ws.cases_dir)
    stamped: dict[str, tuple[str, float]] = {}   # 版マーカーを書いた案件 → 書く前の case.json（内容, mtime）。転送失敗時に戻す
    if not dry:
        for cid in ([case] if case else store.list_case_ids()):
            f = ws.cases_dir / cid / "case.json"
            if not f.exists():
                continue
            if not case and store.local_changes_since_checkin(cid) == []:   # 変更なし（判定不能 None は書く）
                continue
            before = (f.read_text(encoding="utf-8"), f.stat().st_mtime)
            if store.mark_checkin(cid) is not None:
                stamped[cid] = before
    try:
        r = _run(["rclone", verb, str(src), dst, "--backup-dir", backup, "--fast-list", "--transfers", "8",
                  *STATS_ARGS, "-v", *_filters(conf), *_bw(conf)], dry, progress)
    except BaseException:
        for cid, (text, mtime) in stamped.items():
            f = ws.cases_dir / cid / "case.json"
            _atomic_write(f, text)
            os.utime(f, (mtime, mtime))
        raise
    msg = (r.stderr or r.stdout).strip()[-400:]
    if merged:
        msg = f"{msg} [events merged: {len(merged)}]"
    if stamped:
        entries = {cid: e for cid in stamped if (e := store.manifest_entry(cid)) is not None}
        try:
            update_manifest(conf, ws, entries)
        except RcloneError as e:
            raise RcloneError(f"transferred, but manifest update failed ({e}); the next checkin updates it. rclone: {msg}") from e
        msg = f"{msg} [manifest: {len(entries)}]"
    return msg


def checkin_job(conf: Config, ws: Workspace, case: str, agent: str, progress: ProgressFn | None = None) -> dict:
    """MCP の checkin ジョブ本体（kairn/jobs.py のスレッドで走る）: checkin(case) → checkin event の追記。
    checkin() が成功時に case.json.last_checkin_at / last_checkin_events を更新し、その後に event を 1 行足す
    （store.SYNC_EVENT_ACTIONS: この 1 行は open_case の skip 判定で変更に数えない）。返り値は従来の checkin ツールの結果。"""
    st = CaseStore(ws.cases_dir)
    st.load_case(case)
    msg = checkin(conf, ws, case, progress=progress)
    st.append_event(case, {"actor": "ai", "agent": agent, "action": "checkin", "note": msg[-200:]})
    return {"ok": True, "rclone": msg, "last_checkin_at": st.load_case(case).get("last_checkin_at")}


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


_BW_RATE = r"(?:off|\d+(?:\.\d+)?[bBkKmMgGtTpP]?)"
_BW_TOKEN = re.compile(rf"^(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)-)?(?:\d{{1,2}}:\d{{2}},)?{_BW_RATE}(?::{_BW_RATE})?$")


def parse_bwlimit(v) -> str:
    """rclone の --bwlimit 表記を検証して正規化（空白区切りを 1 つに）する: '4M'、'off'、'1M:2M'（上り:下り）、
    '08:00,4M 20:00,off'（時間帯別。曜日付き 'Sat-10:00,1M' も可）。不正なら ValueError。"""
    tokens = str(v).split()
    if not tokens:
        raise ValueError("invalid bwlimit: empty (expected e.g. 4M, off, or \"08:00,4M 20:00,off\")")
    for t in tokens:
        if not _BW_TOKEN.match(t):
            raise ValueError(f"invalid bwlimit token {t!r} (expected e.g. 4M, off, 1M:2M, 08:00,4M)")
    return " ".join(tokens)


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
