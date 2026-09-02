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
    """subprocess.run の代役。rclone: 引数を記録し、lsf はローカルを自前で列挙、move は対象ファイルを削除して成功を返す。
    Drive 側の版マーカー（cases/<case>/.rev/*）は remote_markers（{case: [rev, …]}）で真似る: lsf で読め、案件単位の sync と
    .rev/ 限定の sync（sync_rev_markers / drive_markers）で置き換わり、ワークスペース全体の copy では足されるだけ（古いものは残る）。"""

    def __init__(self, rules, fail_move: bool = False, fail: set[str] | None = None, fail_move_nth: int | None = None,
                 remote_events: dict[str, list[str]] | None = None):
        self.calls: list[list[str]] = []
        self.rules = rules
        self.remote_events = remote_events or {}  # copyto: {case: Drive 版 events.jsonl の行}。無い案件は失敗（object not found）
        self.fail_move = fail_move
        self.fail_move_nth = fail_move_nth  # n 回目の move だけ失敗させる（1 始まり）
        self.moves = 0
        self.fail = fail or set()
        self.remote_markers: dict[str, list[str]] = {}   # Drive 上の cases/<case>/.rev/ の中身（無い案件は .rev/ 自体が無い）
        self.remote_cases: dict[str, dict] = {}          # Drive 上の cases/<case>/case.json（drive_markers の copy --include が取り寄せる）
        self.deleted: list[str] = []                     # deletefile の対象

    def _local_markers(self, case_dir: Path) -> list[str]:
        d = case_dir / ".rev"
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        prog, sub = cmd[0], cmd[1]
        if prog == "rclone" and sub in self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", f"fake rclone {sub} failed")
        if prog == "rclone" and sub == "lsf" and "--include" in cmd and cmd[cmd.index("--include") + 1] == "/cases/*/.rev/*":   # drive_revs
            rows = [f"cases/{cid}/.rev/{m}" for cid in sorted(self.remote_markers) for m in self.remote_markers[cid]]
            return subprocess.CompletedProcess(cmd, 0, "\n".join(rows) + ("\n" if rows else ""), "")
        if prog == "rclone" and sub == "lsf" and cmd[2].endswith("/.rev/"):                                              # drive_rev（1 案件）
            case = cmd[2].rsplit("/", 2)[-2]
            if case not in self.remote_markers:
                return subprocess.CompletedProcess(cmd, 3, "", "fake rclone lsf: directory not found")
            return subprocess.CompletedProcess(cmd, 0, "".join(m + "\n" for m in self.remote_markers[case]), "")
        if prog == "rclone" and sub == "copy" and "--include" in cmd and cmd[cmd.index("--include") + 1] == "/cases/*/case.json":   # drive_markers
            for cid, case in self.remote_cases.items():
                f = Path(cmd[3]) / "cases" / cid / "case.json"
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(case if isinstance(case, str) else json.dumps(case), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "fake rclone copy ok")
        if prog == "rclone" and sub == "sync" and "--dry-run" not in cmd:
            src = Path(cmd[2])
            if "- **" in cmd:                                                                                          # .rev/ 限定の sync
                for x in cmd:
                    if x.startswith("+ /") and x.endswith("/.rev/**"):
                        cid = x[3:-len("/.rev/**")]
                        self.remote_markers[cid] = self._local_markers(src / cid)
            else:                                                                                                       # 案件単位の sync
                self.remote_markers[src.name] = self._local_markers(src)
            return subprocess.CompletedProcess(cmd, 0, "", "fake rclone sync ok")
        if prog == "rclone" and sub == "copy" and "--dry-run" not in cmd and not cmd[2].startswith("my-drive:"):        # ワークスペース全体の copy（local → Drive）
            src = Path(cmd[2])
            for d in src.iterdir():
                if d.is_dir() and self._local_markers(d):
                    self.remote_markers[d.name] = sorted(set(self.remote_markers.get(d.name, [])) | set(self._local_markers(d)))
            return subprocess.CompletedProcess(cmd, 0, "", "fake rclone copy ok")
        if prog == "rclone" and sub == "deletefile":
            self.deleted.append(cmd[2])
            return subprocess.CompletedProcess(cmd, 0, "", "")
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
                if not p.is_file() or p.is_symlink() or ".rev" in p.relative_to(src).parts:
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
    assert cmd[cmd.index("--max-size") + 1] == "50M" and "- *.bag" in cmd and cmd[cmd.index("--filter") + 1] == "+ .rev/**"
    conf.rules["bwlimit"] = "08:00,4M 20:00,off"
    fake.remote_markers = {"CASE-123": ["r1"]}   # ワークスペース全体は Drive のマーカーの rev が違う案件（ローカルに無い）だけ
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
    cmd = [c for c in fake.calls if c[1] in ("sync", "copy")][-2]
    assert cmd[:4] == ["rclone", "copy", str(ws.cases_dir), "my-drive:ws/acme/cases"]
    assert cmd[cmd.index("--backup-dir") + 1].startswith("my-drive:ws/acme/_deleted/")
    cmd = fake.calls[-1]                                                     # 続いて .rev/ 限定の sync（振り直した案件だけ、--backup-dir 無し）
    assert cmd[:4] == ["rclone", "sync", str(ws.cases_dir), "my-drive:ws/acme/cases"] and cmd[4:] == ["--filter", "+ /CASE-1/.rev/**", "--filter", "- **"]
    sync.checkin(conf, ws, "CASE-1")
    cmd = [c for c in fake.calls if c[1] in ("sync", "copy")][-1]
    assert cmd[:4] == ["rclone", "sync", str(ws.cases_dir / "CASE-1"), "my-drive:ws/acme/cases/CASE-1"] and "--backup-dir" in cmd
    sync.daily(conf, ws)
    assert [c[1] for c in fake.calls if c[1] in ("sync", "copy")][-1] == "copy"   # 変更の無い案件は振り直さず、マーカーの sync も無い


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
    monkeypatch.setattr(sync, "checkout_workspace", lambda *a, **k: order.append("checkout") or "fetched 0")
    monkeypatch.setattr(sync, "checkin", lambda *a, **k: order.append("checkin") or (_ for _ in ()).throw(sync.RcloneError("remote down")))
    monkeypatch.setattr(sync, "raw_move", lambda *a, **k: order.append("raw_move") or {"files": 0})
    monkeypatch.setattr(sync, "drive_index", lambda *a, **k: order.append("drive_index") or ws.index_dir / "drive-index.txt")
    r = sync.daily(conf, ws)
    assert order == ["bag2zst", "checkout", "checkin", "raw_move", "drive_index"]
    assert list(r["steps"]) == ["bag2zst", "checkout", "checkin", "raw_move", "drive_index", "index"]
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
    assert r["steps"]["checkout"]["result"].startswith("drive: 0 case(s); would fetch 0") and not (ws.index_dir / "drive_revs.cache.json").exists()


