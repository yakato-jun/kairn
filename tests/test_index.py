from kairn.index import Index, split_sections
from kairn.store import CaseStore


def test_split_and_search(tmp_path):
    cases = tmp_path / "cases"; st = CaseStore(cases)
    st.create_case("CASE-7", "uart overflow", "acme", actor="human", elements={"component": ["uartif"], "symptom": ["frame drop"]})
    (cases / "CASE-7" / "worklog.md").write_text("# t\n## Objective\nUART 460800 の上限で送信量が超過する\n## Decision Log\n### 2026-09-01: 1 バイト送信をやめる\n", encoding="utf-8")
    assert [h for h, _ in split_sections((cases / "CASE-7" / "worklog.md").read_text())] == ["(先頭)", "Objective", "Decision Log"]
    ix = Index(tmp_path / "index", cases)
    r = ix.rebuild()
    assert r["cases"] == 1
    hits = ix.search_sections("UART 460800")
    assert hits and hits[0]["case"] == "CASE-7" and hits[0]["heading"] == "Objective"
    found = ix.find_cases("uartif frame drop")
    assert found and found[0]["case"] == "CASE-7"
    assert ix.search_sections("送信量（超過）")  # 記号入りでも落ちない


def test_short_terms_fall_back_to_like(tmp_path):
    """trigram は 3 文字未満の語を索引しないので、短語だけの問いは case_id / title の LIKE で補う。"""
    cases = tmp_path / "cases"; st = CaseStore(cases)
    st.create_case("C1-widget", "boot", "acme", actor="human")
    st.create_case("C2-other", "x1 sensor", "acme", actor="human")
    (cases / "C1-widget" / "worklog.md").write_text("# t\n## Notes\nnothing here\n", encoding="utf-8")
    ix = Index(tmp_path / "index", cases); ix.rebuild()
    assert [r["case"] for r in ix.find_cases("C1")] == ["C1-widget"]
    assert ix.find_cases("C1")[0]["reasons"] == ["案件 ID / title に部分一致"]
    assert [r["case"] for r in ix.find_cases("x1")] == ["C2-other"]        # title
    assert sorted(r["case"] for r in ix.find_cases("C1 x1")) == ["C1-widget", "C2-other"]
    assert ix.find_cases("zz") == [] and ix.find_cases("") == []
    assert [r["case"] for r in ix.search_sections("C1")] == ["C1-widget", "C1-widget"]  # 節は case_id でも当たる
    assert ix.search_sections("zz") == []
