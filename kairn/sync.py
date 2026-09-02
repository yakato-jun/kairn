"""Drive 同期（rclone）。ワークスペース単位。remote は設定済みのものだけ使う。

- checkout(ws, case):    <remote>:<root>/<ws>/cases/<case> -> local（テキスト層のみ、削除は追従しない）。
                         events.jsonl は「新しい方で上書き」せず、Drive 版を取り寄せてローカル版と行の和集合にマージする
                         （merge_case_events → 他ファイルを rclone copy --update）
- checkout(ws):          ワークスペース全体。Drive の版マーカー（drive_revs: rclone lsf 1 回）を取り寄せ、rev がローカルと違う案件
                         （ローカルに無い案件・マーカーが不定の案件を含む）だけを 1 案件ずつ checkout する（checkout_workspace。
                         ワークスペース全体の rclone copy --update はしない）
- checkin(ws[, case]):   local -> remote（テキスト層）。先に events.jsonl を同じくマージし、各案件の case.json に版マーカー
                         （rev = uuid4 / last_checkin_at / checked_in_from。store.mark_checkin）を書き、転送直前に案件フォルダの
                         .rev/<rev>（空ファイル 1 個。store.write_rev_marker）を作り直してから転送する。
                         案件単位は rclone sync（削除・Drive 側の新しい版は _deleted/<日付>/ へ退避。Drive 側の古いマーカーも消える）、
                         ワークスペース全体（daily）は rclone copy（ローカルに無い案件ディレクトリを Drive から消さない。
                         上書きされる Drive 側の版は同じく _deleted/ へ）の後、rev を振り直した案件の .rev/ だけを rclone sync で
                         揃える（古いマーカーを消す。sync_rev_markers）。転送に失敗したら版マーカーは書く前の内容に戻す
- 版マーカー（.rev/）:    <remote>:<root>/<ws>/cases/<case>/.rev/<rev>（空ファイル）。集計ファイルは置かない。
                         drive_rev（1 案件: rclone lsf <case>/.rev/、REV_LSF_TIMEOUT_SEC）/ drive_revs（全案件: rclone lsf -R
                         --include '/cases/*/.rev/*' 1 プロセス）で名前だけを読む。マーカーが 2 個以上ある案件は「不定」（rev 不一致と同じ＝
                         取り寄せ対象）。open_case（kairn/server.py）は drive_rev で当該案件の rev を見て、ローカルの case.json.rev と
                         同じなら取り寄せを省略する。直近に得た版は index/drive_revs.cache.json に置き、list_cases / UI 一覧の印
                         （drive_state）に使う。既存の Drive 案件にマーカーを付けるのは kairn drive-markers <ws>（drive_markers）
- checkin_job(ws, case, agent): MCP の checkin ジョブ本体（checkin → checkin event）。kairn/jobs.py のスレッドで走る
- merge_events(local_path, remote_lines): 行の文字列一致で重複除去した和集合を `t` で安定ソートし、内容が変わる時だけ書き戻す
- drive_index(ws):       remote 上の全ファイル一覧を index/drive-index.txt に保存
- bag2zst(ws[, case]):   *.bag / *.bag.active を zstd 圧縮（<name>.zst、mtime 引き継ぎ、元は削除）
- raw_move(ws[, case]):  生データ（rules.raw_data）を rclone move で Drive へ移動し、所在を case.json / worklog に記録
- daily(ws):             bag2zst -> checkout(ws) -> checkin -> raw_move -> drive_index -> index rebuild（失敗しても次段へ。index/daily.log）

作業領域（案件フォルダ内の git worktree 等）: ディレクトリ直下に .git ファイル（worktree）または .kairn-nosync（空ファイル）が
あるディレクトリは配下ごと同期（checkout / checkin）・退避（raw_move）・bag2zst の対象外（WORKAREA_MARKERS / is_workarea）。
rclone にはローカル側の転送ルートを走査して（workarea_dirs）`--filter '- /<dir>/**'` を _filters / _raw_filter_sets の先頭に付ける
（--exclude-if-present は rclone 1.70 で同名ディレクトリがあると転送全体が失敗するため使わない）。通常の clone の .git ディレクトリは
rules.exclude の既定 `.git/**` で除く（ソース本体は同期される）。

生データ判定は既存の _filters（テキスト層の除外）と同じ規則: (拡張子が raw_data.extensions に含まれる OR
サイズが min_size 超) AND 更新から min_age 超。rclone には include パスとサイズパスの 2 回に分けて渡す
（1 回の呼び出しでは --include と --min-size が AND になるため）。

rules.rclone_flags（既定は空。`kairn rules set rclone_flags "--transfers 8 …"`）は rclone を呼ぶすべての箇所（checkout / checkin /
raw_move / drive_index / 版マーカーの lsf・sync / drive-markers の copy・sync・deletefile / events の copyto / ws の lsd・mkdir・lsf）で
共通引数の後ろに付ける（_flags）。

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
from .store import REV_DIR, CaseStore, _atomic_write, now_iso, parse_iso, validate_case_id


class RcloneError(RuntimeError):
    pass


REV_KEEP_FILTER = f"+ {REV_DIR}/**"   # 版マーカー（<case>/.rev/<rev>）は rules.exclude / raw_data の除外より先に必ず含める

# 作業領域の目印。ディレクトリ直下にこの名前の「ファイル」があれば、そのディレクトリは配下ごと同期・退避・索引の対象外
# （git worktree の .git はファイル。.kairn-nosync は git 以外の作業領域用の空ファイル）。通常の clone（.git がディレクトリ）は
# 当たらず、rules.exclude の既定 `.git/**` で .git だけが除かれる
WORKAREA_MARKERS = (".git", ".kairn-nosync")


def is_workarea(d: Path) -> bool:
    """d の直下に WORKAREA_MARKERS のいずれかがファイルとしてあるか（ディレクトリの .git は当たらない）。"""
    return any((d / m).is_file() for m in WORKAREA_MARKERS)


def workarea_dirs(root: Path, exclude: list[str] = ()) -> list[str]:
    """root 配下の作業領域ディレクトリの相対パス（'/' 区切り。root 自身が作業領域なら ['']）。見つけた作業領域の配下・
    シンボリックリンク・rules.exclude のディレクトリ（target/** 等）は辿らない。root が無ければ []。"""
    if not root.is_dir():
        return []
    if is_workarea(root):
        return [""]
    out: list[str] = []
    for r, dirs, _files in os.walk(root):
        keep = []
        for d in sorted(dirs):
            p = Path(r) / d
            if p.is_symlink() or excluded_dir(d, exclude):
                continue
            if is_workarea(p):
                out.append(p.relative_to(root).as_posix())
                continue
            keep.append(d)
        dirs[:] = keep
    return sorted(out)


def _workarea_filters(conf: Config, root: Path | None) -> list[str]:
    """rclone に渡す作業領域の除外: 転送元／先のローカル root を走査し（workarea_dirs）、見つけた各ディレクトリを
    `--filter '- /<rel>/**'`（root からの絶対パターン）で配下ごと除く。root が None なら []。
    rclone の --exclude-if-present は使わない: rclone 1.70 では目印と同名のディレクトリ（通常の clone の .git/）が
    ツリー内に 1 つでもあると「is a directory not a file」で転送全体が失敗する。"""
    if root is None:
        return []
    out: list[str] = []
    for rel in workarea_dirs(root, [str(x) for x in conf.rules.get("exclude", [])]):
        out += ["--filter", f"- /{rel}/**" if rel else "- /**"]
    return out


def _filters(conf: Config, root: Path | None = None) -> list[str]:
    """テキスト層の転送（checkout / checkin）のフィルタ: 先頭に作業領域の除外（_workarea_filters。root = ローカル側の転送ルート）、
    版マーカーの保護（REV_KEEP_FILTER）、続いて rules.exclude と raw_data.extensions の除外を rclone の `--filter` 規則
    （順序が確定する）で渡し、min_size は --max-size。最後の規則が除外なので、どの規則にも当たらないファイルは含まれる（rclone の既定）。"""
    args: list[str] = _workarea_filters(conf, root) + ["--filter", REV_KEEP_FILTER]
    for pat in conf.rules.get("exclude", []):
        args += ["--filter", f"- {pat}"]
    raw = conf.rules.get("raw_data", {})
    for ext in raw.get("extensions", []):
        args += ["--filter", f"- *.{ext}"]
    if raw.get("min_size"):
        args += ["--max-size", str(raw["min_size"])]
    return args


def _bw(conf: Config) -> list[str]:
    """帯域制限（任意）。rules.bwlimit をそのまま rclone の --bwlimit に渡す。"""
    bw = conf.rules.get("bwlimit")
    return ["--bwlimit", str(bw)] if bw else []


def parse_rclone_flags(v) -> list[str]:
    """rules.rclone_flags の検証: 空白区切りの文字列（またはリスト）→ トークンのリスト。各オプションは `--` で始まるトークンで、
    その直後に値を 1 つだけ置ける（'--transfers 8 --checkers 16 --drive-pacer-min-sleep 10ms' / '--transfers=8'）。
    先頭が `--` でないトークン（'-v'、値の連続、先頭の値）は拒否（ValueError）。空は []。"""
    tokens = [str(t) for t in v] if isinstance(v, (list, tuple)) else str(v).split()
    out: list[str] = []
    prev_flag = False   # 直前のトークンが `--` で始まるオプションだった（次のトークンは値でよい）
    for t in tokens:
        if t.startswith("--"):
            if t == "--":
                raise ValueError("invalid rclone flag '--' (expected e.g. --transfers 8 or --transfers=8)")
            prev_flag = "=" not in t
        elif t.startswith("-") or not prev_flag:
            raise ValueError(f"invalid rclone flag token {t!r} (each option must start with -- and take at most one value: --transfers 8)")
        else:
            prev_flag = False
        out.append(t)
    return out


def _flags(conf: Config) -> list[str]:
    """rules.rclone_flags（既定は空）。rclone を呼ぶすべての箇所で共通引数の後ろに付ける（同じオプションは後ろが勝つ）。"""
    return parse_rclone_flags(conf.rules.get("rclone_flags") or [])


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
            _run(["rclone", "copyto", src, str(tmp), *_bw(conf), *_flags(conf)])
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
# 版マーカー（cases/<case>/.rev/<rev>。集計ファイルは置かない）
# ---------------------------------------------------------------------------

DRIVE_REVS_CACHE_NAME = "drive_revs.cache.json"
REV_LSF_TIMEOUT_SEC = 10      # open_case が 1 案件の rclone lsf を待つ上限秒（越えたら drive unavailable としてローカルを返す）
DRIVE_REVS_TIMEOUT_SEC = 120  # 全案件の rclone lsf -R を待つ上限秒（checkout <ws> / UI の更新確認 / daily）


def rev_dir_path(conf: Config, ws: Workspace, case: str) -> str:
    return conf.drive_path(ws.name, "cases", case, REV_DIR)


def parse_rev_listing(lines: list[str]) -> dict[str, str | None]:
    """`rclone lsf -R --files-only --include '/cases/*/.rev/*' <ws>` の出力（cases/<case>/.rev/<rev> 形の行）→ {case: rev}。
    マーカーが 2 個以上ある案件は None（不定＝取り寄せ対象）。形の違う行・不正な案件 id は無視。"""
    seen: dict[str, list[str]] = {}
    for line in lines:
        parts = line.strip().split("/")
        if len(parts) != 4 or parts[0] != "cases" or parts[2] != REV_DIR or not parts[3]:
            continue
        try:
            validate_case_id(parts[1])
        except ValueError:
            continue
        seen.setdefault(parts[1], []).append(parts[3])
    return {cid: (revs[0] if len(revs) == 1 else None) for cid, revs in seen.items()}


def drive_revs(conf: Config, ws: Workspace, timeout: float = DRIVE_REVS_TIMEOUT_SEC) -> dict[str, str | None] | None:
    """ワークスペース全案件の Drive 側の版: rclone lsf -R（1 プロセス）で cases/*/.rev/* の名前だけを読む → {case: rev | None(不定)}。
    マーカーの無い案件は載らない。rclone 不在・タイムアウト・非ゼロ終了（オフライン等）→ None（呼び出し側は「drive unavailable」）。"""
    try:
        r = subprocess.run(["rclone", "lsf", "-R", "--files-only", "--include", f"/cases/*/{REV_DIR}/*", conf.drive_path(ws.name), *_flags(conf)],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return parse_rev_listing(r.stdout.splitlines())


def drive_rev(conf: Config, ws: Workspace, case: str, timeout: float = REV_LSF_TIMEOUT_SEC) -> dict:
    """1 案件の Drive 側の版: rclone lsf <ws>/cases/<case>/.rev/ の名前を読む。
    返り値: {available, rev, markers}。available=False は rclone 不在・タイムアウト・（ディレクトリ不在以外の）失敗（Drive の状態は不明）。
    .rev/ が無い（終了コード 3: directory not found）は available=True・markers=[]（マーカー無し＝取り寄せ対象）。
    markers が 2 個以上なら rev は None（不定＝取り寄せ対象）。"""
    try:
        r = subprocess.run(["rclone", "lsf", rev_dir_path(conf, ws, case) + "/", *_flags(conf)], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"available": False, "rev": None, "markers": [], "error": f"{type(e).__name__}: {e}"[:200]}
    if r.returncode == 3:
        return {"available": True, "rev": None, "markers": []}
    if r.returncode != 0:
        return {"available": False, "rev": None, "markers": [], "error": (r.stderr or r.stdout).strip()[-200:]}
    markers = sorted(x.strip().rstrip("/") for x in r.stdout.splitlines() if x.strip() and not x.strip().endswith("/"))
    return {"available": True, "rev": markers[0] if len(markers) == 1 else None, "markers": markers}


def load_drive_revs_cache(ws: Workspace) -> dict | None:
    """index/drive_revs.cache.json = {"revs": {case: rev | None}, "fetched_at"}（無い・壊れていれば None）。"""
    p = ws.index_dir / DRIVE_REVS_CACHE_NAME
    if not p.exists():
        return None
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None
    if not isinstance(m, dict) or not isinstance(m.get("revs"), dict):
        return None
    return m


def save_drive_revs_cache(ws: Workspace, revs: dict[str, str | None]) -> Path:
    """全案件の版（drive_revs の結果）を index/drive_revs.cache.json に置く（fetched_at 付き。ローカルのみ、同期しない）。"""
    p = ws.index_dir / DRIVE_REVS_CACHE_NAME
    _atomic_write(p, json.dumps({"revs": revs, "fetched_at": now_iso()}, ensure_ascii=False, indent=1) + "\n")
    return p


def update_drive_revs_cache(ws: Workspace, revs: dict[str, str | None], remove: list[str] = ()) -> Path:
    """キャッシュの一部の案件だけを更新する（open_case の 1 案件の照会、checkin の完了）。fetched_at（全案件を読んだ時刻）は変えない
    （無ければ null）。remove の案件はエントリを消す（Drive にマーカーが無い＝不明）。"""
    cache = load_drive_revs_cache(ws) or {"revs": {}, "fetched_at": None}
    merged = {**cache["revs"], **revs}
    for cid in remove:
        merged.pop(cid, None)
    p = ws.index_dir / DRIVE_REVS_CACHE_NAME
    _atomic_write(p, json.dumps({"revs": merged, "fetched_at": cache.get("fetched_at")}, ensure_ascii=False, indent=1) + "\n")
    return p


def refresh_drive_revs(conf: Config, ws: Workspace, timeout: float = DRIVE_REVS_TIMEOUT_SEC, cache: bool = True) -> dict[str, str | None] | None:
    """全案件の版を読み、取れたらキャッシュを更新して返す（取れなければ None。キャッシュは触らない）。"""
    revs = drive_revs(conf, ws, timeout)
    if revs is not None and cache:
        save_drive_revs_cache(ws, revs)
    return revs


DRIVE_STATES = ("synced", "drive_newer", "local_changes", "unknown")


def drive_state(st: CaseStore, revs: dict[str, str | None] | None, case_id: str) -> dict:
    """list_cases / UI 一覧の印。Drive 側の版（通常はキャッシュの revs）と case.json を比べる:
    local_changes（last_checkin_at より新しいローカル変更がある。files に一覧。drive_differs は Drive の rev も違うか）、
    synced（rev が一致）、drive_newer（rev が違う、またはマーカーが不定＝Drive に別の版がある）、
    unknown（revs が無い／案件のマーカーが無い／未 checkin）。checked_in_at / from はローカル case.json の last_checkin_at / checked_in_from。"""
    case = st.load_case(case_id)
    known = revs is not None and case_id in revs
    drive = revs.get(case_id) if known else None
    out: dict = {"rev": case.get("rev"), "drive_rev": drive,
                 "checked_in_at": case.get("last_checkin_at"), "from": case.get("checked_in_from")}
    if known and drive is None:
        out["ambiguous"] = True   # マーカーが 2 個以上
    changed = st.local_changes_since_checkin(case_id)
    if changed:
        out.update(state="local_changes", files=changed, drive_differs=known and drive != case.get("rev"))
    elif not known or not case.get("rev"):
        out["state"] = "unknown"
    elif drive == case["rev"]:
        out["state"] = "synced"
    else:
        out["state"] = "drive_newer"
    return out


def _rev_sync_filters(case_ids: list[str]) -> list[str]:
    """ワークスペース全体の rclone sync を、指定した案件の .rev/ だけに限定するフィルタ（他の案件・他のファイルには触れない）。"""
    args: list[str] = []
    for cid in case_ids:
        args += ["--filter", f"+ /{cid}/{REV_DIR}/**"]
    return args + ["--filter", "- **"]


def sync_rev_markers(conf: Config, ws: Workspace, case_ids: list[str], dry: bool = False) -> None:
    """ワークスペース全体の checkin（rclone copy）の後: rev を振り直した案件の .rev/ を rclone sync（1 プロセス。当該案件の .rev/ だけに
    限定したフィルタ）で Drive と揃え、古いマーカーを消す。copy は Drive 側の古いマーカーを消さないため。"""
    if not case_ids:
        return
    _run(["rclone", "sync", str(ws.cases_dir), conf.drive_path(ws.name, "cases"), *_rev_sync_filters(sorted(case_ids)), *_bw(conf), *_flags(conf)], dry)


MANIFEST_NAME = "manifest.json"   # 旧方式の集計ファイル（もう読み書きしない。kairn drive-markers --remove-manifest が消すだけ）


def drive_markers(conf: Config, ws: Workspace, dry: bool = False, remove_manifest: bool = False) -> dict:
    """既存の Drive 案件に版マーカーを付ける移行（kairn drive-markers <ws>）:
    1. Drive の cases/*/case.json を rclone copy --include（1 プロセス）で一時ディレクトリへ取り寄せる
    2. 各案件の Drive 側 rev をローカル case.json の rev と比べ、一致した案件だけ一時ディレクトリに cases/<case>/.rev/<rev> を作る
    3. それらを rclone sync（1 プロセス。当該案件の .rev/ だけに限定したフィルタ）で Drive へ置く（古いマーカーがあれば消える）
    不一致（mismatch）・Drive 側 rev 無し（drive_no_rev）・ローカル無し（local_absent）・読めない（errors）は報告するだけで触らない。
    remove_manifest なら旧方式の <ws>/manifest.json を rclone deletefile で消す（無ければ無視）。dry では 1 だけ行い何も書かない。
    返り値: {dry, marked: {case: rev}, mismatch: {case: {drive, local}}, drive_no_rev: [case], local_absent: [case], errors: {case: reason},
             manifest_removed: bool | None}"""
    st = CaseStore(ws.cases_dir)
    out: dict = {"dry": dry, "marked": {}, "mismatch": {}, "drive_no_rev": [], "local_absent": [], "errors": {}, "manifest_removed": None}
    with tempfile.TemporaryDirectory(prefix="kairn-markers-") as td:
        tmp = Path(td)
        r = subprocess.run(["rclone", "copy", conf.drive_path(ws.name), str(tmp / "drive"), "--include", "/cases/*/case.json", *_flags(conf)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RcloneError((r.stderr or r.stdout).strip()[-800:])
        cases_dir = tmp / "drive" / "cases"
        stage = tmp / "stage" / "cases"
        for f in sorted(cases_dir.glob("*/case.json")) if cases_dir.is_dir() else []:
            cid = f.parent.name
            try:
                validate_case_id(cid)
                remote = json.loads(f.read_text(encoding="utf-8"))
                if not isinstance(remote, dict):
                    raise ValueError("case.json is not an object")
            except ValueError as e:
                out["errors"][cid] = str(e)
                continue
            drive = remote.get("rev")
            if not drive:
                out["drive_no_rev"].append(cid)
                continue
            if not (ws.cases_dir / cid / "case.json").exists():
                out["local_absent"].append(cid)
                continue
            local = st.load_case(cid).get("rev")
            if local != drive:
                out["mismatch"][cid] = {"drive": drive, "local": local}
                continue
            out["marked"][cid] = drive
            if not dry:
                (stage / cid / REV_DIR).mkdir(parents=True, exist_ok=True)
                (stage / cid / REV_DIR / drive).touch()
        if not dry and out["marked"]:
            _run(["rclone", "sync", str(stage), conf.drive_path(ws.name, "cases"), *_rev_sync_filters(sorted(out["marked"])), *_flags(conf)])
    if remove_manifest:
        out["manifest_removed"] = False
        if not dry:
            r = subprocess.run(["rclone", "deletefile", conf.drive_path(ws.name, MANIFEST_NAME), *_flags(conf)], capture_output=True, text=True)
            out["manifest_removed"] = r.returncode == 0
            if r.returncode not in (0, 3, 4):
                raise RcloneError(f"rclone deletefile {conf.drive_path(ws.name, MANIFEST_NAME)} failed: {(r.stderr or r.stdout).strip()[-400:]}")
    return out


def _local_case_dirs(ws: Workspace) -> list[str]:
    if not ws.cases_dir.exists():
        return []
    return sorted(p.name for p in ws.cases_dir.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith("."))


def checkout(conf: Config, ws: Workspace, case: str | None = None, dry: bool = False, progress: ProgressFn | None = None) -> str:
    """Drive → ローカル（1 案件）。events.jsonl は先に Drive 版を取り寄せてマージし（merge_case_events）、転送から除外する
    （--update の mtime 比較でマージ済みの行を失わないため）。他のファイルは rclone copy --update（ローカルの方が新しいファイルは
    上書きしない）。取得できない案件（Drive に無い・rclone 不在）はマージを飛ばして従来どおり転送する。dry ではマージしない（ローカルを書かない）。
    progress は転送本体（rclone copy）の出力を行単位に受け取る（MCP のジョブが進捗として表示する。kairn/jobs.py）。
    転送後に .rev/ を case.json の rev から作り直す（Drive から来た新しいマーカーと並んだ古いマーカーを残さない）。
    case を省略するとワークスペース全体（checkout_workspace: Drive の版がローカルと違う案件だけを 1 案件ずつ）。"""
    if not case:
        return checkout_workspace(conf, ws, dry, progress)
    src = conf.drive_path(ws.name, "cases", case)
    dst = ws.cases_dir / case
    dst.mkdir(parents=True, exist_ok=True)
    merged = False
    exclude: list[str] = []
    if not dry and merge_case_events(conf, ws, case):
        merged = True
        exclude = ["--filter", "- /events.jsonl"]
    r = _run(["rclone", "copy", src, str(dst), "--update", "--fast-list", "--transfers", "8", *STATS_ARGS, "-v",
              *exclude, *_filters(conf, dst), *_bw(conf), *_flags(conf)], dry, progress)
    if not dry:
        CaseStore(ws.cases_dir).write_rev_marker(case)
    msg = (r.stderr or r.stdout).strip()[-400:]
    return f"{msg} [events merged: 1]" if merged else msg


def checkout_workspace(conf: Config, ws: Workspace, dry: bool = False, progress: ProgressFn | None = None) -> str:
    """ワークスペース全体の checkout（kairn checkout <ws> / UI の更新確認 / daily）: Drive の版マーカーを rclone lsf 1 回で読み
    （キャッシュ更新。dry では更新しない）、マーカーのある案件のうち rev がローカルの case.json.rev と違うもの（ローカルに無い案件・
    マーカーが不定の案件を含む）だけを checkout(case) する。last_checkin_at より新しいローカル変更がある案件は取り寄せない（skipped）。
    版が読めなければ RcloneError（オフライン等）。1 案件の失敗は残りを止めず、最後にまとめて RcloneError。"""
    revs = refresh_drive_revs(conf, ws, cache=not dry)
    if revs is None:
        raise RcloneError(f"drive unavailable: could not list {conf.drive_path(ws.name, 'cases', '*', REV_DIR)} (offline, or rclone failed)")
    st = CaseStore(ws.cases_dir)
    fetched: list[str] = []; same: list[str] = []; skipped: list[str] = []; errors: list[str] = []
    for cid, rev in sorted(revs.items()):
        if (ws.cases_dir / cid / "case.json").exists():
            if st.local_changes_since_checkin(cid):
                skipped.append(cid)
                continue
            if rev is not None and st.load_case(cid).get("rev") == rev:
                same.append(cid)
                continue
        try:
            checkout(conf, ws, cid, dry, progress)
            fetched.append(cid)
        except (RcloneError, OSError) as e:
            errors.append(f"{cid}: {e}")
    msg = (f"drive: {len(revs)} case(s); {'would fetch' if dry else 'fetched'} {len(fetched)}"
           f"{' (' + ', '.join(fetched) + ')' if fetched else ''}, up to date {len(same)}, "
           f"skipped (local changes newer than last checkin) {len(skipped)}{' (' + ', '.join(skipped) + ')' if skipped else ''}")
    if errors:
        raise RcloneError(msg + "; errors: " + "; ".join(errors))
    return msg


def checkin(conf: Config, ws: Workspace, case: str | None = None, dry: bool = False, progress: ProgressFn | None = None) -> str:
    """ローカル → Drive。先に各案件の events.jsonl を Drive 版とマージしてから転送する（Drive にしか無い行を消さない）。
    転送の前に case.json へ版マーカー（rev / last_checkin_at / checked_in_from / last_checkin_events。store.mark_checkin）を書く
    （Drive に置く case.json に同じ rev が入る）。案件指定は必ず書く。ワークスペース全体（daily）は last_checkin_at より新しい
    ローカル変更がある案件と未 checkin の案件だけに書く（内容が変わっていない案件の rev を毎日変えて他環境に取り寄せさせない）。
    転送直前に対象の全案件の .rev/ を case.json の rev から作り直す（store.write_rev_marker。マーカーの欠けた案件も揃う）。
    case 指定は `rclone sync`（案件内の削除を追従。Drive 側の古いマーカーも消える）、ワークスペース全体は `rclone copy`
    （ローカルに無い案件ディレクトリは消してよい＝Drive から削除しない。README 原則 2）の後、rev を振り直した案件の .rev/ だけを
    rclone sync で揃える（sync_rev_markers）。どちらも上書きされる Drive 側の版は `_deleted/<日付>/` に退避する（--backup-dir）。
    転送が失敗したら版マーカーは書く前の内容（mtime も）に戻し、.rev/ もそれに合わせる。
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
                store.write_rev_marker(cid)   # rev はそのまま。マーカーだけ揃える
                continue
            before = (f.read_text(encoding="utf-8"), f.stat().st_mtime)
            if store.mark_checkin(cid) is not None:   # mark_checkin が .rev/ も作り直す
                stamped[cid] = before
    try:
        r = _run(["rclone", verb, str(src), dst, "--backup-dir", backup, "--fast-list", "--transfers", "8",
                  *STATS_ARGS, "-v", *_filters(conf, src), *_bw(conf), *_flags(conf)], dry, progress)
        if not case:
            sync_rev_markers(conf, ws, list(stamped), dry)
    except BaseException:
        for cid, (text, mtime) in stamped.items():
            f = ws.cases_dir / cid / "case.json"
            _atomic_write(f, text)
            os.utime(f, (mtime, mtime))
            store.write_rev_marker(cid)
        raise
    msg = (r.stderr or r.stdout).strip()[-400:]
    if merged:
        msg = f"{msg} [events merged: {len(merged)}]"
    if stamped:
        update_drive_revs_cache(ws, {cid: store.load_case(cid).get("rev") for cid in stamped})
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
    r = _run(["rclone", "lsf", "-R", "--files-only", "--format", "pst", "--separator", "\t", "--fast-list", conf.drive_path(ws.name), *_flags(conf)])
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
    r = subprocess.run(["rclone", "lsd", conf.drive_path(ws_name), *_flags(conf)], capture_output=True, text=True)
    return r.returncode == 0


