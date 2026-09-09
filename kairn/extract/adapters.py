"""抽出エージェントのアダプタ（docs/extract-agents.md の表そのまま）。

アダプタが担うのは 3 つだけ:
  (a) build_command(prompt_path, schema_path, case_dir, timeout, out_path=None) -> list[str]   コマンドの組み立て（純関数）
  (b) run(...)                                                                                process.run(cwd=case_dir, timeout=…)
  (c) extract_json(stdout, out_file=None) -> dict | None                                       出力から JSON を取り出す
プロンプトとスキーマは共通（kairn/extract/prompt.md, schema.json）。スキーマ検証は kairn/extract/__init__.py が行う。
docs/extract-agents.md に無いオプションは使わない。
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .. import process

DEFAULT_TIMEOUT_SEC = 600  # 既定値。実際の値は設定 extract.timeout（config.DEFAULT_EXTRACT_TIMEOUT_SEC と同じ既定）を extract_card が渡す
# 子プロセスへ渡す環境変数（最小限）。各 CLI が必要とするものはアダプタの env_keys に足す
COMMON_ENV_KEYS = ("HOME", "PATH", "LANG", "LC_ALL", "TERM", "TMPDIR", "SHELL", "USER",
                   "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
                   "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP")
_FENCE_RE = re.compile(r"```(?:json)?[ \t]*\r?\n(.*?)```", re.S)


@dataclass
class RunResult:
    cmd: list[str]
    returncode: int | None          # timeout のとき None
    stdout: str
    stderr: str
    timed_out: bool = False
    out_file: str | None = None     # codex の -o ファイルの内容（あれば）


def _text(x: str | bytes | None) -> str:
    if x is None:
        return ""
    return x.decode("utf-8", "replace") if isinstance(x, bytes) else x


def find_json_object(text: str) -> dict | None:
    """本文から JSON オブジェクトを 1 つ取り出す。順に (1) 全体が JSON、(2) ```json フェンスの中、
    (3) 前後に説明文がある場合は `{` から raw_decode して一番長く読めたオブジェクト。無ければ None。"""
    if not text:
        return None
    s = text.strip()
    try:
        v = json.loads(s)
        if isinstance(v, dict):
            return v
    except ValueError:
        pass
    for m in _FENCE_RE.finditer(s):
        try:
            v = json.loads(m.group(1).strip())
        except ValueError:
            continue
        if isinstance(v, dict):
            return v
    dec = json.JSONDecoder()
    best: tuple[dict, int] | None = None
    i = 0
    while True:
        i = s.find("{", i)
        if i < 0:
            break
        try:
            v, end = dec.raw_decode(s, i)
        except ValueError:
            i += 1
            continue
        if isinstance(v, dict) and (best is None or end - i > best[1]):
            best = (v, end - i)
        i = end
    return best[0] if best else None


def json_lines(text: str) -> list[dict]:
    """JSON イベント列（1 行 1 オブジェクト）を dict のリストに。JSON でない行は無視する。"""
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if isinstance(v, dict):
            out.append(v)
    return out


def duration(timeout: int) -> str:
    """秒 → CLI の期間表記（600 → "10m"、90 → "90s"）。"""
    timeout = int(timeout)
    return f"{timeout // 60}m" if timeout > 0 and timeout % 60 == 0 else f"{timeout}s"


class Adapter:
    name = ""
    env_keys: tuple[str, ...] = ()

    def build_command(self, prompt_path: Path, schema_path: Path, case_dir: Path, timeout: int, out_path: Path | None = None) -> list[str]:
        raise ValueError(f"adapter {self.name!r} has no command")  # 具象クラスで定義する

    def extract_json(self, stdout: str, out_file: str | None = None) -> dict | None:
        return find_json_object(stdout)

    def env(self, base: dict[str, str]) -> dict[str, str]:
        return {k: base[k] for k in (*COMMON_ENV_KEYS, *self.env_keys) if k in base}

    def run(self, prompt_path: Path, schema_path: Path, case_dir: Path, timeout: int, env: dict[str, str],
            out_path: Path | None = None) -> RunResult:
        """子プロセスを案件ディレクトリで実行する。stdin は閉じる。timeout は RunResult.timed_out で返す
        （CLI 不在の FileNotFoundError は呼び出し元へ）。"""
        cmd = self.build_command(prompt_path, schema_path, case_dir, timeout, out_path)
        try:
            p = process.run(cmd, cwd=str(case_dir), env=env, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            return RunResult(cmd, None, _text(e.stdout), _text(e.stderr), timed_out=True)
        out_file = None
        if out_path is not None and Path(out_path).is_file():
            out_file = Path(out_path).read_text(encoding="utf-8", errors="replace")
        return RunResult(cmd, p.returncode, _text(p.stdout), _text(p.stderr), out_file=out_file)


def _prompt(prompt_path: Path) -> str:
    return Path(prompt_path).read_text(encoding="utf-8")


class ClaudeAdapter(Adapter):
    """claude -p "<prompt>" --output-format json --allowedTools Read,Grep,Glob --strict-mcp-config（MCP を読まない＝再帰防止）"""
    name = "claude"
    env_keys = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR")

    def build_command(self, prompt_path, schema_path, case_dir, timeout, out_path=None):
        return ["claude", "-p", _prompt(prompt_path), "--output-format", "json",
                "--allowedTools", "Read,Grep,Glob", "--strict-mcp-config"]

    def extract_json(self, stdout, out_file=None):
        # --output-format json: {"type":"result","result":"<最終テキスト>",…}。複数行なら type=result の行
        envelope = None
        try:
            v = json.loads(stdout.strip())
            envelope = v if isinstance(v, dict) else None
        except ValueError:
            for ev in json_lines(stdout):
                if ev.get("type") == "result":
                    envelope = ev
        if envelope is not None and isinstance(envelope.get("result"), str):
            return find_json_object(envelope["result"])
        return find_json_object(stdout)


class CodexAdapter(Adapter):
    """codex exec "<prompt>" -C <case_dir> --json --output-schema schema.json -o out.json -s read-only --ephemeral --skip-git-repo-check"""
    name = "codex"
    env_keys = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_HOME")

    def build_command(self, prompt_path, schema_path, case_dir, timeout, out_path=None):
        if out_path is None:
            raise ValueError("codex adapter needs out_path (-o)")
        return ["codex", "exec", _prompt(prompt_path), "-C", str(case_dir), "--json",
                "--output-schema", str(schema_path), "-o", str(out_path),
                "-s", "read-only", "--ephemeral", "--skip-git-repo-check"]

    def extract_json(self, stdout, out_file=None):
        # -o のファイル（スキーマ強制の最終メッセージ）を優先。無ければ --json のイベント列から最後の agent_message
        if out_file:
            v = find_json_object(out_file)
            if v is not None:
                return v
        last = None
        for ev in json_lines(stdout):
            item = ev.get("item")
            if ev.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message" \
                    and isinstance(item.get("text"), str):
                last = item["text"]
        if last is not None:
            return find_json_object(last)
        return find_json_object(stdout)


class OpencodeAdapter(Adapter):
    """opencode run "<message>" --format json --agent kairn-extract --pure（読み取り専用は agent 定義で担保、--pure でプラグイン無効）"""
    name = "opencode"
    env_keys = ("OPENCODE_CONFIG", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")

    def build_command(self, prompt_path, schema_path, case_dir, timeout, out_path=None):
        return ["opencode", "run", _prompt(prompt_path), "--format", "json", "--agent", "kairn-extract", "--pure"]

    def extract_json(self, stdout, out_file=None):
        # JSON イベント列。text 部品（part.type == "text" の part.text、または text / item.text）の最後を採る
        last = None
        for ev in json_lines(stdout):
            part = ev.get("part")
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                last = part["text"]
            elif isinstance(ev.get("text"), str):
                last = ev["text"]
            elif isinstance(ev.get("item"), dict) and isinstance(ev["item"].get("text"), str):
                last = ev["item"]["text"]
        if last is not None:
            v = find_json_object(last)
            if v is not None:
                return v
        return find_json_object(stdout)


class AntigravityAdapter(Adapter):
    """agy --print "<prompt>" --sandbox --print-timeout 10m（--dangerously-skip-permissions は使わない）"""
    name = "antigravity"
    env_keys = ()

    def build_command(self, prompt_path, schema_path, case_dir, timeout, out_path=None):
        return ["agy", "--print", _prompt(prompt_path), "--sandbox", "--print-timeout", duration(timeout)]


ADAPTERS: dict[str, Adapter] = {a.name: a for a in (ClaudeAdapter(), CodexAdapter(), OpencodeAdapter(), AntigravityAdapter())}


def get(name: str) -> Adapter:
    if name not in ADAPTERS:
        raise KeyError(f"unknown extract agent {name!r} (known: {', '.join(ADAPTERS)})")
    return ADAPTERS[name]
