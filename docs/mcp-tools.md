# MCP ツール（10 個以内。説明文は短く）

実装: `kairn/server.py`（mcp 2.x `mcp.server.mcpserver.MCPServer`、streamable HTTP を `/mcp` に提供。UI と同一プロセス）。ツールは 10 個。
判断の規則の実体は `kairn/store.py`（証拠必須・superseded 自動化）。server は引数を検証して委譲する。

## 共通

- **ワークスペースの決定**: 全ツールが `workspace` 省略可。省略時は (1) `case` が 1 つのワークスペースにだけ存在すればそれ、
  (2) 登録ワークスペースが 1 つならそれ、それ以外は拒否（複数に同名案件がある／複数登録で指定なし）。既定で他ワークスペースを見ない。
- **actor / agent**: 書き込み系ツールは `actor="ai"` を自動付与。`agent` 引数（例 `claude-code`）を渡せばイベントに記録、
  省略時は `create_server(conf, default_agent)` の既定値（`serve` では `unknown`）。
- **失敗の返し方**: 規則違反・未知の案件／タスク・rclone 失敗は `ToolError` → `CallToolResult(is_error=True)` で理由の文章を返す
  （エージェントが読める。JSON-RPC エラーにはしない）。
- **索引**: `search` / `find_cases` は呼び出しのたびに SQLite FTS5（trigram）索引を差分更新してから検索する（`kairn/index.py`）。
  シンボリックリンクと生成物ディレクトリ（target / build / node_modules / .venv / __pycache__）は索引しない。
  **trigram の制約**: 3 文字未満の語は索引に載らない（MATCH に渡せない）。3 文字未満の語だけの問いは LIKE（`search`: 節本文・案件 ID、
  `find_cases`: `case_id` / `title`）で補う。3 文字以上の語と混在する場合は、長い語で MATCH してから短い語を本文の部分一致で絞る。

## ツール

