"""sync（段階 5）: raw 判定・bag2zst・rclone コマンド組み立て・Data location 記録・daily。
rclone / zstd は subprocess.run を monkeypatch して引数を記録する（クラウドには接続しない）。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from kairn import sync
from kairn.store import CaseStore

DAY = 86400


def _touch(p: Path, size: int = 10, age_sec: float = 0) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    t = time.time() - age_sec
    os.utime(p, (t, t))
    return p


class FakeRun:
    """subprocess.run の代役。rclone: 引数を記録し、lsf はローカルを自前で列挙、move は対象ファイルを削除して成功を返す。"""

    def __init__(self, rules, fail_move: bool = False, fail: set[str] | None = None, fail_move_nth: int | None = None,
                 remote_events: dict[str, list[str]] | None = None):
        self.calls: list[list[str]] = []
        self.rules = rules
        self.remote_events = remote_events or {}  # copyto: {case: Drive 版 events.jsonl の行}。無い案件は失敗（object not found）
        self.fail_move = fail_move
        self.fail_move_nth = fail_move_nth  # n 回目の move だけ失敗させる（1 始まり）
        self.moves = 0
        self.fail = fail or set()
        self.manifest: dict | None = None            # Drive の manifest.json（cat / rcat）。None なら未作成（cat は 3 で失敗）
        self.remote_cases: dict[str, dict] = {}      # Drive 上の cases/<case>/case.json（lsf --include /*/case.json / cat / rcat）
        self.remote_case_mtime = "2026-08-15 09:30:00"
        self.rcats: list[tuple[str, str]] = []       # (path, 本文)

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        prog, sub = cmd[0], cmd[1]
        if prog == "rclone" and sub in self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", f"fake rclone {sub} failed")
        if prog == "rclone" and sub == "cat":
            target = cmd[2]
            if target.endswith("/manifest.json") and self.manifest is not None:
                return subprocess.CompletedProcess(cmd, 0, json.dumps(self.manifest), "")
            if target.endswith("/case.json") and target.rsplit("/", 2)[-2] in self.remote_cases:
                return subprocess.CompletedProcess(cmd, 0, json.dumps(self.remote_cases[target.rsplit("/", 2)[-2]]), "")
            return subprocess.CompletedProcess(cmd, 3, "", "fake rclone cat: object not found")
        if prog == "rclone" and sub == "rcat":
            target, text = cmd[2], kw.get("input", "")
            self.rcats.append((target, text))
            if target.endswith("/manifest.json"):
                self.manifest = json.loads(text)
            elif target.endswith("/case.json"):
                self.remote_cases[target.rsplit("/", 2)[-2]] = json.loads(text)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if prog == "rclone" and sub == "lsf" and "/*/case.json" in cmd:   # manifest_rebuild の列挙（path \t mtime）
            rows = [f"{cid}/case.json\t{self.remote_case_mtime}" for cid in sorted(self.remote_cases)]
            return subprocess.CompletedProcess(cmd, 0, "\n".join(rows) + ("\n" if rows else ""), "")
        if prog == "rclone" and sub == "copyto":
            case = cmd[2].rsplit("/", 2)[-2]
            if case not in self.remote_events:
                return subprocess.CompletedProcess(cmd, 3, "", "fake rclone copyto: object not found")
            Path(cmd[3]).write_text("".join(l + "\n" for l in self.remote_events[case]), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "fake rclone copyto ok")
        if prog == "rclone" and sub == "lsf" and "--separator" not in cmd:   # list_ws_on_drive（--dirs-only）: 空
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if prog == "rclone" and sub == "lsf":
            src = Path(cmd[cmd.index("--separator") + 2])
            include = any(x.startswith("+ *.{") for x in cmd)
            min_size = "--min-size" in cmd
            rows = []
            for p in sorted(src.rglob("*")):
                if not p.is_file() or p.is_symlink():
                    continue
                if not sync.is_raw(p, {**self.rules, "min_size": self.rules["min_size"] if min_size else None,
                                       "extensions": self.rules["extensions"] if include else []}):
                    continue
                rows.append(f"{p.relative_to(src)}\t{p.stat().st_size}")
            return subprocess.CompletedProcess(cmd, 0, "\n".join(rows) + ("\n" if rows else ""), "")
        if prog == "rclone" and sub == "move":
            self.moves += 1
            if self.fail_move or self.moves == self.fail_move_nth:
                return subprocess.CompletedProcess(cmd, 1, "", "fake rclone move failed")
            if "--dry-run" not in cmd:
                src = Path(cmd[2])
                lsf = self([ "rclone", "lsf", "-R", "--files-only", "--format", "ps", "--separator", "\t", str(src), *cmd[3:]])
                for line in lsf.stdout.splitlines():
                    (src / line.split("\t")[0]).unlink()
            return subprocess.CompletedProcess(cmd, 0, "", "fake rclone move ok")
        if prog == "rclone":
            return subprocess.CompletedProcess(cmd, 0, "", f"fake rclone {sub} ok")
        if prog == "zstd":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(f"unexpected command: {cmd}")


@pytest.fixture
def fake(conf, monkeypatch):
    rr = sync.raw_rules(conf)
    f = FakeRun(rr)
    monkeypatch.setattr(subprocess, "run", f)
    return f


# ---------- 判定 ----------

def test_parse_size_and_age():
    assert sync.parse_size("50M") == 50 * 1024 ** 2
    assert sync.parse_size("1.5G") == int(1.5 * 1024 ** 3)
    assert sync.parse_size("100k") == 100 * 1024
    assert sync.parse_size("10") == 10 * 1024  # 単位なしは rclone と同じ KiB
    assert sync.parse_size("2b") == 2
    assert sync.parse_age("14d") == 14 * DAY
    assert sync.parse_age("12h") == 12 * 3600
    assert sync.parse_age("2w") == 14 * DAY
    with pytest.raises(ValueError):
        sync.parse_size("fifty")
    with pytest.raises(ValueError):
        sync.parse_age("14days")


def test_is_raw(conf, tmp_path):
    rr = sync.raw_rules(conf)  # extensions=[bag,...], min_size=50M, min_age=14d
    assert rr["min_size"] == 50 * 1024 ** 2 and rr["min_age"] == 14 * DAY
    old = 20 * DAY
    assert sync.is_raw(_touch(tmp_path / "a.bag", 10, old), rr)                 # 拡張子で該当（小さくても）
    assert sync.is_raw(_touch(tmp_path / "a.bag.active", 10, old), rr)          # .active
    assert not sync.is_raw(_touch(tmp_path / "a.bag", 10, 3 * DAY), rr)         # 経過日数不足
    assert not sync.is_raw(_touch(tmp_path / "notes.md", 10, old), rr)          # 対象外の拡張子・小さい
    def _sparse(name, size):  # truncate は mtime を更新するので、その後に古い mtime を付け直す
        f = tmp_path / name; f.write_bytes(b""); os.truncate(f, size); os.utime(f, (time.time() - old, time.time() - old)); return f
    assert sync.is_raw(_sparse("big.log", 50 * 1024 ** 2 + 1), rr)             # サイズ超（拡張子は対象外）
    assert not sync.is_raw(_sparse("edge.log", 50 * 1024 ** 2), rr)            # ちょうど min_size は「超」ではない
    link = tmp_path / "link.bag"; link.symlink_to(tmp_path / "a.bag")
    assert not sync.is_raw(link, rr)                                            # シンボリックリンクは対象外


def test_bag_candidates(conf, tmp_path):
    case = conf.workspaces["acme"].cases_dir / "CASE-123"
    old = _touch(case / "run1.bag", 10, 3600)
    act = _touch(case / "run2.bag.active", 10, 3600)
    _touch(case / "fresh.bag", 10, 60)                                          # 30 分未満は除外
    _touch(case / "done.bag", 10, 3600); _touch(case / "done.bag.zst", 10, 3600)  # 圧縮済みは除外
    _touch(case / "other.txt", 10, 3600)
    sub = _touch(case / "nested" / "x.bag", 10, 3600)
    _touch(case / "build" / "x.bag", 10, 3600); _touch(case / "sub" / "target" / "y.bag", 10, 3600)   # rules.exclude 配下は対象外（L-6）
    exclude = conf.rules["exclude"]
    assert sync.bag_candidates(case, exclude=exclude) == sorted([old, act, sub])
    assert sync.bag_candidates(case) == sorted([old, act, sub, case / "build" / "x.bag", case / "sub" / "target" / "y.bag"])  # 明示しなければ除外なし


def test_bag2zst_skips_excluded_dirs(conf, fake):
    ws = conf.workspaces["acme"]
    _touch(ws.cases_dir / "CASE-123" / "run.bag", 10, 3600)
    _touch(ws.cases_dir / "CASE-123" / "target" / "debug" / "x.bag", 10, 3600)
    r = sync.bag2zst(conf, ws, dry=True)
    assert [x["src"] for x in r["done"]] == ["CASE-123/run.bag"]


# ---------- bag2zst ----------

def test_bag2zst_disabled(conf, tmp_path, fake):
    conf.rules["bag_to_zst"] = False
    _touch(conf.workspaces["acme"].cases_dir / "CASE-123" / "run.bag", 10, 3600)
    r = sync.bag2zst(conf, conf.workspaces["acme"])
    assert r["enabled"] is False and r["done"] == [] and fake.calls == []


def test_bag2zst_dry_lists_without_running(conf, fake):
    ws = conf.workspaces["acme"]
    _touch(ws.cases_dir / "CASE-123" / "run.bag", 10, 3600)
    r = sync.bag2zst(conf, ws, dry=True)
    assert [x["src"] for x in r["done"]] == ["CASE-123/run.bag"] and fake.calls == []
    assert (ws.cases_dir / "CASE-123" / "run.bag").exists()


@pytest.mark.skipif(not shutil.which("zstd"), reason="zstd not installed")
def test_bag2zst_real_compress_keeps_mtime(conf, tmp_path):
    ws = conf.workspaces["acme"]
    src = _touch(ws.cases_dir / "CASE-123" / "run.bag", 5000, 3600)
    act = _touch(ws.cases_dir / "CASE-123" / "run2.bag.active", 5000, 7200)
    mt, mt2 = src.stat().st_mtime, act.stat().st_mtime
    r = sync.bag2zst(conf, ws, "CASE-123")
    assert r["errors"] == [] and {x["dst"] for x in r["done"]} == {"CASE-123/run.bag.zst", "CASE-123/run2.bag.active.zst"}
    assert not src.exists() and not act.exists()
    dst = ws.cases_dir / "CASE-123" / "run.bag.zst"
    assert abs(dst.stat().st_mtime - mt) < 1e-3 and abs((act.with_name("run2.bag.active.zst")).stat().st_mtime - mt2) < 1e-3
    assert not list((ws.cases_dir / "CASE-123").glob("*.part"))
    assert subprocess.run(["zstd", "-t", "-q", str(dst)]).returncode == 0
    assert subprocess.run(["zstd", "-dc", str(dst)], capture_output=True).stdout == b"x" * 5000


def test_bag2zst_failure_removes_part_and_keeps_source(conf, tmp_path, monkeypatch):
    ws = conf.workspaces["acme"]
    src = _touch(ws.cases_dir / "CASE-123" / "run.bag", 100, 3600)

    def run(cmd, **kw):
        if cmd[0] == "zstd" and "-o" in cmd:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"broken")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "zstd" and "-t" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "corrupt")
        raise AssertionError(cmd)
    monkeypatch.setattr(subprocess, "run", run)
    r = sync.bag2zst(conf, ws)
    assert r["done"] == [] and len(r["errors"]) == 1 and "zstd -t failed" in r["errors"][0]["error"]
    assert src.exists() and not (src.with_name("run.bag.zst")).exists() and not (src.with_name("run.bag.zst.part")).exists()


# ---------- rclone コマンド組み立て ----------

def test_checkout_uses_update_and_bwlimit(conf, fake):
    ws = conf.workspaces["acme"]
    sync.checkout(conf, ws, "CASE-123")
    cmd = fake.calls[-1]
    assert cmd[:4] == ["rclone", "copy", "my-drive:ws/acme/cases/CASE-123", str(ws.cases_dir / "CASE-123")]
    assert "--update" in cmd and "--bwlimit" not in cmd and "--dry-run" not in cmd
    assert cmd[cmd.index("--max-size") + 1] == "50M" and "*.bag" in cmd
    conf.rules["bwlimit"] = "08:00,4M 20:00,off"
    fake.manifest = {"cases": {"CASE-123": {"rev": "r1"}}}   # ワークスペース全体は manifest の rev が違う案件（ローカルに無い）だけ
    sync.checkout(conf, ws, dry=True)
    cmd = fake.calls[-1]
    assert cmd[:2] == ["rclone", "copy"] and cmd[cmd.index("--bwlimit") + 1] == "08:00,4M 20:00,off" and cmd[-1] == "--dry-run"
    sync.checkin(conf, ws, "CASE-123") if (ws.cases_dir / "CASE-123").mkdir(parents=True, exist_ok=True) is None else None
    cmd = [c for c in fake.calls if c[1] in ("sync", "copy")][-1]
    assert cmd[:2] == ["rclone", "sync"] and "--bwlimit" in cmd and "--backup-dir" in cmd


def test_checkin_workspace_copies_but_case_syncs(conf, fake):
    """H-2: ワークスペース全体の checkin（daily）は rclone copy（ローカルに無い案件を Drive から消さない）。案件単位は sync のまま。"""
    ws = conf.workspaces["acme"]
    CaseStore(ws.cases_dir).create_case("CASE-1", "t", "acme", actor="human")
    sync.checkin(conf, ws)
    cmd = [c for c in fake.calls if c[1] in ("sync", "copy")][-1]
    assert cmd[:4] == ["rclone", "copy", str(ws.cases_dir), "my-drive:ws/acme/cases"]
    assert cmd[cmd.index("--backup-dir") + 1].startswith("my-drive:ws/acme/_deleted/")
    sync.checkin(conf, ws, "CASE-1")
    cmd = [c for c in fake.calls if c[1] in ("sync", "copy")][-1]
    assert cmd[:4] == ["rclone", "sync", str(ws.cases_dir / "CASE-1"), "my-drive:ws/acme/cases/CASE-1"] and "--backup-dir" in cmd
    sync.daily(conf, ws)
    assert [c[1] for c in fake.calls if c[1] in ("sync", "copy")][-1] == "copy"


def test_raw_move_command_and_dry_run(conf, fake):
    ws = conf.workspaces["acme"]
    conf.rules["bwlimit"] = "4M"
    case = ws.cases_dir / "CASE-123"
    bag = _touch(case / "data" / "run.bag", 10, 20 * DAY)
    _touch(case / "worklog.md", 10, 20 * DAY)
    r = sync.raw_move(conf, ws, "CASE-123", dry=True)
    assert r["cases"]["CASE-123"]["planned"] == ["data/run.bag"] and r["files"] == 0
    assert bag.exists()
    lsf = [c for c in fake.calls if c[1] == "lsf"]
    moves = [c for c in fake.calls if c[1] == "move"]
    assert len(lsf) == 2 and len(moves) == 2  # 拡張子パスとサイズパス
    inc, size = moves
    assert inc[2:4] == [str(case), "my-drive:ws/acme/cases/CASE-123/"]
    plus = [x for x in inc if x.startswith("+ ")]
    assert plus == ["+ *.{bag,zst,pgm,npz,zip,gz,tar,active,pcd,mp4}"] and "--min-size" not in inc
    assert inc[inc.index(plus[0]) + 2] == "- **"                               # include の直後に「残りは除外」
    assert size[size.index("--min-size") + 1] == "50M" and not any(x.startswith("+ ") for x in size)
    for m in moves:
        assert m[m.index("--min-age") + 1] == "14d" and m[-1] == "--dry-run" and m[m.index("--bwlimit") + 1] == "4M"
        assert "--include" not in m and "--exclude" not in m                  # 併用は順序不定（rclone の警告）
        assert "- target/**" in m and m.index("- target/**") < (m.index("- **") if "- **" in m else len(m))  # exclude が先
    # lsf は move と同じフィルタ（lsf: ソースの後ろ全部 / move: -v の後ろ、--bwlimit の前）
    for l, m in zip(lsf, moves):
        assert l[l.index(str(case)) + 1:] == m[m.index("-v") + 1:m.index("--bwlimit")]


# ---------- Data location 記録 ----------

def test_raw_move_records_case_json_and_worklog(conf, fake):
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-123", "widget boot failure", "acme", actor="human")
    case = ws.cases_dir / "CASE-123"
    _touch(case / "data" / "run.bag", 1000, 20 * DAY)
    _touch(case / "new.bag", 1000, 1 * DAY)          # 若い: 残る
    (case / "worklog.md").write_text("# t\n\n## Notes\nx\n\n## Data location\n\n## Later\ny\n", encoding="utf-8")
    r = sync.raw_move(conf, ws)
    c = r["cases"]["CASE-123"]
    assert c["moved"] == ["data/run.bag"] and c["bytes"] == 1000 and r["files"] == 1
    assert not (case / "data" / "run.bag").exists() and (case / "new.bag").exists()
    data = st.load_case("CASE-123")["data"]
    assert len(data) == 1 and data[0]["drive"] == "my-drive:ws/acme/cases/CASE-123/" and data[0]["files"] == 1 and data[0]["bytes"] == 1000
    assert data[0]["list"].startswith("index/raw-moved-") and data[0]["moved_at"][:4].isdigit()
    lst = (ws.data_dir / data[0]["list"]).read_text(encoding="utf-8")
    assert lst == "CASE-123/data/run.bag\t1000\tmy-drive:ws/acme/cases/CASE-123/data/run.bag\n"
    wl = (case / "worklog.md").read_text(encoding="utf-8")
    sec = wl.split("## Data location\n")[1].split("## Later")[0]
    assert "1 file(s)" in sec and "my-drive:ws/acme/cases/CASE-123/" in sec and "rclone copy my-drive:ws/acme/cases/CASE-123/<file>" in sec
    assert wl.endswith("## Later\ny\n")
    ev = st.events("CASE-123")[-1]
    assert ev["action"] == "progress" and ev["actor"] == "kairn" and "raw_move: 1 file(s)" in ev["note"]


def test_raw_move_writes_data_md_without_case_json(conf, fake):
    ws = conf.workspaces["acme"]
    case = ws.cases_dir / "0815_legacy"
    _touch(case / "run.bag", 10, 20 * DAY)
    sync.raw_move(conf, ws)
    md = (case / "DATA.md").read_text(encoding="utf-8")
    assert md.startswith("# 0815_legacy") and "## Data location\n- " in md and "my-drive:ws/acme/cases/0815_legacy/" in md
    assert not (case / "case.json").exists() and not (case / "events.jsonl").exists()
    sync.raw_move(conf, ws)  # 対象なし: 追記されない
    assert (case / "DATA.md").read_text(encoding="utf-8") == md


def test_append_data_location_creates_section_at_end(tmp_path):
    p = tmp_path / "worklog.md"
    p.write_text("# t\n\n## Notes\nx\n", encoding="utf-8")
    sync._append_data_location(p, "- line1", "t")
    sync._append_data_location(p, "- line2", "t")
    assert p.read_text(encoding="utf-8") == "# t\n\n## Notes\nx\n\n## Data location\n- line1\n- line2\n"


def test_raw_move_records_files_moved_before_second_move_fails(conf, fake):
    """M-2: 拡張子パス（1 回目）で移動済みのファイルは、サイズパス（2 回目）が失敗しても記録される（所在不明にしない）。"""
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-123", "t", "acme", actor="human")
    _touch(ws.cases_dir / "CASE-123" / "run.bag", 10, 20 * DAY)
    fake.fail_move_nth = 2
    r = sync.raw_move(conf, ws)
    c = r["cases"]["CASE-123"]
    assert "error" in c and "move failed" in c["error"]
    assert c["moved"] == ["run.bag"] and c["bytes"] == 10 and r["files"] == 1
    assert not (ws.cases_dir / "CASE-123" / "run.bag").exists()
    data = st.load_case("CASE-123")["data"]
    assert len(data) == 1 and data[0]["files"] == 1
    assert (ws.data_dir / data[0]["list"]).read_text(encoding="utf-8") == "CASE-123/run.bag\t10\tmy-drive:ws/acme/cases/CASE-123/run.bag\n"
    assert "## Data location\n- " in (ws.cases_dir / "CASE-123" / "worklog.md").read_text(encoding="utf-8")
    assert st.events("CASE-123")[-1]["action"] == "progress"
    assert sync.raw_move(conf, ws, dry=True)["cases"]["CASE-123"]["planned"] == []  # 次回の planned に出ない


def test_raw_move_failure_is_reported_not_recorded(conf, fake):
    ws = conf.workspaces["acme"]
    fake.fail_move = True
    CaseStore(ws.cases_dir).create_case("CASE-123", "t", "acme", actor="human")
    _touch(ws.cases_dir / "CASE-123" / "run.bag", 10, 20 * DAY)
    r = sync.raw_move(conf, ws)
    assert "error" in r["cases"]["CASE-123"] and r["files"] == 0
    assert CaseStore(ws.cases_dir).load_case("CASE-123")["data"] == []


# ---------- daily ----------

def test_daily_order_and_continue_on_failure(conf, fake, monkeypatch):
    ws = conf.workspaces["acme"]
    CaseStore(ws.cases_dir).create_case("CASE-123", "t", "acme", actor="human")
    order = []
    monkeypatch.setattr(sync, "bag2zst", lambda *a, **k: order.append("bag2zst") or {"ok": 1})
    monkeypatch.setattr(sync, "checkin", lambda *a, **k: order.append("checkin") or (_ for _ in ()).throw(sync.RcloneError("remote down")))
    monkeypatch.setattr(sync, "raw_move", lambda *a, **k: order.append("raw_move") or {"files": 0})
    monkeypatch.setattr(sync, "drive_index", lambda *a, **k: order.append("drive_index") or ws.index_dir / "drive-index.txt")
    r = sync.daily(conf, ws)
    assert order == ["bag2zst", "checkin", "raw_move", "drive_index"]
    assert list(r["steps"]) == ["bag2zst", "checkin", "raw_move", "drive_index", "index"]
    assert r["ok"] is False and r["steps"]["checkin"] == {"ok": False, "error": "RcloneError: remote down"}
    assert r["steps"]["index"]["ok"] and r["steps"]["index"]["result"]["cases"] == 1
    log = (ws.index_dir / "daily.log").read_text(encoding="utf-8")
    assert "checkin: ERROR RcloneError: remote down" in log and "index: ok" in log and "daily end ok=False" in log


def test_daily_passes_dry_run(conf, fake):
    ws = conf.workspaces["acme"]
    _touch(ws.cases_dir / "CASE-123" / "run.bag", 10, 20 * DAY)
    r = sync.daily(conf, ws, dry=True)
    assert r["ok"] is True and r["dry"] is True
    assert r["steps"]["bag2zst"]["result"]["dry"] and r["steps"]["raw_move"]["result"]["dry"]
    assert (ws.cases_dir / "CASE-123" / "run.bag").exists()
    assert [c for c in fake.calls if c[1] == "copy"][0][-1] == "--dry-run"  # ワークスペース全体は copy


# ---------- rclone 除外パターン（項目 8）: 実 rclone でローカル間コピー（クラウド接続なし） ----------

@pytest.mark.skipif(not shutil.which("rclone"), reason="rclone not installed")
def test_exclude_patterns_match_root_and_nested(conf, tmp_path, monkeypatch):
    monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "rclone-empty.conf"))
    src = tmp_path / "src"; dst = tmp_path / "dst"
    for rel in ("target/x.txt", "sub/target/x.txt", "build/y.txt", "a/b/node_modules/m.js", ".venv/lib/z.py", "__pycache__/c.pyc",
                "keep.txt", "sub/keep.md", "targets/keep.txt", "obj.o"):
        _touch(src / rel, 5)
    r = subprocess.run(["rclone", "copy", str(src), str(dst), *sync._filters(conf)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    got = sorted(str(p.relative_to(dst)) for p in dst.rglob("*") if p.is_file())
    assert got == ["keep.txt", "sub/keep.md", "targets/keep.txt"]


@pytest.mark.skipif(not shutil.which("rclone"), reason="rclone not installed")
def test_raw_filter_sets_with_real_rclone(conf, tmp_path, monkeypatch):
    """L-7: _raw_filter_sets を実 rclone（ローカル→ローカル copy）で検証。拡張子パス／サイズパス／14 日未満除外／target/ 除外。"""
    monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "rclone-empty.conf"))
    src = tmp_path / "src"
    old = 20 * DAY
    _touch(src / "a.bag", 10, old)                         # 拡張子 → 拡張子パス
    _touch(src / "sub" / "b.bag.active", 10, old)          # 拡張子（ネスト）
    _touch(src / "new.bag", 10, 1 * DAY)                   # 14 日未満 → どちらにも出ない
    _touch(src / "notes.md", 10, old)                      # 対象外の拡張子・小さい
    _touch(src / "target" / "c.bag", 10, old)              # rules.exclude
    _touch(src / "sub" / "target" / "d.bag", 10, old)      # rules.exclude（ネスト）
    big = src / "big.log"; big.write_bytes(b""); os.truncate(big, 50 * 1024 ** 2 + 1); os.utime(big, (time.time() - old, time.time() - old))
    bigt = src / "target" / "big.log"; bigt.write_bytes(b""); os.truncate(bigt, 50 * 1024 ** 2 + 1); os.utime(bigt, (time.time() - old, time.time() - old))
    sets = sync._raw_filter_sets(conf, sync.raw_rules(conf))
    assert len(sets) == 2
    got = []
    for i, filt in enumerate(sets):
        dst = tmp_path / f"dst{i}"
        r = subprocess.run(["rclone", "copy", str(src), str(dst), *filt], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        got.append(sorted(str(p.relative_to(dst)) for p in dst.rglob("*") if p.is_file()))
    assert got == [["a.bag", "sub/b.bag.active"], ["big.log"]]


def test_checkin_marks_last_checkin_at(conf, fake):
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "t", "acme", actor="human"); st.create_case("CASE-2", "t", "acme", actor="human")
    _touch(ws.cases_dir / "0815_legacy" / "notes.md")
    sync.checkin(conf, ws, "CASE-1", dry=True)
    assert "last_checkin_at" not in st.load_case("CASE-1")                 # dry では記録しない
    sync.checkin(conf, ws, "CASE-1")
    assert st.load_case("CASE-1")["last_checkin_at"] and "last_checkin_at" not in st.load_case("CASE-2")
    sync.checkin(conf, ws)                                                  # ワークスペース全体（daily）も全案件に記録
    assert st.load_case("CASE-2")["last_checkin_at"]
    assert st.local_changes_since_checkin("CASE-2") == []


# ---------- Data location の記録先（項目 14）: worklog.md があればそこ、無ければ DATA.md ----------

def test_data_location_goes_to_worklog_if_present_else_data_md(conf, fake):
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    # case.json あり・worklog.md なし → DATA.md（worklog.md は作らない）
    st.create_case("CASE-1", "t", "acme", actor="human")
    (ws.cases_dir / "CASE-1" / "worklog.md").unlink()
    _touch(ws.cases_dir / "CASE-1" / "run.bag", 10, 20 * DAY)
    # case.json なし・worklog.md あり → worklog.md
    _touch(ws.cases_dir / "0815_legacy" / "run.bag", 10, 20 * DAY)
    (ws.cases_dir / "0815_legacy" / "worklog.md").write_text("# legacy\n\n## Notes\nx\n", encoding="utf-8")
    r = sync.raw_move(conf, ws)
    assert r["files"] == 2
    assert not (ws.cases_dir / "CASE-1" / "worklog.md").exists() and "## Data location\n- " in (ws.cases_dir / "CASE-1" / "DATA.md").read_text()
    assert st.load_case("CASE-1")["data"][0]["files"] == 1 and st.events("CASE-1")[-1]["actor"] == "kairn"
    assert not (ws.cases_dir / "0815_legacy" / "DATA.md").exists()
    assert "## Data location\n- " in (ws.cases_dir / "0815_legacy" / "worklog.md").read_text()
    assert not (ws.cases_dir / "0815_legacy" / "case.json").exists()


def test_daily_dry_run_does_not_write_drive_index_or_sqlite(conf, fake):
    ws = conf.workspaces["acme"]
    CaseStore(ws.cases_dir).create_case("CASE-1", "t", "acme", actor="human")
    r = sync.daily(conf, ws, dry=True)
    assert r["ok"] is True
    assert not (ws.index_dir / "drive-index.txt").exists() and not (ws.index_dir / "kairn.sqlite").exists()
    assert "not written" in r["steps"]["drive_index"]["result"] and "skipped" in r["steps"]["index"]["result"]
    assert (ws.index_dir / "daily.log").exists()
    assert "last_checkin_at" not in CaseStore(ws.cases_dir).load_case("CASE-1")
    r = sync.daily(conf, ws)
    assert (ws.index_dir / "drive-index.txt").exists() and (ws.index_dir / "kairn.sqlite").exists() and r["steps"]["index"]["result"]["cases"] == 1


# ---------- events.jsonl のマージ ----------

def _ev(t: str, note: str, action: str = "progress") -> str:
    return json.dumps({"t": t, "case": "CASE-123", "actor": "ai", "agent": "x", "action": action, "note": note}, ensure_ascii=False)


def test_merge_events_union_dedup_sorted(tmp_path):
    """両側に固有の行 → 和集合。同一行は 1 つ。`t` で安定ソート（同時刻はローカル → Drive の順）。"""
    local = tmp_path / "events.jsonl"
    shared = _ev("2026-09-01T10:00:00+09:00", "opened", "opened")
    l1 = _ev("2026-09-01T12:00:00+09:00", "local only")
    l2 = _ev("2026-09-01T13:00:00+09:00", "same time, local")
    r1 = _ev("2026-09-01T11:00:00+09:00", "remote only")
    r2 = _ev("2026-09-01T13:00:00+09:00", "same time, remote")
    local.write_text(shared + "\n" + l1 + "\n" + l2 + "\n", encoding="utf-8")
    merged = sync.merge_events(local, [shared, r1, r2, ""])
    assert merged == [shared, r1, l1, l2, r2]
    assert local.read_text(encoding="utf-8") == "".join(l + "\n" for l in merged)
    # 変化が無ければ書き戻さない（mtime を触らない）
    mt = local.stat().st_mtime_ns
    assert sync.merge_events(local, [shared, r1]) == merged and local.stat().st_mtime_ns == mt
    # ローカルが無い → Drive 版そのまま。Drive 版が空 → 空のファイルは作らない
    fresh = tmp_path / "fresh.jsonl"
    assert sync.merge_events(fresh, [r1, shared]) == [shared, r1] and fresh.exists()
    none = tmp_path / "none.jsonl"
    assert sync.merge_events(none, []) == [] and not none.exists()
    # JSON でない行・t の無い行は捨てず先頭に寄せる
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n" + l1 + "\n", encoding="utf-8")
    assert sync.merge_events(bad, ['{"note": "no t"}']) == ["not json", '{"note": "no t"}', l1]


def _events_of(ws, case):
    return [json.loads(l) for l in (ws.cases_dir / case / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def _excludes(cmd) -> list[str]:
    """rclone コマンドの --exclude の値（rules.exclude 由来の target/** 等も含む）。"""
    return [cmd[i + 1] for i, x in enumerate(cmd) if x == "--exclude"]


def test_checkout_case_fetches_merges_then_copies(conf, monkeypatch):
    """checkout(case): rclone copyto（Drive 版 events を一時ファイルへ）→ マージして書き戻し → rclone copy --update（events.jsonl を除外）の順。"""
    ws = conf.workspaces["acme"]
    CaseStore(ws.cases_dir).create_case("CASE-123", "t", "acme", actor="human")
    remote = [_ev("2026-08-01T00:00:00+09:00", "from another environment")]
    f = FakeRun(sync.raw_rules(conf), remote_events={"CASE-123": remote})
    monkeypatch.setattr(subprocess, "run", f)
    msg = sync.checkout(conf, ws, "CASE-123")
    assert [c[1] for c in f.calls] == ["copyto", "copy"]
    copyto, copy = f.calls
    assert copyto[2] == "my-drive:ws/acme/cases/CASE-123/events.jsonl" and copyto[3].endswith("/events.jsonl")
    assert not Path(copyto[3]).exists()  # 一時ファイルは消えている
    assert copy[:4] == ["rclone", "copy", "my-drive:ws/acme/cases/CASE-123", str(ws.cases_dir / "CASE-123")]
    assert "--update" in copy and "/events.jsonl" in _excludes(copy)
    ev = _events_of(ws, "CASE-123")
    assert [e["note"] for e in ev] == ["from another environment", "case created: t"]  # Drive の行が t 順で先頭に入った
    assert msg.endswith("[events merged: 1]")


def test_checkout_case_without_remote_events_falls_back(conf, monkeypatch):
    """取得失敗（Drive にその案件が無い）: マージを飛ばし、従来どおり events.jsonl を除外せずに copy --update。ローカルは変わらない。"""
    ws = conf.workspaces["acme"]
    CaseStore(ws.cases_dir).create_case("CASE-123", "t", "acme", actor="human")
    ev = ws.cases_dir / "CASE-123" / "events.jsonl"
    before = (ev.read_text(encoding="utf-8"), ev.stat().st_mtime_ns)
    f = FakeRun(sync.raw_rules(conf))
    monkeypatch.setattr(subprocess, "run", f)
    msg = sync.checkout(conf, ws, "CASE-123")
    assert [c[1] for c in f.calls] == ["copyto", "copy"] and "/events.jsonl" not in _excludes(f.calls[1]) and "--update" in f.calls[1]
    assert (ev.read_text(encoding="utf-8"), ev.stat().st_mtime_ns) == before and "events merged" not in msg
    # rclone コマンド不在（FileNotFoundError）でもマージを飛ばして copy を試みる（copy 自体の失敗は従来どおり伝播）
    def missing(cmd, **kw):
        raise FileNotFoundError("rclone")
    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(FileNotFoundError):
        sync.checkout(conf, ws, "CASE-123")
    assert (ev.read_text(encoding="utf-8"), ev.stat().st_mtime_ns) == before
    # dry-run ではマージしない（copyto を呼ばない）
    monkeypatch.setattr(subprocess, "run", f)
    f.calls.clear()
    sync.checkout(conf, ws, "CASE-123", dry=True)
    assert [c[1] for c in f.calls] == ["copy"] and f.calls[0][-1] == "--dry-run"


def test_checkin_case_merges_then_syncs(conf, monkeypatch):
    """checkin(case): copyto → マージ → rclone sync の順。Drive にしか無い行を消さず、last_checkin_events はマージ後の行数。"""
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-123", "t", "acme", actor="human")
    remote = [_ev("2026-08-01T00:00:00+09:00", "remote only"), _ev("2026-08-02T00:00:00+09:00", "remote only 2")]
    f = FakeRun(sync.raw_rules(conf), remote_events={"CASE-123": remote})
    monkeypatch.setattr(subprocess, "run", f)
    msg = sync.checkin(conf, ws, "CASE-123")
    assert [c[1] for c in f.calls] == ["copyto", "sync", "cat", "rcat"]   # マージ → 転送 → manifest（cat → rcat）
    assert f.calls[1][:4] == ["rclone", "sync", str(ws.cases_dir / "CASE-123"), "my-drive:ws/acme/cases/CASE-123"]
    assert not any(x.endswith("events.jsonl") for x in _excludes(f.calls[1]))  # マージ済みの events.jsonl をそのまま Drive へ
    assert [e["note"] for e in _events_of(ws, "CASE-123")] == ["remote only", "remote only 2", "case created: t"]
    assert st.load_case("CASE-123")["last_checkin_events"] == 3 and "[events merged: 1]" in msg and msg.endswith("[manifest: 1]")
    # 取得失敗 → マージなしで sync（従来どおり）
    f2 = FakeRun(sync.raw_rules(conf), fail={"copyto"})
    monkeypatch.setattr(subprocess, "run", f2)
    sync.checkin(conf, ws, "CASE-123")
    assert [c[1] for c in f2.calls] == ["copyto", "sync", "cat", "rcat"] and len(_events_of(ws, "CASE-123")) == 3


def test_checkout_and_checkin_workspace_merge_per_case(conf, monkeypatch):
    """ワークスペース全体: checkout は manifest（cat）を見て rev が違う案件（ローカルに無い案件を含む）だけを 1 案件ずつ
    マージ → copy --update する（ワークスペース全体の copy はしない）。checkin は各案件をマージ → copy → manifest 更新。"""
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "a", "acme", actor="human")
    st.create_case("CASE-2", "b", "acme", actor="human")
    remote = {"CASE-1": [_ev("2026-08-01T00:00:00+09:00", "r1")], "CASE-9": [_ev("2026-08-01T00:00:00+09:00", "r9")]}
    f = FakeRun(sync.raw_rules(conf), remote_events=remote)
    f.manifest = {"cases": {"CASE-1": {"rev": "d1"}, "CASE-2": {"rev": "d2"}, "CASE-9": {"rev": "d9"}}}
    monkeypatch.setattr(subprocess, "run", f)
    msg = sync.checkout(conf, ws)
    assert [(c[1], c[2].rsplit("/", 2)[-2] if c[1] == "copyto" else c[2].rsplit("/", 1)[-1]) for c in f.calls] == \
        [("cat", "manifest.json"), ("copyto", "CASE-1"), ("copy", "CASE-1"), ("copyto", "CASE-2"), ("copy", "CASE-2"), ("copyto", "CASE-9"), ("copy", "CASE-9")]
    assert all("--update" in c and "/events.jsonl" in _excludes(c) for c in f.calls if c[1] == "copy" and c[2].endswith(("CASE-1", "CASE-9")))
    assert "/events.jsonl" not in _excludes([c for c in f.calls if c[1] == "copy"][1])   # CASE-2 は Drive に events が無い → 除外しない
    assert [e["note"] for e in _events_of(ws, "CASE-1")] == ["r1", "case created: a"]
    assert [e["note"] for e in _events_of(ws, "CASE-2")] == ["case created: b"]
    assert [e["note"] for e in _events_of(ws, "CASE-9")] == ["r9"]
    assert "fetched 3 (CASE-1, CASE-2, CASE-9)" in msg and "up to date 0" in msg
    f.calls.clear()
    sync.checkin(conf, ws)
    assert [c[1] for c in f.calls] == ["copyto", "copyto", "copyto", "copy", "cat", "rcat"]
    assert not any(x.endswith("events.jsonl") for x in _excludes(f.calls[3]))
    assert st.load_case("CASE-1")["last_checkin_events"] == 2
    assert set(f.manifest["cases"]) == {"CASE-1", "CASE-2", "CASE-9"} and f.manifest["cases"]["CASE-9"] == {"rev": "d9"}   # 案件なしのディレクトリは触らない
    assert f.manifest["cases"]["CASE-1"]["rev"] == st.load_case("CASE-1")["rev"] != "d1"


# ---------- 版マーカー（rev）と manifest.json ----------

def test_checkin_stamps_rev_and_updates_manifest(conf, fake):
    """checkin(case): 転送前に case.json へ rev（uuid4）/ last_checkin_at / checked_in_from を書き、転送後に manifest を
    cat → 当該案件を更新 → rcat の順で書き戻す（他案件のエントリは保つ）。キャッシュ index/manifest.cache.json も更新。"""
    from kairn import store as store_mod
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "t", "acme", actor="human")
    fake.manifest = {"cases": {"CASE-0": {"rev": "keep", "checked_in_at": "2026-08-01T00:00:00+09:00", "from": "other-host"}}, "updated_at": "x"}
    msg = sync.checkin(conf, ws, "CASE-1")
    c = st.load_case("CASE-1")
    assert len(c["rev"]) == 36 and c["last_checkin_at"] and c["checked_in_from"] == store_mod.hostname()
    assert [x[1] for x in fake.calls] == ["copyto", "sync", "cat", "rcat"]
    assert fake.calls[2][2] == "my-drive:ws/acme/manifest.json" and fake.calls[3][2] == "my-drive:ws/acme/manifest.json"
    path, text = fake.rcats[-1]
    written = json.loads(text)
    assert written["cases"]["CASE-1"] == {"rev": c["rev"], "checked_in_at": c["last_checkin_at"], "from": c["checked_in_from"]}
    assert written["cases"]["CASE-0"]["rev"] == "keep" and written["updated_at"] != "x" and msg.endswith("[manifest: 1]")
    cache = json.loads((ws.index_dir / "manifest.cache.json").read_text(encoding="utf-8"))
    assert cache["cases"] == written["cases"] and cache["fetched_at"]
    # 2 回目は rev が変わる
    rev1 = c["rev"]
    sync.checkin(conf, ws, "CASE-1")
    assert st.load_case("CASE-1")["rev"] != rev1 and fake.manifest["cases"]["CASE-1"]["rev"] == st.load_case("CASE-1")["rev"]
    # manifest が無ければ新規作成（cat 失敗 → rcat）
    fake.manifest = None
    sync.checkin(conf, ws, "CASE-1")
    assert set(fake.manifest["cases"]) == {"CASE-1"}
    # dry では何も書かない
    fake.calls.clear(); rev = st.load_case("CASE-1")["rev"]
    sync.checkin(conf, ws, "CASE-1", dry=True)
    assert [x[1] for x in fake.calls] == ["sync"] and st.load_case("CASE-1")["rev"] == rev


def test_checkin_transfer_failure_restores_case_json(conf, monkeypatch):
    """転送（rclone sync）が失敗したら版マーカーは書く前の内容・mtime に戻り、manifest は触らない。"""
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "t", "acme", actor="human")
    f = FakeRun(sync.raw_rules(conf), fail={"sync"})
    monkeypatch.setattr(subprocess, "run", f)
    cj = ws.cases_dir / "CASE-1" / "case.json"
    before = (cj.read_text(encoding="utf-8"), cj.stat().st_mtime)
    with pytest.raises(sync.RcloneError, match="sync failed"):
        sync.checkin(conf, ws, "CASE-1")
    assert (cj.read_text(encoding="utf-8"), cj.stat().st_mtime) == before and "rev" not in st.load_case("CASE-1")
    assert [x[1] for x in f.calls] == ["copyto", "sync"] and f.rcats == []
    # manifest の書き戻しだけが失敗: 転送は済んでいるので版マーカーは残し、理由を RcloneError で返す
    f2 = FakeRun(sync.raw_rules(conf), fail={"rcat"})
    monkeypatch.setattr(subprocess, "run", f2)
    with pytest.raises(sync.RcloneError, match="transferred, but manifest update failed"):
        sync.checkin(conf, ws, "CASE-1")
    assert st.load_case("CASE-1")["rev"] and st.load_case("CASE-1")["last_checkin_at"]


def test_checkin_workspace_stamps_only_changed_cases(conf, fake):
    """ワークスペース全体（daily）: 未 checkin の案件と last_checkin_at より新しい変更のある案件だけ rev を振り直す
    （変更の無い案件の rev を毎日変えない）。案件が 1 つも対象でなければ manifest も触らない。"""
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "a", "acme", actor="human"); st.create_case("CASE-2", "b", "acme", actor="human")
    sync.checkin(conf, ws)
    r1, r2 = st.load_case("CASE-1")["rev"], st.load_case("CASE-2")["rev"]
    assert r1 and r2 and set(fake.manifest["cases"]) == {"CASE-1", "CASE-2"}
    fake.calls.clear()
    assert sync.checkin(conf, ws).count("[manifest") == 0
    assert [x[1] for x in fake.calls] == ["copyto", "copyto", "copy"]                     # 変更なし: cat / rcat 無し
    assert (st.load_case("CASE-1")["rev"], st.load_case("CASE-2")["rev"]) == (r1, r2)
    t = time.time() + 5
    os.utime(ws.cases_dir / "CASE-1" / "worklog.md", (t, t))
    fake.calls.clear()
    sync.checkin(conf, ws)
    assert [x[1] for x in fake.calls] == ["copyto", "copyto", "copy", "cat", "rcat"]
    assert st.load_case("CASE-1")["rev"] != r1 and st.load_case("CASE-2")["rev"] == r2
    assert fake.manifest["cases"]["CASE-1"]["rev"] == st.load_case("CASE-1")["rev"] and fake.manifest["cases"]["CASE-2"]["rev"] == r2
    os.utime(ws.cases_dir / "CASE-1" / "worklog.md", None)


def test_checkout_workspace_fetches_only_rev_mismatch(conf, fake):
    """kairn checkout <ws>: manifest の rev と違う案件（ローカルに無い案件を含む）だけ取り寄せる。一致は省略、
    未 checkin のローカル変更がある案件は skip。manifest が無ければ RcloneError（manifest rebuild を案内）。"""
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    for cid in ("CASE-1", "CASE-2", "CASE-4"):
        st.create_case(cid, "t", "acme", actor="human")
        st.mark_checkin(cid)
    fake.manifest = {"cases": {"CASE-1": {"rev": st.load_case("CASE-1")["rev"]}, "CASE-2": {"rev": "other"}, "CASE-3": {"rev": "new"}, "../x": {"rev": "bad"}}}
    with pytest.raises(sync.RcloneError, match="invalid case id") as ei:
        sync.checkout(conf, ws)
    assert "fetched 2 (CASE-2, CASE-3)" in str(ei.value) and "up to date 1" in str(ei.value)
    assert [c[2].rsplit("/", 1)[-1] for c in fake.calls if c[1] == "copy"] == ["CASE-2", "CASE-3"]
    assert json.loads((ws.index_dir / "manifest.cache.json").read_text(encoding="utf-8"))["cases"]["CASE-3"] == {"rev": "new"}
    del fake.manifest["cases"]["../x"]
    t = time.time() + 5
    os.utime(ws.cases_dir / "CASE-2" / "worklog.md", (t, t))
    fake.calls.clear()
    msg = sync.checkout(conf, ws)
    assert [c[2].rsplit("/", 1)[-1] for c in fake.calls if c[1] == "copy"] == ["CASE-3"] and "skipped (local changes newer than last checkin) 1 (CASE-2)" in msg
    os.utime(ws.cases_dir / "CASE-2" / "worklog.md", None)
    # dry: rclone に --dry-run、キャッシュは更新しない
    (ws.index_dir / "manifest.cache.json").unlink()
    fake.calls.clear()
    assert "would fetch 2" in sync.checkout(conf, ws, dry=True)
    assert all(c[-1] == "--dry-run" for c in fake.calls if c[1] == "copy") and not (ws.index_dir / "manifest.cache.json").exists()
    fake.manifest = None
    with pytest.raises(sync.RcloneError, match="manifest unavailable.*kairn manifest rebuild acme"):
        sync.checkout(conf, ws)


def test_manifest_fetch_failures_are_none(conf, monkeypatch):
    """rclone 不在・タイムアウト・非ゼロ・JSON でない → None。timeout を渡している。"""
    ws = conf.workspaces["acme"]
    seen = {}

    def run(cmd, **kw):
        seen["kw"] = kw
        return subprocess.CompletedProcess(cmd, 0, "not json", "")
    monkeypatch.setattr(subprocess, "run", run)
    assert sync.fetch_manifest(conf, ws) is None and seen["kw"]["timeout"] == sync.MANIFEST_TIMEOUT_SEC == 10
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, '{"cases": []}', ""))
    assert sync.fetch_manifest(conf, ws) is None
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, 10)))
    assert sync.fetch_manifest(conf, ws) is None
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError("rclone")))
    assert sync.fetch_manifest(conf, ws) is None and sync.refresh_manifest(conf, ws) is None
    assert not (ws.index_dir / "manifest.cache.json").exists() and sync.load_manifest_cache(ws) is None
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, '{"cases": {"CASE-1": {"rev": "a"}}}', ""))
    assert sync.refresh_manifest(conf, ws)["cases"]["CASE-1"]["rev"] == "a" and sync.load_manifest_cache(ws)["fetched_at"]


def test_drive_state(conf, fake):
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "t", "acme", actor="human")
    assert sync.drive_state(st, None, "CASE-1")["state"] == "unknown"                                   # manifest なし・未 checkin
    st.mark_checkin("CASE-1"); rev = st.load_case("CASE-1")["rev"]
    m = {"cases": {"CASE-1": {"rev": rev, "checked_in_at": "2026-09-01T00:00:00+09:00", "from": "host-a"}}}
    assert sync.drive_state(st, m, "CASE-1") == {"state": "synced", "rev": rev, "drive_rev": rev, "checked_in_at": "2026-09-01T00:00:00+09:00", "from": "host-a"}
    assert sync.drive_state(st, {"cases": {}}, "CASE-1")["state"] == "unknown"                            # エントリなし
    m["cases"]["CASE-1"]["rev"] = "newer"
    assert sync.drive_state(st, m, "CASE-1")["state"] == "drive_newer"
    t = time.time() + 5
    os.utime(ws.cases_dir / "CASE-1" / "worklog.md", (t, t))
    d = sync.drive_state(st, m, "CASE-1")
    assert d["state"] == "local_changes" and d["files"] == ["worklog.md"] and d["drive_differs"] is True
    m["cases"]["CASE-1"]["rev"] = rev
    assert sync.drive_state(st, m, "CASE-1")["drive_differs"] is False
    os.utime(ws.cases_dir / "CASE-1" / "worklog.md", None)


# ---------- manifest の同時更新（ホスト内ロック） ----------

class SlowCatRun(FakeRun):
    """cat の応答を遅らせる FakeRun（read-modify-write の競合窓を広げる）。"""

    def __init__(self, rules, delay: float = 0.05, **kw):
        super().__init__(rules, **kw)
        self.delay = delay

    def __call__(self, cmd, **kw):
        if cmd[:2] == ["rclone", "cat"]:
            time.sleep(self.delay)
        return super().__call__(cmd, **kw)


def _entries(*ids: str) -> dict[str, dict]:
    return {cid: {"rev": f"rev-{cid}", "checked_in_at": "2026-08-01T00:00:00+09:00", "from": "host-a"} for cid in ids}


def test_update_manifest_parallel_keeps_every_entry(conf, monkeypatch):
    """別案件の update_manifest を 4 スレッドで同時に呼んでも全エントリが残る（ロックで cat → rcat が直列化される）。
    ロックファイルは $XDG_STATE_HOME/kairn/locks/<ws>.manifest.lock。"""
    import threading
    ws = conf.workspaces["acme"]
    f = SlowCatRun(sync.raw_rules(conf)); f.manifest = {"cases": {"CASE-0": {"rev": "keep"}}, "updated_at": "x"}
    monkeypatch.setattr(subprocess, "run", f)
    ids = [f"CASE-{i}" for i in range(1, 5)]
    errors: list[BaseException] = []

    def go(cid):
        try:
            sync.update_manifest(conf, ws, _entries(cid))
        except BaseException as e:
            errors.append(e)
    ts = [threading.Thread(target=go, args=(cid,)) for cid in ids]
    for t in ts: t.start()
    for t in ts: t.join(10)
    assert errors == [] and set(f.manifest["cases"]) == {"CASE-0", *ids} and f.manifest["cases"]["CASE-0"] == {"rev": "keep"}
    assert [c[1] for c in f.calls] == ["cat", "rcat"] * 4    # 交錯しない
    assert sync.manifest_lock_path(ws) == Path(os.environ["XDG_STATE_HOME"]) / "kairn" / "locks" / "acme.manifest.lock"
    assert sync.manifest_lock_path(ws).exists()


def test_update_manifest_reads_only_after_lock(conf, fake):
    """ロックが他に握られている間は cat しない（ロック取得後に読み直す）。解放後に cat → rcat。"""
    import threading
    ws = conf.workspaces["acme"]
    fake.manifest = {"cases": {"CASE-0": {"rev": "keep"}}}
    done = threading.Event()

    def go():
        sync.update_manifest(conf, ws, _entries("CASE-1"))
        done.set()
    with sync.manifest_lock(ws):
        t = threading.Thread(target=go); t.start()
        time.sleep(0.4)
        assert fake.calls == [] and not done.is_set()
        fake.manifest = {"cases": {"CASE-0": {"rev": "keep"}, "CASE-2": {"rev": "written while waiting"}}}
    assert done.wait(5) and [c[1] for c in fake.calls] == ["cat", "rcat"]
    assert set(fake.manifest["cases"]) == {"CASE-0", "CASE-1", "CASE-2"}    # ロック前の値ではなく待った後の値に足す


def test_manifest_lock_timeout_is_recorded_in_checkin_result(conf, fake, monkeypatch):
    """ロック待ちが上限を超えたら ManifestLockTimeout（RcloneError）。checkin は「転送は済んだが manifest 更新失敗」として返し、
    版マーカーは残す。ジョブ経路（checkin_job。転送は Popen）では job.error に残る。"""
    from kairn.jobs import JobTable
    from tests.test_jobs import FakePopen
    FakePopen.calls = []; FakePopen.rc = 0
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(sync, "MANIFEST_LOCK_TIMEOUT_SEC", 0.3)
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "t", "acme", actor="human")
    with sync.manifest_lock(ws):
        t0 = time.monotonic()
        with pytest.raises(sync.RcloneError, match=r"transferred, but manifest update failed \(manifest lock .*not acquired within 0.3s"):
            sync.checkin(conf, ws, "CASE-1")
        assert 0.3 <= time.monotonic() - t0 < 5
        assert [c[1] for c in fake.calls] == ["copyto", "sync"] and fake.manifest is None    # cat / rcat は呼ばない
        assert st.load_case("CASE-1")["rev"] and st.load_case("CASE-1")["last_checkin_at"]
        table = JobTable()
        job, _ = table.submit("checkin", ws.name, "CASE-1", lambda p: sync.checkin_job(conf, ws, "CASE-1", "test-agent", p))
        assert job.wait(5) and job.status == "failed" and "manifest update failed" in job.error and "manifest lock" in job.error
        with pytest.raises(sync.ManifestLockTimeout):
            with sync.manifest_lock(ws, timeout=0.1):
                pass
    # 解放後は通る
    fake.calls.clear()
    sync.checkin(conf, ws, "CASE-1")
    assert [c[1] for c in fake.calls] == ["copyto", "sync", "cat", "rcat"] and set(fake.manifest["cases"]) == {"CASE-1"}


def test_merge_manifest_never_drops_entries(conf, fake, monkeypatch):
    """update_manifest は読み込んだ manifest のエントリを減らさない: merge_manifest は自分の案件だけ置き換えて他を保ち、
    書き戻す直前の refuse_entry_loss が（万一）減っていれば ManifestWriteRefused で rcat を止める。"""
    read = {"cases": {"CASE-1": {"rev": "a"}, "CASE-2": {"rev": "b"}}, "updated_at": "x", "extra": 1}
    m = sync.merge_manifest(read, {"CASE-2": {"rev": "b2"}, "CASE-3": {"rev": "c"}})
    assert m["cases"] == {"CASE-1": {"rev": "a"}, "CASE-2": {"rev": "b2"}, "CASE-3": {"rev": "c"}} and m["extra"] == 1 and m["updated_at"] != "x"
    assert read["cases"]["CASE-2"] == {"rev": "b"}                                     # 引数は変更しない
    assert sync.merge_manifest(None, {"CASE-1": {"rev": "a"}}) ["cases"] == {"CASE-1": {"rev": "a"}}
    assert sync.merge_manifest({"cases": {"CASE-1": {"rev": "a"}}}, {})["cases"] == {"CASE-1": {"rev": "a"}}
    for base in ({"cases": {}}, None, read):
        for ents in ({}, {"CASE-1": {"rev": "z"}}, {"CASE-9": {"rev": "n"}}):
            assert set((base or {}).get("cases", {})) <= set(sync.merge_manifest(base, ents)["cases"])
    sync.refuse_entry_loss(read, m); sync.refuse_entry_loss(None, m); sync.refuse_entry_loss(read, read)
    with pytest.raises(sync.ManifestWriteRefused, match=r"1 existing entry would be dropped \(CASE-2\)"):
        sync.refuse_entry_loss(read, {"cases": {"CASE-1": {"rev": "a"}, "CASE-3": {}}})
    # update_manifest の経路: マージ結果が減っていたら rcat しない（RcloneError の一種なので checkin は「manifest 更新失敗」で返す）
    ws = conf.workspaces["acme"]
    fake.manifest = {"cases": {"CASE-1": {"rev": "a"}, "CASE-2": {"rev": "b"}}}
    monkeypatch.setattr(sync, "merge_manifest", lambda base, entries: {"cases": dict(entries)})
    with pytest.raises(sync.ManifestWriteRefused, match="CASE-1, CASE-2"):
        sync.update_manifest(conf, ws, {"CASE-3": {"rev": "c"}})
    assert [c[1] for c in fake.calls] == ["cat"] and set(fake.manifest["cases"]) == {"CASE-1", "CASE-2"}
    assert not (ws.index_dir / "manifest.cache.json").exists()


def test_manifest_rebuild_holds_lock(conf, fake):
    """manifest_rebuild（dry でない）は列挙〜書き戻しをロックの中で行う。dry はロックを取らない。"""
    ws = conf.workspaces["acme"]
    fake.remote_cases = {"CASE-1": {"id": "CASE-1", "title": "t", "rev": "r1", "last_checkin_at": "2026-08-01T00:00:00+09:00"}}
    seen = {}
    orig = sync.write_manifest

    def probe(conf_, ws_, manifest):
        try:
            with sync.manifest_lock(ws_, timeout=0.1):
                seen["locked_during_write"] = False
        except sync.ManifestLockTimeout:
            seen["locked_during_write"] = True
        return orig(conf_, ws_, manifest)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sync, "write_manifest", probe)
        sync.manifest_rebuild(conf, ws)
    assert seen == {"locked_during_write": True} and set(fake.manifest["cases"]) == {"CASE-1"}
    with sync.manifest_lock(ws):
        assert sync.manifest_rebuild(conf, ws, dry=True)["cases"]["CASE-1"]["rev"] == "r1"     # dry は待たない


# ---------- manifest rebuild（既存 Drive データの移行） ----------

def test_manifest_rebuild_assigns_rev_and_aligns_local(conf, fake):
    """Drive の cases/*/case.json を列挙して読み、rev が無ければ付与して rcat で書き戻し（last_checkin_at は既存値を維持、無ければ
    Drive のファイル更新時刻）、manifest.json を作り直す。ローカルの同じ案件（checkin 済み・変更なし）にも同じ rev を書く。"""
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "one", "acme", actor="human"); st.create_case("CASE-2", "two", "acme", actor="human")
    st.create_case("CASE-5", "local only", "acme", actor="human")
    for cid in ("CASE-1", "CASE-2"):   # 旧方式で checkin 済み（rev 無し）: last_checkin_at より古い mtime にする
        c = st.load_case(cid); c["last_checkin_at"] = "2026-08-20T10:00:00+09:00"; c["last_checkin_events"] = 1; st.save_case(c)
        for name in ("case.json", "worklog.md", "events.jsonl"):
            old = time.time() - 30 * DAY
            os.utime(ws.cases_dir / cid / name, (old, old))
    fake.remote_cases = {"CASE-1": {"id": "CASE-1", "title": "one", "status": "open"},                                            # rev / last_checkin_at 無し
                         "CASE-2": {"id": "CASE-2", "title": "two", "status": "open", "rev": "r2", "last_checkin_at": "2026-08-21T00:00:00+09:00", "checked_in_from": "host-b"},
                         "CASE-3": {"id": "CASE-3", "title": "drive only", "status": "closed"},
                         "CASE-4": "not an object"}
    t = time.time() + 5
    os.utime(ws.cases_dir / "CASE-2" / "worklog.md", (t, t))     # 未 checkin のローカル変更 → ローカルは触らない
    cj1 = ws.cases_dir / "CASE-1" / "case.json"; mtime1 = cj1.stat().st_mtime
    # dry-run: 変更内容を返すだけで何も書かない
    r = sync.manifest_rebuild(conf, ws, dry=True)
    assert r["dry"] is True and fake.rcats == [] and fake.manifest is None and "rev" not in st.load_case("CASE-1")
    assert r["cases"]["CASE-1"]["rev_assigned"] and r["cases"]["CASE-1"]["local"] == "updated" and r["cases"]["CASE-1"]["remote"] == "updated"
    assert not r["cases"]["CASE-2"]["rev_assigned"] and r["cases"]["CASE-2"]["local"] == "skipped" and r["local_skipped"] == ["CASE-2"]
    assert r["cases"]["CASE-3"]["local"] == "absent" and "CASE-4" in r["errors"] and "CASE-5" not in r["cases"]
    assert not (ws.index_dir / "manifest.cache.json").exists()
    # 実行
    r = sync.manifest_rebuild(conf, ws)
    lsf = [c for c in fake.calls if c[1] == "lsf"][0]
    assert lsf[2:] == ["-R", "--files-only", "--format", "pt", "--separator", "\t", "--max-depth", "2", "--include", "/*/case.json", "my-drive:ws/acme/cases"]
    assert [p.rsplit("/", 2)[-2] if p.endswith("case.json") else p for p, _ in fake.rcats] == ["CASE-1", "CASE-3", "my-drive:ws/acme/manifest.json"]
    rev1 = fake.remote_cases["CASE-1"]["rev"]
    assert len(rev1) == 36 and fake.remote_cases["CASE-1"]["last_checkin_at"] == sync._lsf_time_to_iso(fake.remote_case_mtime)
    assert fake.remote_cases["CASE-2"] == {"id": "CASE-2", "title": "two", "status": "open", "rev": "r2", "last_checkin_at": "2026-08-21T00:00:00+09:00", "checked_in_from": "host-b"}
    assert fake.manifest["cases"] == {"CASE-1": {"rev": rev1, "checked_in_at": fake.remote_cases["CASE-1"]["last_checkin_at"], "from": ""},
                                      "CASE-2": {"rev": "r2", "checked_in_at": "2026-08-21T00:00:00+09:00", "from": "host-b"},
                                      "CASE-3": {"rev": fake.remote_cases["CASE-3"]["rev"], "checked_in_at": fake.remote_cases["CASE-3"]["last_checkin_at"], "from": ""}}
    assert fake.manifest["updated_at"] and r["cases"]["CASE-1"]["rev"] == rev1 == st.load_case("CASE-1")["rev"]
    assert cj1.stat().st_mtime == mtime1 and st.local_changes_since_checkin("CASE-1") == []          # ローカルの mtime は動かさない
    assert "rev" not in st.load_case("CASE-2") and "rev" not in st.load_case("CASE-5")
    assert json.loads((ws.index_dir / "manifest.cache.json").read_text(encoding="utf-8"))["cases"] == fake.manifest["cases"]
    # 2 回目: すべて既存の rev を保つ（Drive にもローカルにも書かない）
    fake.rcats.clear()
    r = sync.manifest_rebuild(conf, ws)
    assert [p for p, _ in fake.rcats] == ["my-drive:ws/acme/manifest.json"] and r["cases"]["CASE-1"]["local"] == "same"
    os.utime(ws.cases_dir / "CASE-2" / "worklog.md", None)
    # 未 checkin（last_checkin_at 無し）のローカル案件は触らない（never checked in）
    c5 = st.load_case("CASE-5"); fake.remote_cases["CASE-5"] = {"id": "CASE-5", "rev": "r5", "last_checkin_at": "2026-08-01T00:00:00+09:00"}
    r = sync.manifest_rebuild(conf, ws)
    assert r["cases"]["CASE-5"]["local"] == "skipped" and r["cases"]["CASE-5"]["local_reason"] == "never checked in" and "rev" not in st.load_case("CASE-5")
    assert sync.manifest_rebuild(conf, ws)["cases"]["CASE-5"]["remote"] == "same"
    # lsf の失敗は RcloneError
    fake.fail = {"lsf"}
    with pytest.raises(sync.RcloneError):
        sync.manifest_rebuild(conf, ws)


def test_lsf_time_to_iso():
    iso = sync._lsf_time_to_iso("2026-08-15 09:30:00.123456789")
    assert iso is not None and iso.startswith("2026-08-15T09:30:00") and ("+" in iso[19:] or "-" in iso[19:])
    assert sync._lsf_time_to_iso("garbage") is None


# ---------- rules.rclone_flags: rclone を呼ぶすべての箇所で共通引数の後ろに付く ----------

FLAGS = ["--transfers", "8", "--checkers", "16", "--drive-pacer-min-sleep", "10ms", "--drive-pacer-burst", "200"]


def _tail_flags(cmd: list[str]) -> list[str]:
    """rclone コマンドの末尾（--dry-run があればその前）が FLAGS か。"""
    body = cmd[:-1] if cmd[-1] == "--dry-run" else cmd
    return body[-len(FLAGS):]


def test_rclone_flags_appended_to_every_rclone_command(conf, fake, monkeypatch):
    """checkout（copyto + copy）/ checkin（copyto + sync|copy + manifest cat・rcat）/ raw_move（lsf + move）/ drive_index（lsf）/
    manifest（fetch・write・rebuild の lsf・cat・rcat）/ ws（lsd・mkdir・lsf）の全 rclone 呼び出しの末尾に rules.rclone_flags が付く。
    既定（未設定）では何も付かない。"""
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-123", "t", "acme", actor="human")
    _touch(ws.cases_dir / "CASE-123" / "run.bag", 10, 20 * DAY)
    fake.manifest = {"cases": {}, "updated_at": "x"}
    fake.remote_cases = {"CASE-9": {"id": "CASE-9", "title": "t"}}
    conf.rules["rclone_flags"] = list(FLAGS)
    sync.checkout(conf, ws, "CASE-123")
    sync.checkin(conf, ws, "CASE-123")
    sync.checkin(conf, ws)
    sync.raw_move(conf, ws, "CASE-123")
    sync.drive_index(conf, ws)
    sync.fetch_manifest(conf, ws); sync.write_manifest(conf, ws, {"cases": {}})
    sync.fetch_remote_events(conf, ws, "CASE-123")
    sync.manifest_rebuild(conf, ws)
    sync.ws_exists_on_drive(conf, "acme"); sync.create_ws_on_drive(conf, "acme"); sync.list_ws_on_drive(conf)
    subs = {c[1] for c in fake.calls}
    assert {"copyto", "copy", "sync", "cat", "rcat", "lsf", "move", "lsd", "mkdir"} <= subs
    for c in fake.calls:
        assert c[0] == "rclone" and _tail_flags(c) == FLAGS, c
    # --dry-run は flags の後ろ（_run が最後に足す）
    fake.calls.clear()
    sync.checkin(conf, ws, "CASE-123", dry=True)
    assert fake.calls[-1][-1] == "--dry-run" and _tail_flags(fake.calls[-1]) == FLAGS
    # 既定は何も付かない
    fake.calls.clear(); conf.rules.pop("rclone_flags")
    sync.checkout(conf, ws, "CASE-123"); sync.drive_index(conf, ws)
    assert all("--checkers" not in c and "--drive-pacer-burst" not in c for c in fake.calls)
    # 設定ファイルに不正な値が入っていたら rclone を呼ぶ前に ValueError（黙って渡さない）
    conf.rules["rclone_flags"] = ["-v"]
    with pytest.raises(ValueError, match="invalid rclone flag"):
        sync.drive_index(conf, ws)


def test_rclone_flags_on_progress_path_uses_popen(conf, fake, monkeypatch):
    """progress 付き（MCP のジョブ経路）は subprocess.Popen。そこにも flags が付く。"""
    from tests.test_jobs import FakePopen
    FakePopen.calls = []; FakePopen.rc = 0
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    ws = conf.workspaces["acme"]
    CaseStore(ws.cases_dir).create_case("CASE-123", "t", "acme", actor="human")
    conf.rules["rclone_flags"] = list(FLAGS)
    sync.checkout(conf, ws, "CASE-123", progress=lambda line: None)
    sync.checkin(conf, ws, "CASE-123", progress=lambda line: None)
    assert [c[1] for c in FakePopen.calls] == ["copy", "sync"]
    assert all(_tail_flags(c) == FLAGS for c in FakePopen.calls)


def test_parse_rclone_flags():
    ok = "--transfers 8 --checkers 16 --drive-pacer-min-sleep 10ms --drive-pacer-burst 200"
    assert sync.parse_rclone_flags(ok) == ok.split()
    assert sync.parse_rclone_flags("  --fast-list   --transfers=8 --checkers 16 ") == ["--fast-list", "--transfers=8", "--checkers", "16"]
    assert sync.parse_rclone_flags("") == [] and sync.parse_rclone_flags([]) == []
    assert sync.parse_rclone_flags(["--transfers", "8"]) == ["--transfers", "8"]
    for bad in ("-v", "--transfers 8 16", "8 --transfers", "--", "--transfers=8 16", "--transfers -1", "rm -rf"):
        with pytest.raises(ValueError, match="invalid rclone flag"):
            sync.parse_rclone_flags(bad)


@pytest.mark.skipif(not shutil.which("rclone"), reason="rclone not installed")
def test_recommended_rclone_flags_accepted_by_real_rclone(tmp_path, monkeypatch):
    """README の推奨値（--drive-pacer-* を含む）を実 rclone がローカル間 copy でも受け付ける（クラウド接続なし。RCLONE_CONFIG は空ファイル）。"""
    monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "rclone-empty.conf"))
    src, dst = tmp_path / "src", tmp_path / "dst"
    _touch(src / "a.txt", 3)
    flags = sync.parse_rclone_flags("--transfers 8 --checkers 16 --drive-pacer-min-sleep 10ms --drive-pacer-burst 200")
    r = subprocess.run(["rclone", "copy", str(src), str(dst), *flags], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (dst / "a.txt").exists()
