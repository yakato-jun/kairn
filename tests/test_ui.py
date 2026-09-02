"""UI（Starlette TestClient）。/ui は末尾スラッシュ無しで 200、日本語フォームが正しく記録される、鮮度表示。"""
from __future__ import annotations

from datetime import timedelta

from starlette.testclient import TestClient

from kairn.jobs import JobTable
from kairn.store import CaseStore
from kairn.ui import STALE_DAYS, build_ui, case_freshness, task_freshness


def _seed(conf):
    st = CaseStore(conf.workspaces["acme"].cases_dir)
    st.create_case("CASE-123", "起動時に driver が初期化されない", "acme", actor="human", elements={"machine": ["unit-2"]})
    st.new_plan_version("CASE-123", "boot works", [{"title": "調査"}, {"title": "修正"}], reason="initial", actor="ai")
    return st


def test_ui_root_without_trailing_slash(conf):
    _seed(conf)
    c = TestClient(build_ui(conf))
    for path in ("/ui", "/ui/"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert "CASE-123" in r.text and "起動時に driver" in r.text
    assert c.get("/ui/acme/CASE-123").status_code == 200
    assert c.get("/ui/nowhere/CASE-123").status_code == 404
    assert c.get("/ui/acme/CASE-999").status_code == 404


def test_forms_utf8_percent_encoding(conf):
    st = _seed(conf)
    c = TestClient(build_ui(conf))
    # 差し戻し（日本語、記号）
    r = c.post("/ui/acme/CASE-123/sendback", data={"task": "T001", "note": "unit-6 でも確認して（％と&も）"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/acme/CASE-123"
    # コメント（生の percent-encoding で送る）
    r = c.post("/ui/acme/CASE-123/comment", content="note=%E6%97%A5%E6%9C%AC%E8%AA%9E%E3%81%AE%E3%82%B3%E3%83%A1%E3%83%B3%E3%83%88",
               headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    assert r.status_code == 303
    # タスク追加 → 新版で既存タスクは引き継がれる
    r = c.post("/ui/acme/CASE-123/task", data={"title": "unit-6 で再現確認", "owner": "human"}, follow_redirects=False)
    assert r.status_code == 303
    ev = st.events("CASE-123")
    sb = [e for e in ev if e["action"] == "sendback"][0]
    assert sb["actor"] == "human" and sb["task"] == "T001" and sb["note"] == "unit-6 でも確認して（％と&も）"
    assert [e for e in ev if e["action"] == "comment"][0]["note"] == "日本語のコメント"
    plan = st.current_plan("CASE-123")
    assert plan["version"] == 2 and [t["id"] for t in plan["tasks"]] == ["T001", "T002", "T003"] and plan["superseded"] == []
    assert plan["tasks"][2]["owner"] == "human" and plan["tasks"][2]["title"] == "unit-6 で再現確認"
    page = c.get("/ui/acme/CASE-123").text
    assert "unit-6 でも確認して（％と&amp;も）" in page and "日本語のコメント" in page and "unit-6 で再現確認" in page
    # 不正入力は 400
    assert c.post("/ui/acme/CASE-123/sendback", data={"task": "T999", "note": "x"}).status_code == 400
    assert c.post("/ui/acme/CASE-123/comment", data={"note": ""}).status_code == 400
    # 案件の状態変更
    r = c.post("/ui/acme/CASE-123/status", data={"status": "suspended", "note": "保留"}, follow_redirects=False)
    assert r.status_code == 303 and st.load_case("CASE-123")["status"] == "suspended"
    assert "CASE-123" not in c.get("/ui").text and "CASE-123" in c.get("/ui?status=all").text


def test_freshness_marks_stale_open_tasks(conf):
    st = _seed(conf)
    from kairn import store as store_mod
    # T001 の最終イベントを 10 日前に偽装（events.jsonl を書き換え）
    f = conf.workspaces["acme"].cases_dir / "CASE-123" / "events.jsonl"
    old = (store_mod.datetime.now(store_mod.JST) - timedelta(days=STALE_DAYS + 3)).isoformat(timespec="seconds")
    f.write_text(f.read_text().replace(st.events("CASE-123")[-1]["t"], old))
    plan = st.current_plan("CASE-123")
    for t in plan["tasks"]:
        t["created_at"] = old
    (conf.workspaces["acme"].cases_dir / "CASE-123" / "plan" / "v0001.json").write_text(__import__("json").dumps(plan))
    ev = st.events("CASE-123")
    fr = task_freshness(ev, plan["tasks"][0])
    assert fr["days"] == STALE_DAYS + 3 and fr["stale"]
    assert case_freshness(st, "CASE-123")["stale"]
    c = TestClient(build_ui(conf))
    assert "class='card stale'" in c.get("/ui/acme/CASE-123").text
    assert "age stale" in c.get("/ui").text
    # 動きがあれば鮮度が戻る
    st.set_task_status("CASE-123", "T001", "doing", [], "着手", actor="ai")
    assert not task_freshness(st.events("CASE-123"), st.current_plan("CASE-123")["tasks"][0])["stale"]


def test_element_filter_and_related(conf):
    st = _seed(conf)
    st.create_case("CASE-100", "older", "acme", actor="human", related=["CASE-123"])
    c = TestClient(build_ui(conf))
    t = c.get("/ui?element=unit-2").text
    assert "CASE-123" in t and "CASE-100" not in t
    assert "href='/ui/acme/CASE-123'" in c.get("/ui/acme/CASE-100").text


def test_post_rejects_cross_site(conf):
    """CSRF: Origin / Referer / Sec-Fetch-Site がリクエストの Host と食い違う POST は 403（何も記録しない）。"""
    st = _seed(conf)
    c = TestClient(build_ui(conf))
    n = len(st.events("CASE-123"))
    for headers in ({"Origin": "http://evil.example"}, {"Referer": "http://evil.example/ui/acme/CASE-123"},
                    {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site", "Origin": "http://testserver"},
                    {"Origin": "null"}):
        r = c.post("/ui/acme/CASE-123/comment", data={"note": "injected"}, headers=headers, follow_redirects=False)
        assert r.status_code == 403, headers
    assert len(st.events("CASE-123")) == n
    for headers in ({"Origin": "http://testserver"}, {"Referer": "http://testserver/ui/acme/CASE-123"},
                    {"Sec-Fetch-Site": "same-origin", "Origin": "http://testserver"}, {"Sec-Fetch-Site": "none"}, {}):
        r = c.post("/ui/acme/CASE-123/comment", data={"note": "ok"}, headers=headers, follow_redirects=False)
        assert r.status_code == 303, headers
    assert len(st.events("CASE-123")) == n + 5


def test_invalid_case_id_is_404_not_500(conf):
    _seed(conf)
    c = TestClient(build_ui(conf))
    for path in ("/ui/acme/%2e%2e", "/ui/acme/.hidden"):
        assert c.get(path).status_code == 404, path
    assert c.post("/ui/acme/%2e%2e/comment", data={"note": "x"}).status_code == 404
    assert c.post("/ui/acme/.hidden/comment", data={"note": "x"}).status_code == 404


def test_status_query_is_normalized(conf):
    _seed(conf)
    c = TestClient(build_ui(conf))
    evil = "<script>alert(1)</script>"
    r = c.get("/ui", params={"status": evil, "element": "unit-2"})
    assert r.status_code == 200 and evil not in r.text and "href='/ui?status=open'" in r.text
    assert "CASE-123" in r.text  # open として扱う
    assert "CASE-123" in c.get("/ui?status=all").text and "CASE-123" not in c.get("/ui?status=closed").text


def test_task_form_rejects_unknown_owner(conf):
    st = _seed(conf)
    c = TestClient(build_ui(conf))
    assert c.post("/ui/acme/CASE-123/task", data={"title": "x", "owner": "robot"}).status_code == 400
    assert st.current_plan("CASE-123")["version"] == 1


def test_case_page_shows_running_jobs(conf):
    """進行中の checkin / 取り寄せジョブ（種類・進捗行・経過時間）を案件ページに出す。終われば消える。他案件のジョブは出ない。"""
    import threading
    _seed(conf)
    st = CaseStore(conf.workspaces["acme"].cases_dir)
    st.create_case("CASE-100", "other", "acme", actor="human")
    jobs = JobTable()
    c = TestClient(build_ui(conf, jobs=jobs))
    assert "進行中のジョブ" not in c.get("/ui/acme/CASE-123").text
    started = threading.Event(); release = threading.Event()

    def fn(progress):
        progress("Transferred:   \t  1.234 MiB / 700 MiB, 0%, 1.2 MiB/s, ETA 10m")
        started.set()
        release.wait(5)
    job, _ = jobs.submit("checkin", "acme", "CASE-123", fn)
    other, _ = jobs.submit("checkout", "acme", "CASE-100", lambda p: release.wait(5))
    assert started.wait(5)
    page = c.get("/ui/acme/CASE-123").text
    assert "進行中のジョブ" in page and "<b>checkin</b>" in page and "running" in page and job.id in page
    assert "1.234 MiB / 700 MiB" in page and "ETA 10m" in page and "s · " in page
    assert "<b>checkout</b>" not in page and other.id not in page
    assert "<b>checkout</b>" in c.get("/ui/acme/CASE-100").text
    release.set(); job.wait(5); other.wait(5)
    assert "進行中のジョブ" not in c.get("/ui/acme/CASE-123").text
    assert "進行中のジョブ" not in TestClient(build_ui(conf)).get("/ui/acme/CASE-123").text  # jobs 無し（UI 単体）でも動く
