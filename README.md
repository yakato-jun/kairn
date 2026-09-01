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
contrib/systemd/       systemd user unit（kairn-serve.service: MCP+UI 常駐 / kairn-daily.service + .timer: 日次同期）
contrib/opencode/agents/kairn-extract.md  OpenCode 用の読み取り専用エージェント定義（extract の opencode アダプタが `--agent kairn-extract` で使う）
workspaces/<ws>/       データ実体（.gitignore、Drive 同期）
  cases/<case>/        worklog.md、作業ファイル、plan/、events.jsonl
  index/               各環境で再生成する索引（kairn.sqlite）、drive-index.txt、daily.log、raw-moved-YYYYMMDD.txt
skills/kairn/SKILL.md  各エージェント共通の運用手順（`kairn install-skill` が ~/.agents/skills/kairn と ~/.claude/skills/kairn からリンクする）
docs/                  データモデル・MCP ツール・UI の仕様
```

## CLI（`kairn --help`）

設定ファイル `~/.config/kairn/config.yaml` はこれらのコマンドが書く（人は編集しない）。`<ws>` 省略時は cwd が属するワークスペース。

```
kairn setup --remote <rclone remote> [--agent claude|codex|opencode|antigravity] [--extract-timeout <sec>]   # 使う remote（これ以外は使わない）と抽出エージェント（--agent / --extract-timeout 省略時は既存値を保つ）
kairn ws list | kairn ws create <name> [--description "…"]   # ワークスペース一覧（Drive 上の有無つき）／作成
kairn attach <ws> [<repo path>...]        # リポジトリを所属させ <repo>/<link_name>（既定 tmp）を cases/ へのリンクにする（省略時は cwd）
kairn detach [<repo path>]                # 所属を外す（glob: 由来なら exclude: を書く）
kairn status                              # 設定・cwd の所属・各ワークスペースの案件数とリンク状態
kairn cases [<ws>] [--all]                # 案件一覧（既定は open のみ）
kairn new <case id> "<title>" [--ws <ws>] # 案件を作る（case.json）
kairn checkout <ws> [<case>] [--dry-run]  # Drive → ローカル（rclone copy --update）＋索引更新
kairn checkin <ws> [<case>] [--dry-run]   # ローカル → Drive（案件単位は rclone sync、ワークスペース全体は rclone copy。last_checkin_at 更新）
kairn index <ws> [--full]                 # 索引（SQLite FTS5）の差分再生成（--full で全部）
kairn drive-index <ws>                    # Drive 上の全ファイル一覧を index/drive-index.txt に
kairn bag2zst <ws> [<case>] [--dry-run]   # *.bag / *.bag.active を zstd 圧縮
kairn raw-move <ws> [<case>] [--dry-run]  # 生データを Drive へ移動し所在を記録
kairn daily <ws> [--dry-run]              # bag2zst → checkin → raw-move → drive-index → index
kairn extract <case> [--ws <ws>] [--agent …] [--json]   # 子エージェントで case.json の下書き（書き込まない）
kairn serve [--host 127.0.0.1] [--port 8765]            # MCP（/mcp）＋ UI（/ui）
kairn install-skill [--home <dir>]        # skills/kairn を ~/.agents/skills と ~/.claude/skills からリンク
```

## 各エージェントへの適用

### 1. サーバーを常駐させる（`kairn serve`）

MCP（`http://127.0.0.1:8765/mcp`）と UI（`http://127.0.0.1:8765/ui`）は同じプロセス。systemd user service で常駐させる:

```
cp contrib/systemd/kairn-serve.service ~/.config/systemd/user/
# clone 先が %h/kairn でなければ ExecStart の %h/kairn を書き換える
systemctl --user daemon-reload && systemctl --user enable --now kairn-serve.service
systemctl --user status kairn-serve.service; journalctl --user -u kairn-serve
```

### 2. skill を置く（Claude Code / Codex / OpenCode 共通の SKILL.md）

```
kairn install-skill      # ~/.agents/skills/kairn と ~/.claude/skills/kairn を skills/kairn へのシンボリックリンクにする（既存があれば上書きせず報告）
```

### 3. MCP を登録する

