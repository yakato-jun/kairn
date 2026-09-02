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
    # L-1: リポジトリ内で ignore されていない → 拒否（次の git add で入ってしまう）
    with pytest.raises(SystemExit, match="not ignored"):
        cfg.assert_data_not_tracked(repo / "data")
    (repo / ".gitignore").write_text("data/\n")
    cfg.assert_data_not_tracked(repo / "data")                       # ignore 済み・未追跡なら OK
    cfg.assert_data_not_tracked(repo / "data" / "acme")              # 配下も ignore 扱い
    cfg.assert_data_not_tracked(tmp_path / "outside")                # リポジトリ外・存在しないなら OK
    subprocess.run(["git", "-C", str(repo), "add", "-f", "data"], check=True)
    with pytest.raises(SystemExit, match="tracked by git"):
        cfg.assert_data_not_tracked(repo / "data")
    with pytest.raises(SystemExit):
        cfg.assert_data_not_tracked(repo / "data" / "acme")          # 配下でも検出


def test_cli_checkout_dry_run_does_not_rebuild_index(conf, monkeypatch, capsys, fake_drive):
    """L-5: kairn checkout --dry-run は索引（kairn.sqlite）を書き換えない。ワークスペース全体の checkout は Drive の版マーカーが違う案件だけ。"""
    import subprocess
    from kairn import cli, sync
    from kairn.store import CaseStore
    ws = conf.workspaces["acme"]
    CaseStore(ws.cases_dir).create_case("CASE-1", "t", "acme", actor="human")
    monkeypatch.setattr(cfg, "load", lambda path=None: conf)
    monkeypatch.setattr(cfg, "assert_data_not_tracked", lambda data_root=None: None)
    monkeypatch.setattr(sync, "_run", lambda cmd, dry=False, progress=None: subprocess.CompletedProcess(cmd, 0, "fake", ""))
    fake_drive.set_rev("CASE-1", "on-drive")
    monkeypatch.setattr("sys.argv", ["kairn", "checkout", "acme", "--dry-run"])
    cli.main()
    out = capsys.readouterr().out
    assert not (ws.index_dir / "kairn.sqlite").exists() and "index not rebuilt" in out and "would fetch 1 (CASE-1)" in out
    monkeypatch.setattr("sys.argv", ["kairn", "checkout", "acme"])
    cli.main()
    assert (ws.index_dir / "kairn.sqlite").exists()


def test_cli_drive_markers(conf, monkeypatch, capsys):
    """kairn drive-markers <ws> [--dry-run] [--remove-manifest]: sync.drive_markers の結果を 1 案件 1 行で表示。dry は何も書かない旨を出す。エラーは exit 1。"""
    from kairn import cli, sync
    monkeypatch.setattr(cfg, "load", lambda path=None: conf)
    monkeypatch.setattr(cfg, "assert_data_not_tracked", lambda data_root=None: None)
    seen = []
    result = {"marked": {"CASE-1": "r1"}, "mismatch": {"CASE-2": {"drive": "d2", "local": "l2"}}, "drive_no_rev": ["CASE-3"],
              "local_absent": ["CASE-4"], "errors": {}, "manifest_removed": None}
    monkeypatch.setattr(sync, "drive_markers", lambda c, ws, dry=False, remove_manifest=False: seen.append((ws.name, dry, remove_manifest))
                        or {**result, "dry": dry, "manifest_removed": (False if dry else True) if remove_manifest else None})
    monkeypatch.setattr("sys.argv", ["kairn", "drive-markers", "acme", "--dry-run"])
    cli.main()
    out = capsys.readouterr().out
    assert seen == [("acme", True, False)] and "[dry] CASE-1" in out and "marked r1" in out and "mismatch (drive d2, local l2): not touched" in out
    assert "CASE-3" in out and "has no rev" in out and "CASE-4" in out and "not on this host" in out
    assert "1 marked, 1 mismatch, 1 without rev on drive, 1 not local, 0 error(s) (dry-run: nothing written)" in out and "manifest" not in out
    monkeypatch.setattr("sys.argv", ["kairn", "drive-markers", "acme", "--remove-manifest"])
    cli.main()
    out = capsys.readouterr().out
    assert seen[-1] == ("acme", False, True) and "dry" not in out and out.rstrip().endswith("0 error(s), manifest.json removed")
    result["errors"] = {"CASE-9": "case.json is not an object"}
    with pytest.raises(SystemExit) as ei:
        cli.main()
    assert ei.value.code == 1 and "ERROR CASE-9" in capsys.readouterr().err
    monkeypatch.setattr(sync, "drive_markers", lambda c, ws, dry=False, remove_manifest=False: (_ for _ in ()).throw(sync.RcloneError("copy failed")))
    with pytest.raises(SystemExit, match="copy failed"):
        cli.main()


