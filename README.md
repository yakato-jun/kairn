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

## AI エージェント運用・リポジトリ公開の指針

本リポジトリの開発・運用における AI エージェント運用の基本指針や、実データ・固有名詞（顧客名・案件名）の排除、リモートリポジトリ公開ルールについては [docs/guidelines.md](docs/guidelines.md) を参照してください。

## ディレクトリ

```
kairn/                 パッケージ（config / store / index / server(MCP) / ui / sync / extract）
kairn/extract/         案件カードの下書き抽出: prompt.md（子エージェントへの指示）/ schema.json（出力の JSON Schema）/ adapters.py（claude / codex / opencode / antigravity）
config/config.example.yaml  環境ローカル設定の書式例（架空名）。実体は ~/.config/kairn/config.yaml（kairn のコマンドが書く。コミットしない）
kairn/service.py       systemd user unit のテンプレート（`kairn install-service` が生成・登録）と `kairn ensure`
contrib/opencode/agents/kairn-extract.md  OpenCode 用の読み取り専用エージェント定義（extract の opencode アダプタが `--agent kairn-extract` で使う）
workspaces/<ws>/       データ実体（.gitignore、Drive 同期）。案件の置き場はここだけ（リポジトリ側にはリンクも作らない）
  cases/<case>/        worklog.md、作業ファイル、plan/、events.jsonl。エージェントは open_case が返す paths.case_dir（絶対パス）で読み書きする
  index/               各環境で再生成する索引（kairn.sqlite）、drive-index.txt、daily.log、raw-moved-YYYYMMDD.txt
skills/kairn/SKILL.md  各エージェント共通の運用手順（`kairn install-skill` が ~/.agents/skills/kairn と ~/.claude/skills/kairn からリンクする）
docs/                  データモデル・MCP ツール・UI の仕様
```

## 導入

コマンドは `uv tool install` で入れる（`~/.local/bin/kairn`。専用の隔離環境に依存ごと入る）。開発用の `.venv`（`uv sync --group dev` → `pytest`）とは別物で、
片方を作り直してももう片方には影響しない。

```
git clone <このリポジトリ> ~/kairn && cd ~/kairn
uv tool install --editable . --python 3.13     # ~/.local/bin/kairn（--editable なので clone 先の変更がそのまま効く）
kairn setup --remote <rclone remote>           # 使う remote（これ以外は使わない）。rclone config create <name> drive scope=drive で先に作る（後述「専用 OAuth クライアント」を推奨）
kairn attach <ws>                              # 各リポジトリで。ワークスペースが無ければ kairn ws create <ws>
kairn install-skill                            # 各エージェントに skill を置き、workspaces/ の許可手順を表示
kairn install-service                          # systemd user service（常駐・日次同期）を生成・登録（後述）
```

- 更新: `cd ~/kairn && git pull && uv tool upgrade kairn`（`--editable` なので通常は `git pull` だけで反映される。依存が変わった時に upgrade）。
  常駐中の `kairn serve` は古いコードのまま動いているので `systemctl --user restart kairn-serve.service`（「設定の反映と再起動」）
- 削除: `uv tool uninstall kairn`
- 開発（テスト）: `uv sync --group dev && .venv/bin/pytest`。`.venv/bin/kairn` も同じ CLI だが、常駐 unit には `install-service` を実行した側の `kairn` のパスが入る

### 専用 OAuth クライアント（推奨）

rclone の Google Drive バックエンドはファイルごとに Drive API を呼ぶ。`rclone config create … drive` だけで作った remote は
rclone 共有の client_id を使い、全 rclone 利用者で API レートを分け合うため、小ファイル多数の案件では転送が極端に遅い
（実測: 1.3 MiB・多数の小ファイルで約 5 分）。rclone 公式も自前の OAuth client_id を推奨している（ https://rclone.org/drive/#making-your-own-client-id ）。
`kairn setup` / `kairn status` は remote が `type = drive` で `client_id` が無いと「共有 client_id のため Drive API が絞られます」と警告する
（`rclone config show <remote>` の `type` と `client_id` だけを見る。token 等の値は読まない）。

