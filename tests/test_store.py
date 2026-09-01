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