def test_daily_checks_out_only_differing_cases(conf, fake):
    """daily の checkout 段: Drive の版を lsf 1 回で読み、rev が違う案件だけ取り寄せてから checkin する。"""
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    for cid in ("CASE-1", "CASE-2"):
        st.create_case(cid, cid, "acme", actor="human"); st.mark_checkin(cid)
    fake.remote_markers = {"CASE-1": [st.load_case("CASE-1")["rev"]], "CASE-2": ["from-another-host"], "CASE-3": ["r3"]}
    r = sync.daily(conf, ws)
    assert r["ok"] is True and "fetched 2 (CASE-2, CASE-3)" in r["steps"]["checkout"]["result"] and "up to date 1" in r["steps"]["checkout"]["result"]
    lsf = [c for c in fake.calls if c[1] == "lsf" and "/cases/*/.rev/*" in c]
    assert len(lsf) == 1 and [c[2].rsplit("/", 1)[-1] for c in fake.calls if c[1] == "copy" and c[2].startswith("my-drive:")] == ["CASE-2", "CASE-3"]
    assert json.loads((ws.index_dir / "drive_revs.cache.json").read_text(encoding="utf-8"))["revs"]["CASE-3"] == "r3"


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
    """rclone コマンドの除外パターン（`--filter '- <pat>'` の <pat>。rules.exclude 由来の target/** 等も含む）。"""
    return [cmd[i + 1][2:] for i, x in enumerate(cmd) if x == "--filter" and cmd[i + 1].startswith("- ")]


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
    assert [c[1] for c in f.calls] == ["copyto", "sync"]   # マージ → 転送（集計ファイルの更新は無い）
    assert f.calls[1][:4] == ["rclone", "sync", str(ws.cases_dir / "CASE-123"), "my-drive:ws/acme/cases/CASE-123"]
    assert not any(x.endswith("events.jsonl") for x in _excludes(f.calls[1]))  # マージ済みの events.jsonl をそのまま Drive へ
    assert [e["note"] for e in _events_of(ws, "CASE-123")] == ["remote only", "remote only 2", "case created: t"]
    assert st.load_case("CASE-123")["last_checkin_events"] == 3 and msg.endswith("[events merged: 1]") and "manifest" not in msg
    # 取得失敗 → マージなしで sync（従来どおり）
    f2 = FakeRun(sync.raw_rules(conf), fail={"copyto"})
    monkeypatch.setattr(subprocess, "run", f2)
    sync.checkin(conf, ws, "CASE-123")
    assert [c[1] for c in f2.calls] == ["copyto", "sync"] and len(_events_of(ws, "CASE-123")) == 3


