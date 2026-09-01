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
kairn/                 パッケージ（config / store / server(MCP) / ui / sync / extract）
config/workspaces.yaml ワークスペース定義（機密なし）
workspaces/<ws>/       データ実体（.gitignore、Drive 同期）
  cases/<case>/        worklog.md、作業ファイル、plan/、events.jsonl
  index/               各環境で再生成する索引（sqlite 等）
  drive-index.txt      Drive 上の全ファイル一覧
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
の数行だけを書き、対応表は `config/workspaces.yaml` を正とする。

## 状態

骨組みのみ（2026-09-01）。実装順は docs/roadmap.md。
