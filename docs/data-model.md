# データモデル

すべてワークスペース配下のファイル。人は読まない前提（UI と MCP が読み書き）。Drive 同期に耐えるよう、
1 レコード 1 ファイルか追記専用 JSONL にする。SQLite の索引は各環境で再生成する派生物。

```
workspaces/<ws>/
  cases/<case_id>/
    case.json            案件の正本（下記）
    worklog.md           AI が書く経緯・調査・決定（従来の worklog。Tasks 節は持たない）
    plan/v0001.json …    計画の版（下記）。最新版が「今やること」
    events.jsonl         追記専用のイベント（下記）。checkout / checkin で Drive 版と行の和集合にマージされる
    .rev/<rev>           版マーカー（空ファイル 1 個。名前 = case.json の rev。下記）。Drive の同じ場所にも同期される
    …                    作業ファイル（MMdd_hhmm_ prefix 等、従来どおり）
  index/kairn.sqlite     索引（case / plan / task / event / section の検索用）。再生成可
  index/drive-index.txt  Drive 上の全ファイル一覧（path, size, mtime）。`kairn drive-index` / `kairn daily` が生成
  index/access.log       open_case の閲覧記録（1 行 `<時刻>\t<案件>\t<agent>`。他 ws の案件からの跨ぎ参照は `\tcross_from=<ws>/<case>\ttool=<tool>` 付き）。ローカルのみ、Drive に同期しない
  index/drive_revs.cache.json  直近に読んだ Drive の版（`{"revs": {case: rev | null}, "fetched_at"}`）。list_cases / UI 一覧の印に使う。ローカルのみ
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
  "related": ["CASE-100", "beta/CASE-7"],   // 人／AI が明示的に書く関係（抽出に頼らない）。同 ws の案件 ID か "<ws>/<case>"（他 ws。下記「跨ぎ参照」）
  "elements": {"machine": ["unit-2"], "component": ["acme-plc"], "symptom": ["起動時に driver init 未完了"]},
  "summary": "起動直後に driver init が終わらない。UART の送信量超過が原因。",   // 3 行以内（extract の下書きを人が適用した時に入る）
  "causal": [{"symptom": "起動時に driver init 未完了", "component": "acme-plc", "cause": "UART 460800 で送信量が超過",
              "evidence": "## Notes: UART 460800 で送信量が超過する"}],      // 症状→部品→原因。evidence は worklog の見出し／行の引用
  "data": [{"drive": "my-drive:ws/acme/cases/CASE-123/", "files": 3, "bytes": 1234567890,
            "moved_at": "2026-09-01T12:30:00+09:00", "list": "index/raw-moved-20260901.txt"}],
  "created_at": "...", "updated_at": "...", "current_plan": 3,
  "last_checkin_at": "2026-09-01T18:00:00+09:00",  // この案件を最後に checkin した時刻（checkin ツール / kairn checkin / daily が更新）
  "last_checkin_events": 42,                         // その時点の events.jsonl の行数（同上。マージ後の行数。open_case の checkout skip 判定に使う）
  "rev": "6f1c2b4e-…",                               // 版マーカー（uuid4）。checkin のたびに振り直す。同じ名前の空ファイルが .rev/ に置かれる
  "checked_in_from": "my-laptop"                     // その checkin をしたホスト名（socket.gethostname()）
}
```
- `rev` / `checked_in_from`: すべての checkin 経路（MCP `checkin`、`kairn checkin`、`daily`）が**転送の前に**書き、`.rev/` を作り直してから転送する
  （Drive に置く case.json と `.rev/<rev>` に同じ `rev` が入る）。転送が失敗したら書く前の内容に戻す（`.rev/` も）。
  `kairn checkin <ws>`（案件指定なし。`daily`）は `last_checkin_at` より新しいローカル変更がある案件と未 checkin の案件だけに書く
  （内容の変わらない案件の `rev` を毎日変えて他環境に取り寄せさせない。作業ファイルだけが増えた案件は対象にならない）。
  `open_case` はローカルの `rev` と Drive の `.rev/` の名前が同じなら取り寄せを省略する（docs/mcp-tools.md）。
