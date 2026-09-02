"""UI（Starlette TestClient）。/ui は末尾スラッシュ無しで 200、日本語フォームが正しく記録される、鮮度表示。"""
from __future__ import annotations

from datetime import timedelta

from starlette.testclient import TestClient

from kairn.jobs import JobTable
from kairn.store import CaseStore
from kairn.ui import STALE_DAYS, build_ui, case_freshness, task_freshness
from tests.conftest import bump_mtime


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
    # 時系列にステータス変更イベントが actor・from → to・note 付きで出る
    ev = st.events("CASE-123")[-1]
    assert ev["action"] == "status" and ev["actor"] == "human" and ev["from"] == "open" and ev["to"] == "suspended" and ev["note"] == "保留"
    page = c.get("/ui/acme/CASE-123").text
    assert "status open → suspended — 保留" in page
    # 同じステータスを選び直しても event は増えない
    n = len(st.events("CASE-123"))
    assert c.post("/ui/acme/CASE-123/status", data={"status": "suspended", "note": "again"}, follow_redirects=False).status_code == 303
    assert len(st.events("CASE-123")) == n
    assert c.post("/ui/acme/CASE-123/status", data={"status": "archived", "note": ""}, follow_redirects=False).status_code == 400


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
    """/ui/settings: rules の現在値を表示し、フォームの POST で検証・保存する（CLI の kairn rules と同じ操作）。一覧のヘッダにリンク。
    保存後の値は holder.current()（次のリクエストで読み直された Config）にも入る。"""
    from kairn import config as cfg
    holder = cfg.ConfigHolder(conf)
    c = TestClient(build_ui(holder))
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
    assert cfg.load(conf.path).rules["bwlimit"] == "4M" and holder.current().rules["bwlimit"] == "4M"     # 実行中のプロセスの設定にも反映
    # rclone_flags: 表示・保存・拒否・空で既定に戻す
    assert "rclone_flags" in c.get("/ui/settings").text and "--drive-pacer-burst 200" in c.get("/ui/settings").text   # 推奨例のヒント
    r = c.post("/ui/settings/set", data={"key": "rclone_flags", "value": "--transfers 8 --checkers 16"}, follow_redirects=False)
    assert r.status_code == 303 and "rclone_flags%20%3D%20--transfers%208%20--checkers%2016" in r.headers["location"]
    assert cfg.load(conf.path).rules["rclone_flags"] == ["--transfers", "8", "--checkers", "16"] and holder.current().rules["rclone_flags"] == ["--transfers", "8", "--checkers", "16"]
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


