"""環境ローカル設定（~/.config/kairn/config.yaml）。人は編集しない。kairn のコマンドが書く。

  drive:      {remote: <rclone remote>, root: ws}          kairn setup --remote が書く
  extract:    {agent: claude|codex|opencode|antigravity}   kairn setup --agent
  workspaces: {<name>: {repos: [<abs path> | {path: <abs path>} | {glob: <pattern>} | {exclude: <abs path>} ...], link_name: tmp}}
              kairn attach / detach が書く（glob: は展開時にディレクトリだけ採る。exclude: は glob: の展開から外す（detach が書く）。
              未知のキー・空文字は拒否）
  rules:      同期・退避規則（既定値あり）

規則:
- drive.remote が無ければ起動しない。設定済みの remote 以外は決して使わない。
- workspaces.<name>.repos に登録されたパス以外に対して kairn は何もしない。
- workspaces/（データ）が git に追跡されていたら起動しない。
"""
from __future__ import annotations

import glob as _glob
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("KAIRN_DATA_ROOT", ROOT / "workspaces"))
USER_CONFIG_PATH = Path(os.environ.get("KAIRN_CONFIG", os.path.expanduser("~/.config/kairn/config.yaml")))
EXAMPLE_CONFIG_PATH = ROOT / "config" / "config.example.yaml"

# exclude は rclone のフィルタ規則。先頭に / の無い `target/**` は任意の階層の target/ に一致する（`**/target/**` はルート直下に一致しない）
DEFAULT_RULES = {
    "exclude": ["target/**", "build/**", "__pycache__/**", "node_modules/**", ".venv/**", "*.o", "*.rlib", "*.pyc"],
    "raw_data": {"extensions": ["bag", "zst", "pgm", "npz", "zip", "gz", "tar", "active", "pcd", "mp4"], "min_size": "50M", "min_age": "14d"},
    "bag_to_zst": True,
}


@dataclass
class Workspace:
    name: str
    description: str = ""
    repos: list[Path] = field(default_factory=list)            # 展開済み（glob: はディレクトリに展開）
    repo_specs: list = field(default_factory=list)             # 設定ファイルに書く形（文字列 / {path} / {glob} / {exclude}）
    link_name: str = "tmp"
    data_root: Path | None = None  # None なら DATA_ROOT（KAIRN_DATA_ROOT）

    def add_repo(self, repo: Path) -> None:
        if repo not in self.repos:
            self.repos.append(repo)
            self.repo_specs.append(str(repo))

    def remove_repo(self, repo: Path) -> None:
        """path 指定の登録を外す。glob: で拾われたものは `{exclude: <path>}` を追記して展開から外す（glob 行は残す）。"""
        keep = []
        via_glob = False
        for spec in self.repo_specs:
            if isinstance(spec, dict) and "glob" in spec:
                if any(r.resolve() == repo for r in _expand_repo_spec(spec, self.name)):
                    via_glob = True
                keep.append(spec)
            elif isinstance(spec, dict) and "exclude" in spec:
                keep.append(spec)
            elif Path(os.path.expanduser(spec if isinstance(spec, str) else spec["path"])).resolve() == repo:
                continue
            else:
                keep.append(spec)
        if via_glob and not any(isinstance(sp, dict) and "exclude" in sp and Path(os.path.expanduser(sp["exclude"])).resolve() == repo for sp in keep):
            keep.append({"exclude": str(repo)})
        self.repo_specs = keep
        self.repos = [r for r in self.repos if r.resolve() != repo]

    @property
    def data_dir(self) -> Path:
        return (self.data_root or DATA_ROOT) / self.name

    @property
    def cases_dir(self) -> Path:
        return self.data_dir / "cases"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"


