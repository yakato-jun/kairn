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