def create_ws_on_drive(conf: Config, ws_name: str) -> None:
    _run(["rclone", "mkdir", conf.drive_path(ws_name, "cases"), *_flags(conf)])


def list_ws_on_drive(conf: Config) -> list[str]:
    r = subprocess.run(["rclone", "lsf", "--dirs-only", f"{conf.remote}:{conf.drive_root}", *_flags(conf)], capture_output=True, text=True)
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
    """root 配下の通常ファイル（シンボリックリンクのディレクトリ・ファイルは辿らない。rules.exclude のディレクトリ・ファイル、
    作業領域（is_workarea: 直下に .git ファイル / .kairn-nosync）の配下も除く）。"""
    if is_workarea(root):
        return
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(r, d)) and not excluded_dir(d, exclude)
                   and not is_workarea(Path(r) / d)]
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


def _raw_filter_sets(conf: Config, rr: dict, root: Path | None = None) -> list[list[str]]:
    """rclone に渡すフィルタ（OR を 2 回の呼び出しで表現）。両方に作業領域の除外（_workarea_filters。root = 案件ディレクトリ）・
    .rev/ の除外・rules.exclude と --min-age を付ける。
    --include と --exclude の併用は rclone が「順序不定」と警告する（実測で除外が効かない）ため、
    順序が確定する --filter 規則（'- <exclude>' → '+ *.{ext}' → '- **'）で組む。"""
    excl: list[str] = [*_workarea_filters(conf, root), "--filter", f"- {REV_DIR}/**"]   # 作業領域と版マーカーは生データとして移動しない
    for pat in conf.rules.get("exclude", []):
        excl += ["--filter", f"- {pat}"]
    age = ["--min-age", rr["min_age_str"]] if rr["min_age_str"] else []
    sets = []
    if rr["extensions"]:
        sets.append(excl + ["--filter", "+ *.{" + ",".join(rr["extensions"]) + "}", "--filter", "- **"] + age)
    if rr["min_size_str"]:
        sets.append(excl + ["--min-size", rr["min_size_str"]] + age)
    return sets


