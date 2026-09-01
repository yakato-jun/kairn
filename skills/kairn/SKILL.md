---
name: kairn
description: 案件（case）単位の作業ログ運用。案件を開く・計画を出す・タスクの進捗を証拠付きで記録する・Drive に戻す。案件、作業ログ（worklog）、タスク、進捗、kairn に関する依頼で使う。
---

# kairn 運用手順（Claude Code / Codex / OpenCode 共通）

判断の規則は MCP サーバー `kairn`（10 ツール）が強制する。ここでは「いつ何を呼ぶか」だけを定める。
ツールの引数・返り値の詳細は kairn リポジトリの `docs/mcp-tools.md`。

## 案件を開く
1. 案件名が分かっていれば `open_case(case)`。分からなければ `find_cases(query)`（理由付きの候補）か
   `list_cases()`（`status`: open|closed|suspended|all、`query` で id/title 絞り込み）で候補を出し、**人に選んでもらう**。
2. `open_case` の返り値は **`human_feedback`（人からの差し戻し sendback・コメント comment）を最初に読む**。
   次に `open_tasks`、`plan`、`recent_events`、`worklog_tail`、`related`。`drive` に `fetched: false` が
   あれば Drive からの取り寄せが skip / 失敗した理由が入っている（ローカル写しで続行している）。
3. 計画が無い案件は `plan(case, objective, tasks, reason)` で v1 を作る（`tasks: [{title, owner?: ai|human}]`）。
4. 案件ディレクトリ（worklog.md・作業ファイル）はリポジトリの外、kairn の `workspaces/<ws>/cases/<case>/` にある。
   `open_case` の返り値 `paths.case_dir` / `paths.worklog`（絶対パス）で読み書きする。リポジトリ内の `tmp/` 等を探さない。
   その領域を読み書きできない（許可の外）と言われたら、`kairn install-skill` が表示する許可設定手順を人に伝える。

## 作業中
- 着手: `update_task(case, task, status="doing")`。
- 進捗の節目: `log_event(case, action="progress", note=…)`。
- 決定: `log_event(case, action="decision", note=根拠)`。worklog.md の Decision Log にも書く。
- 完了: `update_task(case, task, status="done", evidence=[…])`。**証拠なしの done は拒否される**。
  `evidence` の各要素は `{type, …}` で、型ごとに必須キーがある:
  - `commit` → `id`（`repo` 任意） / `pr` → `id`（`repo` 任意） / `file` → `path` / `test` → `cmd`（`result` 任意） / `url` → `url`
  - `note` 型は人（UI）専用。AI の証拠にはならない。
- 行き詰まり: `update_task(status="blocked", note=理由)`。不要になった: `status="dropped"`。
- 方針転換（人の差し戻し・指摘を含む）は既存タスクを直さず **`plan` で計画の新版を出す**。
  引き継ぐタスクは `{carried_from: "T012"}` で載せる（タイトルは前版から引き継がれる）。
  **引き継がなかった open / doing / blocked タスクは自動で `superseded` になる**（閉じる操作は無い）。
  done も引き継がないと新版の進捗に数えない。
- 過去の経緯を探す: `search(query)`（worklog 等の `## ` 節単位。語は AND）。Drive 上の生データの所在: `drive_index(pattern)`。

## 下書き（extract）
- `extract_card(case)` は案件カード（title / summary / elements / related / causal）の**下書きを返すだけ**。
  case.json には書かない。適用は人が UI（案件ページ「下書きを取得」→「適用」）で行う。
- 失敗は `ok: false` と `error` で返る（エラー例外ではない）。

## 終わるとき
1. worklog.md を更新（Current State / Decision Log / Notes / Data location）。
2. `checkin(case)` で Drive に戻す（`case.json.last_checkin_at` が更新される）。
- `checkin(case)` は rclone sync で Drive 側の案件を上書きする（Drive 側の新しい版は `_deleted/<日付>/` に退避）。
  `events.jsonl` だけは Drive 版とマージ（行の和集合）されるので、他環境の event は消えない。
  **他の環境で作業した後は、先に `open_case`（Drive から取り寄せる）か `kairn checkout` をしてから作業する**。
- `open_case` は `events.jsonl` に何も書かない（閲覧記録はローカルの `index/access.log`）。何度開いても Drive との差分にならない。

## してはいけないこと
- ファイルを直接編集してタスク状態・計画・イベントを変える（`case.json` / `plan/` / `events.jsonl` は必ず MCP 経由）。
- ワークスペースをまたいで案件を参照する（人の明示指定があるときだけ `workspace=` を渡す）。
- `extract_card` の下書きを確認なしに case.json や worklog に書き込む。
- 証拠なしで done にする（`note` 型や空の `evidence` は拒否される）。
