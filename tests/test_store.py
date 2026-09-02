import pytest
from kairn.store import CaseStore


@pytest.fixture
def store(tmp_path):
    return CaseStore(tmp_path / "cases")


def test_create_plan_supersede_and_evidence(store):
    store.create_case("CASE-1", "widget boot", "acme", actor="human")
    p1 = store.new_plan_version("CASE-1", "boot works", [{"title": "investigate"}, {"title": "fix"}], reason="initial", actor="ai")
    assert [t["id"] for t in p1["tasks"]] == ["T001", "T002"]
    # done without evidence is refused
    with pytest.raises(ValueError):
        store.set_task_status("CASE-1", "T001", "done", [], "", actor="ai")
    store.set_task_status("CASE-1", "T001", "done", [{"type": "commit", "id": "abc"}], "ok", actor="ai")
    # re-plan: carry T002, drop T001 (done stays done), add new
    p2 = store.new_plan_version("CASE-1", "boot works on unit-6 too", [{"carried_from": "T002"}, {"title": "verify unit-6"}], reason="sendback", actor="ai")
    ids = [t["id"] for t in p2["tasks"]]
    assert ids == ["T002", "T003"] and p2["superseded"] == []
    # re-plan without carrying T002 -> superseded
    p3 = store.new_plan_version("CASE-1", "changed", [{"title": "new direction"}], reason="pivot", actor="human")
    assert set(p3["superseded"]) == {"T002", "T003"}
    v2 = store.list_plans("CASE-1")[1]
    assert all(t["status"] == "superseded" for t in v2["tasks"] if t["id"] in ("T002", "T003"))
    assert store.progress("CASE-1") == {"total": 1, "done": 0, "open": 1, "plan": 3}
    actions = [e["action"] for e in store.events("CASE-1")]
    assert actions[0] == "opened" and "plan" in actions and "done" in actions


def test_invalid_ids(store):
    with pytest.raises(ValueError):
        store.case_dir("../x")


def test_validate_evidence_types_and_required_keys(store):
    from kairn.store import validate_evidence
    assert validate_evidence(None, "ai") == []
    ok = [{"type": "commit", "id": "abc"}, {"type": "pr", "id": 1}, {"type": "file", "path": "x"}, {"type": "test", "cmd": "pytest"}, {"type": "url", "url": "u"}]
    assert validate_evidence(ok, "ai") == ok
    assert validate_evidence([{"type": "note", "text": "seen it"}], "human")        # note は human のみ、text 必須
    for ev, actor in [([{"type": "note", "text": "x"}], "ai"), ([{"type": "note", "text": "x"}], "kairn"), ([{"type": "note"}], "human"),
                      ([{"type": "note", "text": ""}], "human"), ([{"type": "note", "note": "old key"}], "human"), ([{"type": "commit", "id": ""}], "ai"),
                      ([{"type": "file", "id": "x"}], "ai"), ([{"type": "zip", "path": "x"}], "human"), ([{"id": "x"}], "ai"), ("commit abc", "ai")]:
        with pytest.raises(ValueError):
            validate_evidence(ev, actor)
    # append_event / set_task_status も同じ検証を通る
    store.create_case("CASE-1", "t", "acme", actor="human")
    store.new_plan_version("CASE-1", "o", [{"title": "a"}], reason="r", actor="ai")
    with pytest.raises(ValueError, match="human only"):
        store.set_task_status("CASE-1", "T001", "done", [{"type": "note", "text": "x"}], "", actor="ai")
    with pytest.raises(ValueError, match="requires 'text'"):
        store.append_event("CASE-1", {"actor": "human", "action": "comment", "note": "x", "evidence": [{"type": "note"}]})
    with pytest.raises(ValueError, match="requires 'path'"):
        store.append_event("CASE-1", {"actor": "ai", "action": "progress", "note": "x", "evidence": [{"type": "file"}]})
    store.append_event("CASE-1", {"actor": "human", "action": "comment", "note": "x", "evidence": [{"type": "note", "text": "ok"}]})
    store.set_task_status("CASE-1", "T001", "done", [{"type": "test", "cmd": "pytest -q", "result": "30 passed"}], "", actor="ai")


def test_new_plan_version_rejects_duplicates_and_empty_tasks(store):
    store.create_case("CASE-1", "t", "acme", actor="human")
    store.new_plan_version("CASE-1", "o", [{"title": "a"}, {"title": "b"}], reason="r", actor="ai")
    with pytest.raises(ValueError, match="T002 carried twice"):
        store.new_plan_version("CASE-1", "o", [{"carried_from": "T002"}, {"carried_from": "T002"}], reason="r", actor="ai")
    with pytest.raises(ValueError, match="task needs title or carried_from"):
        store.new_plan_version("CASE-1", "o", [{"title": ""}], reason="r", actor="ai")
    with pytest.raises(ValueError, match="owner must be ai \\| human"):
        store.new_plan_version("CASE-1", "o", [{"title": "x", "owner": "robot"}], reason="r", actor="ai")
    with pytest.raises(ValueError, match="unknown task T999"):
        store.new_plan_version("CASE-1", "o", [{"carried_from": "T999"}], reason="r", actor="ai")
    # 失敗した呼び出しは何も残さない
    assert store.current_plan("CASE-1")["version"] == 1 and store.list_plans("CASE-1")[0]["superseded"] == []
    assert all(t["status"] == "open" for t in store.current_plan("CASE-1")["tasks"])