def test_extract_timeout_setting(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("drive: {remote: my-drive}\nworkspaces: {}\n", encoding="utf-8")
    assert cfg.load(p).extract_timeout == 600                                   # 既定
    p.write_text("drive: {remote: my-drive}\nextract: {agent: codex, timeout: 42}\nworkspaces: {}\n", encoding="utf-8")
    conf = cfg.load(p)
    assert conf.extract_timeout == 42 and conf.extract_agent == "codex"
    conf.save()
    assert "timeout: 42" in p.read_text(encoding="utf-8")
    assert cfg.create("my-drive", "claude", path=p).extract_timeout == 42        # 省略時は既存値を保つ
    assert cfg.create("my-drive", "claude", path=p, extract_timeout=90).extract_timeout == 90
    assert cfg.load(p).extract_timeout == 90
    for bad in ("0", "-1", "'10'", "true", "1.5"):
        p.write_text(f"drive: {{remote: my-drive}}\nextract: {{timeout: {bad}}}\nworkspaces: {{}}\n", encoding="utf-8")
        with pytest.raises(SystemExit, match="extract.timeout"):
            cfg.load(p)
    with pytest.raises(SystemExit):
        cfg.create("my-drive", path=tmp_path / "new.yaml", extract_timeout=0)


def test_cli_setup_writes_extract_timeout(tmp_path, monkeypatch, capsys):
    from kairn import cli
    p = tmp_path / "config.yaml"
    monkeypatch.setattr(cfg, "rclone_remotes", lambda: ["my-drive"])
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", p)
    monkeypatch.setattr(cfg, "assert_data_not_tracked", lambda data_root=None: None)
    monkeypatch.setattr("kairn.sync.list_ws_on_drive", lambda conf: [])
    monkeypatch.setattr("sys.argv", ["kairn", "setup", "--remote", "my-drive", "--agent", "codex", "--extract-timeout", "120"])
    cli.main()
    assert "extract.timeout=120s" in capsys.readouterr().out
    conf = cfg.load(p)
    assert conf.extract_timeout == 120 and conf.extract_agent == "codex"
    monkeypatch.setattr("sys.argv", ["kairn", "setup", "--remote", "my-drive"])
    cli.main()
    assert cfg.load(p).extract_timeout == 120                                    # --extract-timeout 省略時は既存値を保つ
    assert cfg.load(p).extract_agent == "codex"                                  # --agent 省略時も既存値を保つ（M-4）


def test_create_keeps_existing_agent_when_omitted(tmp_path):
    p = tmp_path / "config.yaml"
    assert cfg.create("my-drive", path=p).extract_agent == "claude"              # 初回の既定
    assert cfg.create("my-drive", "opencode", path=p).extract_agent == "opencode"
    assert cfg.create("my-drive", path=p).extract_agent == "opencode"            # 省略時は既存値
    assert cfg.load(p).extract_agent == "opencode"


def test_cli_attach_detach_status_record_mapping_only(conf, tmp_path, monkeypatch, capsys):
    """attach はリポジトリ → ワークスペースの対応を設定に書くだけ。リポジトリ側にリンクを作らず、既存の tmp/ 実体があっても拒否しない。
    detach は対応を消すだけ。status は所属リポジトリを列挙する（リンク状態は表示しない）。"""
    from kairn import cli
    repo = tmp_path / "acme-robot"; (repo / "tmp" / "notes").mkdir(parents=True)
    (repo / "tmp" / "notes" / "a.md").write_text("keep")
    # 既存の設定に link_name が残っていても読めて、保存で消える（旧版からの移行）
    p = tmp_path / "old.yaml"
    p.write_text(f"drive: {{remote: my-drive}}\nworkspaces:\n  acme:\n    repos: [{repo}]\n    link_name: tmp\n", encoding="utf-8")
    old = cfg.load(p); assert not hasattr(old.workspaces["acme"], "link_name"); old.save()
    assert "link_name" not in p.read_text(encoding="utf-8") and old.workspaces["acme"].repos == [repo]
    monkeypatch.setattr(cfg, "load", lambda path=None: conf)
    monkeypatch.setattr(cfg, "assert_data_not_tracked", lambda data_root=None: None)
    monkeypatch.setattr("sys.argv", ["kairn", "attach", "acme", str(repo)])
    cli.main()
    out = capsys.readouterr().out
    assert "acme" in out and "link" not in out
    assert not (repo / "tmp").is_symlink() and (repo / "tmp" / "notes" / "a.md").read_text() == "keep"
    assert sorted(p.name for p in repo.iterdir()) == ["tmp"]
    assert conf.workspace_for_path(repo / "src").name == "acme"
    raw = conf.path.read_text(encoding="utf-8")
    assert str(repo) in raw and "link_name" not in raw
    # status: 所属だけ
    monkeypatch.setattr("sys.argv", ["kairn", "status"])
    cli.main()
    out = capsys.readouterr().out
    assert f"    {repo}\n" in out and "linked" not in out and "no link" not in out and "EXISTS" not in out
    # detach: 対応を消すだけ。リポジトリ側は触らない
    monkeypatch.setattr("sys.argv", ["kairn", "detach", str(repo)])
    cli.main()
    assert "detached" in capsys.readouterr().out
    assert conf.workspace_for_path(repo) is None and (repo / "tmp" / "notes" / "a.md").read_text() == "keep"
    assert str(repo) not in conf.path.read_text(encoding="utf-8")


# ---------- rules の編集（kairn rules … / UI）: 検証して保存、他のキーは壊さない ----------

def test_rules_edit_functions_validate_and_save(tmp_path):
    from kairn import sync
    p = tmp_path / "config.yaml"
    p.write_text("drive: {remote: my-drive}\nextract: {agent: codex, timeout: 42}\nserve: {port: 9000}\n"
                 "workspaces: {acme: {description: d, repos: []}}\n", encoding="utf-8")
    conf = cfg.load(p)
    defaults_before = __import__("copy").deepcopy(cfg.DEFAULT_RULES)
    assert cfg.set_rule(conf, "raw_data.min_size", "10M") == "10M"
    assert cfg.set_rule(conf, "raw_data.min_age", "7d") == "7d"
    assert cfg.set_rule(conf, "bag_to_zst", "false") is False and cfg.set_rule(conf, "bag_to_zst", "YES") is True
    assert cfg.set_rule(conf, "bwlimit", "08:00,4M   20:00,off") == "08:00,4M 20:00,off"
    assert cfg.set_rule(conf, "rclone_flags", " --transfers 8  --checkers 16 ") == ["--transfers", "8", "--checkers", "16"]
    assert cfg.add_exclude(conf, "logs/**") is True and cfg.add_exclude(conf, "logs/**") is False    # 重複は no-op
    assert cfg.add_raw_ext(conf, ".MCAP") is True and cfg.add_raw_ext(conf, "mcap") is False        # 先頭の . を外し小文字
    re_ = cfg.load(p)
    assert re_.rules["raw_data"]["min_size"] == "10M" and re_.rules["raw_data"]["min_age"] == "7d" and re_.rules["bag_to_zst"] is True
    assert re_.rules["bwlimit"] == "08:00,4M 20:00,off" and re_.rules["exclude"][-1] == "logs/**" and re_.rules["raw_data"]["extensions"][-1] == "mcap"
    assert re_.rules["rclone_flags"] == ["--transfers", "8", "--checkers", "16"] and sync._flags(re_) == ["--transfers", "8", "--checkers", "16"]
    assert re_.rules["exclude"][:-1] == cfg.DEFAULT_RULES["exclude"]                             # 既存の項目はそのまま
    assert re_.extract_agent == "codex" and re_.extract_timeout == 42 and re_.serve_port == 9000     # 他のキーは壊さない
    assert list(re_.workspaces) == ["acme"] and re_.workspaces["acme"].description == "d"
    assert sync._filters(re_)[-2:] == ["--max-size", "10M"] and "- logs/**" in sync._filters(re_) and sync._bw(re_) == ["--bwlimit", "08:00,4M 20:00,off"]
    cfg.remove_exclude(conf, "logs/**"); cfg.remove_raw_ext(conf, "mcap")
    assert cfg.set_rule(conf, "bwlimit", "off") is None                                             # off = 制限なし（キーを消す）
    assert cfg.set_rule(conf, "rclone_flags", "") == []                                              # 空 = 既定（キーを消す）
    re_ = cfg.load(p)
    assert "logs/**" not in re_.rules["exclude"] and "mcap" not in re_.rules["raw_data"]["extensions"] and "bwlimit" not in re_.rules
    assert "rclone_flags" not in re_.rules and sync._flags(re_) == []
    assert cfg.DEFAULT_RULES == defaults_before                                                      # 既定値の入れ子を壊していない
    view = cfg.rules_view(re_)
    assert view["raw_data.min_size"] == "10M" and view["bwlimit"] is None and view["bag_to_zst"] is True and "*.pyc" in view["exclude"]
    assert view["rclone_flags"] == []


@pytest.mark.parametrize("fn, args, msg", [
    ("set_rule", ("raw_data.min_size", "10X"), "invalid size"),
    ("set_rule", ("raw_data.min_age", "soon"), "invalid age"),
    ("set_rule", ("bag_to_zst", "maybe"), "true or false"),
    ("set_rule", ("bwlimit", "4M,08:00"), "invalid bwlimit"),
    ("set_rule", ("bwlimit", ""), "value is required"),
    ("set_rule", ("exclude", "x"), "unknown rule"),
    ("set_rule", ("rclone_flags", "-v"), "invalid rclone flag"),
    ("set_rule", ("rclone_flags", "--transfers 8 16"), "invalid rclone flag"),
    ("set_rule", ("rclone_flags", "8 --transfers"), "invalid rclone flag"),
    ("set_rule", ("rclone_flags", "rm -rf"), "invalid rclone flag"),
    ("add_exclude", ("",), "non-empty"),
    ("add_exclude", ("a b",), "without whitespace"),
    ("remove_exclude", ("nope/**",), "is not set"),
    ("add_raw_ext", ("a/b",), "expected e.g. bag"),
    ("add_raw_ext", ("*.bag",), "expected e.g. bag"),
    ("remove_raw_ext", ("xyz",), "is not set"),
])
def test_rules_edit_rejects_invalid_values_without_saving(tmp_path, fn, args, msg):
    p = tmp_path / "config.yaml"
    p.write_text("drive: {remote: my-drive}\nworkspaces: {}\n", encoding="utf-8")
    conf = cfg.load(p)
    before = p.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match=msg):
        getattr(cfg, fn)(conf, *args)
    assert p.read_text(encoding="utf-8") == before