1. Google Cloud のプロジェクトで Drive API を有効化する: `gcloud services enable drive.googleapis.com --project <project id>`、
   または Cloud Console の「API とサービス」→「ライブラリ」→ Google Drive API →「有効にする」。
2. **OAuth クライアントの作成は Cloud Console のみ**（`gcloud iap oauth-clients` は IAP 専用で 2026 年に停止済み。gcloud では作れない）:
   Google Auth platform → Clients → Create Client → Application type「Desktop app」。表示される client ID と client secret を控える
   （初回は Branding / Audience（External、テストユーザーに自分のアカウント）の設定を求められる）。
3. remote に設定する: `rclone config` → `e`（edit existing remote）→ 対象の remote → `client_id` / `client_secret` に貼り付け（他はそのまま）。
4. 再認可する: `rclone config reconnect <remote>:`（ブラウザで同意。client_id を変えると既存の token は使えない）。
5. `kairn status` で警告が消えたことを確認し、`kairn rules set rclone_flags "--transfers 8 --checkers 16 --drive-pacer-min-sleep 10ms --drive-pacer-burst 200"`
   で並列度を上げる（「同期」の `rules.rclone_flags`）。

併せて、**小ファイル群（生成物: ビルド産物・ログ・CSV・キャッシュ等）は `kairn rules add-exclude '<pattern>'` で同期対象から外す**
（例 `kairn rules add-exclude 'logs/**'`、`kairn rules add-exclude '*.csv'`）。client_id を変えても 1 ファイル 1 API 呼び出しは変わらないので、
件数を減らすのが最も効く。`kairn checkin <ws> <case> --dry-run` で転送対象を確認できる。

## CLI（`kairn --help`）

設定ファイル `~/.config/kairn/config.yaml` はこれらのコマンドが書く（人は編集しない）。`<ws>` 省略時は cwd が属するワークスペース。

