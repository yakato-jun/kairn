---
name: kairn
description: 案件（case）単位の作業ログ運用。案件を開く・計画を出す・タスクの進捗を証拠付きで記録する・Drive に戻す。案件、作業ログ（worklog）、タスク、進捗、kairn に関する依頼で使う。
---

# kairn 運用手順（Claude Code / Codex / OpenCode 共通）

判断の規則は MCP サーバー `kairn`（12 ツール）が強制する。ここでは「いつ何を呼ぶか」だけを定める。
ツールの引数・返り値の詳細は kairn リポジトリの `docs/mcp-tools.md`。

## 案件を開く
0. 最初に 1 回、シェルで `kairn ensure` を実行してからツールを呼ぶ（MCP `/mcp` が応答しなければ `kairn serve` を
   切り離して起動し、応答するまで待つ。service が止まっていた時の保険。動いていれば何もしない。終了コード 0 以外なら
   ログ `~/.local/state/kairn/serve.log` の内容を人に伝える）。
1. 案件名が分かっていれば `open_case(case)`。分からなければ `find_cases(query)`（理由付きの候補）か
   `list_cases()`（`status`: open|closed|suspended|all、`query` で id/title 絞り込み）で候補を出し、**人に選んでもらう**。
2. `open_case` の返り値は、まず `available` を見る。**`available: false` なら案件はまだローカルに無い**（Drive から取り寄せ中。
   `status: fetching`、`job_id`）: `job_status(job_id)` が `done` になるまで待って `open_case` を再呼び出しする。人には
   「取り寄せ中で今すぐは操作できない」と伝える。`status: failed` なら `error` を人に伝える（`job_id` は再試行のジョブ。
   `job_status` で確認し、失敗が続くなら人の判断を仰ぐ）。エラー（is_error）ではなく通常の結果なので、案件が無いと決めつけない。
   `available: true` なら **`human_feedback`（人からの差し戻し sendback・コメント comment）を最初に読む**。
   次に `open_tasks`、`plan`、`recent_events`、`worklog_tail`、`related`。
   - `drive` は Drive との照合結果。`open_case` は Drive の案件フォルダの版マーカー（`.rev/<rev>`）と案件の版（`rev`）を比べ、同じなら取り寄せない。
     - `drive.up_to_date: true` … ローカルが最新（`fetched: true` なら今取り寄せた）。そのまま作業する。
     - `drive.up_to_date: false` で `drive.job_id` がある … Drive に新しい版があり取り寄せが 20 秒以内に終わらなかった（返り値は
       **取り寄せ前のローカル内容**）。他の環境で作業した後など最新が要るときは `job_status(job_id)` が `done` になってからもう一度
       `open_case` する（`status: failed` なら `error` を人に伝えてローカル写しで続行）。ローカルだけで作業を続けるなら待たなくてよい。
     - `drive.up_to_date: null` … Drive の状態が分からない（オフライン等。`note`）。ローカル写しで続行し、
       他の環境で作業した可能性があるなら人に伝える。
     - `drive.skipped` … 未 checkin のローカル変更があるため取り寄せなかった（ローカル写しで続行。checkin すれば解ける）。
   - 「unknown case …」のエラーは取り寄せを終えても案件が無い（Drive にも無い）とき。`find_cases` / `list_cases` で人に選んでもらう。
3. 計画が無い案件は `plan(case, objective, tasks, reason)` で v1 を作る（`tasks: [{title, owner?: ai|human}]`）。
4. 案件ディレクトリ（worklog.md・作業ファイル）はリポジトリの外、kairn の `workspaces/<ws>/cases/<case>/` にある。
   `open_case` の返り値 `paths.case_dir` / `paths.worklog`（絶対パス）で読み書きする。リポジトリ内の `tmp/` 等を探さない。
   その領域を読み書きできない（許可の外）と言われたら、`kairn install-skill` が表示する許可設定手順を人に伝える。
5. 案件フォルダ内に git worktree（`git worktree add <case_dir>/<name> …`）を作って作業してよい。直下に `.git` ファイル
   （worktree）か `.kairn-nosync`（空ファイル）があるディレクトリは配下ごと同期・索引・退避の対象外。通常の clone は
   `.git/` だけが除かれソース本体は同期されるので、clone ではなく worktree を使うか `.kairn-nosync` を置く。
   成果物（worklog.md、調査メモ、ログの抜粋）は作業領域の中ではなく案件直下に書く。

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