def test_checkout_and_checkin_workspace_merge_per_case(conf, monkeypatch):
    """ワークスペース全体: checkout は Drive の版マーカー（lsf 1 回）を見て rev が違う案件（ローカルに無い案件を含む）だけを 1 案件ずつ
    マージ → copy --update する（ワークスペース全体の copy はしない）。checkin は各案件をマージ → copy → 振り直した案件の .rev/ を sync。"""
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "a", "acme", actor="human")
    st.create_case("CASE-2", "b", "acme", actor="human")
    remote = {"CASE-1": [_ev("2026-08-01T00:00:00+09:00", "r1")], "CASE-9": [_ev("2026-08-01T00:00:00+09:00", "r9")]}
    f = FakeRun(sync.raw_rules(conf), remote_events=remote)
    f.remote_markers = {"CASE-1": ["d1"], "CASE-2": ["d2"], "CASE-9": ["d9"]}
    monkeypatch.setattr(subprocess, "run", f)
    msg = sync.checkout(conf, ws)
    assert [(c[1], c[2].rsplit("/", 2)[-2] if c[1] == "copyto" else c[2].rsplit("/", 1)[-1]) for c in f.calls] == \
        [("lsf", "-R"), ("copyto", "CASE-1"), ("copy", "CASE-1"), ("copyto", "CASE-2"), ("copy", "CASE-2"), ("copyto", "CASE-9"), ("copy", "CASE-9")]
    assert f.calls[0][2:] == ["-R", "--files-only", "--include", "/cases/*/.rev/*", "my-drive:ws/acme"]
    assert all("--update" in c and "/events.jsonl" in _excludes(c) for c in f.calls if c[1] == "copy" and c[2].endswith(("CASE-1", "CASE-9")))
    assert "/events.jsonl" not in _excludes([c for c in f.calls if c[1] == "copy"][1])   # CASE-2 は Drive に events が無い → 除外しない
    assert [e["note"] for e in _events_of(ws, "CASE-1")] == ["r1", "case created: a"]
    assert [e["note"] for e in _events_of(ws, "CASE-2")] == ["case created: b"]
    assert [e["note"] for e in _events_of(ws, "CASE-9")] == ["r9"]
    assert "fetched 3 (CASE-1, CASE-2, CASE-9)" in msg and "up to date 0" in msg
    f.calls.clear()
    sync.checkin(conf, ws)
    assert [c[1] for c in f.calls] == ["copyto", "copyto", "copyto", "copy", "sync"]
    assert not any(x.endswith("events.jsonl") for x in _excludes(f.calls[3]))
    assert st.load_case("CASE-1")["last_checkin_events"] == 2
    assert f.calls[4][2:4] == [str(ws.cases_dir), "my-drive:ws/acme/cases"] and "+ /CASE-1/.rev/**" in f.calls[4] and "+ /CASE-2/.rev/**" in f.calls[4]
    assert "+ /CASE-9/.rev/**" not in f.calls[4] and f.calls[4][-1] == "- **"
    assert f.remote_markers["CASE-9"] == ["d9"]                                                        # 案件なしのディレクトリは触らない
    assert f.remote_markers["CASE-1"] == [st.load_case("CASE-1")["rev"]] and st.load_case("CASE-1")["rev"] != "d1"


# ---------- 版マーカー（rev と cases/<case>/.rev/<rev>） ----------