```
kairn setup --remote <rclone remote> [--agent claude|codex|opencode|antigravity] [--extract-timeout <sec>]   # 使う remote（これ以外は使わない）と抽出エージェント（--agent / --extract-timeout 省略時は既存値を保つ）
kairn ws list | kairn ws create <name> [--description "…"]   # ワークスペース一覧（Drive 上の有無つき）／作成
kairn attach <ws> [<repo path>...]        # リポジトリの所属を設定に記録する（省略時は cwd。リポジトリ側には何も作らない）
kairn detach [<repo path>]                # 所属を外す（glob: 由来なら exclude: を書く）
kairn status                              # 設定・cwd の所属・各ワークスペースの案件数と所属リポジトリ（remote が共有 client_id の Drive なら警告）
kairn cases [<ws>] [--all]                # 案件一覧（既定は open のみ）
kairn new <case id> "<title>" [--ws <ws>] # 案件を作る（case.json）
kairn close <ws> <case> [--note "…"]      # 案件を閉じる（status: closed。actor=human の status event を記録。既に closed なら "already closed" で終了コード 0）
kairn suspend <ws> <case> [--note "…"]    # 案件を保留にする（status: suspended）
kairn reopen <ws> <case> [--note "…"]     # 案件を再開する（status: open）。閉じる・保留・再開は人の判断（AI は MCP set_case_status で人の発言を添えて代行するだけ）
kairn checkout <ws> [<case>] [--dry-run]  # Drive → ローカル（rclone copy --update）＋索引更新（--dry-run では索引を書き換えない）。同期実行（タイムアウト無し）。案件省略時は Drive の版マーカー（cases/*/.rev/）を 1 回読み、rev がローカルと違う案件だけ
kairn checkin <ws> [<case>] [--dry-run]   # ローカル → Drive（案件単位は rclone sync、ワークスペース全体は rclone copy。rev / last_checkin_at を書き、案件フォルダの .rev/<rev> を置く）。同期実行（タイムアウト無し）
kairn index <ws> [--full]                 # 索引（SQLite FTS5）の差分再生成（--full で全部）
kairn drive-index <ws>                    # Drive 上の全ファイル一覧を index/drive-index.txt に
kairn bag2zst <ws> [<case>] [--dry-run]   # *.bag / *.bag.active を zstd 圧縮
kairn raw-move <ws> [<case>] [--dry-run]  # 生データを Drive へ移動し所在を記録
kairn daily <ws> [--dry-run]              # bag2zst → checkout → checkin → raw-move → drive-index → index
kairn drive-markers <ws> [--dry-run] [--remove-manifest]   # 既存 Drive データの移行: Drive の case.json の rev がローカルと一致する案件に版マーカー cases/<case>/.rev/<rev> を置く（--remove-manifest で旧方式の manifest.json を消す）
kairn extract <case> [--ws <ws>] [--agent …] [--json]   # 子エージェントで case.json の下書き（書き込まない）
kairn rules show                          # 同期・退避規則（rules）の現在値
kairn rules set <key> <value>             # raw_data.min_size | raw_data.min_age | bag_to_zst | bwlimit | rclone_flags（値を検証し、不正なら拒否）
kairn rules add-exclude <pattern> | remove-exclude <pattern>   # 同期しないパターン（rclone のフィルタ規則）を足す／外す
kairn rules add-raw-ext <ext> | remove-raw-ext <ext>           # 生データ扱いの拡張子を足す／外す
kairn serve [--host 127.0.0.1] [--port 8765]            # MCP（/mcp）＋ UI（/ui）
kairn install-skill [--home <dir>]        # skills/kairn を ~/.agents/skills と ~/.claude/skills からリンク
kairn install-service [--yes] [--print]   # systemd user unit（常駐 kairn-serve.service ＋ 日次 kairn-daily@<ws>.timer）を生成して登録（対話式）
kairn ensure [--timeout 15]               # /mcp が応答しなければ kairn serve を切り離して起動し応答まで待つ（service が止まっていた時の保険）
```

## 各エージェントへの適用

### 1. サーバーを常駐させる（`kairn install-service`）

MCP（`http://127.0.0.1:8765/mcp`）と UI（`http://127.0.0.1:8765/ui`）は同じプロセス（`kairn serve`）。systemd user service で常駐させる。
unit は雛形ファイルではなく `kairn install-service` がコード内テンプレート（`kairn/service.py`）から生成する:

```
kairn install-service            # 対話式。--yes で既定値のまま非対話、--print で書く内容を表示するだけ（ファイルもコマンドも実行しない）
systemctl --user status kairn-serve.service; journalctl --user -u kairn-serve
```

- 対話項目: バインド先（既定 `127.0.0.1`。他を選ぶと「ネットワークに公開される」確認が出る）、ポート（既定 8765）、日次同期を回すワークスペース
  （設定にあるものから複数選択。無しも可）、日次の時刻（既定 12:30）、`enable --now` するか、ログインしていなくても起動するか
  （`loginctl enable-linger`。管理者認証を求められることがある。失敗しても他は続行）。
- 生成先: `~/.config/systemd/user/kairn-serve.service`（`Restart=on-failure`、`TimeoutStopSec=15`）、`kairn-daily@.service`（テンプレート unit、`%i` = ワークスペース名）、
  `kairn-daily@<ws>.timer`（`Persistent=true`、`RandomizedDelaySec=10m`）。`ExecStart` には install-service を実行した `kairn` 自身の絶対パスが入る
  （`uv tool install` なら `~/.local/bin/kairn`、`.venv/bin/kairn` から実行すればそのパス）。既存の unit と差分があれば表示して上書きを確認する（`--yes` は上書き）。
