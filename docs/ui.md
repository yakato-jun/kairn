# UI（ローカル Web、localhost / Tailscale 内のみ）

**Host の許可リスト検査は未実装**（DNS リバインディング対策なし）。localhost / Tailscale 内からだけ到達できる前提で運用する（`kairn serve --host` を公開アドレスにしない）。

人が操作するのはここだけ。データは MCP と同じファイル（`workspaces/<ws>/cases/<case>/`。エージェント側は `open_case` の `paths.case_dir` で同じ場所を読み書きする）。実装: `kairn/ui.py`。

実装は **Starlette（FastAPI の基盤。MCP SDK の ASGI アプリと同じ Starlette に同居）＋ 素の HTML**。外部依存を増やさない。
`server.build_app()` が `/`（→ `/ui`）、`/ui…`（UI）、`/mcp`（MCP）を 1 つのアプリに載せる。UI のルートは Mount ではなく
`ui_routes(conf, "/ui")` で直接登録する（Mount 配下の `/` は末尾スラッシュ無しの `/ui` で 404 になるため）。

## 画面

- **一覧** `GET /ui[?status=open|closed|suspended|all][&element=<値>][&ws=<名>]`
  ワークスペース内の案件: status、進捗バー（done/全、計画の版）、鮮度、Drive 列、最終イベント、担当 AI の最終動作（actor=ai の最終イベントの agent/action）。
  elements の値をタグ表示し、クリックで絞り込み（横断）。case.json の無いディレクトリは件数だけ表示。
  - **Drive 列**（`sync.drive_state`。直近に取得した manifest のキャッシュ `index/manifest.cache.json` との比較。取得時刻を表の上に出す）:
    同期済み（rev 一致）/ Drive の方が新しい（rev が違う）/ ローカル未 checkin（`last_checkin_at` より新しいローカル変更。ファイル名と、Drive 側も
    違えば「Drive も更新あり」）/ 不明（manifest 未取得・エントリ無し・未 checkin）。title に rev / Drive 側の rev・checkin 元ホストと時刻。
  - **「更新確認」ボタン**（`POST /ui/refresh`、`ws` = 表示中のワークスペース。空なら全部）: Drive の manifest.json を取得してキャッシュを更新し
    一覧に戻る（`?refreshed=<結果>`）。案件は取り寄せない（取り寄せは `open_case` / `kairn checkout`）。rclone は threadpool で実行。
- **案件** `GET /ui/<ws>/<case>`
  - 進行中のジョブ（MCP の `checkin` / `open_case` の取り寄せ。`kairn/jobs.py` の表を MCP と共有）: 種類・状態（running / queued。
    queued は同じ案件の先行ジョブ待ち）・経過秒・job_id・rclone の最新の進捗行。走っているものがある時だけ、見出しの直下に出す（自動更新はしない。再読み込み）。ジョブ一覧のページは無い
  - かんばん（open / doing / blocked / done）。カードに owner・最終動作時刻・証拠・差し戻しフォーム
  - 計画の版履歴（各版の reason / objective / タスクと状態、`carried_from` と、どの版で何が superseded になったか）
  - データ所在（case.json の `data[]`: Drive パスと復元コマンド `rclone copy <remote>:<path> <case_dir>/`）
  - summary（あれば objective の下）、症状 → 部品 → 原因（case.json の `causal[]`。あれば）
  - worklog.md（折りたたみ。全文）
  - 時系列（events の直近 100 件、新しい順。actor で色分け（human / ai。kairn 等その他は既定色）、agent、task、note、証拠）
  - related（存在する案件はリンク）、elements（タグ。クリックで一覧の絞り込み）

- **設定** `GET /ui/settings`（各ページのヘッダ右の「設定」リンク）
  同期・退避規則 `rules` の現在値（`raw_data.min_size` / `raw_data.min_age` / `bag_to_zst` / `bwlimit`、`exclude` の一覧、
  `raw_data.extensions` の一覧）と、CLI の `kairn rules …` と同じ操作のフォーム。設定ファイル（`~/.config/kairn/config.yaml`）は
  kairn が書き、人は手で編集しない。保存後は `?saved=<メッセージ>` 付きで同じページに戻る（303）。案件の event には記録しない。

