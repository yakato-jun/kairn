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


def test_settings_page_shows_and_edits_rules(conf):
    """/ui/settings: rules の現在値を表示し、フォームの POST で検証・保存する（CLI の kairn rules と同じ操作）。一覧のヘッダにリンク。"""
    from kairn import config as cfg
    c = TestClient(build_ui(conf))
    assert "href='/ui/settings'" in c.get("/ui").text
    page = c.get("/ui/settings").text
    assert "raw_data.min_size" in page and "50M" in page and "14d" in page and "target/**" in page and "<code>bag</code>" in page and str(conf.path) in page
    r = c.post("/ui/settings/set", data={"key": "raw_data.min_size", "value": "10M"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/ui/settings?saved=")
    assert cfg.load(conf.path).rules["raw_data"]["min_size"] == "10M"                       # 保存後の再読込
    page = c.get(r.headers["location"]).text
    assert "raw_data.min_size = 10M" in page and "value='10M'" in page
    assert c.post("/ui/settings/set", data={"key": "raw_data.min_age", "value": "later"}, follow_redirects=False).status_code == 400
    assert c.post("/ui/settings/set", data={"key": "nope", "value": "1"}, follow_redirects=False).status_code == 400
    assert cfg.load(conf.path).rules["raw_data"]["min_age"] == "14d"                        # 不正な値は保存しない
    assert c.post("/ui/settings/add-exclude", data={"pattern": "logs/**"}, follow_redirects=False).status_code == 303
    assert "<code>logs/**</code>" in c.get("/ui/settings").text and "logs/**" in cfg.load(conf.path).rules["exclude"]
    assert c.post("/ui/settings/remove-exclude", data={"pattern": "logs/**"}, follow_redirects=False).status_code == 303
    assert c.post("/ui/settings/remove-exclude", data={"pattern": "logs/**"}, follow_redirects=False).status_code == 400
    assert c.post("/ui/settings/add-raw-ext", data={"ext": ".mcap"}, follow_redirects=False).status_code == 303
    assert "mcap" in cfg.load(conf.path).rules["raw_data"]["extensions"]
    assert c.post("/ui/settings/remove-raw-ext", data={"ext": "mcap"}, follow_redirects=False).status_code == 303
    assert c.post("/ui/settings/set", data={"key": "bwlimit", "value": "4M"}, follow_redirects=False).status_code == 303
    assert cfg.load(conf.path).rules["bwlimit"] == "4M" and conf.rules["bwlimit"] == "4M"     # 実行中のプロセスの conf にも反映
    # rclone_flags: 表示・保存・拒否・空で既定に戻す
    assert "rclone_flags" in c.get("/ui/settings").text and "--drive-pacer-burst 200" in c.get("/ui/settings").text   # 推奨例のヒント
    r = c.post("/ui/settings/set", data={"key": "rclone_flags", "value": "--transfers 8 --checkers 16"}, follow_redirects=False)
    assert r.status_code == 303 and "rclone_flags%20%3D%20--transfers%208%20--checkers%2016" in r.headers["location"]
    assert cfg.load(conf.path).rules["rclone_flags"] == ["--transfers", "8", "--checkers", "16"] and conf.rules["rclone_flags"] == ["--transfers", "8", "--checkers", "16"]
    assert "<code>--transfers 8 --checkers 16</code>" in c.get("/ui/settings").text
    assert c.post("/ui/settings/set", data={"key": "rclone_flags", "value": "-v"}, follow_redirects=False).status_code == 400
    assert cfg.load(conf.path).rules["rclone_flags"] == ["--transfers", "8", "--checkers", "16"]
    r = c.post("/ui/settings/set", data={"key": "rclone_flags", "value": ""}, follow_redirects=False)
    assert r.status_code == 303 and "rclone_flags" not in cfg.load(conf.path).rules
    assert c.post("/ui/settings/unknown", data={}, follow_redirects=False).status_code == 404
    # CSRF: 他サイトからの POST は 403（既存の same_origin）
    r = c.post("/ui/settings/set", data={"key": "bwlimit", "value": "off"}, headers={"Origin": "http://evil.example"}, follow_redirects=False)
    assert r.status_code == 403 and cfg.load(conf.path).rules["bwlimit"] == "4M"


def test_case_page_shows_queued_jobs(conf):
    """同じ案件の先行ジョブを待つ queued のジョブも進行中の表示に出る（status: queued、先行待ちの注記）。"""
    import threading
    _seed(conf)
    jobs = JobTable()
    c = TestClient(build_ui(conf, jobs=jobs))
    started = threading.Event(); release = threading.Event()

    def fn(progress):
        started.set(); release.wait(5)
    first, _ = jobs.submit("checkin", "acme", "CASE-123", fn)
    assert started.wait(5)
    second, _ = jobs.submit("checkout", "acme", "CASE-123", lambda p: None)
    assert second.status == "queued"
    page = c.get("/ui/acme/CASE-123").text
    assert "<b>checkin</b>" in page and "running" in page and first.id in page
    assert "<b>checkout</b>" in page and "queued" in page and second.id in page and "waiting for the previous job" in page
    release.set(); first.wait(5); second.wait(5)
    assert "進行中のジョブ" not in c.get("/ui/acme/CASE-123").text


def test_index_shows_drive_state_and_refresh_button(conf, drive_manifest):
    """一覧の Drive 列: キャッシュ無し → 不明、rev 一致 → 同期済み、rev 違い → Drive の方が新しい、未 checkin の変更 → ローカル未 checkin。
    「更新確認」（POST /ui/refresh）は manifest を取得してキャッシュを更新し、一覧に戻る（案件は取り寄せない）。CSRF は既存の same_origin。"""
    import os, time
    from kairn import sync
    st = _seed(conf)
    ws = conf.workspaces["acme"]
    c = TestClient(build_ui(conf))
    page = c.get("/ui").text
    assert "<th>Drive</th>" in page and "class='drive muted'" in page and ">不明<" in page and "manifest 未取得" in page and "action='/ui/refresh'" in page and "更新確認" in page
    st.mark_checkin("CASE-123"); rev = st.load_case("CASE-123")["rev"]
    sync.save_manifest_cache(ws, {"cases": {"CASE-123": {"rev": rev, "checked_in_at": "2026-09-01T00:00:00+09:00", "from": "host-a"}}})
    page = c.get("/ui").text
    assert "class='drive ok'" in page and ">同期済み<" in page and "host-a" in page and "manifest " in page and "取得" in page and "class='drive muted'" not in page
    sync.save_manifest_cache(ws, {"cases": {"CASE-123": {"rev": "newer"}}})
    assert "Drive の方が新しい" in c.get("/ui").text
    t = time.time() + 5
    os.utime(ws.cases_dir / "CASE-123" / "worklog.md", (t, t))
    page = c.get("/ui").text
    assert "ローカル未 checkin（Drive も更新あり）" in page and "worklog.md" in page
    os.utime(ws.cases_dir / "CASE-123" / "worklog.md", None)
    # 更新確認: Drive の manifest（偽物）を取得してキャッシュを更新 → 同期済みに戻る。案件の取り寄せはしない
    drive_manifest.data = {"cases": {"CASE-123": {"rev": rev, "checked_in_at": "2026-09-02T00:00:00+09:00", "from": "host-b"}}}
    r = c.post("/ui/refresh", data={"ws": "acme"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/ui?ws=acme&refreshed=") and drive_manifest.fetches == 1
    page = c.get(r.headers["location"]).text
    assert "acme: manifest 1 case(s)" in page and "同期済み" in page and "host-b" in page
    # 取得失敗（オフライン）: キャッシュはそのまま、メッセージだけ
    drive_manifest.unavailable = True
    r = c.post("/ui/refresh", data={"ws": ""}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/ui?refreshed=")
    page = c.get(r.headers["location"]).text
    assert "acme: manifest unavailable" in page and "同期済み" in page
    assert c.post("/ui/refresh", data={"ws": "nowhere"}, follow_redirects=False).status_code == 404
    assert c.post("/ui/refresh", data={"ws": "acme"}, headers={"Origin": "http://evil.example"}, follow_redirects=False).status_code == 403