def test_parse_bwlimit():
    from kairn.sync import parse_bwlimit
    for ok in ("4M", "off", "1M:2M", "08:00,4M 20:00,off", "Sat-10:00,1M Sun-20:00,off", "512k", "1.5G"):
        assert parse_bwlimit(ok) == ok
    for bad in ("", "fast", "4M,08:00", "1M:", "4M:off:1M", "08:00:4M"):
        with pytest.raises(ValueError):
            parse_bwlimit(bad)


def test_cli_rules_show_set_add_remove(conf, monkeypatch, capsys):
    from kairn import cli
    conf.save()
    orig_load = cfg.load
    monkeypatch.setattr(cfg, "load", lambda path=None: orig_load(conf.path))
    monkeypatch.setattr(cfg, "assert_data_not_tracked", lambda data_root=None: None)

    def run(*argv):
        monkeypatch.setattr("sys.argv", ["kairn", "rules", *argv])
        cli.main()
        return capsys.readouterr().out
    out = run("show")
    assert "raw_data.min_size:   50M" in out and "bag_to_zst:          true" in out and "bwlimit:             (none)" in out and "  target/**" in out
    assert "rclone_flags:        (none)" in out
    assert "raw_data.min_size = 10M" in run("set", "raw_data.min_size", "10M") and cfg.load(conf.path).rules["raw_data"]["min_size"] == "10M"
    assert "bag_to_zst = false" in run("set", "bag_to_zst", "false") and cfg.load(conf.path).rules["bag_to_zst"] is False
    assert "bwlimit = 4M" in run("set", "bwlimit", "4M") and cfg.load(conf.path).rules["bwlimit"] == "4M"
    assert "rclone_flags = --transfers 8 --checkers 16" in run("set", "rclone_flags", "--transfers 8 --checkers 16")
    assert cfg.load(conf.path).rules["rclone_flags"] == ["--transfers", "8", "--checkers", "16"]
    assert "rclone_flags:        --transfers 8 --checkers 16" in run("show")
    assert "rclone_flags = --fast-list" in run("set", "rclone_flags", "--fast-list")                 # 単一トークンも argparse に食われない
    assert "rclone_flags = --transfers 8" in run("set", "rclone_flags", "--", "--transfers 8")       # 明示の -- も可
    assert "rclone_flags = --transfers 8 --checkers 16" in run("set", "rclone_flags", "--transfers 8 --checkers 16")
    with pytest.raises(SystemExit, match="invalid rclone flag"):
        run("set", "rclone_flags", "-v")
    assert cfg.load(conf.path).rules["rclone_flags"] == ["--transfers", "8", "--checkers", "16"]   # 拒否時は保存しない
    assert "rclone_flags = (none)" in run("set", "rclone_flags", "") and "rclone_flags" not in cfg.load(conf.path).rules
    assert "exclude += logs/**" in run("add-exclude", "logs/**") and "logs/**" in cfg.load(conf.path).rules["exclude"]
    assert "already has" in run("add-exclude", "logs/**")
    assert "exclude -= logs/**" in run("remove-exclude", "logs/**") and "logs/**" not in cfg.load(conf.path).rules["exclude"]
    assert "raw_data.extensions += mcap" in run("add-raw-ext", "mcap") and "mcap" in cfg.load(conf.path).rules["raw_data"]["extensions"]
    assert "raw_data.extensions -= mcap" in run("remove-raw-ext", "mcap") and "mcap" not in cfg.load(conf.path).rules["raw_data"]["extensions"]
    assert "raw_data.min_size:   10M" in run("show") and "saved" not in capsys.readouterr().out
    # 検証エラー: 終了コード 1 相当（SystemExit にメッセージ）、保存しない
    before = conf.path.read_text(encoding="utf-8")
    with pytest.raises(SystemExit, match="invalid size"):
        run("set", "raw_data.min_size", "lots")
    with pytest.raises(SystemExit, match="is not set"):
        run("remove-exclude", "nope/**")
    with pytest.raises(SystemExit):                                                 # 未知のキーは argparse が拒否
        run("set", "exclude", "x")
    assert conf.path.read_text(encoding="utf-8") == before


