# データモデル

すべてワークスペース配下のファイル。人は読まない前提（UI と MCP が読み書き）。Drive 同期に耐えるよう、
1 レコード 1 ファイルか追記専用 JSONL にする。SQLite の索引は各環境で再生成する派生物。

```
workspaces/<ws>/
  cases/<case_id>/
    case.json            案件の正本（下記）
    worklog.md           AI が書く経緯・調査・決定（従来の worklog。Tasks 節は持たない）
    plan/v0001.json …    計画の版（下記）。最新版が「今やること」
    events.jsonl         追記専用のイベント（下記）
    …                    作業ファイル（MMdd_hhmm_ prefix 等、従来どおり）
  index/kairn.sqlite     索引（case / plan / task / event / section の検索用）。再生成可
  index/drive-index.txt  Drive 上の全ファイル一覧（path, size, mtime）。`kairn drive-index` / `kairn daily` が生成
```

## case.json
```json
{
  "id": "CASE-123_widget-boot-failure",
  "title": "起動時に widget driver が初期化されない",
  "status": "open",                     // open | closed | suspended
  "workspace": "acme",
  "repos": ["acme-robot", "acme-plc"],
  "tickets": ["CASE-123"], "prs": [42],
  "related": ["CASE-100", "CASE-118"],      // 人／AI が明示的に書く関係（抽出に頼らない）
  "elements": {"machine": ["unit-2"], "component": ["acme-plc"], "symptom": ["起動時に driver init 未完了"]},
  "summary": "起動直後に driver init が終わらない。UART の送信量超過が原因。",   // 3 行以内（extract の下書きを人が適用した時に入る）
  "causal": [{"symptom": "起動時に driver init 未完了", "component": "acme-plc", "cause": "UART 460800 で送信量が超過",
              "evidence": "## Notes: UART 460800 で送信量が超過する"}],      // 症状→部品→原因。evidence は worklog の見出し／行の引用
  "data": [{"drive": "my-drive:ws/acme/cases/CASE-123/", "files": 3, "bytes": 1234567890,
            "moved_at": "2026-09-01T12:30:00+09:00", "list": "index/raw-moved-20260901.txt"}],
  "created_at": "...", "updated_at": "...", "current_plan": 3,
  "last_checkin_at": "2026-09-01T18:00:00+09:00",  // この案件を最後に checkin した時刻（checkin ツール / kairn checkin / daily が更新）
  "last_checkin_events": 42                          // その時点の events.jsonl の行数（同上。open_case の checkout skip 判定に使う）
}
```
- `last_checkin_at`: `open_case` は、これより新しいローカル変更（`case.json` / `events.jsonl` / `worklog.md` / `plan/*.json` の mtime）が
  あれば Drive からの checkout を skip し `drive={"skipped": "local changes newer than last checkin"}` を返す（未 checkin の変更を Drive で上書きしない）。
  `events.jsonl` は、checkin 時点（`last_checkin_events` 行）以後に増えた行が kairn 自身の `checkin` / `checkout` event だけなら変更と数えない（`open_case` 自体が `checkout` event を追記するため）。
  未記録なら checkout する（`--update` なのでローカルの方が新しいファイルは上書きされない）。

### summary / elements / related / causal（抽出の下書きの適用先）
- `kairn/extract`（MCP `extract_card` / `kairn extract` / UI「下書きを取得」）は下書きを返すだけで case.json には書かない。
- UI の「この下書きを case.json に適用」（人の操作）が `title` / `summary` / `elements` / `related` / `causal` を**置き換え**、
  `{actor: human, action: decision, note: "applied extract draft", confidence}` を events に追記する。
- `elements` のキーは `machine` / `component` / `symptom` / `ticket` / `site` / `external`（`kairn/extract/schema.json`）。手で作った案件は一部のキーだけでもよい。
- `causal[]` の各要素は `{symptom, component, cause, evidence}`（すべて文字列）。UI の案件ページに「症状 → 部品 → 原因」として表示する。

### data[]（生データの所在。`kairn raw-move` / `kairn daily` が追記する）
- `drive`: 移動先（`<remote>:<root>/<ws>/cases/<case>/`。案件ディレクトリの相対構造をそのまま保つ）
- `files` / `bytes`: その回に移動した件数・容量。`moved_at`: ISO 8601
- `list`: 移動したファイルの一覧（ワークスペースの data_dir からの相対パス。1 行 `<case>/<相対パス>\t<bytes>\t<drive パス>`。同じ日の分は追記）
- 同じ内容を `worklog.md` の `## Data location` 節（無ければ末尾に作る）に 1 行（日付・件数・容量・Drive パス・復元コマンド）で書く。
  case.json の無いディレクトリでは `DATA.md` に同じ行を書く。events には `{"actor": "kairn", "agent": "sync", "action": "progress", "data": {…}}` を追記する。
- 復元: `rclone copy <drive><file> <案件ディレクトリ>/`

## plan/vNNNN.json（計画の版）
```json
{
  "version": 3, "created_at": "...", "actor": "ai", "reason": "指摘: unit-6 でも確認する",
  "objective": "…",
  "tasks": [
    {"id": "T012", "title": "…", "owner": "ai", "status": "open",
     "carried_from": "v0002", "evidence": []},
    {"id": "T015", "title": "…", "owner": "human", "status": "open"}
  ]
}
```
- 新版を作ると、**旧版の open タスクのうち新版に `carried_from` で引き継がれなかったものは
  自動的に `superseded`（by vN）** になる。閉じる操作は存在しない。
- タスク ID は案件内で単調増加。同じ ID が版をまたいで引き継がれる。
- `status`: open | doing | blocked | done | dropped | superseded。`done` は `evidence` が空だと MCP が拒否する。
- `owner`: ai | human（それ以外は拒否）。新版の各要素は `title` か `carried_from` のどちらかが必須。同じタスクを 2 回 `carried_from` すると拒否。
- MCP `plan` で `done` のタスクを `carried_from` しなかった場合、そのタスクは旧版にだけ残る（superseded にはならないが新版の進捗には数えない）。
  UI のタスク追加は open/doing/blocked/done を全部引き継ぐ。

## events.jsonl（追記専用）
```json
{"t": "2026-08-20T15:10:00+09:00", "actor": "ai", "agent": "claude-code", "case": "CASE-123_…",
 "task": "T012", "action": "done", "note": "実装完了、PR #42",
 "evidence": [{"type": "commit", "repo": "acme-robot", "id": "abc1234"}, {"type": "pr", "id": 42}]}
{"t": "…", "actor": "human", "action": "sendback", "task": "T012", "note": "unit-6 でも確認"}
{"t": "…", "actor": "ai", "action": "plan", "plan": 3, "note": "指摘を受けて再計画"}
```
`action`: opened | plan | started | progress | done | dropped | sendback | comment | decision | checkin | checkout | status | extract
`actor`: ai | human | kairn（kairn の自動処理: raw-move・extract 等。UI では既定色）。extract の event は `agent: "extract:<name>"`、`note`（ok / 失敗理由）、`elapsed_sec`、`exit_code`、`timeout_sec` を持つ

## 証拠（evidence）の型
`type` は commit / pr / file / test / url。`note` は actor=human のみ（AI の証拠にはならない）。型ごとの必須キー:
commit → `id`（`repo` 任意）/ pr → `id`（`repo` 任意）/ file → `path` / test → `cmd`（`result` 任意）/ url → `url` / note → `text`。
不正な型・必須キー欠落は `update_task` / `log_event` とも拒否する（`kairn/store.py` `validate_evidence`）。
