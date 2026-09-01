# 実装順（MVP → 拡張）

段階 1（2026-09-02 完了）: 1〜4。段階 2 以降（5〜8）は未着手。コードに段階外の機能は含めない
（呼ばれ得る箇所があれば「not implemented in this stage: see docs/roadmap.md」を返す）。

1. [完了] `config`: 環境ローカル設定（~/.config/kairn/config.yaml）の読み込み・検証（remote 固定、未登録パスは拒否）、`kairn attach`（<repo>/tmp → cases へのリンク）
2. [完了] `store` / `index`: case.json / plan / events の読み書き、superseded 自動化、evidence 検証、SQLite FTS5（trigram）索引の差分再生成
3. [完了] `server`: MCP（mcp 2.x、streamable HTTP /mcp。open_case / list_cases / plan / update_task / log_event / search / find_cases / checkin / drive_index）
4. [完了] `ui`: 一覧・かんばん・時系列・版履歴・差し戻し・コメント・タスク追加・状態変更・鮮度・elements 絞り込み（/ui、MCP と同一プロセス）
5. `sync`: rclone による checkout / checkin / 日次同期 / 生データ move / drive-index（既存 backup-tmp.sh の設計を取り込む）。
   段階 1 では checkout / checkin / drive-index の最小実装（`kairn/sync.py`）だけがあり、open_case / checkin ツールから呼ばれる。生データ退避・日次同期は未着手
6. skill: skills/kairn/SKILL.md を ~/.agents/skills に配置、Claude 側リンク、/worklog の置き換え
7. `extract`（MCP ツール `extract_card` を含む）: 文脈隔離した子エージェントで case.json の下書きを作る。エージェントはアダプタ方式で
   設定（`extract.agent`）から選ぶ: claude / codex / opencode / antigravity（docs/extract-agents.md）
8. 既存 worklog の移行（別件）