# ---------- 共有 client_id の警告（kairn setup / status）: rclone config show をモック。秘密の値は出力しない ----------

SECRET_TOKEN = '{"access_token":"ya29.SECRET-ACCESS","refresh_token":"1//SECRET-REFRESH","expiry":"2026-09-02T00:00:00Z"}'
SECRET_CLIENT = "GOCSPX-SECRET-CLIENT-SECRET"


def _fake_config_show(stdout: str | None, rc: int = 0, missing: bool = False):
    import subprocess

    def run(cmd, **kw):
        assert cmd[:3] == ["rclone", "config", "show"] and kw.get("capture_output") and kw.get("timeout")
        if missing:
            raise FileNotFoundError("rclone")
        return subprocess.CompletedProcess(cmd, rc, stdout or "", "")
    return run


@pytest.mark.parametrize("stdout, rc, missing, warned", [
    (f"[my-drive]\ntype = drive\nscope = drive\ntoken = {SECRET_TOKEN}\nteam_drive = \n", 0, False, True),          # client_id 無し
    (f"[my-drive]\ntype = drive\nclient_id = \nclient_secret = \ntoken = {SECRET_TOKEN}\n", 0, False, True),         # client_id 空
    (f"[my-drive]\ntype = drive\nclient_id = 123-abc.apps.googleusercontent.com\nclient_secret = {SECRET_CLIENT}\ntoken = {SECRET_TOKEN}\n", 0, False, False),
    (f"[my-drive]\ntype = s3\nprovider = AWS\naccess_key_id = AKIA-SECRET\nsecret_access_key = SECRET\n", 0, False, False),   # drive 以外
    ("", 0, True, False),                                                                                            # rclone 不在
    ("[my-drive]\n# couldn't find type of fs for \"my-drive\"\n", 0, False, False),                                  # remote 不明（rc 0、type 無し）
    ("", 1, False, False),                                                                                           # 失敗
])
def test_shared_client_id_warning(monkeypatch, stdout, rc, missing, warned):
    import subprocess
    monkeypatch.setattr(subprocess, "run", _fake_config_show(stdout, rc, missing))
    w = cfg.shared_client_id_warning("my-drive")
    if warned:
        assert w and "my-drive" in w and "共有 client_id" in w and "専用 OAuth クライアント" in w
        assert "SECRET" not in w and "ya29" not in w and "GOCSPX" not in w
    else:
        assert w is None


