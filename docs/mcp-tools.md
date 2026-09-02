# MCP ツール（説明文は短く）

実装: `kairn/server.py`（mcp 2.x `mcp.server.mcpserver.MCPServer`、streamable HTTP を `/mcp` に提供。UI と同一プロセス）。ツールは 12 個。
判断の規則の実体は `kairn/store.py`（証拠必須・superseded 自動化）。server は引数を検証して委譲する。
rclone の転送（`checkin`、`open_case` の取り寄せ）は**ジョブ**（`kairn/jobs.py`）として走らせ、`checkin` は待たずに `job_id` を返す（後述）。
`open_case` は先に Drive の案件フォルダの版マーカー（`cases/<case>/.rev/<rev>`）で `rev` を比べ、同じなら取り寄せを省略する（「open_case の drive」）。

## 共通

- **ワークスペースの決定**: 全ツールが `workspace` 省略可。省略時は (1) `case` が 1 つのワークスペースにだけ存在すればそれ、
  (2) 登録ワークスペースが 1 つならそれ、それ以外は拒否（複数に同名案件がある／複数登録で指定なし）。既定で他ワークスペースを見ない。
- **actor / agent**: 書き込み系ツールは `actor="ai"` を自動付与。`agent` 引数（例 `claude-code`）を渡せばイベントに記録、
  省略時は `create_server(conf, default_agent)` の既定値（`serve` では `unknown`）。
- **失敗の返し方**: 規則違反・未知の案件／タスク・rclone 失敗は `ToolError` → `CallToolResult(is_error=True)` で理由の文章を返す
  （エージェントが読める。JSON-RPC エラーにはしない）。
- **ジョブ**（`kairn/jobs.py`）: rclone の転送は数百 MB・数百ファイルの案件で数分かかり、MCP クライアント側の呼び出しタイムアウト（300 秒程度）に
  当たる（サーバー側は最後まで走るがクライアントは失敗扱い）。そのため `checkin` と `open_case` の取り寄せはデーモンスレッドのジョブにし、
  `checkin` は即座に `job_id` を返す（`open_case` は最大 20 秒だけ待ち、間に合わなければ `job_id` を返す）。状態は `job_status(job_id)` で見る（queued → running → done | failed。`progress` は rclone の
  `--stats 5s --stats-one-line` の最新行、`elapsed_sec` は開始からの秒数）。**同じ案件に対する同種のジョブが queued / running なら新しく作らず
  既存の `job_id` を返す**（`note` に "already running"）。**同じ案件（workspace, case）のジョブは種類を問わず 1 つずつ実行する**
  （(workspace, case) ごとの FIFO キュー。checkin 実行中に `open_case` の取り寄せが来れば `status: queued` で待ち、先行が done / failed になってから
  走る。`elapsed_sec` は queued の間 0）。別の案件のジョブは並走する。UI の案件ページの「進行中のジョブ」にも queued を出す。
  完了したジョブは 200 件または 24 時間で捨てる。
  **ジョブ表はサーバーのメモリ内にあり、`kairn serve` の再起動で消える**（設計上許容。消えた `job_id` は `job_status` が「unknown job」を返す。
  転送が終わったかは `case.json.last_checkin_at` と `checkin` event で分かる）。**再起動時に running だった checkin / checkout は途中で切れる**が、
  次回の同じ案件の `checkin` / `checkout`（`open_case` の取り寄せ）で整合する（checkin は `events.jsonl` をマージしてから rclone sync、
  checkout は `--update` なので、やり直せば同じ結果になる）。停止時に running だったジョブは `kairn serve` のログに 1 行残る。大きな初回投入（数百 MB）は MCP ではなく CLI の
  `kairn checkin <ws> <case>`（同期・タイムアウト無し）で行う（README「同期」）。
- **索引**: `search` / `find_cases` は呼び出しのたびに SQLite FTS5（trigram）索引を差分更新してから検索する（`kairn/index.py`）。
  シンボリックリンクと生成物ディレクトリ（target / build / node_modules / .venv / __pycache__）は索引しない。
  **trigram の制約**: 3 文字未満の語は索引に載らない（MATCH に渡せない）。3 文字未満の語だけの問いは LIKE（`search`: 節本文・案件 ID、
  `find_cases`: `case_id` / `title`）で補う。3 文字以上の語と混在する場合は、長い語で MATCH してから短い語を本文の部分一致で絞る。

## ツール

