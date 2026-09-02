"""共通フィクスチャ。実設定（~/.config/kairn）・実データ（workspaces/）・rclone には触れない。"""
from __future__ import annotations

import copy
import os
import stat
from pathlib import Path

import pytest

from kairn import config as cfg
from kairn import sync


@pytest.fixture
def conf(tmp_path: Path) -> cfg.Config:
    """一時ディレクトリに閉じた設定: ワークスペース acme、remote my-drive（架空）。"""
    data_root = tmp_path / "data"
    ws = cfg.Workspace(name="acme", description="fixture", data_root=data_root)
    ws.cases_dir.mkdir(parents=True)
    return cfg.Config(remote="my-drive", drive_root="ws", extract_agent="claude", rules=dict(cfg.DEFAULT_RULES),
                      workspaces={"acme": ws}, path=tmp_path / "config.yaml")


class FakeManifest:
    """sync.fetch_manifest / write_manifest の代役（メモリ内。rclone を呼ばない）。data が None か unavailable なら取得失敗（None）。"""

    def __init__(self):
        self.data: dict | None = None
        self.unavailable = False
        self.fetches = 0
        self.writes: list[dict] = []

    def fetch(self, conf, ws, timeout=sync.MANIFEST_TIMEOUT_SEC):
        self.fetches += 1
        return None if (self.unavailable or self.data is None) else copy.deepcopy(self.data)

    def write(self, conf, ws, manifest):
        self.data = copy.deepcopy(manifest)
        self.writes.append(copy.deepcopy(manifest))

    def rev(self, case: str):
        return ((self.data or {}).get("cases", {}).get(case) or {}).get("rev")

    def set_rev(self, case: str, rev: str):
        self.data = self.data or {"cases": {}}
        self.data["cases"].setdefault(case, {})["rev"] = rev


@pytest.fixture
def drive_manifest(monkeypatch) -> FakeManifest:
    """Drive の manifest.json をメモリ内で偽装する（sync.fetch_manifest / write_manifest を差し替え）。"""
    fm = FakeManifest()
    monkeypatch.setattr(sync, "fetch_manifest", fm.fetch)
    monkeypatch.setattr(sync, "write_manifest", fm.write)
    return fm


@pytest.fixture
def fake_rclone(tmp_path: Path) -> Path:
    """PATH の先頭に置く偽 rclone。呼ばれた引数を rclone.log に記録して成功を返す（クラウドには接続しない）。
    cat / rcat だけは <tmp>/drive/<basename> を読み書きする（manifest.json の往復を通すため。無ければ cat は 3 で失敗）。"""
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    log = tmp_path / "rclone.log"
    store = tmp_path / "drive"; store.mkdir()
    script = bin_dir / "rclone"
    script.write_text(
        f"#!/bin/sh\necho \"$@\" >> {log}\n"
        f"case \"$1\" in\n"
        f"  cat) f={store}/$(basename \"$2\"); if [ -f \"$f\" ]; then cat \"$f\"; exit 0; fi; echo 'object not found' >&2; exit 3;;\n"
        f"  rcat) cat > {store}/$(basename \"$2\"); exit 0;;\n"
        f"esac\n"
        f"echo \"fake rclone: $1 ok\"\nexit 0\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def with_fake_rclone_env(bin_dir: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env.update(extra)
    return env