- 実行するもの: `systemctl --user daemon-reload` → `enable [--now] kairn-serve.service kairn-daily@<ws>.timer …` → （選んだ時だけ）`loginctl enable-linger` → `is-active` の表示。
  `systemctl` の無い環境では unit を書くだけにして案内を出す。
- 選んだバインド先とポートは `~/.config/kairn/config.yaml` の `serve:` に書かれ、`kairn ensure` がそれを見る。ポートを変えたら各エージェントの MCP 登録 URL も合わせる。
- 停止・再起動: `kairn serve` は SIGTERM を受けると開いている MCP 接続（SSE セッション）を最大 5 秒しか待たずに終了し、unit 側も
  `TimeoutStopSec=15` で打ち切る。**`systemctl --user restart kairn-serve` が 90 秒待つ（`Failed with result 'timeout'`）場合は古い unit なので
  `kairn install-service` を再実行して unit を更新する**（既存 unit との差分を表示して上書きを確認。`--yes` で確認なし。その後 `daemon-reload` まで行う）。
  停止時に走っていたジョブ（checkin / 取り寄せ）はログ（`journalctl --user -u kairn-serve`）に 1 行残り、次回の checkin / checkout で整合する。
  シグナル受信から 7 秒（5 + 2）経ってもプロセスが残っていれば（同期ツールハンドラの実行中など）、ウォッチドッグが running ジョブをログに出して
  `os._exit(0)` で落とす（`kairn/server.py` `start_shutdown_watchdog`）。
- **版マーカー（`cases/<case>/.rev/<rev>`）の導入時は一度 `kairn drive-markers <ws>` を実行する**（後述「同期」）。実行するまで Drive の既存案件は
  マーカー無しなので、`open_case` は毎回取り寄せ、`kairn checkout <ws>` はそれらを対象にしない（checkin した案件から順にマーカーが付く）。

`kairn ensure`: 設定のポートで `/mcp` が応答しなければ `kairn serve` を切り離して起動（`start_new_session`。出力は `~/.local/state/kairn/serve.log`）し、
応答が出るまで最大 15 秒待つ（`--timeout`）。動いていれば何もしない。終了コード 0 = 応答あり。skill は案件を開く前にこれを 1 回実行する（service が止まっていた時の保険）。

#### 設定の反映と再起動

常駐中の `kairn serve` は、MCP のツール呼び出しと UI のリクエストのたびに `~/.config/kairn/config.yaml` の更新（mtime / size / inode）を
確認し、変わっていれば読み直す（`kairn/config.py` `ConfigHolder`）。**再起動は要らない**:

- `kairn ws create` / `attach` / `detach`、`kairn rules …`、UI の設定ページ、`kairn setup`（remote / extract）の変更は、次のツール呼び出し・
  次のページ表示から効く（新しいワークスペースの案件を `open_case` で開ける。`rclone_flags` は次に起動する checkin / 取り寄せジョブから）。
  1 回の呼び出しの間は入口で読んだ設定を使い、走っているジョブは投入時点の設定のまま終わる。
- 読み直せない設定（壊れた YAML・`drive.remote` 無し・ファイル消失）は無視して直前の設定で動き続け、ログ（`journalctl --user -u kairn-serve`）に
  警告が 1 行出る。

再起動が要るのは次の 2 つだけ: **kairn 自体の更新**（`git pull` / `uv tool upgrade` の後: `systemctl --user restart kairn-serve.service`）と
**unit の変更**（バインド先・ポート: `kairn install-service` を再実行して unit を書き直し、`systemctl --user restart kairn-serve.service`。
`serve:` の `host` / `port` は unit の `ExecStart` と `kairn ensure` が使うもので、常駐中のプロセスが読み直しても bind し直さない）。
日次同期（`kairn-daily@<ws>.service`）は CLI として毎回設定を読むので何もしなくてよい。

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

