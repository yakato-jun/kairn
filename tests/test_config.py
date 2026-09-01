from kairn import config as cfg
import pytest


def test_load_refuses_missing_remote(tmp_path):
    p = tmp_path / "ws.yaml"
    p.write_text("drive: {}\nworkspaces: {}\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        cfg.load(p)


def test_load_refuses_missing_user_config(tmp_path):
    with pytest.raises(SystemExit):
        cfg.load(tmp_path / "missing.yaml")


def test_load_reads_remote(tmp_path):
    p = tmp_path / "ws.yaml"
    p.write_text("drive: {remote: my-drive}\nworkspaces: {}\n", encoding="utf-8")
    assert cfg.load(p).remote == "my-drive"