def test_index_shows_drive_state_and_refresh_button(conf, fake_drive, monkeypatch):
    """一覧の Drive 列: キャッシュ無し → 不明、rev 一致 → 同期済み、rev 違い → Drive の方が新しい、未 checkin の変更 → ローカル未 checkin。
    「更新確認」（POST /ui/refresh）は Drive の版マーカーを 1 回読んでキャッシュを更新し、rev が違う案件だけ取り寄せて一覧に戻る。CSRF は既存の same_origin。"""
    import os, time
    from kairn import sync
    from kairn.store import hostname
    st = _seed(conf)
    ws = conf.workspaces["acme"]
    c = TestClient(build_ui(conf))
    page = c.get("/ui").text
    assert "<th>Drive</th>" in page and "class='drive muted'" in page and ">不明<" in page and "版 未取得" in page and "action='/ui/refresh'" in page and "更新確認" in page
    st.mark_checkin("CASE-123"); rev = st.load_case("CASE-123")["rev"]
    sync.save_drive_revs_cache(ws, {"CASE-123": rev})
    page = c.get("/ui").text
    assert "class='drive ok'" in page and ">同期済み<" in page and f"checked in from {hostname()}" in page and "版 " in page and "取得" in page and "class='drive muted'" not in page
    sync.save_drive_revs_cache(ws, {"CASE-123": "newer"})
    assert "Drive の方が新しい" in c.get("/ui").text
    sync.save_drive_revs_cache(ws, {"CASE-123": None})      # マーカーが 2 個以上（不定）
    page = c.get("/ui").text
    assert "Drive の方が新しい" in page and "(ambiguous)" in page
    t = time.time() + 5
    os.utime(ws.cases_dir / "CASE-123" / "worklog.md", (t, t))
    page = c.get("/ui").text
    assert "ローカル未 checkin（Drive も更新あり）" in page and "worklog.md" in page
    os.utime(ws.cases_dir / "CASE-123" / "worklog.md", None)
    # 更新確認: Drive の版（偽物）を 1 回読んでキャッシュを更新 → 同期済みに戻る。rev が一致する案件は取り寄せない
    fetched = []
    monkeypatch.setattr(sync, "checkout", lambda conf_, w, case=None, dry=False, progress=None: fetched.append(case) or "fake checkout")
    fake_drive.set_rev("CASE-123", rev)
    r = c.post("/ui/refresh", data={"ws": "acme"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/ui?ws=acme&refreshed=") and fake_drive.listings == 1 and fetched == []
    page = c.get(r.headers["location"]).text
    assert "acme: drive: 1 case(s); fetched 0, up to date 1" in page and "同期済み" in page
    # Drive の rev が違う → その案件だけ取り寄せる
    fake_drive.set_rev("CASE-123", "from-another-host")
    r = c.post("/ui/refresh", data={"ws": ""}, follow_redirects=False)
    assert r.status_code == 303 and fetched == ["CASE-123"] and "fetched 1 (CASE-123)" in c.get(r.headers["location"]).text
    # 取得失敗（オフライン）: キャッシュはそのまま、メッセージだけ
    fake_drive.unavailable = True
    r = c.post("/ui/refresh", data={"ws": ""}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/ui?refreshed=")
    page = c.get(r.headers["location"]).text
    assert "acme: drive unavailable" in page and "Drive の方が新しい" in page and fetched == ["CASE-123"]
    assert c.post("/ui/refresh", data={"ws": "nowhere"}, follow_redirects=False).status_code == 404
    assert c.post("/ui/refresh", data={"ws": "acme"}, headers={"Origin": "http://evil.example"}, follow_redirects=False).status_code == 403


def test_ui_reads_config_changes_without_restart(conf):
    """常駐中に config.yaml が変わる（別プロセスの CLI: ws create / attach / rules …、または UI の設定ページ）→ 再起動なしで次のリクエストから反映。
    一覧・案件ページは新しいワークスペースを認識し、設定ページは新しい rules を出す。壊れた設定に書き換わっても直前の設定で動き続ける。"""
    from kairn import config as cfg
    _seed(conf)
    conf.save()
    holder = cfg.ConfigHolder(conf, warn=lambda m: None)
    c = TestClient(build_ui(holder))
    assert "CASE-123" in c.get("/ui").text and c.get("/ui/beta/CASE-7").status_code == 404
    # 別プロセスの `kairn ws create beta` 相当: ファイルから読み直した Config に足して保存（このプロセスの holder は知らない）
    other = cfg.load(conf.path)
    other.workspaces["beta"] = cfg.Workspace(name="beta", description="second")
    other.save(); bump_mtime(conf.path)
    st = CaseStore(other.workspaces["beta"].cases_dir)
    st.create_case("CASE-7", "beta の案件", "beta", actor="human")
    page = c.get("/ui").text
    assert "CASE-7" in page and "beta の案件" in page and "CASE-123" in page
    assert c.get("/ui/beta/CASE-7").status_code == 200 and holder.reloads == 1
    assert c.post("/ui/beta/CASE-7/comment", data={"note": "hello"}, follow_redirects=False).status_code == 303
    assert st.events("CASE-7")[-1]["note"] == "hello"
    # 別プロセスの `kairn rules add-exclude` 相当 → 設定ページに出る。UI の設定ページで保存 → 直後の設定ページ・一覧に反映
    cfg.add_exclude(cfg.load(conf.path), "logs/**"); bump_mtime(conf.path)
    assert "<code>logs/**</code>" in c.get("/ui/settings").text and holder.reloads == 2
    r = c.post("/ui/settings/set", data={"key": "raw_data.min_size", "value": "10M"}, follow_redirects=False)
    assert r.status_code == 303
    assert "value='10M'" in c.get("/ui/settings").text and "<code>logs/**</code>" in c.get("/ui/settings").text   # 直前の変更も失っていない
    assert holder.current().rules["raw_data"]["min_size"] == "10M" and "logs/**" in holder.current().rules["exclude"]
    assert "CASE-7" in c.get("/ui").text
    # 壊れた設定に書き換わっても UI は直前の設定で動き続ける
    conf.path.write_text("drive: {remote: [broken\n", encoding="utf-8"); bump_mtime(conf.path)
    assert c.get("/ui/beta/CASE-7").status_code == 200 and "logs/**" in c.get("/ui/settings").text


def test_case_page_shows_xref_events_and_cross_workspace_related(conf2):
    """時系列: xref event は「他 ws 参照: <ws>/<case> (<tool>)」で出る。関連欄: "<ws>/<case>" は実在すれば /ui/<ws>/<case> へのリンク、
    無ければ文字列のまま。一覧には跨ぎ参照の印を出さない。"""
    _seed(conf2)
    a = CaseStore(conf2.workspaces["acme"].cases_dir)
    b = CaseStore(conf2.workspaces["beta"].cases_dir)
    b.create_case("CASE-9", "beta の案件", "beta", actor="human", related=["acme/CASE-123", "acme/CASE-404", "gamma/CASE-1", "CASE-9"])
    ev = b.append_xref("CASE-9", "acme", "CASE-123", "open_case", agent="claude")
    b.append_xref("CASE-9", "acme", "CASE-404", "search")
    c = TestClient(build_ui(conf2))
    page = c.get("/ui/beta/CASE-9").text
    assert "xref 他 ws 参照: acme/CASE-123 (open_case)" in page and "他 ws 参照: acme/CASE-404 (search)" in page and "<b>ai</b> <small>claude</small>" in page
    assert "<a href='/ui/acme/CASE-123'>acme/CASE-123</a>" in page and "<a href='/ui/beta/CASE-9'>CASE-9</a>" in page
    assert "acme/CASE-404" in page and "href='/ui/acme/CASE-404'" not in page and "gamma/CASE-1" in page and "href='/ui/gamma/CASE-1'" not in page
    assert c.get("/ui/acme/CASE-123").status_code == 200
    # 参照された側（acme/CASE-123）のページ・一覧には何も出ない
    assert "他 ws 参照" not in c.get("/ui/acme/CASE-123").text
    listing = c.get("/ui").text
    assert "CASE-9" in listing and "CASE-123" in listing and "他 ws" not in listing   # 参照元の最終イベント欄に xref が出るのは従来どおり（印は足さない）
    assert a.events("CASE-123")[-1]["action"] != "xref" and ev["t"][:16] in page


def test_case_page_adds_and_removes_related(conf2):
    """関連の追加（POST related）/ 削除（POST unrelated）: case.json.related を変え、actor=human の related event（added / removed）を書き、
    関連欄と時系列に出る。不正な形・空・重複・無いものの削除は 400 で何も書かない。CSRF は same_origin。"""
    st = _seed(conf2)
    b = CaseStore(conf2.workspaces["beta"].cases_dir)
    b.create_case("CASE-9", "beta の案件", "beta", actor="human")
    c = TestClient(build_ui(conf2))
    r = c.post("/ui/acme/CASE-123/related", data={"ref": "beta/CASE-9", "note": "同種の症状"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/acme/CASE-123"
    r = c.post("/ui/acme/CASE-123/related", data={"ref": " CASE-100 "}, follow_redirects=False)
    assert r.status_code == 303
    assert st.load_case("CASE-123")["related"] == ["beta/CASE-9", "CASE-100"]     # 実在しない CASE-100 も形だけで受ける
    ev = [e for e in st.events("CASE-123") if e["action"] == "related"]
    assert [(e["actor"], e["added"], e["note"]) for e in ev] == [("human", ["beta/CASE-9"], "同種の症状"), ("human", ["CASE-100"], "")]
    assert not any(e["action"] == "xref" for e in st.events("CASE-123"))          # UI の追加は跨ぎ参照（xref）にしない
    page = c.get("/ui/acme/CASE-123").text
    assert "<a href='/ui/beta/CASE-9'>beta/CASE-9</a>" in page and "related +beta/CASE-9" in page and "related +CASE-100" in page
    assert "action='/ui/acme/CASE-123/unrelated'" in page and "value='beta/CASE-9'" in page and "action='/ui/acme/CASE-123/related'" in page
    # 400: 不正な形・空・重複
    for data in ({"ref": "a/b/c"}, {"ref": "../x"}, {"ref": ""}, {"ref": "CASE-100"}):
        assert c.post("/ui/acme/CASE-123/related", data=data).status_code == 400, data
    assert st.load_case("CASE-123")["related"] == ["beta/CASE-9", "CASE-100"] and len(st.events("CASE-123")) == 4   # opened + plan + related ×2
    # 削除
    r = c.post("/ui/acme/CASE-123/unrelated", data={"ref": "CASE-100", "note": "誤り"}, follow_redirects=False)
    assert r.status_code == 303 and st.load_case("CASE-123")["related"] == ["beta/CASE-9"]
    e = st.events("CASE-123")[-1]
    assert e["action"] == "related" and e["actor"] == "human" and e["removed"] == ["CASE-100"] and e["note"] == "誤り" and "added" not in e
    assert c.post("/ui/acme/CASE-123/unrelated", data={"ref": "CASE-100"}).status_code == 400   # もう無い
    assert "related -CASE-100" in c.get("/ui/acme/CASE-123").text
    # CSRF
    for kind in ("related", "unrelated"):
        assert c.post(f"/ui/acme/CASE-123/{kind}", data={"ref": "beta/CASE-9"}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert st.load_case("CASE-123")["related"] == ["beta/CASE-9"]
    # 未知の案件は 404
    assert c.post("/ui/acme/CASE-999/related", data={"ref": "CASE-123"}).status_code == 404
