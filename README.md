# kairn（ケルン）

作業ログ（worklog）を **案件（case）** 単位で保存・同期・探索し、AI エージェントの作業を
人が画面から管理するための基盤。MCP サーバー＋ローカル Web UI＋各エージェント共通の skill。

## 設計原則（2026-09-01 の議論で確定）

1. **ワークスペース＝顧客・会社単位**。保存先・索引・抽出はワークスペースの境界を越えない。
   同じ会社の複数リポジトリは 1 つのワークスペースを共有する（案件はリポジトリに属さない）。
2. **Drive が正本、ローカルは写し**。使う rclone remote は `kairn setup` で利用者が指定し、以後その remote 以外は使わない（顧客側のアカウント等を誤って使わないため）。
   ローカルの案件ディレクトリは消してよく、必要になれば取り寄せる。
3. **人はファイルを触らない**。人が触るのは UI（かんばん・時系列・差し戻し）。ファイルは
   AI と kairn が読み書きする。worklog.md は AI が書く経緯・調査・決定の文章。
4. **タスクは「計画の版」に属する**。計画を出し直すと、新版に載らなかった open タスクは
   自動的に `superseded` になる。閉じる操作を人にも AI にも要求しない。
5. **AI の done には証拠が必須**（コミット・PR・ファイル・テスト結果）。MCP が拒否する。
6. **判断ロジックは MCP 側に置く**。エージェント側の skill は「いつ何を呼ぶか」だけの薄い手順。
   エージェント（Claude Code / Codex / OpenCode）の差で運用が崩れないようにする。
7. **LLM 抽出は文脈隔離して行う**（`claude -p` を子プロセスで、読み取り専用・MCP 無効）。
   結果は下書きであり、確定は人。自動で知見を書き込まない。
8. **自動同期しないものを明示する**：ビルド産物・シンボリックリンク。生データ（bag/zst 等・50MB 超）は
   一定期間後に Drive へ移動し、所在を案件に記録する。
9. 既存 worklog の移行は別件。今後の運用を主軸に置く。

## ディレクトリ

```
kairn/                 パッケージ（config / store / index / server(MCP) / ui / sync / extract）
kairn/extract/         案件カードの下書き抽出: prompt.md（子エージェントへの指示）/ schema.json（出力の JSON Schema）/ adapters.py（claude / codex / opencode / antigravity）
config/config.example.yaml  環境ローカル設定の書式例（架空名）。実体は ~/.config/kairn/config.yaml（kairn のコマンドが書く。コミットしない）
contrib/systemd/       日次同期の systemd user unit（kairn-daily.service / .timer）
workspaces/<ws>/       データ実体（.gitignore、Drive 同期）
  cases/<case>/        worklog.md、作業ファイル、plan/、events.jsonl
  index/               各環境で再生成する索引（kairn.sqlite）、drive-index.txt、daily.log、raw-moved-YYYYMMDD.txt
skills/kairn/SKILL.md  各エージェント共通の運用手順（~/.agents/skills/kairn へ配置、~/.claude/skills はリンク）
docs/                  データモデル・MCP ツール・UI の仕様
```

## 各エージェントへの適用

| | 置き場 | MCP 登録 |
|---|---|---|
| Claude Code | `~/.claude/skills/kairn` → `~/.agents/skills/kairn` へのリンク | `claude mcp add --transport http kairn http://127.0.0.1:<port>/mcp` |
| Codex CLI | `~/.agents/skills/kairn`（`$kairn` で明示起動） | `~/.codex/config.toml` `[mcp_servers.kairn] url=…` |
| OpenCode | `~/.claude/skills` / `~/.agents/skills` を自動で読む | `opencode.json` `"mcp": {"kairn": {"type":"remote","url":…}}` |

リポジトリ側の CLAUDE.md / AGENTS.md には「このリポジトリは kairn ワークスペース `<ws>` に属する」
の数行だけを書き、対応表は各環境の `~/.config/kairn/config.yaml`（`kairn attach` が書く）を正とする。

## 同期（sync）