def _markers(ws, case) -> list[str]:
    d = ws.cases_dir / case / ".rev"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def test_checkin_stamps_rev_and_places_marker(conf, fake):
    """checkin(case): 転送前に case.json へ rev（uuid4）/ last_checkin_at / checked_in_from を書き、案件フォルダの .rev/ を <rev> 1 個に
    作り直してから rclone sync（Drive 側の古いマーカーは sync が消す）。集計ファイルは読み書きしない。キャッシュの当該案件も更新。"""
    from kairn import store as store_mod
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "t", "acme", actor="human")
    (ws.cases_dir / "CASE-1" / ".rev").mkdir(); (ws.cases_dir / "CASE-1" / ".rev" / "stale-local").touch()
    fake.remote_markers = {"CASE-0": ["keep"], "CASE-1": ["stale-drive"]}
    msg = sync.checkin(conf, ws, "CASE-1")
    c = st.load_case("CASE-1")
    assert len(c["rev"]) == 36 and c["last_checkin_at"] and c["checked_in_from"] == store_mod.hostname()
    assert [x[1] for x in fake.calls] == ["copyto", "sync"] and "manifest" not in msg
    assert _markers(ws, "CASE-1") == [c["rev"]] and fake.remote_markers == {"CASE-0": ["keep"], "CASE-1": [c["rev"]]}
    assert "- **" not in fake.calls[1] and "+ .rev/**" in fake.calls[1]       # 案件単位の sync は .rev/ を含めて全体を揃える
    cache = json.loads((ws.index_dir / "drive_revs.cache.json").read_text(encoding="utf-8"))
    assert cache == {"revs": {"CASE-1": c["rev"]}, "fetched_at": None}
    # 2 回目は rev が変わり、マーカーも入れ替わる
    rev1 = c["rev"]
    sync.checkin(conf, ws, "CASE-1")
    assert st.load_case("CASE-1")["rev"] != rev1 and _markers(ws, "CASE-1") == [st.load_case("CASE-1")["rev"]] == fake.remote_markers["CASE-1"]
    # dry では何も書かない
    fake.calls.clear(); rev = st.load_case("CASE-1")["rev"]
    sync.checkin(conf, ws, "CASE-1", dry=True)
    assert [x[1] for x in fake.calls] == ["sync"] and st.load_case("CASE-1")["rev"] == rev and _markers(ws, "CASE-1") == [rev]


def test_checkin_transfer_failure_restores_case_json_and_marker(conf, monkeypatch):
    """転送（rclone sync）が失敗したら版マーカーは書く前の内容・mtime に戻り、.rev/ もそれに合わせる（未付与なら消える）。"""
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
    assert [x[1] for x in f.calls] == ["copyto", "sync"] and not (ws.cases_dir / "CASE-1" / ".rev").exists()
    # checkin 済みの案件: 失敗すると前の rev とそのマーカーに戻る
    st.mark_checkin("CASE-1"); rev = st.load_case("CASE-1")["rev"]
    with pytest.raises(sync.RcloneError, match="sync failed"):
        sync.checkin(conf, ws, "CASE-1")
    assert st.load_case("CASE-1")["rev"] == rev and _markers(ws, "CASE-1") == [rev]


def test_checkin_workspace_stamps_only_changed_cases(conf, fake):
    """ワークスペース全体（daily）: 未 checkin の案件と last_checkin_at より新しい変更のある案件だけ rev を振り直す
    （変更の無い案件の rev を毎日変えない）。copy の後、振り直した案件の .rev/ だけを sync で揃える（古いマーカーを消す）。
    変更の無い案件も転送前にマーカーを作り直す（欠けていれば補われ、copy で Drive に足される）。"""
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "a", "acme", actor="human"); st.create_case("CASE-2", "b", "acme", actor="human")
    sync.checkin(conf, ws)
    r1, r2 = st.load_case("CASE-1")["rev"], st.load_case("CASE-2")["rev"]
    assert r1 and r2 and fake.remote_markers == {"CASE-1": [r1], "CASE-2": [r2]}
    assert [x[1] for x in fake.calls] == ["copyto", "copyto", "copy", "sync"] and fake.calls[3][-1] == "- **"
    assert [x for x in fake.calls[3] if x.startswith("+ ")] == ["+ /CASE-1/.rev/**", "+ /CASE-2/.rev/**"]
    fake.calls.clear()
    shutil.rmtree(ws.cases_dir / "CASE-2" / ".rev")                                         # マーカーが欠けた案件
    fake.remote_markers["CASE-2"] = []
    sync.checkin(conf, ws)
    assert [x[1] for x in fake.calls] == ["copyto", "copyto", "copy"]                          # 変更なし: rev はそのまま、マーカー sync も無し
    assert (st.load_case("CASE-1")["rev"], st.load_case("CASE-2")["rev"]) == (r1, r2)
    assert _markers(ws, "CASE-2") == [r2] and fake.remote_markers["CASE-2"] == [r2]           # 欠けたマーカーは補われ copy で Drive へ
    t = time.time() + 5
    os.utime(ws.cases_dir / "CASE-1" / "worklog.md", (t, t))
    fake.calls.clear()
    sync.checkin(conf, ws)
    assert [x[1] for x in fake.calls] == ["copyto", "copyto", "copy", "sync"]
    assert [x for x in fake.calls[3] if x.startswith("+ ")] == ["+ /CASE-1/.rev/**"]       # 振り直した案件だけ
    new1 = st.load_case("CASE-1")["rev"]
    assert new1 != r1 and st.load_case("CASE-2")["rev"] == r2
    assert fake.remote_markers == {"CASE-1": [new1], "CASE-2": [r2]} and _markers(ws, "CASE-1") == [new1]   # 古い r1 は Drive からも消える
    os.utime(ws.cases_dir / "CASE-1" / "worklog.md", None)


