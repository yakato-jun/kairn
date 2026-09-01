# 抽出エージェントのアダプタ

抽出（case.json の下書き: elements / related / 要約 / 症状→部品→原因）は、本セッションの文脈を汚さないよう
**子プロセスのエージェント**で行う。どのエージェントを使うかは環境ごとの設定 `extract.agent` で選ぶ。
アダプタが担うのは「コマンド組み立て・隔離オプション・出力からの JSON 取り出し」だけで、
プロンプトと出力スキーマは共通（`kairn/extract/prompt.md`, `kairn/extract/schema.json`）。

| agent | 起動 | 出力 | 読み取り専用・隔離 |
|---|---|---|---|
| claude | `claude -p "<prompt>" --output-format json` | JSON | `--allowedTools Read,Grep,Glob --strict-mcp-config`（MCP を読まない＝再帰防止） |
| codex | `codex exec "<prompt>" -C <case_dir> --json --output-schema schema.json -o out.json` | JSON（スキーマ強制） | `-s read-only --ephemeral --skip-git-repo-check` |
| opencode | `opencode run "<message>" --format json --agent kairn-extract --pure` | JSON イベント列（最終メッセージを抽出） | 読み取り専用は agent 定義（tools 制限）で担保。`--pure` でプラグイン無効 |
| antigravity | `agy --print "<prompt>" --sandbox --print-timeout 10m` | テキスト（本文中の JSON を抽出） | `--sandbox`。`--dangerously-skip-permissions` は使わない |

共通規則:
- 作業ディレクトリは対象案件のディレクトリのみ。ワークスペース外は渡さない。
- 出力はスキーマ検証に通ったものだけ返す。通らなければ「下書き失敗」（採用しない）。
- 書き込みはしない。確定は人（UI で差分を確認）。
- タイムアウト・終了コード・所要時間・使用エージェントを events に記録する。
- 将来、差し戻し対応の自動着手（書き込みを伴う）に流用する場合は別途設計する。

## 実装（`kairn/extract/`、2026-09-02）

- `prompt.md`: 子エージェントへの指示（cwd の `worklog.md` / `case.json` / その他 md を読み、JSON だけを返す）。
  `extract.render_prompt()` が末尾に **文脈**（この案件の ID、cwd の親にある他の案件 ID、既存 case.json の `elements` の語彙）と
  **出力スキーマ**（schema.json 全文）を付ける。プロンプトは一時ディレクトリのファイルに書き、コマンド引数に展開して渡す
  （案件ディレクトリには何も置かない）。
- `schema.json`: 出力の JSON Schema（draft 2020-12、`additionalProperties: false`）。項目は `title`, `summary`, `elements`
  （`machine` / `component` / `symptom` / `ticket` / `site` / `external` の 6 配列。全部必須、空配列可）, `related`（案件 ID の配列）,
  `causal`（`[{symptom, component, cause, evidence}]`）, `confidence`（0〜1）。検証は `jsonschema`（明示依存）。
  codex には `--output-schema` でそのまま渡す。
- `adapters.py`: 表の 4 アダプタ。各アダプタは (a) `build_command(prompt_path, schema_path, case_dir, timeout, out_path=None)`（純関数。
  codex だけ `out_path` 必須＝`-o`）、(b) `run(...)`（`subprocess.run(cwd=case_dir, env=最小限, timeout=…, stdin=DEVNULL)`）、
  (c) `extract_json(stdout, out_file=None)`。JSON の取り出し順: claude は `--output-format json` の `result` 文字列 → 本文から、
  codex は `-o` ファイル → `--json` イベント列の最後の `item.completed` / `agent_message` → 本文、opencode はイベント列の最後の text 部品
  （`part.type == "text"`）→ 本文、antigravity は本文から。「本文から」＝ 全体が JSON → ```json フェンス → 前後に説明文があれば `{` から
  raw_decode して一番長く読めたオブジェクト（`find_json_object`）。
- 環境変数: `HOME` / `PATH` / `LANG` / `LC_ALL` / `TERM` / `TMPDIR` / `SHELL` / `USER` / `XDG_*` ＋ アダプタごとの API キー等
  （claude: `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` / `CLAUDE_CONFIG_DIR`、codex: `OPENAI_API_KEY` /
  `OPENAI_BASE_URL` / `CODEX_HOME`、opencode: `OPENCODE_CONFIG` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`、antigravity: 追加なし）。
  それ以外（`KAIRN_*` を含む）は渡さない。
- タイムアウトは設定 `extract.timeout`（秒、既定 600。`kairn setup --extract-timeout <sec>` が書く。antigravity の `--print-timeout` には
  `10m` の形で渡す）。`extract_card(..., timeout=)` を明示すればそちらが優先（テスト用）。
- 入口: MCP `extract_card(case, workspace?)`、CLI `kairn extract <case> [--ws] [--agent] [--json]`（失敗は exit 1）、
  UI 案件ページの「下書きを取得」（結果画面で現在値との差分を見て「この下書きを case.json に適用」）。適用は UI からだけ
  （`extract.apply_card`: title / summary / elements / related / causal を置き換え、`{actor: human, action: decision, note: "applied extract draft"}`）。
- 結果は毎回 events に `{actor: kairn, agent: "extract:<name>", action: extract, note: "ok (confidence …)" | 失敗理由, elapsed_sec, exit_code, timeout_sec}`。
- opencode の agent `kairn-extract`（tools を read / grep / glob に制限した定義）の配置は段階 6（skill・登録手順）で扱う。未配置なら opencode が失敗し `ok=false` になる。
