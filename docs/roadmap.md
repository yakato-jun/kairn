# 実装順（MVP → 拡張）

段階 1（2026-09-02 完了）: 1〜4。段階 2（2026-09-02 完了）: 5。段階 3（2026-09-02 完了）: 7。6・8 は未着手。コードに段階外の機能は含めない
（呼ばれ得る箇所があれば「not implemented in this stage: see docs/roadmap.md」を返す）。

1. [完了] `config`: 環境ローカル設定（~/.config/kairn/config.yaml）の読み込み・検証（remote 固定、未登録パスは拒否）、`kairn attach`（<repo>/tmp → cases へのリンク）
2. [完了] `store` / `index`: case.json / plan / events の読み書き、superseded 自動化、evidence 検証、SQLite FTS5（trigram）索引の差分再生成
3. [完了] `server`: MCP（mcp 2.x、streamable HTTP /mcp。open_case / list_cases / plan / update_task / log_event / search / find_cases / checkin / drive_index）
4. [完了] `ui`: 一覧・かんばん・時系列・版履歴・差し戻し・コメント・タスク追加・状態変更・鮮度・elements 絞り込み（/ui、MCP と同一プロセス）
5. [完了] `sync`: rclone による checkout（`--update`）/ checkin / 生データ move（`raw_move`: `rules.raw_data`、所在を case.json.data[] と
   worklog.md の Data location（worklog.md が無ければ DATA.md）に記録）/ bag の zstd 圧縮（`bag2zst`）/ drive-index / 日次同期（`daily`: 各段の失敗を index/daily.log に記録して続行）。
   CLI `bag2zst` / `raw-move` / `daily`、systemd user timer（contrib/systemd、12:30）。`rules.bwlimit` で帯域制限
6. skill: skills/kairn/SKILL.md を ~/.agents/skills に配置、Claude 側リンク、/worklog の置き換え
7. [完了] `extract`（MCP ツール `extract_card`、CLI `kairn extract`、UI「下書きを取得」→「適用」）: 文脈隔離した子エージェントで case.json の
   下書きを作る。エージェントはアダプタ方式で設定（`extract.agent`）から選ぶ: claude / codex / opencode / antigravity（docs/extract-agents.md）。
   下書きは `kairn/extract/schema.json` で検証し、適用（title / summary / elements / related / causal）は UI の人の操作のみ。
   opencode の agent `kairn-extract` の配置・各 CLI の登録手順は 6 で扱う
8. 既存 worklog の移行（別件）