def test_cli_setup_and_status_warn_about_shared_client_id(tmp_path, monkeypatch, capsys):
    """setup / status は type = drive で client_id が無い remote に警告を 1 行（stderr）。client_id があれば出さない。token の値は出さない。"""
    import subprocess
    from kairn import cli
    p = tmp_path / "config.yaml"
    monkeypatch.setattr(cfg, "rclone_remotes", lambda: ["my-drive"])
    monkeypatch.setattr(cfg, "USER_CONFIG_PATH", p)
    monkeypatch.setattr(cfg, "assert_data_not_tracked", lambda data_root=None: None)
    monkeypatch.setattr("kairn.sync.list_ws_on_drive", lambda conf: [])
    monkeypatch.setattr(subprocess, "run", _fake_config_show(f"[my-drive]\ntype = drive\ntoken = {SECRET_TOKEN}\n"))
    monkeypatch.setattr("sys.argv", ["kairn", "setup", "--remote", "my-drive"])
    cli.main()
    out, err = capsys.readouterr()
    assert "config written" in out and err.count("warning:") == 1 and "共有 client_id" in err and "SECRET" not in out + err
    monkeypatch.setattr("sys.argv", ["kairn", "status"])
    cli.main()
    out, err = capsys.readouterr()
    assert "remote: my-drive" in out and err.count("warning:") == 1 and "SECRET" not in out + err
    # client_id があれば何も言わない
    monkeypatch.setattr(subprocess, "run", _fake_config_show(f"[my-drive]\ntype = drive\nclient_id = 123-abc.apps.googleusercontent.com\nclient_secret = {SECRET_CLIENT}\n"))
    cli.main()
    out, err = capsys.readouterr()
    assert "remote: my-drive" in out and err == ""
    # rclone 不在でも何も言わない（status は動く）
    monkeypatch.setattr(subprocess, "run", _fake_config_show(None, missing=True))
    cli.main()
    out, err = capsys.readouterr()
    assert "remote: my-drive" in out and err == ""