def test_mark_checkin_and_local_changes(store):
    import os, time
    store.create_case("CASE-1", "t", "acme", actor="human")
    assert store.local_changes_since_checkin("CASE-1") is None          # 未記録 → 判定不能
    assert store.local_changes_since_checkin("CASE-404") is None        # 案件なし
    assert store.rev_markers("CASE-1") == []                            # rev 未付与: .rev/ も無い
    ts = store.mark_checkin("CASE-1")
    c = store.load_case("CASE-1")
    assert c["last_checkin_at"] == ts and len(c["rev"]) == 36 and c["checked_in_from"]   # 版マーカー（uuid4）とホスト名
    assert store.rev_markers("CASE-1") == [c["rev"]]                    # 案件フォルダの .rev/<rev>（空ファイル 1 個）
    assert (store.case_dir("CASE-1") / ".rev" / c["rev"]).stat().st_size == 0
    assert store.local_changes_since_checkin("CASE-1") == []            # 直後は変更なし（case.json 自身の書き込みは誤検出しない）
    assert store.mark_checkin("CASE-1") and store.load_case("CASE-1")["rev"] != c["rev"]   # 毎回振り直す
    assert store.rev_markers("CASE-1") == [store.load_case("CASE-1")["rev"]]              # 古いマーカーは残らない
    d = store.case_dir("CASE-1")
    store.new_plan_version("CASE-1", "o", [{"title": "a"}], reason="r", actor="ai")
    t = time.time() + 60
    for p in (d / "plan" / "v0001.json", d / "events.jsonl"):
        os.utime(p, (t, t))
    assert store.local_changes_since_checkin("CASE-1") == ["events.jsonl", "plan/v0001.json"]
    assert store.mark_checkin("CASE-404") is None
    # kairn 自身の checkin event だけで events.jsonl が伸びた場合は変更と数えない（H-1）。checkout action はもう無い
    os.utime(d / "plan" / "v0001.json", None)  # 偽装した未来の mtime を戻す
    store.mark_checkin("CASE-1")
    store.append_event("CASE-1", {"actor": "ai", "agent": "x", "action": "checkin", "note": "fake"})
    with pytest.raises(ValueError, match="invalid event action: checkout"):
        store.append_event("CASE-1", {"actor": "ai", "agent": "x", "action": "checkout", "note": "open_case"})
    os.utime(d / "events.jsonl", (t, t))
    assert store.local_changes_since_checkin("CASE-1") == []
    store.append_event("CASE-1", {"actor": "human", "action": "comment", "note": "real change"})
    os.utime(d / "events.jsonl", (t, t))
    assert store.local_changes_since_checkin("CASE-1") == ["events.jsonl"]


def test_write_rev_marker_rebuilds_dir(store):
    """write_rev_marker: .rev/ を空にして case.json の rev を名前にした空ファイルを 1 個置く。古いマーカー・入れ子・無関係なファイルは消える。
    rev が無ければ .rev/ ごと消す。case.json が無ければ何もしない。"""
    store.create_case("CASE-1", "t", "acme", actor="human")
    d = store.case_dir("CASE-1") / ".rev"
    assert store.write_rev_marker("CASE-1") is None and not d.exists()          # rev 未付与
    d.mkdir(); (d / "stale-1").touch(); (d / "stale-2").touch(); (d / "nested").mkdir(); (d / "nested" / "x").touch()
    assert store.write_rev_marker("CASE-1") is None and not d.exists()          # rev 未付与: 中身ごと消す
    store.set_rev("CASE-1", "11111111-2222-3333-4444-555555555555")
    assert store.rev_markers("CASE-1") == ["11111111-2222-3333-4444-555555555555"]
    (d / "stale-3").touch()
    m = store.write_rev_marker("CASE-1")
    assert m == d / "11111111-2222-3333-4444-555555555555" and store.rev_markers("CASE-1") == [m.name] and m.stat().st_size == 0
    assert store.write_rev_marker("CASE-404") is None and store.rev_markers("CASE-404") == []
    assert store.local_changes_since_checkin("CASE-1") is None                  # .rev/ はローカル変更の判定に入らない


def test_set_rev_keeps_mtime_and_updated_at(store):
    """rev だけを書き換え、updated_at と mtime は変えない（open_case の skip 判定に影響させない）。.rev/ は作り直す。"""
    import os, time
    store.create_case("CASE-1", "t", "acme", actor="human")
    store.mark_checkin("CASE-1")
    f = store.case_dir("CASE-1") / "case.json"
    old = time.time() - 3600
    os.utime(f, (old, old))
    before = store.load_case("CASE-1")
    store.set_rev("CASE-1", "11111111-2222-3333-4444-555555555555")
    after = store.load_case("CASE-1")
    assert after["rev"] == "11111111-2222-3333-4444-555555555555" and after["updated_at"] == before["updated_at"]
    assert store.rev_markers("CASE-1") == ["11111111-2222-3333-4444-555555555555"]
    assert abs(f.stat().st_mtime - old) < 1e-3 and {k: v for k, v in after.items() if k != "rev"} == {k: v for k, v in before.items() if k != "rev"}


def test_append_access_log(tmp_path):
    from kairn.store import append_access_log
    log = tmp_path / "index" / "access.log"
    l1 = append_access_log(log, "CASE-1", "agent-a")
    l2 = append_access_log(log, "CASE-2", "agent-b")
    assert log.read_text(encoding="utf-8") == l1 + "\n" + l2 + "\n"
    assert l1.split("\t")[1:] == ["CASE-1", "agent-a"] and l2.split("\t")[1:] == ["CASE-2", "agent-b"]
    with pytest.raises(ValueError):
        append_access_log(log, "../x", "agent-a")