## 操作（すべて event として記録。AI は次に open_case した時に `human_feedback` で受け取る）

| 操作 | POST | 記録 |
|---|---|---|
| 差し戻し | `/ui/<ws>/<case>/sendback` `task, note` | `{actor: human, action: sendback, task, note}`。存在しない task は 400 |
| コメント／指示 | `/ui/<ws>/<case>/comment` `note` | `{actor: human, action: comment, note}`。空は 400 |
| タスク追加 | `/ui/<ws>/<case>/task` `title, owner` | 生きているタスク（open/doing/blocked/done）を全部引き継いだ**計画の新版**＋追加（actor=human）。superseded は出ない |
| 案件の close/suspend | `/ui/<ws>/<case>/status` `status, note` | case.json の status 更新＋ `{actor: human, action: status}` |
| 下書きを取得 | `/ui/<ws>/<case>/extract` | `extract.extract_card` を threadpool で実行し（子プロセス待ちの間も同じプロセスの MCP を止めない）、結果画面（agent・所要時間・ok/失敗理由、現在の case.json と下書きの対比、症状→部品→原因、下書き JSON）を返す（200、リダイレクトしない）。下書きの `related` にワークスペースに実在しない案件 ID があれば `related_unknown` として ⚠ 印を付ける（適用しても related に入らない）。case.json は書かない。`{actor: kairn, agent: extract:<name>, action: extract}` |
| この下書きを case.json に適用 | `/ui/<ws>/<case>/apply` `card`（下書き JSON。結果画面の hidden） | `related_unknown` を捨てて schema.json で再検証し、title / summary / elements / related / causal を置き換え＋ `{actor: human, action: decision, note: "applied extract draft"}`。不正な JSON・スキーマ不一致は 400 |

| 更新確認（一覧） | `/ui/refresh` `ws`（空なら全ワークスペース） | `sync.refresh_manifest`（rclone cat 1 回、10 秒）でキャッシュを更新し 303 で `/ui?ws=…&refreshed=<ws: manifest N case(s) \| manifest unavailable>` へ。未知の ws は 404。event は書かない |
| 設定: 単一値 | `/ui/settings/set` `key, value` | `config.set_rule`（`raw_data.min_size` は `sync.parse_size`、`raw_data.min_age` は `parse_age`、`bag_to_zst` は true/false、`bwlimit` は `parse_bwlimit` で検証。`off` はキーを消す）→ `Config.save()`。不正な値・未知の key は 400 で保存しない。event は書かない |
| 設定: exclude の追加／削除 | `/ui/settings/add-exclude` / `remove-exclude` `pattern` | `rules.exclude` に足す（重複は no-op）／外す（無ければ 400）→ 保存 |
| 設定: 生データ拡張子の追加／削除 | `/ui/settings/add-raw-ext` / `remove-raw-ext` `ext` | `rules.raw_data.extensions` に足す（先頭の `.` は外し小文字。重複は no-op）／外す（無ければ 400）→ 保存 |

フォームは `application/x-www-form-urlencoded`（UTF-8、percent-encoding）。日本語・記号は復号してそのまま記録する。成功時は 303 で案件ページへ戻る。
`owner` は ai | human 以外を 400 で拒否する。

**CSRF**: POST は `Sec-Fetch-Site` が `same-origin` / `none` 以外、または `Origin` / `Referer` のホストがリクエストの `Host` と異なれば 403（`ui.same_origin`）。
どちらのヘッダも無いリクエスト（curl 等）は通す。

**不正な入力**: 未知のワークスペース／案件、不正な案件 ID（`..`、`.` 始まり等）は 404。一覧の `status` は open/closed/suspended/all 以外を open に正規化する（値を HTML に反射するため）。
優先度の操作はデータモデルに項目が無いため未実装（要判断: 段階 1 で追加していない）。

## 鮮度

open / doing / blocked のタスクごとに「そのタスクの最終イベント（無ければタスクの作成・更新時刻）からの経過日数」を表示する。
**7 日超**（`ui.STALE_DAYS`）は赤字＋⚠ とカードの左罫線で目立たせる。一覧では案件内の最も古い open タスクの経過を出す。自動では消さない。