# ---------- ConfigHolder（常駐の設定: リクエストごとに config.yaml の更新を確認して読み直す） ----------

def _holder(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("drive: {remote: my-drive}\nworkspaces: {acme: {repos: []}}\n", encoding="utf-8")
    loads = []
    warnings = []

    def loader(path):
        loads.append(path)
        return cfg.load(path)
    return p, cfg.ConfigHolder(cfg.load(p), loader=loader, warn=warnings.append), loads, warnings


def _touch_newer(p):
    """mtime を確実に進める（同じ ns に書き戻されても変更と分かるように +1 秒）。"""
    import os
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_config_holder_reloads_only_when_file_changes(tmp_path):
    p, h, loads, warnings = _holder(tmp_path)
    first = h.current()
    assert h.current() is first and h.current() is first and loads == [] and h.reloads == 0    # 変更なし → 読み直さない
    assert h.path == p
    p.write_text("drive: {remote: my-drive}\nworkspaces: {acme: {repos: []}, beta: {repos: []}}\n", encoding="utf-8")
    _touch_newer(p)
    second = h.current()
    assert second is not first and list(second.workspaces) == ["acme", "beta"] and loads == [p] and h.reloads == 1
    assert h.current() is second and loads == [p] and warnings == []                          # 読み直したあとは再び安定
    # Config.save()（UI / CLI の rules 編集）でも同じ: 保存後の最初の current() で新しい Config
    cfg.set_rule(second, "raw_data.min_size", "10M")
    _touch_newer(p)
    third = h.current()
    assert third is not second and third.rules["raw_data"]["min_size"] == "10M" and h.reloads == 2


def test_config_holder_keeps_previous_config_when_file_is_broken(tmp_path):
    p, h, loads, warnings = _holder(tmp_path)
    good = h.current()
    p.write_text("drive: {remote: [unclosed\n", encoding="utf-8")                            # 壊れた YAML
    _touch_newer(p)
    assert h.current() is good and loads == [p] and len(warnings) == 1
    assert str(p) in warnings[0] and "could not be reloaded" in warnings[0] and "keeping" in warnings[0]
    assert h.current() is good and loads == [p] and len(warnings) == 1                        # 同じ壊れた版で繰り返し警告しない
    p.write_text("drive: {}\nworkspaces: {}\n", encoding="utf-8")                              # remote 無し（load は SystemExit）
    _touch_newer(p)
    assert h.current() is good and loads == [p, p] and len(warnings) == 2 and "SystemExit" in warnings[1]
    p.write_text("drive: {remote: my-drive}\nworkspaces: {beta: {repos: []}}\n", encoding="utf-8")   # 直ればその版に切り替わる
    _touch_newer(p)
    assert list(h.current().workspaces) == ["beta"] and h.reloads == 1 and len(warnings) == 2


def test_config_holder_keeps_previous_config_when_file_disappears(tmp_path):
    p, h, loads, warnings = _holder(tmp_path)
    good = h.current()
    p.unlink()
    assert h.current() is good and loads == [] and len(warnings) == 1 and "disappeared" in warnings[0] and str(p) in warnings[0]
    assert h.current() is good and len(warnings) == 1                                          # 無いままなら繰り返し警告しない
    p.write_text("drive: {remote: my-drive}\nworkspaces: {beta: {repos: []}}\n", encoding="utf-8")   # 戻れば読み直す
    assert list(h.current().workspaces) == ["beta"] and loads == [p] and len(warnings) == 1


def test_config_holder_default_warning_goes_to_stderr(tmp_path, capsys):
    p = tmp_path / "config.yaml"
    p.write_text("drive: {remote: my-drive}\nworkspaces: {}\n", encoding="utf-8")
    h = cfg.ConfigHolder(cfg.load(p))
    p.write_text("drive: {remote: [unclosed\n", encoding="utf-8")
    _touch_newer(p)
    assert h.current().remote == "my-drive"
    err = capsys.readouterr().err
    assert err.startswith("kairn: config ") and err.count("\n") == 1
