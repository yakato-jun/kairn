"""ジョブ機構（kairn/jobs.py）と sync._run の進捗読み取り。rclone は subprocess.Popen をモック（クラウドには接続しない）。"""
from __future__ import annotations

import subprocess
import threading
import time

import pytest

from kairn import jobs as jobs_mod
from kairn import sync
from kairn.jobs import JobTable
from kairn.store import CaseStore


class FakePopen:
    """subprocess.Popen の代役: 引数を記録し、与えた行を stdout として順に流す。rc で終了コードを決める。"""
    calls: list[list[str]] = []
    lines: list[str] = ["2026/09/02 12:00:00 INFO  : a.txt: Copied (new)\n", "Transferred:   \t  1.234 MiB / 700 MiB, 0%, 1.2 MiB/s, ETA 10m\n",
                        "Transferred:   \t  700 MiB / 700 MiB, 100%, 5.0 MiB/s, ETA 0s\n"]
    rc = 0

    def __init__(self, cmd, **kw):
        FakePopen.calls.append(list(cmd))
        FakePopen.last_kw = kw
        self.args = cmd
        self.stdout = iter(self.lines)
        self.returncode = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.returncode = self.rc
        return False


@pytest.fixture
def fake_popen(monkeypatch):
    FakePopen.calls = []
    FakePopen.rc = 0
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    return FakePopen


# ---------- sync._run の進捗 ----------

def test_run_with_progress_reads_lines_via_popen(fake_popen):
    got = []
    r = sync._run(["rclone", "sync", "a", "b"], progress=got.append)
    assert fake_popen.calls == [["rclone", "sync", "a", "b"]]
    assert got == FakePopen.lines                                                     # 1 行ずつ順に届く
    assert r.returncode == 0 and r.stderr == "".join(FakePopen.lines) and r.stdout == ""  # 出力は stderr にまとめて返す
    assert FakePopen.last_kw["stderr"] is subprocess.STDOUT and FakePopen.last_kw["text"] is True
    fake_popen.rc = 3
    with pytest.raises(sync.RcloneError) as ei:
        sync._run(["rclone", "sync", "a", "b"], progress=lambda l: None)
    assert "ETA 0s" in str(ei.value)


def test_run_without_progress_still_uses_subprocess_run(monkeypatch, fake_popen):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(list(cmd)) or subprocess.CompletedProcess(cmd, 0, "out", ""))
    sync._run(["rclone", "lsf", "x"])
    assert calls == [["rclone", "lsf", "x"]] and fake_popen.calls == []


def test_checkin_and_checkout_pass_progress_and_stats(conf, fake_popen, monkeypatch):
    """checkin / checkout に progress を渡すと転送本体は Popen で走り、--stats 5s --stats-one-line が付く。events の copyto は従来どおり run。
    転送前に案件フォルダの .rev/<rev> が置かれ、転送のフィルタは .rev/ を含める。"""
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 3, "", "object not found"))
    ws = conf.workspaces["acme"]
    CaseStore(ws.cases_dir).create_case("CASE-123", "t", "acme", actor="human")
    got = []
    msg = sync.checkin(conf, ws, "CASE-123", progress=got.append)
    cmd = fake_popen.calls[-1]
    assert cmd[:2] == ["rclone", "sync"] and cmd[cmd.index("--stats") + 1] == "5s" and "--stats-one-line" in cmd
    assert got == FakePopen.lines and "ETA 0s" in msg
    st = CaseStore(ws.cases_dir)
    assert st.rev_markers("CASE-123") == [st.load_case("CASE-123")["rev"]] and cmd[cmd.index("--filter") + 1] == "+ .rev/**"
    got.clear()
    sync.checkout(conf, ws, "CASE-123", progress=got.append)
    cmd = fake_popen.calls[-1]
    assert cmd[:2] == ["rclone", "copy"] and "--update" in cmd and cmd[cmd.index("--stats") + 1] == "5s"
    assert got == FakePopen.lines


# ---------- JobTable ----------

def _wait(job, timeout=5.0):
    assert job.wait(timeout), f"job did not finish: {job}"
    return job


def test_job_transitions_and_progress():
    table = JobTable()
    started = threading.Event(); release = threading.Event()

    def fn(progress):
        started.set()
        progress("Transferred: 1 / 10")
        release.wait(5)
        progress("  Transferred: 10 / 10, 100%  \n")
        return {"ok": True}

    job, created = table.submit("checkin", "acme", "CASE-123", fn)
    assert created and job.status in ("queued", "running") and job.kind == "checkin" and job.case == "CASE-123"
    assert started.wait(5)
    time.sleep(0.05)
    assert job.status == "running" and job.started_at and job.finished_at is None and job.progress == "Transferred: 1 / 10"
    d = job.to_dict()
    assert d["job_id"] == job.id and d["status"] == "running" and d["result"] is None and d["error"] is None and d["elapsed_sec"] >= 0
    release.set()
    _wait(job)
    assert job.status == "done" and job.result == {"ok": True} and job.finished_at and job.progress == "Transferred: 10 / 10, 100%"
    assert table.get(job.id) is job and table.active() == []