@dataclass
class Config:
    remote: str
    drive_root: str
    extract_agent: str
    rules: dict
    workspaces: dict[str, Workspace]
    path: Path = USER_CONFIG_PATH

    def drive_path(self, ws: str, *parts: str) -> str:
        return f"{self.remote}:{'/'.join([self.drive_root, ws, *parts]).strip('/')}"

    def workspace_for_path(self, path: Path) -> Workspace | None:
        """パスが属するワークスペース（登録リポジトリの配下）。登録外なら None（＝何もしない）。"""
        p = path.resolve()
        for ws in self.workspaces.values():
            for repo in ws.repos:
                try:
                    p.relative_to(repo.resolve())
                    return ws
                except ValueError:
                    continue
        return None

    def to_dict(self) -> dict:
        return {
            "drive": {"remote": self.remote, "root": self.drive_root},
            "extract": {"agent": self.extract_agent},
            "rules": self.rules,
            "workspaces": {
                n: {"description": w.description, "repos": list(w.repo_specs), "link_name": w.link_name}
                for n, w in self.workspaces.items()
            },
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("# kairn 環境ローカル設定。kairn のコマンドが書く（手で編集しない・コミットしない）\n"
                       + yaml.safe_dump(self.to_dict(), allow_unicode=True, sort_keys=False), encoding="utf-8")
        os.replace(tmp, self.path)


def _expand_repo_spec(spec, ws_name: str) -> list[Path]:
    """repos の 1 要素を Path のリストに。文字列 / {path: ...} はそのまま 1 件、{glob: ...} は一致するディレクトリだけ、
    {exclude: ...} は空（除外は expand_repos で引く）。未知のキー・複数キー・空文字は SystemExit
    （Path("") がカレントディレクトリに化けるのを防ぐ）。"""
    where = f"workspaces.{ws_name}.repos"
    if isinstance(spec, str):
        spec = {"path": spec}
    if not isinstance(spec, dict) or len(spec) != 1:
        raise SystemExit(f"kairn: {where}: each entry must be a path string, {{path: ...}}, {{glob: ...}} or {{exclude: ...}} (got {spec!r})")
    (key, val), = spec.items()
    if key not in ("path", "glob", "exclude"):
        raise SystemExit(f"kairn: {where}: unknown key {key!r} (expected path, glob or exclude)")
    if not isinstance(val, str) or not val.strip():
        raise SystemExit(f"kairn: {where}: {key} must be a non-empty string (got {val!r})")
    if key == "path":
        return [Path(os.path.expanduser(val))]
    if key == "exclude":
        return []
    return [Path(p) for p in sorted(_glob.glob(os.path.expanduser(val))) if os.path.isdir(p)]


def expand_repos(specs: list, ws_name: str) -> list[Path]:
    """repos 全体を展開する（重複なし）。{exclude: <path>} に一致するものは glob: の展開から外す（path: の明示登録は外さない）。"""
    excluded = set()
    for spec in specs:
        if isinstance(spec, dict) and len(spec) == 1 and "exclude" in spec:
            _expand_repo_spec(spec, ws_name)  # 検証のみ
            excluded.add(Path(os.path.expanduser(spec["exclude"])).resolve())
    repos: list[Path] = []
    for spec in specs:
        is_glob = isinstance(spec, dict) and "glob" in spec
        for r in _expand_repo_spec(spec, ws_name):
            if is_glob and r.resolve() in excluded:
                continue
            if r not in repos:
                repos.append(r)
    return repos


def _parse(raw: dict, path: Path) -> Config:
    drive = raw.get("drive") or {}
    remote = drive.get("remote")
    if not remote or not isinstance(remote, str):
        raise SystemExit("kairn: drive.remote is not set. run: kairn setup --remote <rclone remote name>")
    wss: dict[str, Workspace] = {}
    for name, w in (raw.get("workspaces") or {}).items():
        w = w or {}
        specs = list(w.get("repos") or [])
        repos = expand_repos(specs, name)
        wss[name] = Workspace(name=name, description=w.get("description", ""), repos=repos, repo_specs=specs, link_name=w.get("link_name", "tmp"))
    rules = {**DEFAULT_RULES, **(raw.get("rules") or {})}
    return Config(remote=remote, drive_root=drive.get("root", "ws"), extract_agent=(raw.get("extract") or {}).get("agent", "claude"),
                  rules=rules, workspaces=wss, path=path)


def load(path: Path | None = None) -> Config:
    path = path or USER_CONFIG_PATH
    if not path.exists():
        raise SystemExit(f"kairn: no config at {path}. run: kairn setup --remote <rclone remote name>")
    return _parse(yaml.safe_load(path.read_text(encoding="utf-8")) or {}, path)


def create(remote: str, agent: str = "claude", path: Path | None = None, drive_root: str = "ws") -> Config:
    path = path or USER_CONFIG_PATH
    existing = load(path) if path.exists() else None
    conf = Config(remote=remote, drive_root=drive_root, extract_agent=agent, rules=dict(existing.rules if existing else DEFAULT_RULES),
                  workspaces=dict(existing.workspaces if existing else {}), path=path)
    conf.save()
    return conf


def _tracked_in_git(path: Path) -> bool:
    """path（またはその配下）が、path を含む git リポジトリで追跡されているか。リポジトリ外・存在しないなら False。"""
    probe = path if path.is_dir() else path.parent
    if not probe.exists():
        return False
    top = subprocess.run(["git", "-C", str(probe), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode != 0:
        return False
    r = subprocess.run(["git", "-C", top.stdout.strip(), "ls-files", "--", str(path)], capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def assert_data_not_tracked(data_root: Path | None = None) -> None:
    """安全弁: workspaces/（リポジトリ内）と KAIRN_DATA_ROOT が git に追跡されていたら起動を拒否する。"""
    data_root = data_root or DATA_ROOT
    for p in {ROOT / "workspaces", data_root}:
        if _tracked_in_git(p):
            raise SystemExit(f"kairn: refusing to run — {p} is tracked by git (data must never be committed)")


def rclone_remotes() -> list[str]:
    r = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True)
    return [x.rstrip(":") for x in r.stdout.split()] if r.returncode == 0 else []