| ツール | 引数 | 返り値 | 実装で強制する規則 |
|---|---|---|---|
| `open_case(case, workspace?, agent?)` | | ローカルにある: `{available: true, case, plan, open_tasks, recent_events(直近20), human_feedback(人の sendback/comment 直近5), related, worklog_tail(末尾3000字), drive, paths}`。ローカルに無く取り寄せ中: `{available: false, status: "fetching", job_id, case: null, note}`。取り寄せが失敗: `{available: false, status: "failed", error, job_id, case: null, note}` | 順序: ワークスペース解決 → **Drive の `cases/<case>/.rev/` を `rclone lsf` で 1 回読む**（10 秒でタイムアウト。キャッシュ `index/drive_revs.cache.json` の当該案件も更新）→ 案件の `rev` を比較 → 必要なときだけ取り寄せ**ジョブ**（`sync.checkout`: `events.jsonl` は Drive 版と行の和集合にマージ、他は `rclone copy --update` でローカルの方が新しいファイルは上書きしない）を起動し最大 **20 秒**待つ → ローカル内容を読んで返す。`drive` のパターン（「open_case の drive」）: (a) `case.json.last_checkin_at` より新しいローカル変更（人／AI の実質的な変更。`events.jsonl` が kairn 自身の `checkin` event で伸びただけなら数えない）があれば Drive を見ずに skip: `{fetched: false, skipped: "local changes newer than last checkin", files: […]}`。(b) マーカーの `rev` がローカルの `case.json.rev` と同じ: `{fetched: false, up_to_date: true, checked, rev}`（取り寄せなし）。(c) `rev` が違う／マーカーが無い／2 個以上ある（不定。どちらも未知＝安全側で取り寄せ）: ジョブが 20 秒以内に done なら `{fetched: true, up_to_date: true, checked, job_id, rev, drive_rev, note}`（返り値は取り寄せ後の内容）、failed なら `{fetched: false, up_to_date: false, status: "failed", error, job_id, …}`（ローカル写し）、間に合わなければ `{fetched: false, up_to_date: false, job_id, status: "queued"\|"running", note}`（ローカル写し。`job_status` が `done` になってから再度 `open_case`。同じ案件の取り寄せが走っていればその `job_id`）。(d) Drive が読めない（オフライン・タイムアウト）: 取り寄せず `{fetched: false, up_to_date: null, checked, note: "drive unavailable …"}`（ローカル写し）。**Drive にしか無い案件はエラーにしない**: ローカルに無ければ Drive を照会せず取り寄せジョブを起動し（待たない）、1 回目は `{available: false, status: "fetching", job_id, case: null, note}`（同じ案件の取り寄せが既に走っていればその `job_id`）。`job_status(job_id)` が `done` になってから再実行すると開ける（`available: true`）。取り寄せジョブが `failed` なら `{available: false, status: "failed", error, job_id}`（`error` は失敗したジョブのもの。`job_id` は再試行として起動した新しいジョブ。失敗が続くなら `error` を人に伝える）。ジョブが `done` でも案件が無い（Drive にも無い）場合だけ `unknown case …` の `ToolError`。rclone の失敗はジョブの `failed` / `error` に残り、`open_case` はローカル写しを返す。**events.jsonl には書かない**（閲覧記録は `index/access.log` にローカルで 1 行追記） |
| `list_cases(workspace?, status="open", query="")` | `status`: open\|closed\|suspended\|all。`query` は id/title 部分一致 | `[{case, title, status, progress{total,done,open,plan}, last_event, drive{state, rev, drive_rev, checked_in_at, from, checked, files?, drive_differs?, ambiguous?}}]` | `drive.state` は `index/drive_revs.cache.json`（直近に読んだ Drive の版マーカー。`checked` は全案件を読んだ時刻、無ければ null）との比較: `synced`（rev 一致）\| `drive_newer`（rev が違う、またはマーカーが 2 個以上で不定: `ambiguous`）\| `local_changes`（未 checkin のローカル変更。`files`、`drive_differs`）\| `unknown`（キャッシュ無し・マーカー無し・未 checkin）。`checked_in_at` / `from` はローカル case.json の `last_checkin_at` / `checked_in_from`。Drive には接続しない（全案件の取得は `kairn checkout <ws>` / UI の「更新確認」/ `daily`、当該案件だけの更新は open_case / checkin） |
| `plan(case, objective, tasks[], reason, workspace?, agent?)` | `tasks: [{title, owner?: ai\|human, carried_from?: "T012"}]` | 新版の plan（`superseded: [...]` を含む） | 版番号は自動。`carried_from` で引き継がれなかった open/doing/blocked は前版で `superseded`。未知の `carried_from`・同じタスクの二重 `carried_from`・`title` も `carried_from` も無い要素・`owner` が ai/human 以外は拒否。**`done` を `carried_from` しないと旧版にだけ残る**（UI のタスク追加は done も引き継ぐ） |
| `update_task(case, task, status, evidence[]?, note?, workspace?, agent?)` | `status`: open\|doing\|blocked\|done\|dropped | 更新後の task | `done` は `evidence` 必須。各要素は `{type: commit\|pr\|file\|test\|url, ...}` で型ごとの必須キー（commit/pr→`id`、file→`path`、test→`cmd`、url→`url`）を検証、`note` 型（必須キー `text`）は human のみ（docs/data-model.md）。存在しない task・計画未作成は拒否。event（started/done/dropped/progress）を追記 |
| `log_event(case, action, note, evidence[]?, workspace?, agent?)` | `action`: progress\|decision\|comment | 追記した event | actor/agent 自動付与。他の action は拒否。`evidence` は update_task と同じ検証 |
| `set_case_status(case, status, instruction, workspace?, agent?)` | `status`: closed\|suspended\|open。`instruction`: 人がそう指示した発言そのもの（必須。空・空白のみは拒否） | `{case, status, previous_status, changed, event, open_tasks}` | **案件を閉じる・保留する・再開するのは人の判断。人が明示した時だけ、その発言を `instruction` に入れて呼ぶ。AI の判断で呼ばない**（docs/decisions.md 19）。`set_case_status(actor="ai", agent, note=instruction)` を呼び、event `{action: status, from, to, note}` を追記。同じステータスへの変更は `changed: false` で何も書かない（`event: null`）。open タスクが残ったまま `closed` にしても拒否せず、`open_tasks`（open/doing/blocked の件数）で知らせる（閉じるかは人の判断）。不正な `status`・未知の案件／ワークスペースは `ToolError` |
| `search(query, cases[]?, workspace?, limit=10)` | | `[{case, file, heading, snippet, score}]` | worklog 等の `## ` 節単位の全文検索。語は AND。3 文字未満の語は本文・案件 ID の部分一致（LIKE）で絞る。ワークスペース内のみ |
| `find_cases(query, workspace?, k=5)` | | `[{case, score, reasons[]}]` | 案件カード（title/tickets/related/elements）×3 ＋ 本文節の bm25 を合算。語は OR（問いの一部にでも当たる案件を拾う）。3 文字未満の語だけなら `case_id` / `title` の LIKE で補う。案件を選ぶのは人 |
| `checkin(case, workspace?, agent?)` | | `{job_id, status: "queued"\|"running", note}` | ローカル → Drive（`sync.checkin_job`、設定済み remote のみ）を**ジョブ**として起動し即座に返す。未知の案件はジョブを作らず `ToolError`。同じ案件の checkin が走っていればその `job_id`（新しく作らない）。完了時に**ジョブ側で** `case.json.last_checkin_at` / `last_checkin_events` の更新と `checkin` event の追記を行う（失敗時はどちらも書かない）。`job_status(job_id)` が `done` なら `result={ok, rclone, last_checkin_at}`（従来の返り値）、`failed` なら `error` に rclone の末尾。先に `events.jsonl` を Drive 版とマージするので他環境の event は消えない。**それ以外のファイルは Drive 側に新しい版があっても `_deleted/<日付>/` に退避して上書きする**（案件単位は rclone sync。ワークスペース全体の `kairn checkin <ws>` / `daily` は rclone copy で、ローカルに無い案件を Drive から消さない）。他環境で作業した後は先に `checkout` する運用 |
| `job_status(job_id)` | | `{job_id, kind: checkin\|checkout, workspace, case, status: queued\|running\|done\|failed, created_at, started_at, finished_at, elapsed_sec, progress, result, error}` | `checkin` / `open_case` が返した `job_id` の状態。`queued` は同じ案件の先行ジョブが終わるのを待っている（同一案件のジョブは 1 つずつ）。`progress` は rclone の出力の最新行（`Transferred: … ETA …`）、`elapsed_sec` は開始からの秒数。`done` なら `result`（checkin: `{ok, rclone, last_checkin_at}`、checkout: rclone の末尾）、`failed` なら `error`（`RcloneError: …` 等）。未知の `job_id`（捨てられた／サーバー再起動で消えた）は `ToolError` |
| `drive_index(pattern, workspace?, limit=50)` | 正規表現 | `[{path, size, mtime}]` | `index/drive-index.txt`（`kairn drive-index` で生成）を検索。無ければ空 |
| `extract_card(case, workspace?)` | | `{ok, card, agent, elapsed_sec, error, raw_excerpt}` | 設定 `extract.agent` の子エージェント（`kairn/extract/adapters.py`）を案件ディレクトリの写し（一時ディレクトリ: 自案件＋兄弟案件の `case.json` のみ）を cwd に、最小限の環境変数で起動し、出力を `kairn/extract/schema.json` で検証した下書きを `card` に返す（docs/extract-agents.md）。`card.related` のうち実在しない案件 ID は `card.related_unknown` に分ける。**case.json には書かない**（適用は UI の人の操作のみ）。タイムアウト・非ゼロ終了・JSON 無し・スキーマ不一致は `ok=false, error` で返す（`is_error` にしない。未知の案件だけ `ToolError`）。毎回 `{actor: kairn, agent: "extract:<name>", action: extract, note, elapsed_sec, exit_code, timeout_sec}` を events に追記 |

