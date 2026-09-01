"""抽出（docs/roadmap.md 7、docs/extract-agents.md）: 文脈隔離した子エージェントで case.json の下書きを作る。

- extract_card(conf, ws, case): 設定 extract.agent のアダプタで子プロセスを案件ディレクトリ（cwd）で実行し、
  出力を schema.json で検証して {ok, card, agent, elapsed_sec, error, raw_excerpt} を返す。**ファイルには書かない**。
  結果（成功／失敗理由・所要時間・終了コード）は events に {actor: kairn, agent: "extract:<name>", action: extract} で記録する。
- apply_card(store, case, card): 人の操作（UI）でのみ呼ぶ。title / summary / elements / related / causal を case.json に書き、
  {actor: human, action: decision, note: "applied extract draft"} を記録する。
プロンプト（prompt.md）にはワークスペース内の他案件 ID と既存 elements の語彙を添える（他ワークスペースは見ない）。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import jsonschema

from .. import config as cfg
from ..store import CaseStore
from . import adapters
from .adapters import DEFAULT_TIMEOUT_SEC

PROMPT_PATH = Path(__file__).with_name("prompt.md")
SCHEMA_PATH = Path(__file__).with_name("schema.json")
ELEMENT_KEYS = ("machine", "component", "symptom", "ticket", "site", "external")
APPLY_KEYS = ("title", "summary", "elements", "related", "causal")
MAX_SIBLINGS = 300      # プロンプトに載せる他案件 ID の上限
MAX_VOCAB_PER_KEY = 60  # 同、elements の語彙（キーごと）
EXCERPT_CHARS = 800


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_card(card: object) -> str | None:
    """schema.json で検証。通れば None、通らなければ理由（1 行）。"""
    try:
        jsonschema.Draft202012Validator(load_schema()).validate(card)
    except jsonschema.ValidationError as e:
        where = "/".join(str(p) for p in e.absolute_path) or "(root)"
        return f"schema: {where}: {e.message}"
    return None


def workspace_context(cases_dir: Path, case: str) -> tuple[list[str], dict[str, list[str]]]:
    """同じワークスペースの他案件 ID と、既存 case.json の elements の語彙（キーごと、出現順・重複なし）。"""
    st = CaseStore(cases_dir)
    siblings = [c for c in st.list_case_ids() if c != case]
    vocab: dict[str, list[str]] = {k: [] for k in ELEMENT_KEYS}
    for cid in siblings:
        try:
            el = st.load_case(cid).get("elements") or {}
        except (ValueError, OSError):
            continue
        for k, vs in el.items():
            if k in vocab and isinstance(vs, list):
                for v in vs:
                    if isinstance(v, str) and v not in vocab[k] and len(vocab[k]) < MAX_VOCAB_PER_KEY:
                        vocab[k].append(v)
    return siblings[:MAX_SIBLINGS], vocab


def render_prompt(cases_dir: Path, case: str) -> str:
    """prompt.md ＋ 文脈（案件 ID・他案件 ID・既存語彙）＋ 出力スキーマ。"""
    siblings, vocab = workspace_context(cases_dir, case)
    lines = [PROMPT_PATH.read_text(encoding="utf-8").rstrip(), "", "## 文脈", "", f"- この案件の ID: `{case}`（`related` に含めない）"]
    lines.append("- 親ディレクトリにある他の案件（`related` はここからだけ選ぶ）: " + (", ".join(f"`{s}`" for s in siblings) if siblings else "(なし)"))
    vocab_lines = [f"  - {k}: " + ", ".join(vocab[k]) for k in ELEMENT_KEYS if vocab[k]]
    lines.append("- 既存の案件で使われている要素名（同じ対象ならこの表記に寄せる）:" + ("" if vocab_lines else " (なし)"))
    lines.extend(vocab_lines)
    lines += ["", "## 出力スキーマ", "", "```json", SCHEMA_PATH.read_text(encoding="utf-8").rstrip(), "```", ""]
    return "\n".join(lines)


def _excerpt(r: adapters.RunResult) -> str:
    out = r.stdout[-EXCERPT_CHARS:]
    err = r.stderr[-(EXCERPT_CHARS // 2):]
    return out + (f"\n[stderr] {err}" if err.strip() else "")


def extract_card(conf: cfg.Config, ws: cfg.Workspace, case: str, agent: str | None = None,
                 timeout: int = DEFAULT_TIMEOUT_SEC) -> dict:
    """案件カードの下書きを子エージェントで作る（読み取りのみ。case.json には書かない。結果は events に記録）。"""
    st = CaseStore(ws.cases_dir)
    st.load_case(case)  # 未知の案件は CaseNotFound
    case_dir = st.case_dir(case)
    name = agent or conf.extract_agent
    result = {"ok": False, "card": None, "agent": name, "elapsed_sec": 0.0, "error": None, "raw_excerpt": ""}
    event = {"actor": "kairn", "agent": f"extract:{name}", "action": "extract", "timeout_sec": int(timeout)}
    try:
        ad = adapters.get(name)
    except KeyError as e:
        result["error"] = str(e.args[0])
        st.append_event(case, {**event, "note": result["error"], "elapsed_sec": 0.0})
        return result
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="kairn-extract-") as tmp:  # プロンプト・-o は案件ディレクトリの外
        prompt_path = Path(tmp) / "prompt.md"
        prompt_path.write_text(render_prompt(ws.cases_dir, case), encoding="utf-8")
        out_path = Path(tmp) / "out.json"
        try:
            r = ad.run(prompt_path, SCHEMA_PATH, case_dir, int(timeout), ad.env(dict(os.environ)), out_path)
        except FileNotFoundError:
            r = None
            result["error"] = f"command not found: {ad.build_command(prompt_path, SCHEMA_PATH, case_dir, int(timeout), out_path)[0]}"
    result["elapsed_sec"] = round(time.monotonic() - t0, 2)
    if r is not None:
        result["raw_excerpt"] = _excerpt(r)
        event["exit_code"] = r.returncode
        if r.timed_out:
            result["error"] = f"timeout after {int(timeout)}s"
        elif r.returncode != 0:
            result["error"] = f"exit code {r.returncode}"
        else:
            card = ad.extract_json(r.stdout, r.out_file)
            if card is None:
                result["error"] = "no JSON object in output"
            else:
                err = validate_card(card)
                if err:
                    result["error"] = err
                else:
                    result.update(ok=True, card=card)
    note = f"ok (confidence {result['card'].get('confidence')})" if result["ok"] else result["error"]
    st.append_event(case, {**event, "note": note, "elapsed_sec": result["elapsed_sec"]})
    return result


def apply_card(st: CaseStore, case: str, card: dict) -> dict:
    """人が UI で確定した下書きを case.json に適用する（title / summary / elements / related / causal）。
    スキーマ検証に通らなければ ValueError。decision event を記録し、更新後の case を返す。"""
    err = validate_card(card)
    if err:
        raise ValueError(err)
    c = st.load_case(case)
    for k in APPLY_KEYS:
        c[k] = card[k]
    st.save_case(c)
    st.append_event(case, {"actor": "human", "action": "decision", "note": "applied extract draft",
                           "confidence": card.get("confidence")})
    return c
