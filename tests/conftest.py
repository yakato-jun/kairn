"""共通フィクスチャ。実設定（~/.config/kairn）・実データ（workspaces/）・rclone には触れない。"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from kairn import config as cfg


@pytest.fixture
def conf(tmp_path: Path) -> cfg.Config:
    """一時ディレクトリに閉じた設定: ワークスペース acme、remote my-drive（架空）。"""
    data_root = tmp_path / "data"
    ws = cfg.Workspace(name="acme", description="fixture", data_root=data_root)
    ws.cases_dir.mkdir(parents=True)
    return cfg.Config(remote="my-drive", drive_root="ws", extract_agent="claude", rules=dict(cfg.DEFAULT_RULES),
                      workspaces={"acme": ws}, path=tmp_path / "config.yaml")


@pytest.fixture
def fake_rclone(tmp_path: Path) -> Path:
    """PATH の先頭に置く偽 rclone。呼ばれた引数を rclone.log に記録して成功を返す（クラウドには接続しない）。"""
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    log = tmp_path / "rclone.log"
    script = bin_dir / "rclone"
    script.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\necho \"fake rclone: $1 ok\"\nexit 0\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def with_fake_rclone_env(bin_dir: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env.update(extra)
    return env
