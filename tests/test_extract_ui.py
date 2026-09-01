"""extract の入口（MCP / CLI / UI）のテスト。subprocess はモック（実 CLI は起動しない）。"""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from kairn.extract import adapters
from kairn.ui import build_ui

from test_extract import FakeRun, _claude_stdout, _seed, good_card


# ---------- UI ----------

def test_ui_extract_and_apply(conf, monkeypatch):
    st = _seed(conf)
    c = TestClient(build_ui(conf))
    page = c.get("/ui/acme/CASE-123").text
    assert "下書きを取得" in page and "/ui/acme/CASE-123/extract" in page
    # 失敗はページに理由を出し、適用ボタンは出ない
    monkeypatch.setattr(adapters.subprocess, "run", FakeRun(stdout="boom", returncode=3))
    r = c.post("/ui/acme/CASE-123/extract")
    assert r.status_code == 200 and "exit code 3" in r.text and "apply" not in r.text
    # 成功: 差分と適用フォーム。case.json はまだ変わらない
    monkeypatch.setattr(adapters.subprocess, "run", FakeRun(stdout=_claude_stdout(good_card())))
    r = c.post("/ui/acme/CASE-123/extract")
    assert r.status_code == 200 and "/ui/acme/CASE-123/apply" in r.text and "widget-driver" in r.text and "confidence: 0.8" in r.text
    assert st.load_case("CASE-123")["title"] == "起動時に driver が初期化されない" and "summary" not in st.load_case("CASE-123")
    # 適用（フォームの hidden card をそのまま送る）
    import html as _html, re
    card_attr = re.search(r"name=card value='([^']*)'", r.text).group(1)
    r = c.post("/ui/acme/CASE-123/apply", data={"card": _html.unescape(card_attr)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/acme/CASE-123"
    saved = st.load_case("CASE-123")
    assert saved["title"] == good_card()["title"] and saved["elements"]["component"] == ["widget-driver"] and saved["causal"] == good_card()["causal"]
    ev = st.events("CASE-123")
    assert ev[-1]["actor"] == "human" and ev[-1]["action"] == "decision" and ev[-1]["note"] == "applied extract draft"
    assert [e["action"] for e in ev[-3:-1]] == ["extract", "extract"]
    page = c.get("/ui/acme/CASE-123").text
    assert "summary:" in page and "UART 460800 で送信量が超過" in page and "widget-driver" in page
    # 不正な下書きは 400、CSRF は 403
    assert c.post("/ui/acme/CASE-123/apply", data={"card": "{bad"}).status_code == 400
    assert c.post("/ui/acme/CASE-123/apply", data={"card": json.dumps(good_card(extra=1))}).status_code == 400
    assert c.post("/ui/acme/CASE-123/extract", headers={"origin": "http://evil.example"}).status_code == 403


def test_ui_extract_does_not_block_other_requests(conf, monkeypatch):
    """M-1: extract（最長 extract.timeout 秒の subprocess）中でも同じアプリの別リクエストが処理される（threadpool で実行）。"""
    import threading
    from kairn import extract, ui
    _seed(conf)
    started, release = threading.Event(), threading.Event()

    def slow_extract(conf_, ws, case, agent=None, timeout=None):
        started.set()
        if not release.wait(timeout=5):
            raise AssertionError("extract was never released")
        return {"ok": False, "card": None, "agent": "fake", "elapsed_sec": 0.0, "error": "released", "raw_excerpt": ""}
    monkeypatch.setattr(extract, "extract_card", slow_extract)
    # with 付き: 全リクエストが 1 つのイベントループ（portal）を共有する（本番の uvicorn と同じ条件）
    with TestClient(build_ui(conf)) as c:
        result = {}
        t = threading.Thread(target=lambda: result.update(extract=c.post("/ui/acme/CASE-123/extract")))
        t.start()
        assert started.wait(timeout=5)
        got = {}
        g = threading.Thread(target=lambda: got.update(page=c.get("/ui/acme/CASE-123").status_code))
        g.start(); g.join(timeout=3)
        blocked = g.is_alive()      # extract がイベントループを塞いでいると GET が終わらない
        release.set(); t.join(timeout=10); g.join(timeout=10)
    assert not blocked and got.get("page") == 200
    assert result["extract"].status_code == 200 and "released" in result["extract"].text