def test_checkout_writes_marker_from_case_json(conf, fake):
    """checkout(case): copy --update の後に .rev/ を case.json の rev から作り直す（Drive から来た古いマーカーと並ばない）。
    dry では触らない。rev の無い案件では .rev/ を消す。"""
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "t", "acme", actor="human")
    st.set_rev("CASE-1", "new-rev")
    d = ws.cases_dir / "CASE-1" / ".rev"
    (d / "old-from-drive").touch()                       # copy --update が持ち込んだ古いマーカーを模す
    sync.checkout(conf, ws, "CASE-1", dry=True)
    assert _markers(ws, "CASE-1") == ["new-rev", "old-from-drive"]
    sync.checkout(conf, ws, "CASE-1")
    assert _markers(ws, "CASE-1") == ["new-rev"]
    c = st.load_case("CASE-1"); del c["rev"]; st.save_case(c)
    sync.checkout(conf, ws, "CASE-1")
    assert not d.exists()


def test_checkout_workspace_fetches_only_rev_mismatch(conf, fake):
    """kairn checkout <ws>: Drive のマーカーの rev と違う案件（ローカルに無い案件・マーカーが 2 個以上の不定な案件を含む）だけ取り寄せる。
    一致は省略、未 checkin のローカル変更がある案件は skip。不正な案件 id の行は無視。版が読めなければ RcloneError。"""
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    for cid in ("CASE-1", "CASE-2", "CASE-4", "CASE-5"):
        st.create_case(cid, "t", "acme", actor="human")
        st.mark_checkin(cid)
    fake.remote_markers = {"CASE-1": [st.load_case("CASE-1")["rev"]], "CASE-2": ["other"], "CASE-3": ["new"],
                           "CASE-5": [st.load_case("CASE-5")["rev"], "another"], "../x": ["bad"]}
    msg = sync.checkout(conf, ws)
    assert "fetched 3 (CASE-2, CASE-3, CASE-5)" in msg and "up to date 1" in msg and msg.startswith("drive: 4 case(s)")
    assert [c[2].rsplit("/", 1)[-1] for c in fake.calls if c[1] == "copy"] == ["CASE-2", "CASE-3", "CASE-5"]
    cache = json.loads((ws.index_dir / "drive_revs.cache.json").read_text(encoding="utf-8"))
    assert cache["revs"] == {"CASE-1": st.load_case("CASE-1")["rev"], "CASE-2": "other", "CASE-3": "new", "CASE-5": None} and cache["fetched_at"]
    assert _markers(ws, "CASE-5") == [st.load_case("CASE-5")["rev"]]                     # 取り寄せ後はローカルの rev のマーカー 1 個
    t = time.time() + 5
    os.utime(ws.cases_dir / "CASE-2" / "worklog.md", (t, t))
    fake.calls.clear()
    msg = sync.checkout(conf, ws)
    assert [c[2].rsplit("/", 1)[-1] for c in fake.calls if c[1] == "copy"] == ["CASE-3", "CASE-5"] and "skipped (local changes newer than last checkin) 1 (CASE-2)" in msg
    os.utime(ws.cases_dir / "CASE-2" / "worklog.md", None)
    # dry: rclone に --dry-run、キャッシュは更新しない
    (ws.index_dir / "drive_revs.cache.json").unlink()
    fake.calls.clear()
    assert "would fetch 3" in sync.checkout(conf, ws, dry=True)
    assert all(c[-1] == "--dry-run" for c in fake.calls if c[1] == "copy") and not (ws.index_dir / "drive_revs.cache.json").exists()
    # 1 案件の失敗は残りを止めず、最後にまとめて RcloneError
    fake.fail = {"copy"}
    with pytest.raises(sync.RcloneError, match="errors: CASE-2: .*; CASE-3: .*; CASE-5:"):
        sync.checkout(conf, ws)
    fake.fail = {"lsf"}
    with pytest.raises(sync.RcloneError, match="drive unavailable"):
        sync.checkout(conf, ws)