| エージェント | skill の読み込み元 | MCP 登録 |
|---|---|---|
| Claude Code | `~/.claude/skills/kairn` | `claude mcp add --transport http kairn http://127.0.0.1:8765/mcp -s user` |
| Codex CLI | `~/.agents/skills/kairn`（`$kairn` で明示起動） | `~/.codex/config.toml` に `[mcp_servers.kairn]` / `url = "http://127.0.0.1:8765/mcp"` |
| OpenCode | `~/.claude/skills` / `~/.agents/skills` を自動で読む | `~/.config/opencode/opencode.json` に `"mcp": {"kairn": {"type": "remote", "url": "http://127.0.0.1:8765/mcp", "enabled": true}}` |

Codex の `~/.codex/config.toml`:
```toml
[mcp_servers.kairn]
url = "http://127.0.0.1:8765/mcp"
```

OpenCode の `~/.config/opencode/opencode.json`（プロジェクト直下の `opencode.json` でも可。書式は https://opencode.ai/docs/mcp-servers/ ）:
```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "kairn": {"type": "remote", "url": "http://127.0.0.1:8765/mcp", "enabled": true}
  }
}
```

### 4. OpenCode で extract を使う場合（`extract.agent: opencode`）

読み取り専用のエージェント定義 `contrib/opencode/agents/kairn-extract.md` を `~/.config/opencode/agents/` に置く
（書式は https://opencode.ai/docs/agents/ 。`tools:` は deprecated のため `permission:` で read / grep / glob / list 以外を deny、`external_directory: deny` で cwd 外を拒否）:
```
mkdir -p ~/.config/opencode/agents && cp contrib/opencode/agents/kairn-extract.md ~/.config/opencode/agents/
```
未配置だと opencode の extract は失敗し `ok=false` になる。

### 5. リポジトリ側に書くこと（CLAUDE.md / AGENTS.md）

紐付けの正は各環境の `~/.config/kairn/config.yaml`（`kairn attach` が書く）。リポジトリには所属ワークスペースと入口だけを書く:

```
## 作業ログ（kairn）
このリポジトリは kairn ワークスペース `acme` に属する。案件（case）の作業は kairn skill の手順に従う
（MCP `kairn` の open_case で開き、done は証拠付きで update_task、終わったら checkin）。案件ディレクトリは `tmp/<case>/`（`kairn attach` が張るリンク）。
```

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
- `checkin <ws> <case>`（MCP の `checkin(case)` も）は `rclone sync`: 案件内の削除を追従し、Drive 側に新しい版があっても `_deleted/<日付>/` に退避して上書きする。**他環境で作業した後は先に `checkout` する**。
- `checkin <ws>`（案件指定なし。`daily` が毎日呼ぶ）は `rclone copy`: ローカルに無い案件ディレクトリを Drive から消さない（原則 2「ローカルの案件ディレクトリは消してよい」）。上書きされる Drive 側の版は同じく `_deleted/` へ。
- `open_case` の checkout skip 判定は人／AI の実質的な変更だけを見る: `events.jsonl` が checkin 時点（`case.json.last_checkin_events` 行）以後に kairn 自身の `checkin` / `checkout` event で伸びただけなら変更と数えない。
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
タイムアウトは `extract.timeout`（秒、既定 600。`kairn setup --extract-timeout <sec>`）。
子プロセスは**案件ディレクトリの写し**（一時ディレクトリ。自案件の全体＋同じワークスペースの兄弟案件の `case.json` だけ）を cwd に、読み取り専用オプション・最小限の環境変数で起動し、出力は `kairn/extract/schema.json` で検証する（ワークスペース境界はファイルシステムで切る。docs/extract-agents.md）。
**下書きは書き込まない**。適用は UI の案件ページ「下書きを取得」→ 差分を見て「この下書きを case.json に適用」（人の操作）だけ。

```
kairn extract <case> [--ws <ws>] [--agent claude|codex|opencode|antigravity] [--json]   # 下書きを表示（失敗は exit 1）
```
MCP からは `extract_card(case)`（失敗も `ok=false` の結果として返す）。毎回 events に `{actor: kairn, agent: "extract:<name>", action: extract}` が残る。

## 状態

段階 1（config / store / index / server(MCP, mcp 2.x) / ui）完了（2026-09-02）。段階 5（sync: bag2zst / raw-move / daily / systemd timer）完了（2026-09-02）。
段階 7（extract: MCP `extract_card` / `kairn extract` / UI の取得・適用）完了（2026-09-02）。
段階 6（skill の最終化、`kairn install-skill`、各エージェントの MCP 登録手順、opencode agent、`kairn-serve.service`）完了（2026-09-02）。
実装順は docs/roadmap.md（8 の既存 worklog の移行は別件）。