### 5. 各エージェントに `workspaces/` の読み書きを許可する

案件ディレクトリはリポジトリの外、kairn のデータ領域 `workspaces/<ws>/cases/`（既定 `~/kairn/workspaces`。`KAIRN_DATA_ROOT` で変えられる）にある。
エージェントは `open_case` が返す `paths.case_dir` の絶対パスで読み書きするので、各エージェントの「追加ディレクトリ許可」でこの領域を開ける。
kairn は各エージェントの設定を自動では書き換えない（`kairn install-skill` の最後に、実際のパスを入れた同じ手順を表示する）:

| エージェント | 許可の与え方 |
|---|---|
| Claude Code | 起動時 `claude --add-dir ~/kairn/workspaces`、または `~/.claude/settings.json` の `permissions.additionalDirectories` に `"~/kairn/workspaces"` を追加（ユーザー設定。フォルダを trust した後に有効） |
| Codex CLI | `codex --add-dir ~/kairn/workspaces`（workspace に加えて書き込み可）。読み取り範囲も広げるなら `-c 'sandbox_permissions=["disk-full-read-access"]'` |
| OpenCode | `opencode.json` の `permission.external_directory` に `{"~/kairn/workspaces/**": "allow"}`（該当エージェントの `permission:` でも可。書式は https://opencode.ai/docs/permissions/ ） |
| Antigravity | `agy --add-dir ~/kairn/workspaces`（複数指定可） |

Claude Code の `~/.claude/settings.json`:
```json
{
  "permissions": {
    "additionalDirectories": ["~/kairn/workspaces"]
  }
}
```

OpenCode の `~/.config/opencode/opencode.json`:
```json
{
  "$schema": "https://opencode.ai/config.json",
  "permission": {
    "external_directory": {"~/kairn/workspaces/**": "allow"}
  }
}
```

### 6. リポジトリ側に書くこと（CLAUDE.md / AGENTS.md）

紐付けの正は各環境の `~/.config/kairn/config.yaml`（`kairn attach` が書く）。リポジトリには所属ワークスペースと入口だけを書く:

```
## 作業ログ（kairn）
このリポジトリは kairn ワークスペース `acme` に属する。案件（case）の作業は kairn skill の手順に従う
（MCP `kairn` の open_case で開き、done は証拠付きで update_task、終わったら checkin）。
案件ディレクトリはこのリポジトリの外（kairn の workspaces 配下）にある。open_case が返す `paths.case_dir` の絶対パスで読み書きする。
```

## 同期（sync）

テキスト層（worklog / case.json / plan / events 等）は `checkout` / `checkin` で Drive と往復する。生データ層
（`rules.raw_data`: 拡張子が bag/zst/… **または** `min_size` 超、かつ更新から `min_age` 超）はテキスト層の同期から除外し、
`raw-move` で Drive へ**移動**（ローカルから削除）して所在を案件に記録する。

```
kairn bag2zst <ws> [<case>] [--dry-run]   # *.bag / *.bag.active → <name>.zst（zstd -T0 -6、検証後に置換、mtime 引き継ぎ。30 分以内に更新されたものと rules.exclude（target/** 等）配下は対象外）
kairn raw-move <ws> [<case>] [--dry-run]  # rclone move → <remote>:<root>/<ws>/cases/<case>/…。移動後に case.json.data[]、worklog.md の
                                          #   「## Data location」（case.json が無ければ DATA.md）、index/raw-moved-YYYYMMDD.txt、progress event に記録
kairn daily <ws> [--dry-run]              # bag2zst → checkout → checkin → raw-move → drive-index → index を順に実行。段が失敗しても次へ進み、index/daily.log に記録
```