def test_parse_rev_listing_and_drive_revs(conf, monkeypatch):
    """lsf -R の出力 → {case: rev}: マーカー 0 個の案件は載らない、1 個は rev、2 個以上は None（不定）。形の違う行・不正な id は無視。
    drive_revs: rclone 不在・タイムアウト・非ゼロ → None。timeout を渡している。"""
    lines = ["cases/CASE-1/.rev/r1", "cases/CASE-2/.rev/r2a", "cases/CASE-2/.rev/r2b", "cases/CASE-3/notes.md", "cases/CASE-3/.rev/",
             "cases/CASE-4/sub/.rev/x", "cases/../x/.rev/bad", "other/CASE-5/.rev/r5", "", "cases/CASE-6/.rev/r6"]
    assert sync.parse_rev_listing(lines) == {"CASE-1": "r1", "CASE-2": None, "CASE-6": "r6"}
    ws = conf.workspaces["acme"]
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd; seen["kw"] = kw
        return subprocess.CompletedProcess(cmd, 0, "cases/CASE-1/.rev/r1\n", "")
    monkeypatch.setattr(subprocess, "run", run)
    assert sync.drive_revs(conf, ws) == {"CASE-1": "r1"} and seen["kw"]["timeout"] == sync.DRIVE_REVS_TIMEOUT_SEC
    assert seen["cmd"] == ["rclone", "lsf", "-R", "--files-only", "--include", "/cases/*/.rev/*", "my-drive:ws/acme"]
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "offline"))
    assert sync.drive_revs(conf, ws) is None
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, 10)))
    assert sync.drive_revs(conf, ws) is None
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError("rclone")))
    assert sync.drive_revs(conf, ws) is None and sync.refresh_drive_revs(conf, ws) is None
    assert not (ws.index_dir / "drive_revs.cache.json").exists() and sync.load_drive_revs_cache(ws) is None
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "cases/CASE-1/.rev/a\n", ""))
    assert sync.refresh_drive_revs(conf, ws) == {"CASE-1": "a"} and sync.load_drive_revs_cache(ws)["fetched_at"]
    # 一部だけの更新（open_case / checkin）は fetched_at を変えない。remove でエントリを消す
    at = sync.load_drive_revs_cache(ws)["fetched_at"]
    sync.update_drive_revs_cache(ws, {"CASE-2": "b", "CASE-3": None}, remove=["CASE-1"])
    assert sync.load_drive_revs_cache(ws) == {"revs": {"CASE-2": "b", "CASE-3": None}, "fetched_at": at}
    (ws.index_dir / "drive_revs.cache.json").write_text("not json", encoding="utf-8")
    assert sync.load_drive_revs_cache(ws) is None


def test_drive_rev_single_case(conf, monkeypatch):
    """drive_rev: rclone lsf <case>/.rev/ の名前を読む。0 個 → rev None・markers []、1 個 → rev、2 個以上 → rev None（不定）。
    ディレクトリ不在（終了 3）はマーカー無し（available）、それ以外の失敗・タイムアウト・rclone 不在は available=False。timeout 10 秒。"""
    ws = conf.workspaces["acme"]
    seen = {}

    def run_with(rc, out, err=""):
        def run(cmd, **kw):
            seen["cmd"] = cmd; seen["kw"] = kw
            return subprocess.CompletedProcess(cmd, rc, out, err)
        return run
    monkeypatch.setattr(subprocess, "run", run_with(0, "r1\n"))
    assert sync.drive_rev(conf, ws, "CASE-1") == {"available": True, "rev": "r1", "markers": ["r1"]}
    assert seen["cmd"] == ["rclone", "lsf", "my-drive:ws/acme/cases/CASE-1/.rev/"] and seen["kw"]["timeout"] == sync.REV_LSF_TIMEOUT_SEC == 10
    monkeypatch.setattr(subprocess, "run", run_with(0, "r2\nr1\nsub/\n"))
    assert sync.drive_rev(conf, ws, "CASE-1") == {"available": True, "rev": None, "markers": ["r1", "r2"]}
    monkeypatch.setattr(subprocess, "run", run_with(0, ""))
    assert sync.drive_rev(conf, ws, "CASE-1") == {"available": True, "rev": None, "markers": []}
    monkeypatch.setattr(subprocess, "run", run_with(3, "", "directory not found"))
    assert sync.drive_rev(conf, ws, "CASE-1") == {"available": True, "rev": None, "markers": []}
    monkeypatch.setattr(subprocess, "run", run_with(1, "", "couldn't connect"))
    r = sync.drive_rev(conf, ws, "CASE-1")
    assert r["available"] is False and r["rev"] is None and "couldn't connect" in r["error"]
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, 10)))
    assert sync.drive_rev(conf, ws, "CASE-1")["available"] is False
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError("rclone")))
    assert sync.drive_rev(conf, ws, "CASE-1")["available"] is False