- `last_checkin_at`: `open_case` は、これより新しいローカル変更（`case.json` / `events.jsonl` / `worklog.md` / `plan/*.json` の mtime）が
  あれば Drive からの checkout を skip し `drive={"skipped": "local changes newer than last checkin"}` を返す（未 checkin の変更を Drive で上書きしない）。
  `events.jsonl` は、checkin 時点（`last_checkin_events` 行）以後に増えた行が kairn 自身の `checkin` event だけなら変更と数えない
  （`checkin` ツールは行数を記録した後に `checkin` event を 1 行追記する）。`open_case` は `events.jsonl` に書かない（閲覧記録は `index/access.log`）ので、
  checkin 後に繰り返し開いても skip にならない。未記録なら checkout する（`--update` なのでローカルの方が新しいファイルは上書きされない）。
  checkout で Drive 版の行がマージされ `events.jsonl` が変わった場合は、次の checkin までローカル変更として扱われる（skip）。

## 版マーカー（`cases/<case>/.rev/<rev>`）
```
<remote>:<root>/<ws>/cases/CASE-123_widget-boot-failure/.rev/6f1c2b4e-…     空ファイル。名前が版（case.json の rev）
```
- **版は案件フォルダ内のマーカーファイルで持ち、ワークスペース単位の集計ファイルは置かない。** 一覧が答えるのは版だけで、内容（checkin 時刻・
  ホスト名・タイトル等）は `case.json` にある（`last_checkin_at` / `checked_in_from`）。
- 規則: `.rev/` にはマーカーが **1 個だけ** ある。名前 = `case.json.rev`。中身は空。`rev` を付け替えるたび（checkin、`drive-markers`）に
  `.rev/` を空にして作り直す。checkin は転送直前にも `case.json` の `rev` から作り直してから転送し（案件単位の `rclone sync` が Drive 側の古い
  マーカーを消す。ワークスペース全体の `rclone copy` の後は振り直した案件の `.rev/` だけを `rclone sync` で揃える）、checkout（`rclone copy --update`）
  の後も `case.json` の `rev` から作り直す（取り寄せた古いマーカーがローカルに残らない）。同期のフィルタは先頭で `.rev/**` を必ず含め、
  `rules.exclude` / `rules.raw_data` の除外や `raw-move` の対象にならない。
- 読み方: 1 案件は `rclone lsf <ws>/cases/<case>/.rev/`（`open_case`。10 秒でタイムアウト）、全案件は
  `rclone lsf -R --files-only --include '/cases/*/.rev/*' <ws>`（1 プロセス。`kairn checkout <ws>` / UI の「更新確認」/ `daily`）。
  名前だけを見る（ファイルは読まない）。マーカーが **2 個以上** ある案件は「不定」＝ `rev` 不一致と同じ扱い（取り寄せ対象）。マーカーが無い案件も
  取り寄せ対象（安全側）。`.rev/` が無い（directory not found）のは「マーカー無し」、それ以外の失敗・タイムアウトは「Drive が読めない」
  （`open_case` は取り寄せずローカル写しを返す）。
- 直近に読んだ版は `index/drive_revs.cache.json`（`{"revs": {case: rev | null}, "fetched_at"}`。`null` は不定、無い案件はマーカー無し）に置く。
  全案件を読んだ時に `fetched_at` を更新し、`open_case` の 1 案件の照会と checkin の完了は当該案件だけを更新する（`fetched_at` は変えない）。
- 印（`list_cases` の `drive.state` / UI 一覧）: `synced`（rev 一致）/ `drive_newer`（rev が違う、または不定）/ `local_changes`（未 checkin のローカル変更。
  `drive_differs` で Drive 側も違うか）/ `unknown`（キャッシュ無し・マーカー無し・未 checkin）。判定はキャッシュ時点のもの。
  `checked_in_at` / `from` はローカル `case.json` の `last_checkin_at` / `checked_in_from`。
- 既存の Drive データ（マーカーの無い案件）は `kairn drive-markers <ws>` で一度だけ、Drive の `case.json` の `rev` がローカルと一致する案件に
  マーカーを置く（README「同期」）。それ以外の案件は各環境の次の checkin でマーカーが付く。

