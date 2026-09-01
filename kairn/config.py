"""環境ローカル設定（~/.config/kairn/config.yaml）。人は編集しない。kairn のコマンドが書く。

  drive:      {remote: <rclone remote>, root: ws}          kairn setup --remote が書く
  extract:    {agent: claude|codex|opencode|antigravity}   kairn setup --agent
  workspaces: {<name>: {repos: [<abs path>...], link_name: tmp}}   kairn attach / detach が書く
  rules:      同期・退避規則（既定値あり）

規則:
- drive.remote が無ければ起動しない。設定済みの remote 以外は決して使わない。
- workspaces.<name>.repos に登録されたパス以外に対して kairn は何もしない。
- workspaces/（データ）が git に追跡されていたら起動しない。
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("KAIRN_DATA_ROOT", ROOT / "workspaces"))
USER_CONFIG_PATH = Path(os.environ.get("KAIRN_CONFIG", os.path.expanduser("~/.config/kairn/config.yaml")))
EXAMPLE_CONFIG_PATH = ROOT / "config" / "workspaces.example.yaml"

DEFAULT_RULES = {
    "exclude": ["**/target/**", "**/build/**", "**/__pycache__/**", "**/node_modules/**", "**/.venv/**", "*.o", "*.rlib", "*.pyc"],
    "raw_data": {"extensions": ["bag", "zst", "pgm", "npz", "zip", "gz", "tar", "active", "pcd", "mp4"], "min_size": "50M", "min_age": "14d"},
    "bag_to_zst": True,
}


@dataclass
class Workspace:
    name: str
    description: str = ""
    repos: list[Path] = field(default_factory=list)
    link_name: str = "tmp"
    data_root: Path | None = None  # None なら DATA_ROOT（KAIRN_DATA_ROOT）

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
                n: {"description": w.description, "repos": [str(r) for r in w.repos], "link_name": w.link_name}
                for n, w in self.workspaces.items()
            },
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("# kairn 環境ローカル設定。kairn のコマンドが書く（手で編集しない・コミットしない）\n"
                       + yaml.safe_dump(self.to_dict(), allow_unicode=True, sort_keys=False), encoding="utf-8")
        os.replace(tmp, self.path)


def _parse(raw: dict, path: Path) -> Config:
    drive = raw.get("drive") or {}
    remote = drive.get("remote")
    if not remote or not isinstance(remote, str):
        raise SystemExit("kairn: drive.remote is not set. run: kairn setup --remote <rclone remote name>")
    wss: dict[str, Workspace] = {}
    for name, w in (raw.get("workspaces") or {}).items():
        w = w or {}
        repos = [Path(os.path.expanduser(r if isinstance(r, str) else r.get("path", ""))) for r in (w.get("repos") or [])]
        wss[name] = Workspace(name=name, description=w.get("description", ""), repos=[r for r in repos if str(r)], link_name=w.get("link_name", "tmp"))
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


def assert_data_not_tracked() -> None:
    """安全弁: workspaces/ が git に追跡されていたら起動を拒否する。"""
    r = subprocess.run(["git", "-C", str(ROOT), "ls-files", "workspaces"], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        raise SystemExit("kairn: refusing to run — workspaces/ is tracked by git (data must never be committed)")


def rclone_remotes() -> list[str]:
    r = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True)
    return [x.rstrip(":") for x in r.stdout.split()] if r.returncode == 0 else []