## 他のワークスペースの知見（跨ぎ参照）
- 自ワークスペースに無ければ他ワークスペースも検索してよい。`find_cases` / `search` は `scope=auto` が既定で、自 ws にヒットが無ければ
  自動的に全 ws を検索する（`scope=workspace` で自 ws のみ、`scope=all` で常に全 ws）。返り値は `{workspace, scope, searched, results}` で、
  他 ws のヒットは `cross_workspace: true`。
- **案件の文脈があるときは `from_case="<ws>/<case>"` を必ず渡す**（`open_case` / `find_cases` / `search`。開いている案件の
  `case.workspace` と `case.id`）。これが跨ぎ参照の記録になる（対象 ws の access.log と、自案件の `xref` event）。
  `workspaces/` 配下を直接 grep して他 ws を読む経路は記録に残らないので使わない。
- 他 ws の案件を開くときは `open_case(case, workspace=<ws>, from_case=…)`。`related` の展開（他 ws は title と status だけ）で足りるなら開かない。
- **他 ws の案件から得た内容を worklog・タスク・成果物に書くときは、相手の案件 ID や顧客固有の情報（機体名・拠点名・図面等）を書かず
  一般化した表現にする**（例: 「別案件で同種の UART 送信量超過を 1 バイト送信の廃止で解決した」）。出典は `related` に `"<ws>/<case>"`
  として残す（`case.json.related`）。related を書く MCP ツールは無いので、書いた内容の出典として `<ws>/<case>` を related に足してほしいと
  人に伝える（`case.json` を直接編集しない）。相手の案件 ID を書いてよいのは related だけ。

## 案件のステータス（閉じる・保留する・再開する）
- 案件を閉じる（closed）・保留する（suspended）・再開する（open）のは**人の判断**。AI の判断で変えない
  （タスクが全部 done でも、長く動きが無くても、AI からは閉じない。提案もしない）。
- 人が明示した時だけ `set_case_status(case, status, instruction=<人の発言そのまま>)` を呼ぶ。`instruction` は
  「この案件は閉じて」のような人の発言をそのまま入れる（空は拒否される。AI の要約や理由に置き換えない）。
- 返り値の `open_tasks` が 0 でないまま `closed` にした時は、残っている open タスクの件数を人に伝える（拒否はされない。閉じるかは人）。
  `changed: false` は既にそのステータスだった（何も書かれていない）。
- 人が自分で操作する経路は UI の「案件の状態を変更」と CLI `kairn close | suspend | reopen <ws> <case> [--note]`。

## 下書き（extract）
- `extract_card(case)` は案件カード（title / summary / elements / related / causal）の**下書きを返すだけ**。
  case.json には書かない。適用は人が UI（案件ページ「下書きを取得」→「適用」）で行う。
- 失敗は `ok: false` と `error` で返る（エラー例外ではない）。

## 終わるとき
1. worklog.md を更新（Current State / Decision Log / Notes / Data location）。
2. `checkin(case)` で Drive に戻す。**ジョブとして走る**ので返り値は `{job_id, status, note}`。`job_status(job_id)` を
   `status` が `done` になるまで見て（数十秒〜数分。`progress` に rclone の転送状況、`elapsed_sec` に経過秒）、
   `done` を確認してから作業を終える（`result.last_checkin_at` が更新された時刻）。**`failed` なら `error` の理由を人に報告する**
   （Drive には戻っていない。`kairn checkin <ws> <case>` を CLI で実行してもらう等の判断は人）。
   同じ案件の checkin が既に走っていれば同じ `job_id` が返る（新しく起動しない）。
- `checkin(case)` は rclone sync で Drive 側の案件を上書きする（Drive 側の新しい版は `_deleted/<日付>/` に退避）。
  `events.jsonl` だけは Drive 版とマージ（行の和集合）されるので、他環境の event は消えない。
  **他の環境で作業した後は、先に `open_case`（Drive から取り寄せる）か `kairn checkout` をしてから作業する**。
- `open_case` は `events.jsonl` に何も書かない（閲覧記録はローカルの `index/access.log`）。何度開いても Drive との差分にならない。

## してはいけないこと
- `checkin` の `job_status` が `done` になる前に「Drive に戻した」と報告する（`failed` を黙って流す）。
- ファイルを直接編集してタスク状態・計画・イベントを変える（`case.json` / `plan/` / `events.jsonl` は必ず MCP 経由）。
- 他ワークスペースの案件を `from_case` 無しで開く・検索する、または `workspaces/` を直接 grep する（記録に残らない）。
- 他ワークスペースの案件 ID・顧客固有の情報を自案件の worklog・タスク・成果物にそのまま書く（一般化し、出典は `related`）。
- `extract_card` の下書きを確認なしに case.json や worklog に書き込む。
- 証拠なしで done にする（`note` 型や空の `evidence` は拒否される）。