### related（関係する案件）
- 要素は `"<case>"`（同じワークスペースの案件 ID）か `"<ws>/<case>"`（他ワークスペースの案件）。ワークスペース名・案件 ID とも
  `[A-Za-z0-9][A-Za-z0-9._-]{0,99}`（`..` 不可。`kairn/store.py` `parse_related` / `validate_related`）。`create_case` と extract の
  `apply_card` が検証し、不正な形は ValueError（MCP では `ToolError`）。実在しない案件を指してもよい（検証は形だけ）。
- 書く経路: AI は MCP `link_case`（`CaseStore.link_related`。重複なく追記、既存は保持。実在も検証する。他 ws を足せば `xref` も記録）、
  人は UI の「関連に追加」（`link_related`。形だけ）と関連欄の × （`unlink_related`）。**削除は人の操作のみ**（MCP に削除ツールは無い）。
  どちらも `{action: related, added | removed}` を events に書く（下記）。extract の適用（`apply_card`）は `related` を置き換える。
- `open_case` は各要素を `{ref, workspace, case, cross_workspace, exists, title, status}` に展開して返す。他ワークスペースの案件は
  **title と status だけ**（内容は開かない。開くなら `open_case(case, workspace=…, from_case=…)`）。UI の関連欄は実在する案件へリンクする。
- 他ワークスペースの案件から得た内容を worklog 等に書くときの出典はここに `<ws>/<case>` として残す（`link_case`。skills/kairn/SKILL.md）。

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

## 作業領域の除外（同期・退避・索引の共通規則）
案件フォルダの中に git worktree（`git worktree add <案件ディレクトリ>/wt …`）や作業用ディレクトリを置いて作業してよい。
次のいずれかの**ファイル**が直下にあるディレクトリは「作業領域」で、配下ごと同期（checkout / checkin）・退避（raw-move）・
bag2zst・全文索引の対象外になる（`kairn/sync.py` `WORKAREA_MARKERS` / `is_workarea`）:
- `.git` ファイル（git worktree では `.git` は `gitdir: …` を書いたファイル）
- `.kairn-nosync`（空ファイル。git 以外の作業領域の目印。手で置く）

同期は転送のたびにローカル側の転送ルート（案件ディレクトリ、ワークスペース全体なら `cases/`）を走査し（`workarea_dirs`。作業領域の配下・
シンボリックリンク・`rules.exclude` のディレクトリは辿らない）、見つけた各ディレクトリを rclone の `--filter '- /<dir>/**'` として
他の規則より先に付ける（rclone の `--exclude-if-present` は、同名のディレクトリ（通常の clone の `.git/`）がツリー内にあると転送全体が
失敗するため使わない）。checkout でもローカルの作業領域には書かない。索引（`kairn/index.py`）は同じ判定で配下を走査しない。

通常の clone（`.git` がディレクトリ）は作業領域にならない。`rules.exclude` の既定 `.git/**` で `.git/` だけが除かれ、ソース本体は
同期・索引される。リポジトリの写しを案件フォルダに置くときは worktree を使うか、その clone の直下に `.kairn-nosync` を置く。
成果物（worklog.md、調査メモ、ログの抜粋等）は作業領域の外＝案件直下に書く。

既存の設定ファイル（`~/.config/kairn/config.yaml`）に `.git/**` が無い場合、kairn は自動で足さない: `kairn rules add-exclude '.git/**'`。

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

