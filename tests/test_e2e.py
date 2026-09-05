"""通し試験: 一時ディレクトリの設定・データで CLI（attach → new → serve）を subprocess 起動し、
MCP（streamable HTTP, mcp 2.x Client）と UI（HTTP）を実際に叩く。rclone は PATH 先頭の偽物。"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import anyio
import pytest
from mcp.client import Client

from tests.conftest import with_fake_rclone_env

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read().decode("utf-8")


def _post(url: str, data: dict) -> int:
    body = urllib.parse.urlencode(data, encoding="utf-8").encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        return urllib.request.build_opener(NoRedirect).open(req, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.fixture
def env(tmp_path, fake_rclone):
    config = tmp_path / "config.yaml"
    config.write_text("drive: {remote: my-drive, root: ws}\nextract: {agent: claude}\nworkspaces:\n  acme: {description: e2e, repos: []}\n", encoding="utf-8")
    return with_fake_rclone_env(fake_rclone, KAIRN_CONFIG=str(config), KAIRN_DATA_ROOT=str(tmp_path / "data"),
                                XDG_STATE_HOME=str(tmp_path / "state"))


def _cli(env, *args, **kw):
    return subprocess.run([PY, "-m", "kairn.cli", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, **kw)


async def _job(c, job_id, timeout=30.0):
    """job_status を done / failed になるまでポーリングする（HTTP 経由なので job 表には触れない）。"""
    deadline = time.monotonic() + timeout
    while True:
        s = (await c.call_tool("job_status", {"job_id": job_id})).structured_content
        if s["status"] in ("done", "failed") or time.monotonic() > deadline:
            return s
        await anyio.sleep(0.05)


def test_end_to_end(tmp_path, env, fake_rclone):
    repo = tmp_path / "acme-robot"; (repo / "tmp" / "old-notes").mkdir(parents=True)   # 既存の tmp/ 実体があっても attach できる
    r = _cli(env, "attach", "acme", str(repo)); assert r.returncode == 0, r.stderr
    assert not (repo / "tmp").is_symlink() and (repo / "tmp" / "old-notes").is_dir()      # リポジトリ側には何も作らない・壊さない
    assert sorted(p.name for p in repo.iterdir()) == ["tmp"]
    assert (tmp_path / "data" / "acme" / "cases").is_dir()
    r = _cli(env, "new", "CASE-123", "起動時に driver が初期化されない", "--ws", "acme"); assert r.returncode == 0, r.stderr
    r = _cli(env, "status"); assert r.returncode == 0 and "acme" in r.stdout and str(repo) in r.stdout and "link" not in r.stdout, r.stderr
    case_dir = tmp_path / "data" / "acme" / "cases" / "CASE-123"
    (case_dir / "worklog.md").write_text("# t\n## Objective\n起動時に widget driver の init が終わらない\n## Notes\nUART 460800 で送信量が超過する\n", encoding="utf-8")
    # console script も動く（.venv/bin/kairn / .venv/Scripts/kairn.exe）
    script = Path(PY).parent / "kairn"
    if sys.platform == "win32" and not script.exists():
        script = script.with_suffix(".exe")
    if script.exists():
        r = subprocess.run([str(script), "cases", "acme"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and "CASE-123" in r.stdout, r.stderr

    # Drive の版マーカー（偽 rclone が <tmp>/drive/ を remote として読み書きする）: 別の rev のマーカーを置いて open_case に取り寄せさせる
    drive_case = tmp_path / "drive" / "ws" / "acme" / "cases" / "CASE-123"
    (drive_case / ".rev").mkdir(parents=True); (drive_case / ".rev" / "stale").touch()
    (tmp_path / "drive" / "ws" / "acme" / "manifest.json").write_text("{}", encoding="utf-8")   # 旧方式の集計ファイル（もう読まない）
    port = _free_port()
    proc = subprocess.Popen([PY, "-m", "kairn.cli", "serve", "--port", str(port)], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if _get(base + "/ui")[0] == 200:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            proc.terminate(); pytest.fail("server did not start:\n" + (proc.stdout.read() if proc.stdout else ""))

        async def mcp_flow():
            async with Client(base + "/mcp") as c:
                names = {t.name for t in (await c.list_tools()).tools}
                assert names == {"open_case", "list_cases", "create_case", "plan", "update_task", "log_event", "set_case_status", "search", "find_cases", "checkin", "drive_index", "extract_card", "job_status", "link_case"}
                r = await c.call_tool("plan", {"case": "CASE-123", "objective": "boot works", "reason": "initial", "tasks": [{"title": "調査"}, {"title": "修正"}]})
                assert not r.is_error and r.structured_content["version"] == 1
                r = await c.call_tool("update_task", {"case": "CASE-123", "task": "T001", "status": "done", "note": "x"})
                assert r.is_error and "evidence" in r.content[0].text
                r = await c.call_tool("update_task", {"case": "CASE-123", "task": "T001", "status": "done", "note": "ok", "evidence": [{"type": "pr", "id": 42}]})
                assert not r.is_error and r.structured_content["status"] == "done"
                r = await c.call_tool("search", {"query": "UART 460800"})
                assert not r.is_error and r.structured_content["results"][0]["heading"] == "Notes" and r.structured_content["searched"] == ["acme"]
                r = await c.call_tool("find_cases", {"query": "driver init"})
                assert not r.is_error and r.structured_content["results"][0]["case"] == "CASE-123"
                # UI から差し戻し → open_case の human_feedback に出る
                assert _post(f"{base}/ui/acme/CASE-123/sendback", {"task": "T002", "note": "unit-6 でも確認"}) == 303
                r = await c.call_tool("open_case", {"case": "CASE-123"})
                oc = r.structured_content
                assert not r.is_error and oc["human_feedback"][-1]["note"] == "unit-6 でも確認" and oc["human_feedback"][-1]["task"] == "T002"
                assert oc["drive"]["job_id"] and oc["drive"]["fetched"] is True and oc["drive"]["drive_rev"] == "stale"   # Drive のマーカーの rev が違う → 取り寄せ（偽 rclone は即成功）
                assert (await _job(c, oc["drive"]["job_id"]))["status"] == "done"
                assert not (case_dir / ".rev").exists()                                                          # 取り寄せた古いマーカーは残らない（ローカルは rev 未付与）
                r = await c.call_tool("checkin", {"case": "CASE-123"})
                assert not r.is_error and r.structured_content["job_id"]
                js = await _job(c, r.structured_content["job_id"])
                assert js["status"] == "done" and js["result"]["ok"] and js["result"]["last_checkin_at"], js
                rev = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))["rev"]
                assert [p.name for p in (case_dir / ".rev").iterdir()] == [rev]                                 # ローカルの .rev/<rev>
                assert sorted(p.name for p in (drive_case / ".rev").iterdir()) == [rev]                          # Drive 側: 古い stale は消え、新しいものだけ
                assert json.loads((drive_case / "case.json").read_text(encoding="utf-8"))["rev"] == rev
                r = await c.call_tool("open_case", {"case": "CASE-123"})                                        # rev 一致 → 取り寄せ省略
                assert r.structured_content["drive"]["up_to_date"] is True and "job_id" not in r.structured_content["drive"]
                r = await c.call_tool("list_cases", {})
                assert r.structured_content["result"][0]["drive"]["state"] == "synced"
                r = await c.call_tool("job_status", {"job_id": "nope"})
                assert r.is_error and "unknown job" in r.content[0].text
        anyio.run(mcp_flow)

        # UI 操作: 一覧・案件・タスク追加・コメント
        st, body = _get(base + "/ui"); assert st == 200 and "CASE-123" in body and "1/2" in body
        st, body = _get(base + "/ui/acme/CASE-123"); assert st == 200 and "unit-6 でも確認" in body and "sendback" in body
        assert _post(f"{base}/ui/acme/CASE-123/task", {"title": "unit-6 で再現確認", "owner": "human"}) == 303
        assert _post(f"{base}/ui/acme/CASE-123/comment", {"note": "コメント（日本語）"}) == 303
        st, body = _get(base + "/ui/acme/CASE-123"); assert "unit-6 で再現確認" in body and "コメント（日本語）" in body and "v2" in body
        assert _get(base + "/")[1]  # / は /ui へ（urllib がリダイレクトを追う）
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    log = (tmp_path / "rclone.log").read_text()
    assert "copy my-drive:ws/acme/cases/CASE-123" in log and "sync " in log and "my-drive:ws/acme/cases/CASE-123" in log
    assert "lsf my-drive:ws/acme/cases/CASE-123/.rev/" in log and "manifest" not in log and "cat " not in log
    assert "checkin" in [l.split('"action": "')[1].split('"')[0] for l in (case_dir / "events.jsonl").read_text().splitlines() if '"action"' in l]
