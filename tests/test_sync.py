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

    def __init__(self, rules, fail_move: bool = False, fail: set[str] | None = None):
        self.calls: list[list[str]] = []
        self.rules = rules
        self.fail_move = fail_move
        self.fail = fail or set()

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        prog, sub = cmd[0], cmd[1]
        if prog == "rclone" and sub in self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", f"fake rclone {sub} failed")
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
            if self.fail_move:
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
    sub = _touch(case / "build" / "x.bag", 10, 3600)
    assert sync.bag_candidates(case) == sorted([old, act, sub])


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
    sync.checkout(conf, ws, dry=True)
    cmd = fake.calls[-1]
    assert cmd[cmd.index("--bwlimit") + 1] == "08:00,4M 20:00,off" and cmd[-1] == "--dry-run"
    sync.checkin(conf, ws, "CASE-123") if (ws.cases_dir / "CASE-123").mkdir(parents=True, exist_ok=True) is None else None
    cmd = fake.calls[-1]
    assert cmd[:2] == ["rclone", "sync"] and "--bwlimit" in cmd and "--backup-dir" in cmd


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
        assert "- **/target/**" in m and m.index("- **/target/**") < (m.index("- **") if "- **" in m else len(m))  # exclude が先
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
    assert [c for c in fake.calls if c[1] == "sync"][0][-1] == "--dry-run"


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