def test_drive_state(conf, fake):
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "t", "acme", actor="human")
    assert sync.drive_state(st, None, "CASE-1")["state"] == "unknown"                                   # 版未取得・未 checkin
    st.mark_checkin("CASE-1"); c = st.load_case("CASE-1"); rev = c["rev"]
    assert sync.drive_state(st, {"CASE-1": rev}, "CASE-1") == {"state": "synced", "rev": rev, "drive_rev": rev,
                                                                "checked_in_at": c["last_checkin_at"], "from": c["checked_in_from"]}
    assert sync.drive_state(st, {}, "CASE-1")["state"] == "unknown"                                     # マーカー無し
    assert sync.drive_state(st, {"CASE-1": "newer"}, "CASE-1")["state"] == "drive_newer"
    d = sync.drive_state(st, {"CASE-1": None}, "CASE-1")                                                # 2 個以上（不定）
    assert d["state"] == "drive_newer" and d["drive_rev"] is None and d["ambiguous"] is True
    t = time.time() + 5
    os.utime(ws.cases_dir / "CASE-1" / "worklog.md", (t, t))
    d = sync.drive_state(st, {"CASE-1": "newer"}, "CASE-1")
    assert d["state"] == "local_changes" and d["files"] == ["worklog.md"] and d["drive_differs"] is True
    assert sync.drive_state(st, {"CASE-1": rev}, "CASE-1")["drive_differs"] is False
    assert sync.drive_state(st, {}, "CASE-1")["drive_differs"] is False
    os.utime(ws.cases_dir / "CASE-1" / "worklog.md", None)


def test_filters_keep_rev_markers(conf):
    """_filters は先頭に `+ .rev/**`（rules.exclude / raw_data の除外より先）。除外は --filter の `- ` 規則、min_size は --max-size。
    raw_move のフィルタは .rev/ を除外する（生データとして移動しない）。rules.exclude にマーカーに当たるパターンがあっても保護される。"""
    conf.rules["exclude"] = [".*", "*"]
    f = sync._filters(conf)
    assert f[:2] == ["--filter", "+ .rev/**"] and f[2:6] == ["--filter", "- .*", "--filter", "- *"] and f[-2:] == ["--max-size", "50M"]
    assert "--exclude" not in f and "--include" not in f
    for filt in sync._raw_filter_sets(conf, sync.raw_rules(conf)):
        assert filt[:2] == ["--filter", "- .rev/**"]


# ---------- 実 rclone（ローカル間。クラウド接続なし） ----------