## open_case の drive（取り寄せの判定。`kairn/server.py` `_fetch_from_drive`）

| 状況 | `drive` | 取り寄せ | 返り値の内容 |
|---|---|---|---|
| 未 checkin のローカル変更がある（skip 判定が最優先。Drive は見ない） | `{fetched: false, skipped: "local changes newer than last checkin", files}` | しない | ローカル写し |
| Drive の `.rev/` のマーカーがローカルの `case.json.rev` と同じ | `{fetched: false, up_to_date: true, checked, rev}` | しない | ローカル（＝最新） |
| rev が違う／マーカーが無い／2 個以上（不定）、20 秒以内に取り寄せ完了 | `{fetched: true, up_to_date: true, checked, job_id, rev, drive_rev, note}`（`drive_rev` は無い・不定なら null） | した | 取り寄せ後 |
| 同上、取り寄せが失敗 | `{fetched: false, up_to_date: false, status: "failed", error, job_id, checked, drive_rev, note}` | 失敗 | ローカル写し |
| 同上、20 秒以内に終わらない | `{fetched: false, up_to_date: false, status: "queued"\|"running", job_id, checked, drive_rev, note}` | 実行中 | ローカル写し（`job_status` が done になったら再度 `open_case`） |
| Drive が読めない（オフライン・タイムアウト 10 秒・rclone 不在） | `{fetched: false, up_to_date: null, checked, note: "drive unavailable …"}` | しない | ローカル写し（Drive の状態は不明） |
| ローカルに案件が無い | 返り値は `{available: false, status: "fetching"\|"failed", job_id, …}` | する（待たない） | なし |

