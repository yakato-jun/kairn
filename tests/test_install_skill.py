"""kairn install-skill: 一時 HOME にリンクを作る。実 HOME（~/.agents / ~/.claude）には触れない。"""
from __future__ import annotations

import os
from pathlib import Path

from kairn import cli, config as cfg

SRC = cfg.ROOT / "skills" / "kairn"


def test_install_skill_copies_without_symlink_privilege(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        error = OSError("symlink privilege unavailable")
        error.winerror = 1314
        raise error
    monkeypatch.setattr(Path, "symlink_to", denied)
    lines = cli.install_skill(tmp_path)
    assert all(line.startswith("copied ") for line in lines)
    for base in (".agents", ".claude"):
        installed = tmp_path / base / "skills" / "kairn" / "SKILL.md"
        assert installed.read_bytes() == (SRC / "SKILL.md").read_bytes()
        installed.write_text("user edit", encoding="utf-8")
    assert all(line.startswith("exists:") for line in cli.install_skill(tmp_path))
    assert installed.read_text(encoding="utf-8") == "user edit"


def test_install_skill_links_both_and_keeps_existing(tmp_path: Path, monkeypatch, capsys, requires_symlinks):
    home = tmp_path / "home"
    lines = cli.install_skill(home)
    agents = home / ".agents" / "skills" / "kairn"; claude = home / ".claude" / "skills" / "kairn"
    for d in (agents, claude):
        assert d.is_symlink() and d.resolve() == SRC.resolve() and (d / "SKILL.md").is_file()
    assert [l.split()[0] for l in lines] == ["linked", "linked"]
    # 2 回目: 同じリンクなら already linked
    assert all("already linked" in l for l in cli.install_skill(home))
    # 既存のディレクトリ・別リンクは上書きしない
    home2 = tmp_path / "home2"
    (home2 / ".agents" / "skills" / "kairn").mkdir(parents=True)
    (home2 / ".agents" / "skills" / "kairn" / "SKILL.md").write_text("mine")
    (home2 / ".claude" / "skills").mkdir(parents=True)
    (home2 / ".claude" / "skills" / "kairn").symlink_to(tmp_path)
    lines = cli.install_skill(home2)
    assert lines[0].startswith("exists:") and "dir" in lines[0] and (home2 / ".agents" / "skills" / "kairn" / "SKILL.md").read_text() == "mine"
    raw_target = os.readlink(home2 / ".claude" / "skills" / "kairn")
    plain_target = raw_target[4:] if raw_target.startswith("\\\\?\\") else raw_target  # Windows のディレクトリ symlink は \\?\ 拡張長パスで返る
    assert lines[1].startswith("exists:") and "fix by hand" in lines[1] and plain_target == str(tmp_path)
    # CLI 経由（--home）。実 HOME には触れない
    monkeypatch.setattr(cfg, "assert_data_not_tracked", lambda data_root=None: None)
    home3 = tmp_path / "home3"
    monkeypatch.setattr("sys.argv", ["kairn", "install-skill", "--home", str(home3)])
    cli.main()
    out = capsys.readouterr().out
    assert out.count("linked ") == 2 and (home3 / ".claude" / "skills" / "kairn").is_symlink()
    # 最後に、各エージェントの許可設定手順（実際のデータ領域のパス入り）を表示する
    d = str(cfg.DATA_ROOT.resolve())
    assert out.rstrip().endswith(cli.permission_notes())
    for needle in (f"claude --add-dir {d}", "permissions.additionalDirectories", f"codex --add-dir {d}", "disk-full-read-access",
                   f'external_directory に {{"{d}/**": "allow"}}', f"agy --add-dir {d}", f"{d}/<ws>/cases/"):
        assert needle in out, needle


def test_permission_notes_match_readme(tmp_path: Path):
    """README「各エージェントへの適用」5 と install-skill の表示が同じ手順（コマンド・設定キー）を指す。パスは README が ~/kairn/workspaces、表示は実パス。"""
    readme = (cfg.ROOT / "README.md").read_text(encoding="utf-8")
    notes = cli.permission_notes(tmp_path / "data")
    d = str((tmp_path / "data").resolve())
    assert d in notes and "~/kairn/workspaces" not in notes
    for needle in ("claude --add-dir", "permissions.additionalDirectories", "codex --add-dir", 'sandbox_permissions=["disk-full-read-access"]',
                   "permission.external_directory", "agy --add-dir", "trust"):
        assert needle in readme and needle in notes, needle