def _lsf_local(src: Path, filt: list[str], flags: list[str] = ()) -> dict[str, int]:
    """rclone lsf（同じフィルタ）でローカル側の移動対象を列挙 → {相対パス: bytes}。flags は rules.rclone_flags（move と同じものを付ける）。"""
    r = subprocess.run(["rclone", "lsf", "-R", "--files-only", "--format", "ps", "--separator", "\t", str(src), *filt, *flags],
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
    out: dict = {"dry": dry, "cases": {}, "files": 0, "bytes": 0}
    if not _raw_filter_sets(conf, rr):
        return out
    today = _dt.date.today().strftime("%Y%m%d")
    list_rel = f"index/raw-moved-{today}.txt"
    for d in _case_dirs(ws, case):
        drive = conf.drive_path(ws.name, "cases", d.name) + "/"
        summary: dict = {"planned": [], "moved": [], "bytes": 0, "drive": drive}
        out["cases"][d.name] = summary
        try:
            sets = _raw_filter_sets(conf, rr, d)
            planned: dict[str, int] = {}
            for filt in sets:
                for rel, size in _lsf_local(d, filt, _flags(conf)).items():
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
                          *filt, *_bw(conf), *_flags(conf)], dry)
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
    """bag2zst -> checkout（Drive の版を rclone lsf 1 回で読み、違う案件だけ取り寄せる: checkout_workspace）-> checkin -> raw_move
    -> drive_index -> index rebuild。各段の結果と例外を index/daily.log に追記し、失敗しても次段へ進む。
    dry では rclone に --dry-run を渡し、drive-index.txt と索引（kairn.sqlite）を書き換えない。
    返り値: {workspace, started, finished, dry, ok, steps: {name: {ok, result|error}}}"""
    from .index import Index
    ws.index_dir.mkdir(parents=True, exist_ok=True)
    log = ws.index_dir / "daily.log"
    steps = [
        ("bag2zst", lambda: bag2zst(conf, ws, dry=dry)),
        ("checkout", lambda: checkout_workspace(conf, ws, dry=dry)),
        ("checkin", lambda: checkin(conf, ws, dry=dry)),
        ("raw_move", lambda: raw_move(conf, ws, dry=dry)),
        ("drive_index", lambda: str(drive_index(conf, ws, dry=dry)) + (" (dry-run: not written)" if dry else "")),
        ("index", lambda: "skipped (dry-run: index not rebuilt)" if dry else Index(ws.index_dir, ws.cases_dir, conf.rules.get("exclude")).rebuild()),
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