- 所在の記録先: `worklog.md` があればその `## Data location` 節、無ければ `DATA.md`（case.json の有無に関わらず）。case.json があれば `data[]` と progress event にも記録。
- 復元: `rclone copy <remote>:<root>/<ws>/cases/<case>/<file> <案件ディレクトリ>/`（Data location の行と UI に表示）。
- `events.jsonl`（追記専用ログ）は `checkout` / `checkin` のどちらでも「新しい方で上書き」せず、Drive 版を取り寄せてローカル版と**行の和集合**にマージしてから転送する
  （`rclone copyto` で一時ファイルへ → 文字列一致で重複除去 → `t` で安定ソート → 書き戻し）。複数環境で書いた event が失われない。
  Drive にその案件が無い・rclone が無い等で取得できなければマージを飛ばして従来どおり転送する。
- `checkout <ws> <case>` は events 以外を `rclone copy --update`（ローカルの方が新しいファイルは上書きしない）。`open_case` の取り寄せも同じ。
  `open_case` は `case.json.last_checkin_at`（checkin が更新）より新しいローカル変更があれば checkout を skip する。
- **版マーカー（`cases/<case>/.rev/<rev>`）**（差分なしの取り寄せでも 30 秒かかるため、更新の有無を安価に確認する）: すべての checkin 経路
  （MCP `checkin`・`kairn checkin`・`daily`）は転送の前に案件の `case.json` へ `rev`（uuid4）/ `last_checkin_at` / `checked_in_from`（ホスト名）を書き、
  案件フォルダの `.rev/` を「`<rev>` という空ファイル 1 個」に作り直してから転送する（案件単位の `rclone sync` が Drive 側の古いマーカーを消す。
  ワークスペース全体の `rclone copy` の後は、rev を振り直した案件の `.rev/` だけを `rclone sync` で揃える）。集計ファイルは置かない（docs/data-model.md）。
  `open_case` は `rclone lsf <case>/.rev/` を 1 回（10 秒でタイムアウト）だけ行い、名前がローカルの `rev` と同じなら取り寄せを省略する（`drive.up_to_date: true`）。
  違えば取り寄せジョブを起動して最大 20 秒待ち、間に合えば取り寄せ後の内容を返す（`drive.fetched: true`）。マーカーが無い・2 個以上ある（不定）案件は取り寄せる。
  Drive が読めなければ取り寄せずローカル写し（`drive.up_to_date: null`）。直近に読んだ版は `index/drive_revs.cache.json` に置き、`list_cases` の `drive.state` と
  UI 一覧の Drive 列（同期済み / Drive の方が新しい / ローカル未 checkin / 不明）に使う。
  `kairn checkout <ws>`（案件指定なし）・UI の「更新確認」・`daily` は `rclone lsf -R --include '/cases/*/.rev/*'` 1 回で全案件の版を読み、
  `rev` が違う案件（ローカルに無い案件を含む）だけを取り寄せる。
  `kairn checkin <ws>`（`daily`）は `last_checkin_at` より新しいローカル変更がある案件と未 checkin の案件だけ `rev` を振り直す（内容の変わらない案件の
  `rev` を毎日変えない。作業ファイルだけが増えた案件は次に case.json / worklog / events / plan が変わるまで振り直されない）。
- **新方式導入時は一度 `kairn drive-markers <ws>` を実行する**（既存 Drive データの移行。`--dry-run` で内容の確認のみ）: Drive の `cases/*/case.json` を
  `rclone copy --include` 1 回で一時ディレクトリへ取り寄せ、`rev` がローカルの `case.json` と一致する案件だけ `cases/<case>/.rev/<rev>` を作って
  `rclone sync`（当該案件の `.rev/` に限定）1 回で Drive へ置く。不一致・Drive 側に `rev` が無い・ローカルに無い案件は一覧で報告して触らない
  （それらは各環境の次の checkin でマーカーが付く）。`--remove-manifest` を付けた時だけ旧方式の `manifest.json` を消す。
