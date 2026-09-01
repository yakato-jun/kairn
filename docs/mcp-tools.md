# MCP ツール（10 個以内。説明文は短く）

すべて `workspace` を引数に取るか、呼び出し元 cwd（対応表）から一意に決める。既定で他ワークスペースを見ない。

| ツール | 役割 | 実装で強制する規則 |
|---|---|---|
| `open_case(case)` | 案件を開く: Drive から取り寄せ → case.json・最新 plan・open タスク・直近 events・関連案件・worklog 末尾を **1 回で**返す | 取り寄せ失敗時はローカル写しで続行し、その旨を返す |
| `list_cases(status?, query?)` | 案件一覧（進捗 = done/全、最終イベント） | |
| `plan(case, objective, tasks[], reason)` | 計画の新版を作る | 新版に無い open タスクを superseded にする。版番号は自動 |
| `update_task(case, task, status, evidence[], note)` | タスク状態の更新＋event 追記 | `done` は evidence 必須。存在しない task は拒否 |
| `log_event(case, action, note, evidence[])` | 進捗・決定・コメントの追記 | actor/agent を自動付与 |
| `search(query, cases?)` | worklog 節単位の全文検索（＋front matter/case.json） | ワークスペース内のみ |
| `find_cases(query)` | 問い → 関係する案件を理由付きで上位 k 件 | related / elements / 全文の複合 |
| `checkin(case)` | ローカル → Drive へ戻す（同期）＋ event | |
| `drive_index(pattern)` | Drive 上のファイル一覧を検索（生データの所在） | |
| `extract_card(case)` | `claude -p` を子プロセスで起動し case.json の下書き（elements/related/要約）を返す | 読み取り専用・MCP 無効・スキーマ固定。書き込まない |

server instructions（各エージェントに表示される要約）:
「案件を開くときは open_case。作業したら log_event / update_task（done は証拠必須）。方針が変わったら plan で計画を出し直す。終わったら checkin。」
