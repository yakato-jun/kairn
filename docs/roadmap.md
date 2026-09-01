# 実装順（MVP → 拡張）

1. `config`: workspaces.yaml の読み込み・検証（remote 固定、未登録パスは拒否）、`kairn link`（tmp → cases）
2. `store`: case.json / plan / events の読み書き、superseded 自動化、evidence 検証、sqlite 索引の再生成
3. `server`: MCP（open_case / list_cases / plan / update_task / log_event / search / find_cases / checkin / drive_index）
4. `ui`: 一覧・かんばん・時系列・差し戻し
5. `sync`: rclone による checkout / checkin / 日次同期 / 生データ move / drive-index（既存 backup-tmp.sh の設計を取り込む）
6. skill: skills/kairn/SKILL.md を ~/.agents/skills に配置、Claude 側リンク、/worklog の置き換え
7. `extract`: claude -p による case.json 下書き
8. 既存 worklog の移行（別件）