- `open_case` は `events.jsonl` に書かない。閲覧記録はワークスペースの `index/access.log`（ローカルのみ、同期しない）に 1 行追記する。
- `checkin <ws> <case>`（MCP の `checkin(case)` も）は `rclone sync`: 案件内の削除を追従し、Drive 側に新しい版があっても `_deleted/<日付>/` に退避して上書きする（events.jsonl はマージ済み）。**他環境で作業した後は先に `checkout` する**。
- `checkin <ws>`（案件指定なし。`daily` が毎日呼ぶ）は `rclone copy`: ローカルに無い案件ディレクトリを Drive から消さない（原則 2「ローカルの案件ディレクトリは消してよい」）。上書きされる Drive 側の版は同じく `_deleted/` へ。
- **MCP の `checkin(case)` と `open_case` の取り寄せはジョブ**（`kairn/jobs.py`。サーバー内のスレッド）で、ツールは待たずに `job_id` を返し、`job_status(job_id)` で done / failed を見る
  （docs/mcp-tools.md「ジョブ」。大きな案件で MCP クライアントの呼び出しタイムアウトに当たらないため）。同じ案件のジョブは種類を問わず 1 つずつ実行する
  （checkin 中に取り寄せが来れば `queued` で待つ。別案件は並走）。`open_case` は Drive のマーカーの `rev` が違うときだけ取り寄せジョブを起動し、最大 20 秒待つ。
  ジョブ表はサーバーのメモリ内で、`kairn serve` の再起動で消える。
  **CLI の `kairn checkin` / `kairn checkout` は従来どおり同期**（終わるまで待つ。タイムアウト無し）。**大きな初回投入（数百 MB・数百ファイル）は MCP ではなく CLI で行う**:
  `kairn checkin <ws> <case>`。
- `open_case` の checkout skip 判定は人／AI の実質的な変更だけを見る: `events.jsonl` が checkin 時点（`case.json.last_checkin_events` 行）以後に kairn 自身の `checkin` event で伸びただけなら変更と数えない。
- `daily --dry-run` は rclone に `--dry-run` を渡し、`drive-index.txt` と索引（`kairn.sqlite`）を書き換えない。
- 帯域制限は `rules.bwlimit`（例 `"08:00,4M 20:00,off"`。rclone の `--bwlimit` にそのまま渡す）。
- **rclone の追加引数 `rules.rclone_flags`**（既定は空）: rclone を呼ぶすべての箇所（checkout / checkin / raw-move / drive-index / 版マーカーの
  lsf・sync / drive-markers / events の取り寄せ / ws の lsd・mkdir・lsf）で共通引数の後ろに付ける（同じオプションは後ろが勝つ）。
  `kairn rules set rclone_flags "--transfers 8 --checkers 16 --drive-pacer-min-sleep 10ms --drive-pacer-burst 200"` が推奨例
  （`--` で始まるオプションとその値だけ受け付ける。`kairn rules set rclone_flags ""` で空に戻す。`kairn rules show` / UI の設定ページで確認・編集）。
  Google Drive はファイルごとに API 呼び出しが要り、rclone 共有の client_id では API レートが絞られるため、小ファイル多数の案件で転送が
  極端に遅い（実測: 1.3 MiB・多数の小ファイルで約 5 分）。**上の値は自前の OAuth client_id（後述「専用 OAuth クライアント（推奨）」）が
  前提**で、共有 client_id のまま並列度だけ上げると rate limit のリトライで却って遅くなる。