| ツール | 引数 | 返り値 | 実装で強制する規則 |
|---|---|---|---|
| `open_case(case, workspace?, agent?)` | | `{case, plan, open_tasks, recent_events(直近20), human_feedback(人の sendback/comment 直近5), related, worklog_tail(末尾3000字), drive, paths}` | 順序: ワークスペース解決 → Drive から取り寄せ（`sync.checkout`: `events.jsonl` は Drive 版と行の和集合にマージ、他は `rclone copy --update` でローカルの方が新しいファイルは上書きしない）→ 読み込み。返り値はすべて取り寄せ後のディスクから読む（Drive にしか無い案件も開ける）。`case.json.last_checkin_at` より新しいローカル変更（人／AI の実質的な変更。`events.jsonl` が kairn 自身の `checkin` event で伸びただけなら数えない）があれば取り寄せを skip し `drive={fetched:false, skipped:"local changes newer than last checkin", files:[…]}`。rclone 失敗はローカル写しで続行し `drive={fetched:false, error, note}`。**events.jsonl には書かない**（閲覧記録は `index/access.log` にローカルで 1 行追記） |
| `list_cases(workspace?, status="open", query="")` | `status`: open\|closed\|suspended\|all。`query` は id/title 部分一致 | `[{case, title, status, progress{total,done,open,plan}, last_event}]` | |
| `plan(case, objective, tasks[], reason, workspace?, agent?)` | `tasks: [{title, owner?: ai\|human, carried_from?: "T012"}]` | 新版の plan（`superseded: [...]` を含む） | 版番号は自動。`carried_from` で引き継がれなかった open/doing/blocked は前版で `superseded`。未知の `carried_from`・同じタスクの二重 `carried_from`・`title` も `carried_from` も無い要素・`owner` が ai/human 以外は拒否。**`done` を `carried_from` しないと旧版にだけ残る**（UI のタスク追加は done も引き継ぐ） |
| `update_task(case, task, status, evidence[]?, note?, workspace?, agent?)` | `status`: open\|doing\|blocked\|done\|dropped | 更新後の task | `done` は `evidence` 必須。各要素は `{type: commit\|pr\|file\|test\|url, ...}` で型ごとの必須キー（commit/pr→`id`、file→`path`、test→`cmd`、url→`url`）を検証、`note` 型（必須キー `text`）は human のみ（docs/data-model.md）。存在しない task・計画未作成は拒否。event（started/done/dropped/progress）を追記 |
| `log_event(case, action, note, evidence[]?, workspace?, agent?)` | `action`: progress\|decision\|comment | 追記した event | actor/agent 自動付与。他の action は拒否。`evidence` は update_task と同じ検証 |
| `search(query, cases[]?, workspace?, limit=10)` | | `[{case, file, heading, snippet, score}]` | worklog 等の `## ` 節単位の全文検索。語は AND。3 文字未満の語は本文・案件 ID の部分一致（LIKE）で絞る。ワークスペース内のみ |
| `find_cases(query, workspace?, k=5)` | | `[{case, score, reasons[]}]` | 案件カード（title/tickets/related/elements）×3 ＋ 本文節の bm25 を合算。語は OR（問いの一部にでも当たる案件を拾う）。3 文字未満の語だけなら `case_id` / `title` の LIKE で補う。案件を選ぶのは人 |
| `checkin(case, workspace?, agent?)` | | `{ok, rclone, last_checkin_at}` | ローカル → Drive（`sync.checkin`、設定済み remote のみ）＋ `case.json.last_checkin_at` 更新＋ `checkin` event。先に `events.jsonl` を Drive 版とマージするので他環境の event は消えない。**それ以外のファイルは Drive 側に新しい版があっても `_deleted/<日付>/` に退避して上書きする**（案件単位は rclone sync。ワークスペース全体の `kairn checkin <ws>` / `daily` は rclone copy で、ローカルに無い案件を Drive から消さない）。他環境で作業した後は先に `checkout` する運用 |
| `drive_index(pattern, workspace?, limit=50)` | 正規表現 | `[{path, size, mtime}]` | `index/drive-index.txt`（`kairn drive-index` で生成）を検索。無ければ空 |
| `extract_card(case, workspace?)` | | `{ok, card, agent, elapsed_sec, error, raw_excerpt}` | 設定 `extract.agent` の子エージェント（`kairn/extract/adapters.py`）を案件ディレクトリの写し（一時ディレクトリ: 自案件＋兄弟案件の `case.json` のみ）を cwd に、最小限の環境変数で起動し、出力を `kairn/extract/schema.json` で検証した下書きを `card` に返す（docs/extract-agents.md）。`card.related` のうち実在しない案件 ID は `card.related_unknown` に分ける。**case.json には書かない**（適用は UI の人の操作のみ）。タイムアウト・非ゼロ終了・JSON 無し・スキーマ不一致は `ok=false, error` で返す（`is_error` にしない。未知の案件だけ `ToolError`）。毎回 `{actor: kairn, agent: "extract:<name>", action: extract, note, elapsed_sec, exit_code, timeout_sec}` を events に追記 |

## server instructions（各エージェントに表示される要約）

「kairn: 案件（case）単位の作業ログ。案件を開くときは open_case（無ければ find_cases / list_cases で選ぶ。選ぶのは人）。
作業したら log_event / update_task（done は証拠必須）。方針が変わったら plan で計画を出し直す（載せなかった open タスクは superseded になる）。
終わったら checkin。ワークスペースをまたぐ参照はしない。」

## 起動と登録

- `kairn serve [--host 127.0.0.1] [--port 8765]` → MCP `http://127.0.0.1:8765/mcp`、UI `http://127.0.0.1:8765/ui`
- 常駐: `kairn install-service` が生成する systemd user unit `kairn-serve.service`（README「各エージェントへの適用」）。`kairn ensure` は /mcp が応答しなければ serve を起動する
- Claude Code: `claude mcp add --transport http kairn http://127.0.0.1:8765/mcp -s user`（Codex / OpenCode は README）
- ツール名と必須引数は tests/test_docs.py がこの表と server.py を照合する
- テスト: `mcp.client.Client(server)` で in-process、`Client("http://…/mcp")` で HTTP（tests/test_server.py, tests/test_e2e.py）
