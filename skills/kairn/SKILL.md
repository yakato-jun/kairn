---
name: kairn
description: 案件（case）単位の作業ログ運用。案件を開く・計画を出す・進捗を証拠付きで記録する・Drive に戻す。作業ログ、worklog、案件、タスク、進捗、kairn に関する依頼で使う。
---

# kairn 運用手順（Claude Code / Codex / OpenCode 共通）

判断の規則は MCP 側が強制する。ここでは「いつ何を呼ぶか」だけを定める。

## 案件を開く
1. `open_case(<case>)` を呼ぶ（案件名が不明なら `find_cases(<問い>)` か `list_cases()` で選ぶ。選ぶのは人）。
2. 返ってきた open タスク・直近イベント・人からの差し戻し（sendback）を読む。
3. 方針を変える必要があれば `plan(...)` で計画の新版を出す（引き継ぐタスクだけ載せる。載せなかった open タスクは自動で superseded）。

## 作業中
- タスクに着手したら `update_task(status=doing)`、進捗の節目に `log_event(progress)`。
- 決定をしたら `log_event(decision, note=根拠)`。worklog.md の Decision Log にも書く。
- 完了は `update_task(status=done, evidence=[...])`。**証拠なしの done は拒否される**。
- 人からの指摘・方針転換は、既存タスクを直さず `plan` で計画を出し直す。

## 終わるとき
- worklog.md を更新（Current State / Decision Log / Notes / Data location）。
- `checkin(<case>)` で Drive に戻す。

## してはいけないこと
- ファイルを直接編集してタスク状態を変える（必ず MCP 経由）。
- ワークスペースをまたいで案件を参照する（明示指定があるときだけ）。
- 知見の抽出結果（extract_card）を確認なしに書き込む。