`checked` は Drive を読んだ時刻（ISO 8601）。版マーカーの規則は docs/data-model.md。既存 Drive データには一度 `kairn drive-markers <ws>` を実行して
マーカーを置く（それまでマーカーの無い案件は `open_case` のたびに取り寄せる）。

## server instructions（各エージェントに表示される要約）

「kairn: 案件（case）単位の作業ログ。案件を開くときは open_case（無ければ find_cases / list_cases で選ぶ。選ぶのは人）。
作業したら log_event / update_task（done は証拠必須）。方針が変わったら plan で計画を出し直す（載せなかった open タスクは superseded になる）。
終わったら checkin（ジョブとして走る。job_status で done を確認する）。ワークスペースをまたぐ参照はしない。」

## 起動と登録

- `kairn serve [--host 127.0.0.1] [--port 8765]` → MCP `http://127.0.0.1:8765/mcp`、UI `http://127.0.0.1:8765/ui`
- 常駐: `kairn install-service` が生成する systemd user unit `kairn-serve.service`（README「各エージェントへの適用」）。`kairn ensure` は /mcp が応答しなければ serve を起動する
- Claude Code: `claude mcp add --transport http kairn http://127.0.0.1:8765/mcp -s user`（Codex / OpenCode は README）
- ツール名と必須引数は tests/test_docs.py がこの表と server.py を照合する
- テスト: `mcp.client.Client(server)` で in-process、`Client("http://…/mcp")` で HTTP（tests/test_server.py, tests/test_e2e.py）