def test_job_failure_records_error_string():
    table = JobTable()

    def fn(progress):
        progress("half way")
        raise sync.RcloneError("remote unreachable")

    job, _ = table.submit("checkin", "acme", "CASE-1", fn)
    _wait(job)
    assert job.status == "failed" and job.error == "RcloneError: remote unreachable" and job.result is None
    assert job.progress == "half way" and job.finished_at
    assert table.get(job.id).to_dict()["error"] == "RcloneError: remote unreachable"


def test_same_case_same_kind_is_not_started_twice():
    table = JobTable()
    release = threading.Event()
    runs = []

    def fn(progress):
        runs.append(1)
        release.wait(5)

    j1, c1 = table.submit("checkin", "acme", "CASE-1", fn)
    j2, c2 = table.submit("checkin", "acme", "CASE-1", fn)
    j3, c3 = table.submit("checkout", "acme", "CASE-1", fn)   # 種類が違えば別ジョブ
    j4, c4 = table.submit("checkin", "acme", "CASE-2", fn)    # 案件が違えば別ジョブ
    j5, c5 = table.submit("checkin", "other", "CASE-1", fn)   # ワークスペースが違えば別ジョブ
    assert c1 and not c2 and j2 is j1 and c3 and c4 and c5
    assert {j.id for j in table.active("acme", "CASE-1")} == {j1.id, j3.id}
    assert [j.id for j in table.active("acme")] == [j1.id, j3.id, j4.id]
    release.set()
    for j in (j1, j3, j4, j5):
        _wait(j)
    assert len(runs) == 4
    # 終わった後は同じ案件で新しいジョブが作れる
    j6, c6 = table.submit("checkin", "acme", "CASE-1", lambda p: "again")
    assert c6 and j6 is not j1
    _wait(j6)
    assert j6.result == "again"


def test_finished_jobs_are_pruned_by_count_and_age(monkeypatch):
    table = JobTable(max_done=3, ttl_sec=60)
    done = [_wait(table.submit("checkin", "acme", f"CASE-{i}", lambda p: None)[0]) for i in range(5)]
    assert table.get(done[0].id) is None and table.get(done[1].id) is None       # 古い 2 件は件数上限で消えた
    assert all(table.get(j.id) is not None for j in done[2:])
    # TTL: 終了時刻を過去にずらす
    done[2]._finished_mono -= 61
    assert table.get(done[2].id) is None and table.get(done[3].id) is not None
    # 実行中のジョブは件数・TTL に関わらず残る
    release = threading.Event()
    running, _ = table.submit("checkin", "acme", "CASE-R", lambda p: release.wait(5))
    for i in range(5):
        _wait(table.submit("checkin", "acme", f"CASE-X{i}", lambda p: None)[0])
    assert table.get(running.id) is running and len(table.all()) == 4
    release.set(); _wait(running)


# ---------- checkin ジョブ本体（sync.checkin_job）: mark_checkin とイベント記録はジョブ側 ----------

def test_checkin_job_marks_checkin_and_appends_event(conf, fake_popen, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 3, "", "object not found"))
    ws = conf.workspaces["acme"]
    st = CaseStore(ws.cases_dir)
    st.create_case("CASE-123", "t", "acme", actor="human")
    table = JobTable()
    job, _ = table.submit("checkin", ws.name, "CASE-123", lambda p: sync.checkin_job(conf, ws, "CASE-123", "test-agent", p))
    _wait(job)
    assert job.status == "done", job.error
    c = st.load_case("CASE-123")
    assert c["last_checkin_at"] and job.result["last_checkin_at"] == c["last_checkin_at"] and job.result["ok"] is True
    assert c["rev"] and st.rev_markers("CASE-123") == [c["rev"]] and c["checked_in_from"]
    ev = st.events("CASE-123")[-1]
    assert ev["action"] == "checkin" and ev["agent"] == "test-agent" and "ETA 0s" in ev["note"]
    assert c["last_checkin_events"] == len(st.events("CASE-123")) - 1    # 行数は checkin event を足す前
    assert st.local_changes_since_checkin("CASE-123") == []              # その 1 行は変更に数えない
    assert job.progress.startswith("Transferred:") and "100%" in job.progress
    # 失敗（rclone 非ゼロ）: failed、last_checkin_at は更新されず、event も増えない
    fake_popen.rc = 1
    n = len(st.events("CASE-123")); before = (c["last_checkin_at"], c["rev"])
    job, _ = table.submit("checkin", ws.name, "CASE-123", lambda p: sync.checkin_job(conf, ws, "CASE-123", "test-agent", p))
    _wait(job)
    assert job.status == "failed" and job.error.startswith("RcloneError:") and "ETA 0s" in job.error
    assert len(st.events("CASE-123")) == n and (st.load_case("CASE-123")["last_checkin_at"], st.load_case("CASE-123")["rev"]) == before
    assert st.rev_markers("CASE-123") == [before[1]]   # 転送失敗: rev は戻り、.rev/ のマーカーも戻る
    # 未知の案件: failed（例外がそのまま error に）
    job, _ = table.submit("checkin", ws.name, "CASE-404", lambda p: sync.checkin_job(conf, ws, "CASE-404", "a", p))
    _wait(job)
    assert job.status == "failed" and "CaseNotFound" in job.error