- **テキスト層の上限と除外**（大量のログ・CSV を持つ案件向け）。`checkout` / `checkin`（MCP の `open_case` / `checkin` も）が転送するのは
  `rules.exclude` に当たらず、`rules.raw_data.extensions` の拡張子でなく、**`rules.raw_data.min_size`（既定 `50M`）以下**のファイルだけ
  （rclone の `--filter '- <pattern>'` / `--max-size`。`kairn/sync.py` `_filters`。版マーカー `.rev/` は先頭の `+ .rev/**` で常に含める）。それを超えるものは生データ層で、`raw-move` が `min_age`（既定 `14d`）後に Drive へ移動する。
  1 ファイルは小さくても件数が多い（数百ファイル・数百 MB のログや CSV）と転送に数分かかるので、案件に合わせて調整する:
  - 上限を下げる: `rules.raw_data.min_size: 10M` 等（超えたファイルはテキスト層から外れ、`min_age` 後に `raw-move` の対象になる）
  - 同期しないパターンを足す: `rules.exclude` に `logs/**`、`*.csv`、`*.log` 等（`**` で終わるパターンはどの階層のそのディレクトリにも、
    ファイルパターンはどの階層のそのファイルにも一致。rclone のフィルタ規則）。除外したものは同期も `raw-move` もされない（ローカルにだけ残る）
  - 書き換えは **`kairn rules …` か UI の設定ページ（`/ui/settings`）で行う**（`~/.config/kairn/config.yaml` の `rules:` を kairn が書く。
    手では編集しない。値は検証され、不正なら拒否）: `kairn rules set raw_data.min_size 10M`、`kairn rules add-exclude 'logs/**'`、
    `kairn rules add-raw-ext mcap`、`kairn rules set bwlimit "08:00,4M 20:00,off"`（`off` で制限なし）、
    `kairn rules set rclone_flags "--transfers 8 --checkers 16"`（空文字で既定の空に戻す）、`kairn rules show` で現在値。
    `kairn setup` / `attach` / `install-service` は既存の `rules` を引き継ぐ。常駐中の `kairn serve` はリクエストのたびに設定ファイルの
    更新を確認して読み直すので、CLI で変えても UI で変えても次の呼び出しから効く（再起動不要。「設定の反映と再起動」）。
    変更後は `kairn checkin <ws> <case> --dry-run` で転送対象を確認する（`-v` の出力に転送するファイル名が出る）。
- **作業領域は同期しない**: 案件フォルダ内に git worktree（`.git` がファイル）や `.kairn-nosync`（空ファイル）を直下に置いたディレクトリは、
  配下ごと checkout / checkin / raw-move / 索引の対象外（docs/data-model.md「作業領域の除外」）。通常の clone は `.git/` だけが既定の
  `rules.exclude` `.git/**` で除かれる（古い設定ファイルには `kairn rules add-exclude '.git/**'` で足す）。
- 日次実行（systemd user timer、既定は毎日 12:30 ± 10 分、停止中だった分は次回起動時に実行）は `kairn install-service` がワークスペースごとに
  `kairn-daily@<ws>.timer` を生成・登録する（「各エージェントへの適用」1）。確認:
  ```
  systemctl --user list-timers 'kairn-daily@*'; journalctl --user -u 'kairn-daily@*'
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

## 動作環境

- Python 3.11〜3.13（venv は 3.13 で検証。3.14 は依存の pydantic が CPython 3.14.0 の
  `typing._eval_type` 変更と衝突するため現時点では動かない。依存側の対応後に追従する）
- 依存はすべて `uv.lock` で固定（作成時点の各最新版）。更新は `uv lock --upgrade && uv sync --group dev` の後にテスト全緑を確認する

## 状態

段階 1（config / store / index / server(MCP, mcp 2.x) / ui）完了（2026-09-02）。段階 5（sync: bag2zst / raw-move / daily / systemd timer）完了（2026-09-02）。
段階 7（extract: MCP `extract_card` / `kairn extract` / UI の取得・適用）完了（2026-09-02）。
段階 6（skill の最終化、`kairn install-skill`、各エージェントの MCP 登録手順、opencode agent、`kairn install-service` / `kairn ensure`）完了（2026-09-02）。
実装順は docs/roadmap.md（8 の既存 worklog の移行は別件）。