## events.jsonl（追記専用・環境間でマージ）
追記専用ログを「新しい方で上書き」すると複数環境の行が失われるため、`checkout` / `checkin` の前に Drive 版を取り寄せて
**ローカル版と行の和集合**（行の文字列一致で重複除去）を `t` で安定ソートして書き戻す（`kairn/sync.py` `merge_events`）。
同時刻の行はローカルの行 → Drive にしか無い行の順。内容が変わらなければ書き戻さない（mtime を触らない）。
`open_case` はこのファイルに書かない（閲覧記録は `index/access.log`、ローカルのみ）。
```json
{"t": "2026-08-20T15:10:00+09:00", "actor": "ai", "agent": "claude-code", "case": "CASE-123_…",
 "task": "T012", "action": "done", "note": "実装完了、PR #42",
 "evidence": [{"type": "commit", "repo": "acme-robot", "id": "abc1234"}, {"type": "pr", "id": 42}]}
{"t": "…", "actor": "human", "action": "sendback", "task": "T012", "note": "unit-6 でも確認"}
{"t": "…", "actor": "ai", "action": "plan", "plan": 3, "note": "指摘を受けて再計画"}
{"t": "…", "actor": "human", "action": "status", "from": "open", "to": "closed", "note": "対応完了"}
{"t": "…", "actor": "ai", "agent": "claude-code", "action": "status", "from": "closed", "to": "open", "note": "この案件を再開して"}
```
`action`: opened | plan | started | progress | done | dropped | sendback | comment | decision | checkin | status | extract | xref | related
（`checkout` は旧版が `open_case` のたびに書いていた action。読めるが、もう書かない）
`opened`（案件の作成。`CaseStore.create_case`）は案件の最初の 1 行（`note: "case created: <title>"`）。人が作った場合（UI 無し。CLI `kairn new`）は
`actor: human`、AI が MCP `create_case` で作った場合は `actor: ai` と `agent`（誰が作った案件かを後から追える）。
`xref`（跨ぎ参照。`CaseStore.append_xref`）は**参照元**の案件の events に書く: `{actor: ai, agent, action: xref, workspace: <対象 ws>, case: <対象案件>, tool: open_case | search | find_cases | link_case | create_case}`
（`case` は他の event と違い**参照した相手**の案件 ID）。同じ対象（workspace, case）への参照は同じ日（JST）に 1 回だけ（ツールの違いは数えない）。
参照された側の events には書かない（対象 ws の `index/access.log` に `cross_from=` 付きで 1 行。ローカルのみ）。
`related`（`case.json.related` の変更。`CaseStore.link_related` / `unlink_related`）: 追記は `{actor, agent, action: related, added: [<ref>, …], note}`、
削除は `{actor: human, action: related, removed: [<ref>], note}`。MCP `link_case` は `actor: ai`（他 ws の案件を足したときは `xref`（`tool: link_case`）も 1 行）、
UI は `actor: human`。変化が無ければ（全部含まれている・無いものの削除）書かない。
`status`（案件のステータス変更。`CaseStore.set_case_status`）は `from` / `to`（open | closed | suspended）と `note` を持つ。人の操作（UI の「案件の状態を変更」・
CLI `kairn close | suspend | reopen`）は `actor: human`、MCP `set_case_status` は `actor: ai` で `note` に人の発言（`instruction`）そのもの。
同じステータスへの変更は event を書かない（case.json も触らない）。旧版が書いた `from` / `to` の無い `status` 行（`note: "closed: 理由"`）は読める。
`actor`: ai | human | kairn（kairn の自動処理: raw-move・extract 等。UI では既定色）。extract の event は `agent: "extract:<name>"`、`note`（ok / 失敗理由）、`elapsed_sec`、`exit_code`、`timeout_sec` を持つ

## 跨ぎ参照（ワークスペースをまたぐ参照）の記録
ワークスペース（顧客・組織）は保存先・索引・抽出の境界だが、**参照**はまたいでよい（docs/decisions.md 22）。記録は 2 か所:
- 対象ワークスペースの `index/access.log`: `<時刻>\t<対象案件>\t<agent>\tcross_from=<from ws>/<from 案件>\ttool=<open_case | search | find_cases>`
  （通常の閲覧行は 3 列。`search` / `find_cases` は他 ws のヒット案件ごとに 1 行）。ローカルのみ、同期しない。
- 参照元の案件の `events.jsonl`: 上の `xref` event（同期される。UI の時系列に「他 ws 参照: <ws>/<case>」で出る）。
記録されるのは MCP ツールに `from_case="<ws>/<case>"` が渡されたときだけ。`related` の展開（`open_case` の返り値）は参照に数えない。
`related` に他 ws の案件を入れる経路（MCP `link_case` / `create_case`）は閲覧ではないので `access.log` には書かず、`xref` event だけを残す。

## 証拠（evidence）の型
`type` は commit / pr / file / test / url。`note` は actor=human のみ（AI の証拠にはならない）。型ごとの必須キー:
commit → `id`（`repo` 任意）/ pr → `id`（`repo` 任意）/ file → `path` / test → `cmd`（`result` 任意）/ url → `url` / note → `text`。
不正な型・必須キー欠落は `update_task` / `log_event` とも拒否する（`kairn/store.py` `validate_evidence`）。