テキスト層（worklog / case.json / plan / events 等）は `checkout` / `checkin` で Drive と往復する。生データ層
（`rules.raw_data`: 拡張子が bag/zst/… **または** `min_size` 超、かつ更新から `min_age` 超）はテキスト層の同期から除外し、
`raw-move` で Drive へ**移動**（ローカルから削除）して所在を案件に記録する。

```
kairn bag2zst <ws> [<case>] [--dry-run]   # *.bag / *.bag.active → <name>.zst（zstd -T0 -6、検証後に置換、mtime 引き継ぎ。30 分以内に更新されたものは対象外）
kairn raw-move <ws> [<case>] [--dry-run]  # rclone move → <remote>:<root>/<ws>/cases/<case>/…。移動後に case.json.data[]、worklog.md の
                                          #   「## Data location」（case.json が無ければ DATA.md）、index/raw-moved-YYYYMMDD.txt、progress event に記録
kairn daily <ws> [--dry-run]              # bag2zst → checkin → raw-move → drive-index → index を順に実行。段が失敗しても次へ進み、index/daily.log に記録
```

- 所在の記録先: `worklog.md` があればその `## Data location` 節、無ければ `DATA.md`（case.json の有無に関わらず）。case.json があれば `data[]` と progress event にも記録。
- 復元: `rclone copy <remote>:<root>/<ws>/cases/<case>/<file> <案件ディレクトリ>/`（Data location の行と UI に表示）。
- `checkout` は `rclone copy --update`（ローカルの方が新しいファイルは上書きしない）。`open_case` が毎回 checkout するため。
  `open_case` は `case.json.last_checkin_at`（checkin が更新）より新しいローカル変更があれば checkout を skip する。
- `checkin` は `rclone sync`: Drive 側に新しい版があっても `_deleted/<日付>/` に退避して上書きする。**他環境で作業した後は先に `checkout` する**。
- `daily --dry-run` は rclone に `--dry-run` を渡し、`drive-index.txt` と索引（`kairn.sqlite`）を書き換えない。
- 帯域制限は `rules.bwlimit`（例 `"08:00,4M 20:00,off"`。rclone の `--bwlimit` にそのまま渡す）。
- 日次実行（systemd user timer、毎日 12:30 ± 10 分、停止中だった分は次回起動時に実行）:
  ```
  cp contrib/systemd/kairn-daily.{service,timer} ~/.config/systemd/user/
  sed -i 's/<workspace>/acme/' ~/.config/systemd/user/kairn-daily.service   # 自分のワークスペース名に（clone 先が %h/kairn でなければ ExecStart も書き換える）
  systemctl --user daemon-reload && systemctl --user enable --now kairn-daily.timer
  systemctl --user list-timers kairn-daily.timer; journalctl --user -u kairn-daily
  ```

## 抽出（extract）

案件カード（case.json）の下書き（title / summary / elements / related / 症状→部品→原因）を、**文脈隔離した子エージェント**が案件ディレクトリを読んで作る
（docs/extract-agents.md）。使うエージェントは `~/.config/kairn/config.yaml` の `extract.agent`（`kairn setup --agent`）: claude / codex / opencode / antigravity。
子プロセスは案件ディレクトリを cwd に、読み取り専用オプション・最小限の環境変数で起動し、出力は `kairn/extract/schema.json` で検証する。
**下書きは書き込まない**。適用は UI の案件ページ「下書きを取得」→ 差分を見て「この下書きを case.json に適用」（人の操作）だけ。

```
kairn extract <case> [--ws <ws>] [--agent claude|codex|opencode|antigravity] [--json]   # 下書きを表示（失敗は exit 1）
```
MCP からは `extract_card(case)`（失敗も `ok=false` の結果として返す）。毎回 events に `{actor: kairn, agent: "extract:<name>", action: extract}` が残る。

## 状態

段階 1（config / store / index / server(MCP, mcp 2.x) / ui）完了（2026-09-02）。段階 5（sync: bag2zst / raw-move / daily / systemd timer）完了（2026-09-02）。
段階 7（extract: MCP `extract_card` / `kairn extract` / UI の取得・適用）完了（2026-09-02）。skill 配置（6）と各 CLI の登録手順は未着手。実装順は docs/roadmap.md。
