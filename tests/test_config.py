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


def test_repos_glob_and_path_forms(tmp_path):
    for d in ("Acme1", "Acme2", "other"):
        (tmp_path / d).mkdir()
    (tmp_path / "Acme3.txt").write_text("not a dir")
    p = tmp_path / "config.yaml"
    p.write_text(f"drive: {{remote: my-drive}}\nworkspaces:\n  acme:\n    repos:\n      - glob: {tmp_path}/Acme*\n      - path: {tmp_path}/other\n      - {tmp_path}/other\n", encoding="utf-8")
    conf = cfg.load(p)
    ws = conf.workspaces["acme"]
    assert ws.repos == [tmp_path / "Acme1", tmp_path / "Acme2", tmp_path / "other"]   # ディレクトリのみ、重複は 1 つ
    assert conf.workspace_for_path(tmp_path / "Acme2" / "src").name == "acme"
    assert conf.workspace_for_path(tmp_path / "Acme3.txt") is None
    # 保存しても glob: の形は保たれる（展開済みパスに書き換えない）
    conf.save()
    raw = p.read_text(encoding="utf-8")
    assert f"glob: {tmp_path}/Acme*" in raw and f"path: {tmp_path}/other" in raw
    (tmp_path / "Acme9").mkdir()
    assert tmp_path / "Acme9" in cfg.load(p).workspaces["acme"].repos
    # attach / detach 相当
    ws = cfg.load(p).workspaces["acme"]
    ws.add_repo(tmp_path / "solo"); assert ws.repo_specs[-1] == str(tmp_path / "solo")
    ws.remove_repo((tmp_path / "solo").resolve()); assert str(tmp_path / "solo") not in ws.repo_specs and tmp_path / "solo" not in ws.repos
    ws.remove_repo((tmp_path / "other").resolve()); assert not any(("other" in str(s)) for s in ws.repo_specs)
    # glob: 由来は {exclude: <path>} を追記して外す（glob 行は残る）。再読み込みでも除外されたまま。二重 detach でも exclude は 1 行
    ws.remove_repo((tmp_path / "Acme1").resolve())
    assert {"exclude": str(tmp_path / "Acme1")} in ws.repo_specs and any(isinstance(s, dict) and "glob" in s for s in ws.repo_specs)
    assert tmp_path / "Acme1" not in ws.repos and tmp_path / "Acme2" in ws.repos
    ws.remove_repo((tmp_path / "Acme1").resolve())
    assert sum(1 for s in ws.repo_specs if isinstance(s, dict) and "exclude" in s) == 1
    conf.workspaces["acme"] = ws; conf.save()
    ws2 = cfg.load(p).workspaces["acme"]
    assert tmp_path / "Acme1" not in ws2.repos and tmp_path / "Acme2" in ws2.repos and tmp_path / "Acme9" in ws2.repos
    assert cfg.load(p).workspace_for_path(tmp_path / "Acme1" / "src") is None


def test_exclude_does_not_remove_explicit_path(tmp_path):
    (tmp_path / "Acme1").mkdir()
    p = tmp_path / "config.yaml"
    p.write_text(f"drive: {{remote: my-drive}}\nworkspaces:\n  acme:\n    repos:\n      - glob: {tmp_path}/Acme*\n      - exclude: {tmp_path}/Acme1\n      - path: {tmp_path}/Acme1\n", encoding="utf-8")
    assert cfg.load(p).workspaces["acme"].repos == [tmp_path / "Acme1"]   # path: の明示登録は exclude で外れない


@pytest.mark.parametrize("entry", ["- ''", "- path: ''", "- glob: ''", "- exclude: ''", "- {}", "- foo: /x", "- {path: /x, glob: /y}", "- 3"])
def test_repos_rejects_unknown_or_empty_entries(tmp_path, entry):
    p = tmp_path / "config.yaml"
    p.write_text(f"drive: {{remote: my-drive}}\nworkspaces:\n  acme:\n    repos:\n      {entry}\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        cfg.load(p)


def test_assert_data_not_tracked_checks_data_root(tmp_path):
    import subprocess
    repo = tmp_path / "repo"; (repo / "data" / "acme").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "data" / "acme" / "case.json").write_text("{}")
    cfg.assert_data_not_tracked(repo / "data")                       # 未追跡なら OK
    cfg.assert_data_not_tracked(tmp_path / "outside")                # リポジトリ外・存在しないなら OK
    subprocess.run(["git", "-C", str(repo), "add", "data"], check=True)
    with pytest.raises(SystemExit, match="tracked by git"):
        cfg.assert_data_not_tracked(repo / "data")
    with pytest.raises(SystemExit):
        cfg.assert_data_not_tracked(repo / "data" / "acme")          # 配下でも検出