@pytest.mark.skipif(not shutil.which("rclone"), reason="rclone not installed")
def test_rev_markers_round_trip_with_real_rclone(conf, tmp_path, monkeypatch):
    """実 rclone でローカルのディレクトリを Drive に見立てる: checkin(case) で Drive の古いマーカーが消えて新しいものだけ残り、
    drive_rev / drive_revs がそれを読む。checkin(ws)（copy + .rev/ 限定 sync）でも同じ。checkout(case) は取り寄せたマーカーを
    case.json の rev に合わせて 1 個にする。"""
    monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "rclone-empty.conf"))
    drive = tmp_path / "drive"
    monkeypatch.setattr(conf, "drive_path", lambda ws_name, *parts: str(drive / ws_name / "/".join(parts)))
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-1", "one", "acme", actor="human"); st.create_case("CASE-2", "two", "acme", actor="human")
    for cid in ("CASE-1", "CASE-2"):
        (drive / "acme" / "cases" / cid / ".rev").mkdir(parents=True); (drive / "acme" / "cases" / cid / ".rev" / f"stale-{cid}").touch()
    (drive / "acme" / "cases" / "CASE-9" / ".rev").mkdir(parents=True); (drive / "acme" / "cases" / "CASE-9" / ".rev" / "r9").touch()
    assert sync.drive_rev(conf, ws, "CASE-1") == {"available": True, "rev": "stale-CASE-1", "markers": ["stale-CASE-1"]}
    assert sync.drive_rev(conf, ws, "CASE-7") == {"available": True, "rev": None, "markers": []}          # .rev/ 無し（終了 3）
    # 案件単位: sync が古いマーカーを消す（--backup-dir に退避）
    sync.checkin(conf, ws, "CASE-1")
    rev1 = st.load_case("CASE-1")["rev"]
    assert sorted(p.name for p in (drive / "acme" / "cases" / "CASE-1" / ".rev").iterdir()) == [rev1]
    assert json.loads((drive / "acme" / "cases" / "CASE-1" / "case.json").read_text(encoding="utf-8"))["rev"] == rev1
    assert sync.drive_rev(conf, ws, "CASE-1")["rev"] == rev1
    assert sync.drive_revs(conf, ws) == {"CASE-1": rev1, "CASE-2": "stale-CASE-2", "CASE-9": "r9"}
    # ワークスペース全体: copy は古いマーカーを残すが、続く .rev/ 限定の sync が振り直した案件の分だけ消す
    sync.checkin(conf, ws)
    rev2 = st.load_case("CASE-2")["rev"]
    assert st.load_case("CASE-1")["rev"] == rev1                                                           # 変更なし: 振り直さない
    assert sorted(p.name for p in (drive / "acme" / "cases" / "CASE-2" / ".rev").iterdir()) == [rev2]
    assert sync.drive_revs(conf, ws) == {"CASE-1": rev1, "CASE-2": rev2, "CASE-9": "r9"}
    # 別環境の checkin を模す: Drive の case.json の rev とマーカーを変える → checkout(case) で取り寄せ、ローカルのマーカーは 1 個
    remote = json.loads((drive / "acme" / "cases" / "CASE-2" / "case.json").read_text(encoding="utf-8")); remote["rev"] = "from-another-host"
    (drive / "acme" / "cases" / "CASE-2" / "case.json").write_text(json.dumps(remote), encoding="utf-8")
    (drive / "acme" / "cases" / "CASE-2" / ".rev" / rev2).unlink(); (drive / "acme" / "cases" / "CASE-2" / ".rev" / "from-another-host").touch()
    t = time.time() + 5
    for p in (drive / "acme" / "cases" / "CASE-2").rglob("*"):
        os.utime(p, (t, t))
    assert sync.drive_state(st, sync.drive_revs(conf, ws), "CASE-2")["state"] == "drive_newer"
    sync.checkout(conf, ws, "CASE-2")
    assert st.load_case("CASE-2")["rev"] == "from-another-host" and st.rev_markers("CASE-2") == ["from-another-host"]
    msg = sync.checkout(conf, ws)                                                                            # CASE-9 はローカルに無い → 取り寄せ。CASE-2 は取り寄せた分が次の checkin までローカル変更
    assert msg.startswith("drive: 3 case(s); fetched 1 (CASE-9), up to date 1, skipped (local changes newer than last checkin) 1 (CASE-2)")
    assert (ws.cases_dir / "CASE-9" / ".rev" / "r9").exists() and not (ws.cases_dir / "CASE-9" / "case.json").exists()   # case.json の無いディレクトリは触らない（取り寄せたまま）


# ---------- rules.rclone_flags: rclone を呼ぶすべての箇所で共通引数の後ろに付く ----------

FLAGS = ["--transfers", "8", "--checkers", "16", "--drive-pacer-min-sleep", "10ms", "--drive-pacer-burst", "200"]


def _tail_flags(cmd: list[str]) -> list[str]:
    """rclone コマンドの末尾（--dry-run があればその前）が FLAGS か。"""
    body = cmd[:-1] if cmd[-1] == "--dry-run" else cmd
    return body[-len(FLAGS):]


def test_rclone_flags_appended_to_every_rclone_command(conf, fake, monkeypatch):
    """checkout（copyto + copy）/ checkin（copyto + sync|copy + .rev/ の sync）/ raw_move（lsf + move）/ drive_index（lsf）/
    版マーカー（drive_rev・drive_revs の lsf）/ events の copyto / ws（lsd・mkdir・lsf）の
    全 rclone 呼び出しの末尾に rules.rclone_flags が付く。既定（未設定）では何も付かない。"""
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-123", "t", "acme", actor="human")
    _touch(ws.cases_dir / "CASE-123" / "run.bag", 10, 20 * DAY)
    conf.rules["rclone_flags"] = list(FLAGS)
    sync.checkout(conf, ws, "CASE-123")
    sync.checkin(conf, ws, "CASE-123")
    sync.checkin(conf, ws)
    fake.remote_cases = {"CASE-123": dict(st.load_case("CASE-123"))}
    sync.raw_move(conf, ws, "CASE-123")
    sync.drive_index(conf, ws)
    sync.drive_rev(conf, ws, "CASE-123"); sync.drive_revs(conf, ws); sync.checkout(conf, ws)
    sync.fetch_remote_events(conf, ws, "CASE-123")
    sync.ws_exists_on_drive(conf, "acme"); sync.create_ws_on_drive(conf, "acme"); sync.list_ws_on_drive(conf)
    subs = {c[1] for c in fake.calls}
    assert {"copyto", "copy", "sync", "lsf", "move", "lsd", "mkdir"} <= subs and "cat" not in subs and "rcat" not in subs
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
