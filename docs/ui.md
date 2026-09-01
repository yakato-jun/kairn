# UI（ローカル Web、localhost / Tailscale 内のみ）

人が操作するのはここだけ。データは MCP と同じファイル。

- 一覧: ワークスペース内の案件（status, 進捗バー = done/全, 最終イベント, 担当 AI の最終動作）
- 案件: かんばん（open / doing / blocked / done）、時系列（events、証拠へのリンク）、計画の版履歴
  （どの版で何が superseded になったか）、worklog の該当節、データ所在（Drive パス・復元コマンド）
- 操作: タスク追加・差し戻し（sendback）・コメント・優先度・案件の close/suspend。すべて event として記録され、
  AI は次に open_case した時に受け取る
- 鮮度: open タスクの最終イベントからの経過を表示。一定期間動きが無いものを目立たせる（自動では消さない）
- 横断: related で繋がる案件、elements で絞り込み

実装: FastAPI（MCP と同居）＋ 素の HTML/JS。外部依存を増やさない。
