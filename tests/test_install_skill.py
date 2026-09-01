"""kairn install-skill: 一時 HOME にリンクを作る。実 HOME（~/.agents / ~/.claude）には触れない。"""
from __future__ import annotations

import os
from pathlib import Path

from kairn import cli, config as cfg

SRC = cfg.ROOT / "skills" / "kairn"


def test_install_skill_links_both_and_keeps_existing(tmp_path: Path, monkeypatch, capsys):
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
    assert lines[1].startswith("exists:") and "fix by hand" in lines[1] and os.readlink(home2 / ".claude" / "skills" / "kairn") == str(tmp_path)
    # CLI 経由（--home）。実 HOME には触れない
    monkeypatch.setattr(cfg, "assert_data_not_tracked", lambda data_root=None: None)
    home3 = tmp_path / "home3"
    monkeypatch.setattr("sys.argv", ["kairn", "install-skill", "--home", str(home3)])
    cli.main()
    out = capsys.readouterr().out
    assert out.count("linked ") == 2 and (home3 / ".claude" / "skills" / "kairn").is_symlink()
