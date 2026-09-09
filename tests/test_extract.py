"""extract（段階 7）: アダプタの build_command / extract_json、スキーマ検証、extract_card（subprocess はモック。実 CLI は起動しない）、apply_card。
MCP / CLI / UI の入口は tests/test_extract_mcp_cli.py, tests/test_extract_ui.py。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from kairn import extract
from kairn.extract import adapters
from kairn.store import CaseStore

KEYS = ("machine", "component", "symptom", "ticket", "site", "external")


def good_card(**over) -> dict:
    card = {"title": "起動時に widget driver が初期化されない", "summary": "起動直後に driver init が終わらない。\nUART の送信量超過が原因。",
            "elements": {"machine": ["unit-2"], "component": ["widget-driver"], "symptom": ["起動時に driver init 未完了"],
                         "ticket": ["CASE-123"], "site": [], "external": []},
            "related": ["CASE-100"],
            "causal": [{"symptom": "起動時に driver init 未完了", "component": "widget-driver", "cause": "UART 460800 で送信量が超過",
                        "evidence": "## Notes: UART 460800 で送信量が超過する"}],
            "confidence": 0.8}
    card.update(over)
    return card


def _seed(conf) -> CaseStore:
    st = CaseStore(conf.workspaces["acme"].cases_dir)
    st.create_case("CASE-100", "older case", "acme", actor="human", elements={"machine": ["unit-1"], "component": ["widget-driver"]})
    st.create_case("CASE-123", "起動時に driver が初期化されない", "acme", actor="human", elements={"machine": ["unit-2"]})
    (st.case_dir("CASE-123") / "worklog.md").write_text("# t\n## Notes\nUART 460800 で送信量が超過する\n", encoding="utf-8")
    return st


@pytest.fixture
def paths(tmp_path: Path):
    prompt = tmp_path / "prompt.md"; prompt.write_text("PROMPT TEXT", encoding="utf-8")
    return prompt, extract.SCHEMA_PATH, tmp_path / "cases" / "CASE-123", tmp_path / "out.json"


# ---------- build_command ----------

def test_build_command_claude(paths):
    prompt, schema, case_dir, out = paths
    cmd = adapters.get("claude").build_command(prompt, schema, case_dir, 600)
    assert cmd == ["claude", "-p", "PROMPT TEXT", "--output-format", "json", "--allowedTools", "Read,Grep,Glob", "--strict-mcp-config"]
    assert "--dangerously-skip-permissions" not in cmd and "--mcp-config" not in cmd


def test_build_command_codex(paths):
    prompt, schema, case_dir, out = paths
    cmd = adapters.get("codex").build_command(prompt, schema, case_dir, 600, out)
    assert cmd[:3] == ["codex", "exec", "PROMPT TEXT"]
    assert cmd[3:5] == ["-C", str(case_dir)] and "--json" in cmd
    assert cmd[cmd.index("--output-schema") + 1] == str(schema) and cmd[cmd.index("-o") + 1] == str(out)
    assert cmd[cmd.index("-s") + 1] == "read-only" and "--ephemeral" in cmd and "--skip-git-repo-check" in cmd
    with pytest.raises(ValueError):
        adapters.get("codex").build_command(prompt, schema, case_dir, 600)  # -o 無しは組み立てない


def test_build_command_opencode_and_antigravity(paths):
    prompt, schema, case_dir, out = paths
    assert adapters.get("opencode").build_command(prompt, schema, case_dir, 600) == \
        ["opencode", "run", "PROMPT TEXT", "--format", "json", "--agent", "kairn-extract", "--pure"]
    assert adapters.get("antigravity").build_command(prompt, schema, case_dir, 600) == \
        ["agy", "--print", "PROMPT TEXT", "--sandbox", "--print-timeout", "10m"]
    assert adapters.get("antigravity").build_command(prompt, schema, case_dir, 90)[-1] == "90s"
    with pytest.raises(KeyError):
        adapters.get("gemini")


def test_env_is_minimal():
    base = {"HOME": "/home/u", "PATH": "/bin", "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "o", "KAIRN_CONFIG": "x", "SECRET_TOKEN": "s"}
    env = adapters.get("claude").env(base)
    assert env == {"HOME": "/home/u", "PATH": "/bin", "ANTHROPIC_API_KEY": "k"}
    assert "OPENAI_API_KEY" in adapters.get("codex").env(base) and "ANTHROPIC_API_KEY" not in adapters.get("codex").env(base)


# ---------- extract_json ----------

def test_find_json_object_variants():
    card = good_card()
    raw = json.dumps(card, ensure_ascii=False)
    assert adapters.find_json_object(raw) == card
    assert adapters.find_json_object(f"下書きです。\n```json\n{raw}\n```\n以上。") == card
    assert adapters.find_json_object(f"Here is the card: {raw} — done. {{\"small\": 1}}") == card
    assert adapters.find_json_object("no json here {broken") is None
    assert adapters.find_json_object("") is None
    assert adapters.find_json_object("[1, 2]") is None


def test_extract_json_claude_envelope():
    card = good_card()
    env = {"type": "result", "subtype": "success", "result": "```json\n" + json.dumps(card, ensure_ascii=False) + "\n```", "is_error": False}
    ad = adapters.get("claude")
    assert ad.extract_json(json.dumps(env, ensure_ascii=False)) == card
    # stream 風（複数行）でも type=result の行を採る
    lines = json.dumps({"type": "system"}) + "\n" + json.dumps(env, ensure_ascii=False) + "\n"
    assert ad.extract_json(lines) == card
    assert ad.extract_json("not json") is None


def test_extract_json_codex_prefers_out_file_then_events():
    card = good_card()
    ad = adapters.get("codex")
    events = "\n".join(json.dumps(e, ensure_ascii=False) for e in [
        {"type": "thread.started", "thread_id": "x"},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "{\"not\": \"this\"}"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "説明: " + json.dumps(card, ensure_ascii=False)}},
        {"type": "turn.completed", "usage": {}}])
    assert ad.extract_json(events, out_file=json.dumps(good_card(title="from file"))) == good_card(title="from file")
    assert ad.extract_json(events) == card
    assert ad.extract_json(events, out_file="garbage") == card
    assert ad.extract_json("plain text without json") is None


def test_extract_json_opencode_events():
    card = good_card()
    ad = adapters.get("opencode")
    events = "\n".join(json.dumps(e, ensure_ascii=False) for e in [
        {"type": "step_start", "part": {"type": "step-start"}},
        {"type": "tool", "part": {"type": "tool", "tool": "read", "state": {"output": "{\"x\": 1}"}}},
        {"type": "text", "part": {"type": "text", "text": json.dumps(card, ensure_ascii=False)}},
        {"type": "step_finish", "part": {"type": "step-finish"}}])
    assert ad.extract_json(events) == card
    assert ad.extract_json("") is None
    # antigravity はテキスト本文から取り出す
    assert adapters.get("antigravity").extract_json("結果:\n" + json.dumps(card, ensure_ascii=False) + "\n") == card


# ---------- schema ----------

def test_schema_validation():
    assert extract.validate_card(good_card()) is None
    assert "summary" in extract.validate_card({k: v for k, v in good_card().items() if k != "summary"})
    assert "extra" in extract.validate_card(good_card(extra=1))
    assert "elements" in extract.validate_card(good_card(elements={"machine": ["unit-2"]}))          # 6 キー必須
    assert "site" in extract.validate_card(good_card(elements={**good_card()["elements"], "site": "x"}))  # 配列でない
    assert "confidence" in extract.validate_card(good_card(confidence="high"))
    assert "confidence" in extract.validate_card(good_card(confidence=1.5))
    assert "causal" in extract.validate_card(good_card(causal=[{"symptom": "s"}]))
    assert "related" in extract.validate_card(good_card(related=[1]))
    assert extract.validate_card("str") is not None


def test_render_prompt_has_siblings_and_vocabulary(conf):
    _seed(conf)
    text = extract.render_prompt(conf.workspaces["acme"].cases_dir, "CASE-123")
    assert "`CASE-100`" in text and "widget-driver" in text and "unit-1" in text
    assert "この案件の ID: `CASE-123`" in text and '"additionalProperties": false' in text
    assert "- 親ディレクトリにある他の案件（`related` はここからだけ選ぶ）: `CASE-100`" in text


# ---------- extract_card（subprocess をモック） ----------

class FakeRun:
    """adapters.process.run の代わり。呼ばれた cmd / cwd / env を記録し、設定した結果を返す。"""

    def __init__(self, stdout="", returncode=0, timeout=False, out_file=None, missing=False):
        self.stdout, self.returncode, self.timeout, self.out_file, self.missing = stdout, returncode, timeout, out_file, missing
        self.calls = []

    def __call__(self, cmd, **kw):
        cwd = Path(kw["cwd"]) if kw.get("cwd") else None
        # cwd（一時ディレクトリの写し）は実行後に消えるので、その時点の親ディレクトリ配下の一覧を取っておく
        tree = sorted(p.relative_to(cwd.parent).as_posix() for p in cwd.parent.rglob("*")) if cwd and cwd.exists() else None
        self.calls.append({"cmd": cmd, **kw, "tree": tree})
        if self.missing:
            raise FileNotFoundError(cmd[0])
        if self.timeout:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"), output="partial", stderr="")
        if self.out_file is not None:
            Path(cmd[cmd.index("-o") + 1]).write_text(self.out_file, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "some stderr" if self.returncode else "")


def _claude_stdout(card: dict) -> str:
    return json.dumps({"type": "result", "result": json.dumps(card, ensure_ascii=False)}, ensure_ascii=False)


def test_extract_card_success_records_event_and_writes_nothing(conf, monkeypatch):
    st = _seed(conf); ws = conf.workspaces["acme"]
    fake = FakeRun(stdout=_claude_stdout(good_card()))
    monkeypatch.setattr(adapters.process, "run", fake)
    monkeypatch.setenv("SECRET_TOKEN", "s")
    before = st.load_case("CASE-123")
    r = extract.extract_card(conf, ws, "CASE-123", timeout=42)
    assert r["ok"] is True and r["card"] == {**good_card(), "related_unknown": []} and r["agent"] == "claude" and r["error"] is None
    assert r["elapsed_sec"] >= 0 and "result" in r["raw_excerpt"]
    call = fake.calls[0]
    assert Path(call["cwd"]).name == "CASE-123" and call["cwd"] != str(ws.cases_dir / "CASE-123")  # 写し（M-3）
    assert call["timeout"] == 42 and call["stdin"] is subprocess.DEVNULL
    assert "SECRET_TOKEN" not in call["env"] and "PATH" in call["env"]
    assert call["cmd"][0] == "claude" and "CASE-100" in call["cmd"][2] and "PROMPT" not in call["cmd"][2]
    after = st.load_case("CASE-123")
    assert {k: v for k, v in after.items()} == before  # case.json は書かない
    ev = st.events("CASE-123")[-1]
    assert ev["actor"] == "kairn" and ev["agent"] == "extract:claude" and ev["action"] == "extract"
    assert ev["note"].startswith("ok") and "0.8" in ev["note"] and ev["exit_code"] == 0 and ev["timeout_sec"] == 42 and "elapsed_sec" in ev


def test_extract_card_failures(conf, monkeypatch):
    st = _seed(conf); ws = conf.workspaces["acme"]
    cases = [
        (FakeRun(timeout=True), "timeout after 5s", None),
        (FakeRun(stdout="boom", returncode=2), "exit code 2", 2),
        (FakeRun(stdout=_claude_stdout(good_card(confidence="high"))), "schema: confidence", 0),
        (FakeRun(stdout=_claude_stdout({"title": "only"})), "schema: (root)", 0),
        (FakeRun(stdout=json.dumps({"type": "result", "result": "nothing here"})), "no JSON object", 0),
        (FakeRun(missing=True), "command not found: claude", None),
    ]
    for fake, err, code in cases:
        monkeypatch.setattr(adapters.process, "run", fake)
        r = extract.extract_card(conf, ws, "CASE-123", timeout=5)
        assert r["ok"] is False and r["card"] is None and err in r["error"], (err, r)
        ev = st.events("CASE-123")[-1]
        assert ev["action"] == "extract" and ev["agent"] == "extract:claude" and err in ev["note"] and ev.get("exit_code") == code, (err, ev)
    # 未知の agent 設定
    r = extract.extract_card(conf, ws, "CASE-123", agent="gemini")
    assert r["ok"] is False and "unknown extract agent" in r["error"] and st.events("CASE-123")[-1]["agent"] == "extract:gemini"
    # 未知の案件は例外（server が ToolError にする）
    with pytest.raises(KeyError):
        extract.extract_card(conf, ws, "CASE-404")


def test_extract_card_codex_reads_out_file(conf, monkeypatch):
    st = _seed(conf); ws = conf.workspaces["acme"]
    fake = FakeRun(stdout=json.dumps({"type": "turn.completed"}), out_file=json.dumps(good_card(title="from -o"), ensure_ascii=False))
    monkeypatch.setattr(adapters.process, "run", fake)
    r = extract.extract_card(conf, ws, "CASE-123", agent="codex")
    assert r["ok"] and r["card"]["title"] == "from -o" and r["agent"] == "codex"
    cmd = fake.calls[0]["cmd"]
    assert cmd[:2] == ["codex", "exec"] and cmd[cmd.index("-C") + 1] == fake.calls[0]["cwd"] and Path(fake.calls[0]["cwd"]).name == "CASE-123"
    assert not (ws.cases_dir / "CASE-123" / "out.json").exists()  # -o は案件ディレクトリの外
    assert not any(p.name.startswith("prompt") for p in (ws.cases_dir / "CASE-123").iterdir())
    assert st.events("CASE-123")[-1]["agent"] == "extract:codex"


# ---------- apply_card ----------

def test_apply_card_writes_case_and_decision(conf):
    st = _seed(conf)
    c = extract.apply_card(st, "CASE-123", good_card())
    saved = st.load_case("CASE-123")
    assert saved == c and saved["title"] == good_card()["title"] and saved["summary"].startswith("起動直後")
    assert saved["elements"] == good_card()["elements"] and saved["related"] == ["CASE-100"] and saved["causal"] == good_card()["causal"]
    assert saved["status"] == "open" and saved["workspace"] == "acme"  # 他のキーは保持
    ev = st.events("CASE-123")[-1]
    assert ev == {**ev, "actor": "human", "action": "decision", "note": "applied extract draft", "confidence": 0.8}
    with pytest.raises(ValueError):
        extract.apply_card(st, "CASE-123", good_card(extra=1))


def test_extract_card_uses_config_timeout(conf, monkeypatch):
    """timeout を省略すると設定 extract.timeout（conf.extract_timeout）が subprocess と event に渡る。"""
    _seed(conf); ws = conf.workspaces["acme"]
    conf.extract_timeout = 77
    fake = FakeRun(stdout=_claude_stdout(good_card()))
    monkeypatch.setattr(adapters.process, "run", fake)
    r = extract.extract_card(conf, ws, "CASE-123")
    assert r["ok"] and fake.calls[-1]["timeout"] == 77
    assert CaseStore(ws.cases_dir).events("CASE-123")[-1]["timeout_sec"] == 77


# ---------- M-3: 子エージェントの cwd は一時ディレクトリの写し（自案件＋兄弟の case.json だけ） ----------

def test_extract_cwd_is_staged_copy_with_siblings_case_json_only(conf, monkeypatch, tmp_path, requires_symlinks):
    st = _seed(conf); ws = conf.workspaces["acme"]
    case = st.case_dir("CASE-123")
    (case / "sub").mkdir(); (case / "sub" / "0901_notes.md").write_text("x", encoding="utf-8")
    (case / "target").mkdir(); (case / "target" / "a.o").write_bytes(b"o")            # rules.exclude
    (case / "run.bag").write_bytes(b"b" * 10)                                          # 生データ（拡張子）
    (case / "link.md").symlink_to(case / "worklog.md")                                 # シンボリックリンク
    (ws.cases_dir / "CASE-100" / "secret-notes.md").write_text("sibling worklog", encoding="utf-8")
    (ws.cases_dir / "0815_legacy").mkdir(); (ws.cases_dir / "0815_legacy" / "worklog.md").write_text("legacy", encoding="utf-8")
    (ws.data_dir / "index").mkdir(parents=True); (ws.data_dir / "index" / "drive-index.txt").write_text("i", encoding="utf-8")
    other = tmp_path / "data" / "other-ws" / "cases" / "CASE-777"; other.mkdir(parents=True); (other / "worklog.md").write_text("other ws", encoding="utf-8")
    fake = FakeRun(stdout=_claude_stdout(good_card()))
    monkeypatch.setattr(adapters.process, "run", fake)
    r = extract.extract_card(conf, ws, "CASE-123")
    assert r["ok"]
    call = fake.calls[0]
    cwd = Path(call["cwd"])
    assert cwd.name == "CASE-123" and cwd.parent.name == "cases" and not str(cwd).startswith(str(ws.data_dir))
    assert call["tree"] == ["CASE-100", "CASE-100/case.json", "CASE-123", "CASE-123/case.json", "CASE-123/events.jsonl",
                            "CASE-123/sub", "CASE-123/sub/0901_notes.md", "CASE-123/worklog.md"]
    # 元の案件ディレクトリは変わらず、写しは実行後に消える
    assert (case / "run.bag").exists() and (case / "link.md").is_symlink() and not cwd.exists()
    assert "CASE-100" in call["cmd"][2]  # プロンプトの文脈（兄弟 ID）は従来どおり


# ---------- L-3: related に実在しない案件 ID → related_unknown ----------

def test_extract_card_splits_unknown_related(conf, monkeypatch):
    st = _seed(conf); ws = conf.workspaces["acme"]
    monkeypatch.setattr(adapters.process, "run", FakeRun(stdout=_claude_stdout(good_card(related=["CASE-100", "CASE-999", "CASE-100"]))))
    r = extract.extract_card(conf, ws, "CASE-123")
    assert r["ok"] and r["card"]["related"] == ["CASE-100", "CASE-100"] and r["card"]["related_unknown"] == ["CASE-999"]
    # apply では related_unknown を捨て、related に含めない
    c = extract.apply_card(st, "CASE-123", r["card"])
    assert c["related"] == ["CASE-100", "CASE-100"] and "related_unknown" not in c
    assert "related_unknown" not in st.load_case("CASE-123")
    with pytest.raises(ValueError):
        extract.apply_card(st, "CASE-123", "not an object")
