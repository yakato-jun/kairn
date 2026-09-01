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