def test_constants():
    assert jobs_mod.MAX_DONE == 200 and jobs_mod.TTL_SEC == 24 * 3600 and jobs_mod.JOB_STATUSES == ("queued", "running", "done", "failed")


# ---------- 同一案件のジョブの直列化（(workspace, case) ごとの FIFO） ----------

def test_same_case_jobs_run_one_at_a_time_in_fifo_order():
    """同じ案件のジョブは種類を問わず 1 つずつ: checkin 実行中の checkout は queued で待ち、先行が終わってから走る。別案件は並走する。"""
    table = JobTable()
    events: list[str] = []
    gates = {k: threading.Event() for k in ("ci", "co", "ci2", "other")}
    started = {k: threading.Event() for k in gates}

    def fn(name):
        def run(progress):
            events.append(f"start {name}"); started[name].set()
            assert gates[name].wait(5)
            events.append(f"end {name}")
            return name
        return run

    ci, c1 = table.submit("checkin", "acme", "CASE-1", fn("ci"))
    assert started["ci"].wait(5) and c1
    co, c2 = table.submit("checkout", "acme", "CASE-1", fn("co"))       # 同じ案件・別種 → queued
    other, c3 = table.submit("checkin", "acme", "CASE-2", fn("other"))  # 別案件 → 並走
    assert c2 and c3 and co.status == "queued" and co.started_at is None and co.elapsed_sec() == 0.0
    assert started["other"].wait(5) and other.status == "running"
    time.sleep(0.05)
    assert not started["co"].is_set() and co.status == "queued"
    assert co.to_dict()["status"] == "queued" and [j.id for j in table.active("acme", "CASE-1")] == [ci.id, co.id]
    # queued のジョブも同種の重複起動抑止の対象（既存の queued を返す）
    co_dup, c4 = table.submit("checkout", "acme", "CASE-1", fn("co"))
    assert not c4 and co_dup is co
    # 先行に対しても同種は既存を返す
    ci_dup, c5 = table.submit("checkin", "acme", "CASE-1", fn("ci"))
    assert not c5 and ci_dup is ci
    gates["ci"].set(); _wait(ci)
    assert ci.status == "done" and started["co"].wait(5) and co.status == "running" and co.started_at
    gates["co"].set(); _wait(co)
    gates["other"].set(); _wait(other)
    assert events[:2] == ["start ci", "start other"] or events[:2] == ["start other", "start ci"]
    assert events.index("end ci") < events.index("start co") < events.index("end co")
    assert co.result == "co" and table.active() == []
    # 先行が failed でも次は走る
    def boom(progress):
        started["ci2"].set(); assert gates["ci2"].wait(5); raise RuntimeError("x")
    f1, _ = table.submit("checkin", "acme", "CASE-1", boom)
    assert started["ci2"].wait(5)
    f2, _ = table.submit("checkout", "acme", "CASE-1", lambda p: "after failure")
    assert f2.status == "queued"
    gates["ci2"].set(); _wait(f1); _wait(f2)
    assert f1.status == "failed" and f2.status == "done" and f2.result == "after failure"


def test_queue_order_is_fifo_for_three_jobs_and_inline_start_does_not_deadlock(monkeypatch):
    """3 つ以上でも登録順。テスト用の同期 _start（スレッド無し）でもロックを再入しない。"""
    table = JobTable()
    monkeypatch.setattr(JobTable, "_start", lambda self, job, fn: self._run(job, fn))
    order = []
    a, _ = table.submit("checkin", "acme", "CASE-1", lambda p: order.append("a"))   # 同期実行なので登録時に終わる
    assert a.status == "done" and order == ["a"]
    # 同期実行でも running 中に登録されたものを FIFO で流す: _run の中から submit する
    def outer(p):
        order.append("outer")
        table.submit("checkout", "acme", "CASE-1", lambda q: order.append("q1"))
        table.submit("bag2zst", "acme", "CASE-1", lambda q: order.append("q2"))
        assert [j.status for j in table.active("acme", "CASE-1")][1:] == ["queued", "queued"]
    b, _ = table.submit("checkin", "acme", "CASE-1", outer)
    assert order == ["a", "outer", "q1", "q2"] and table.active() == []
