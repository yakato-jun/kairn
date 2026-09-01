"""workspaces.yaml の読み込みと検証。

規則:
- drive.remote は `kairn setup --remote <name>` が書く。kairn はこの remote 以外を決して使わない
  （顧客側のアカウント等を誤って使わないため）。remote が未設定なら起動しない。
- workspaces.<name>.repos に登録されたパス以外に対して kairn は何もしない。
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
USER_CONFIG_PATH = Path(os.path.expanduser("~/.config/kairn/workspaces.yaml"))  # 実体（コミットしない）
EXAMPLE_CONFIG_PATH = ROOT / "config" / "workspaces.example.yaml"           # 書式の例
DATA_ROOT = ROOT / "workspaces"


@dataclass
class Workspace:
    name: str
    description: str = ""
    repos: list[Path] = field(default_factory=list)
    link_name: str = "tmp"

    @property
    def data_dir(self) -> Path:
        return DATA_ROOT / self.name

    @property
    def cases_dir(self) -> Path:
        return self.data_dir / "cases"


@dataclass
class Config:
    remote: str
    drive_root: str
    rules: dict
    workspaces: dict[str, Workspace]

    def workspace_for_path(self, path: Path) -> Workspace | None:
        """パスが属するワークスペースを返す。登録外なら None（＝何もしない）。"""
        p = path.resolve()
        for ws in self.workspaces.values():
            for repo in ws.repos:
                try:
                    p.relative_to(repo.resolve())
                    return ws
                except ValueError:
                    continue
        return None


def load(path: Path | None = None) -> Config:
    path = path or USER_CONFIG_PATH
    if not path.exists():
        raise SystemExit(
            f"no workspace config at {path}. Create it from {EXAMPLE_CONFIG_PATH} "
            "(it holds customer names and local paths, so it is never committed)."
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    remote = (raw.get("drive") or {}).get("remote")
    if not remote or not isinstance(remote, str):
        raise SystemExit("refusing to run: drive.remote is not set (run: kairn setup --remote <rclone remote name>)")
    workspaces = {}
    for name, w in (raw.get("workspaces") or {}).items():
        repos: list[Path] = []
        for r in (w.get("repos") or []):
            if "glob" in r:
                repos += sorted(Path(p) for p in glob.glob(os.path.expanduser(r["glob"])) if Path(p).is_dir())
            elif "path" in r:
                repos.append(Path(os.path.expanduser(r["path"])))
        workspaces[name] = Workspace(name=name, description=w.get("description", ""), repos=repos, link_name=w.get("link_name", "tmp"))
    return Config(remote=remote, drive_root=raw["drive"].get("root", "ws"), rules=raw.get("rules", {}), workspaces=workspaces)


def assert_data_not_tracked() -> None:
    """安全弁: workspaces/ が git に追跡されていたら起動を拒否する。"""
    import subprocess
    r = subprocess.run(["git", "-C", str(ROOT), "ls-files", "workspaces"], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        raise SystemExit("refusing to run: workspaces/ is tracked by git (data must never be committed)")
