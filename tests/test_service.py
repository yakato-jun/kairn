"""kairn install-service / kairn ensure。unit は一時ディレクトリ（XDG_CONFIG_HOME / XDG_STATE_HOME）に書き、
systemctl / loginctl / Popen / HTTP 応答はすべてモック。実 HOME・実サービスには触れない。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from kairn import cli, config as cfg, service

KAIRN = "/opt/acme/bin/kairn"


@pytest.fixture
def env(tmp_path: Path, monkeypatch, conf):
    """XDG を一時ディレクトリへ、argv[0] を偽の実行パスへ、systemctl/loginctl の呼び出しを記録するモックへ。"""
    (tmp_path / "xdg").mkdir(); (tmp_path / "state").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(service, "self_command", lambda: [KAIRN])
    monkeypatch.setattr(service, "have_systemctl", lambda: True)
    monkeypatch.setattr(service, "interactive", lambda: False)
    monkeypatch.setattr(cfg, "load", lambda path=None: conf)
    monkeypatch.setattr(cfg, "assert_data_not_tracked", lambda data_root=None: None)
    monkeypatch.setenv("USER", "someone")
    calls: list[list[str]] = []

    def fake_run(argv):
        calls.append(list(argv))
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            return 0, "active"
        return 0, ""
    monkeypatch.setattr(service, "run_cmd", fake_run)
    return {"calls": calls, "units": tmp_path / "xdg" / "systemd" / "user", "state": tmp_path / "state" / "kairn", "conf": conf}


def _main(monkeypatch, *args) -> int:
    monkeypatch.setattr("sys.argv", ["kairn", *args])
    try:
        cli.main()
    except SystemExit as e:
        return int(e.code or 0)
    return 0


def test_render_units_embeds_exec_path_port_and_workspaces():
    opts = service.ServiceOptions(host="127.0.0.1", port=9999, workspaces=["acme", "personal"], daily_time="3:05")
    units = service.render_units(opts, ["/home/someone/.local/bin/kairn"])
    assert set(units) == {"kairn-serve.service", "kairn-daily@.service", "kairn-daily@acme.timer", "kairn-daily@personal.timer"}
    serve = units["kairn-serve.service"]
    assert "ExecStart=/home/someone/.local/bin/kairn serve --host 127.0.0.1 --port 9999" in serve
    assert "Restart=on-failure" in serve and "WantedBy=default.target" in serve
    assert "TimeoutStopSec=15" in serve and "KillSignal=SIGTERM" in serve   # 停止が systemd 既定の 90 秒を待たない
    daily = units["kairn-daily@.service"]
    assert "ExecStart=/home/someone/.local/bin/kairn daily %i" in daily and "Type=oneshot" in daily
    timer = units["kairn-daily@acme.timer"]
    assert "OnCalendar=*-*-* 03:05:00" in timer and "Persistent=true" in timer and "RandomizedDelaySec=10m" in timer
    assert "Unit=kairn-daily@acme.service" in timer and "WantedBy=timers.target" in timer
    # 空白を含む実行パスは引用符で囲む。python -m 形式も通る
    assert 'ExecStart="/opt/my tools/kairn" serve' in service.render_units(opts, ["/opt/my tools/kairn"])["kairn-serve.service"]
    assert "ExecStart=/usr/bin/python3 -m kairn.cli daily %i" in service.render_units(opts, ["/usr/bin/python3", "-m", "kairn.cli"])["kairn-daily@.service"]
    with pytest.raises(ValueError):
        service.render_units(service.ServiceOptions(daily_time="25:00", workspaces=["acme"]), [KAIRN])


def test_self_command_prefers_console_script(tmp_path: Path, monkeypatch):
    script = tmp_path / "bin" / "kairn"; script.parent.mkdir(); script.write_text("#!/bin/sh\n")
    monkeypatch.setattr("sys.argv", [str(script), "install-service"])
    assert service.self_command() == [str(script.resolve())]
    monkeypatch.setattr("sys.argv", [str(tmp_path / "kairn" / "cli.py")])
    cmd = service.self_command()
    assert cmd[1:] == ["-m", "kairn.cli"] and cmd[0] == __import__("sys").executable


def test_install_service_print_only(env, monkeypatch, capsys):
    rc = _main(monkeypatch, "install-service", "--print")
    out = capsys.readouterr().out
    assert rc == 0
    assert f"ExecStart={KAIRN} serve --host 127.0.0.1 --port 8765" in out
    assert f"ExecStart={KAIRN} daily %i" in out
    assert "kairn-daily@acme.timer" in out and "Unit=kairn-daily@acme.service" in out and "OnCalendar=*-*-* 12:30:00" in out
    assert str(env["units"] / "kairn-serve.service") in out
    assert "TimeoutStopSec=15" in out
    assert not env["units"].exists() and env["calls"] == []      # ファイルもコマンドも実行しない
    assert not env["conf"].path.exists()


def test_install_service_yes_writes_units_and_enables(env, monkeypatch, capsys):
    rc = _main(monkeypatch, "install-service", "--yes")
    out = capsys.readouterr().out
    assert rc == 0, out
    units = env["units"]
    assert sorted(p.name for p in units.iterdir()) == ["kairn-daily@.service", "kairn-daily@acme.timer", "kairn-serve.service"]
    assert f"ExecStart={KAIRN} serve --host 127.0.0.1 --port 8765" in (units / "kairn-serve.service").read_text()
    assert env["calls"] == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "kairn-serve.service", "kairn-daily@acme.timer"],
        ["systemctl", "--user", "is-active", "kairn-serve.service"],
        ["systemctl", "--user", "is-active", "kairn-daily@acme.timer"],
    ]
    assert "kairn-serve.service: active" in out and "kairn-daily@acme.timer: active" in out
    # 設定に serve.host / serve.port が書かれる（ensure が使う）
    saved = cfg._parse(yaml.safe_load(env["conf"].path.read_text(encoding="utf-8")), env["conf"].path)
    assert (saved.serve_host, saved.serve_port) == ("127.0.0.1", 8765)
    # 同じ内容で再実行: unchanged、上書き確認なし
    rc = _main(monkeypatch, "install-service", "--yes")
    out = capsys.readouterr().out
    assert rc == 0 and out.count("unchanged:") == 3 and "overwrote:" not in out


def test_install_service_interactive_answers(env, monkeypatch, capsys):
    monkeypatch.setattr(service, "interactive", lambda: True)
    answers = iter([
        "0.0.0.0", "n",          # 公開アドレス → 警告 → 取りやめ
        "127.0.0.1",             # ローカルに戻す
        "70000", "9000",         # 不正なポート → 再入力
        "acme,nope", "1",        # 設定に無い名前 → 再入力（番号）
        "9:99", "06:15",         # 不正な時刻 → 再入力
        "n",                     # enable --now しない
        "y",                     # linger する
    ])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    rc = _main(monkeypatch, "install-service")
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "ネットワークに公開されます" in out
    serve = (env["units"] / "kairn-serve.service").read_text()
    assert "--host 127.0.0.1 --port 9000" in serve
    assert "OnCalendar=*-*-* 06:15:00" in (env["units"] / "kairn-daily@acme.timer").read_text()
    assert ["systemctl", "--user", "enable", "kairn-serve.service", "kairn-daily@acme.timer"] in env["calls"]
    assert ["loginctl", "enable-linger", "someone"] in env["calls"]
    assert "管理者認証" in out
    assert "MCP 登録 URL を http://127.0.0.1:9000/mcp" in out


def test_install_service_linger_failure_does_not_stop(env, monkeypatch, capsys):
    calls = env["calls"]

    def fake_run(argv):
        calls.append(list(argv))
        if argv[0] == "loginctl":
            return 1, "Interactive authentication required."
        return 0, "active" if "is-active" in argv else ""
    monkeypatch.setattr(service, "run_cmd", fake_run)
    opts = service.ServiceOptions(workspaces=["acme"], linger=True)
    rc = service.install(env["conf"], opts, yes=True, cmd=[KAIRN])
    out = capsys.readouterr().out
    assert rc == 0 and "警告: enable-linger に失敗" in out
    assert calls[-1] == ["systemctl", "--user", "is-active", "kairn-daily@acme.timer"]


def test_install_service_existing_unit_refused_without_yes_and_overwritten_with_yes(env, monkeypatch, capsys):
    units = env["units"]; units.mkdir(parents=True)
    (units / "kairn-serve.service").write_text("[Service]\nExecStart=/somewhere/else/kairn serve\n")
    rc = _main(monkeypatch, "install-service")                 # 非対話（端末でない）・--yes なし → 拒否、何も書かない
    out = capsys.readouterr().out
    assert rc == 1 and "上書きしません" in out and "+ExecStart=" in out and "-ExecStart=/somewhere/else/kairn serve" in out
    assert (units / "kairn-serve.service").read_text().startswith("[Service]") and not (units / "kairn-daily@.service").exists()
    assert env["calls"] == []
    # 対話で n → 中止
    monkeypatch.setattr(service, "interactive", lambda: True)
    answers = iter(["", "", "", "", "", "", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    rc = _main(monkeypatch, "install-service")
    assert rc == 1 and "中止" in capsys.readouterr().out and not (units / "kairn-daily@.service").exists()
    # --yes → 上書き
    monkeypatch.setattr(service, "interactive", lambda: False)
    rc = _main(monkeypatch, "install-service", "--yes")
    out = capsys.readouterr().out
    assert rc == 0 and "overwrote:" in out and f"ExecStart={KAIRN} serve" in (units / "kairn-serve.service").read_text()


def test_install_service_without_systemctl_writes_units_and_explains(env, monkeypatch, capsys):
    monkeypatch.setattr(service, "have_systemctl", lambda: False)
    rc = _main(monkeypatch, "install-service", "--yes")
    out = capsys.readouterr().out
    assert rc == 0 and (env["units"] / "kairn-serve.service").exists()
    assert env["calls"] == []
    assert "systemctl が見つかりません" in out and "systemctl --user daemon-reload && systemctl --user enable --now kairn-serve.service kairn-daily@acme.timer" in out


def test_install_service_systemctl_failure_returns_its_code(env, monkeypatch, capsys):
    monkeypatch.setattr(service, "run_cmd", lambda argv: (3, "Failed to connect to bus"))
    rc = service.install(env["conf"], service.ServiceOptions(workspaces=[]), yes=True, cmd=[KAIRN])
    assert rc == 3 and "Failed to connect to bus" in capsys.readouterr().out


def test_interview_defaults_without_workspaces(conf, monkeypatch):
    conf.workspaces = {}
    opts = service.interview(conf, yes=True)
    assert opts.workspaces == [] and opts.host == "127.0.0.1" and opts.port == 8765 and opts.enable_now and not opts.linger


# ---- ensure ------------------------------------------------------------------------------------

class _Proc:
    pid = 4242

    def __init__(self, exit_after: int | None = None):
        self.polls = 0; self.exit_after = exit_after

    def poll(self):
        self.polls += 1
        return 0 if self.exit_after is not None and self.polls >= self.exit_after else None


def test_ensure_does_nothing_when_alive(env, capsys):
    started = []
    rc = service.ensure(env["conf"], alive=lambda h, p: True, popen=lambda *a, **k: started.append(a) or _Proc(), cmd=[KAIRN])
    assert rc == 0 and started == [] and "is running" in capsys.readouterr().out


def test_ensure_starts_detached_and_waits(env, capsys):
    conf = env["conf"]; conf.serve_host, conf.serve_port = "127.0.0.1", 9100
    probes = iter([False, False, False, True])
    started = []

    def fake_popen(argv, **kw):
        started.append((argv, kw)); return _Proc()
    rc = service.ensure(conf, alive=lambda h, p: next(probes), popen=fake_popen, sleep=lambda s: None, cmd=[KAIRN])
    out = capsys.readouterr().out
    assert rc == 0 and "is up: http://127.0.0.1:9100/mcp" in out
    (argv, kw), = started
    assert argv == [KAIRN, "serve", "--host", "127.0.0.1", "--port", "9100"]
    assert kw["start_new_session"] is True and kw["stdin"] is subprocess.DEVNULL and kw["stderr"] is subprocess.STDOUT
    log = env["state"] / "serve.log"
    assert kw["stdout"].name == str(log) and log.exists() and "kairn ensure" in log.read_text()


def test_ensure_starts_detached_windows(env, monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    conf = env["conf"]; conf.serve_host, conf.serve_port = "127.0.0.1", 9100
    started = []

    def fake_popen(argv, **kw):
        started.append((argv, kw)); return _Proc()

    rc = service.ensure(conf, alive=lambda h, p: True if started else False, popen=fake_popen, sleep=lambda s: None, cmd=[KAIRN])
    assert rc == 0
    (argv, kw), = started
    assert "creationflags" in kw and "start_new_session" not in kw


def test_ensure_times_out_and_reports_early_exit(env, capsys):
    rc = service.ensure(env["conf"], alive=lambda h, p: False, popen=lambda *a, **k: _Proc(), sleep=lambda s: None, timeout=0.05, cmd=[KAIRN])
    assert rc == 1 and "no answer" in capsys.readouterr().out
    rc = service.ensure(env["conf"], alive=lambda h, p: False, popen=lambda *a, **k: _Proc(exit_after=2), sleep=lambda s: None, timeout=5, cmd=[KAIRN])
    assert rc == 1 and "exited with 0" in capsys.readouterr().out


def test_ensure_cli(env, monkeypatch, capsys):
    monkeypatch.setattr(service, "is_alive", lambda h, p, timeout=1.0: True)
    assert _main(monkeypatch, "ensure") == 0 and "is running" in capsys.readouterr().out


def test_is_alive_treats_http_error_as_alive(monkeypatch):
    import urllib.error
    import urllib.request

    def raise_http(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 406, "Not Acceptable", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", raise_http)
    assert service.is_alive("127.0.0.1", 1)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: (_ for _ in ()).throw(urllib.error.URLError("refused")))
    assert not service.is_alive("127.0.0.1", 1)


def test_config_serve_section_roundtrip(tmp_path: Path):
    c = cfg.create("my-drive", path=tmp_path / "c.yaml")
    assert (c.serve_host, c.serve_port) == ("127.0.0.1", 8765)
    c.serve_host, c.serve_port = "localhost", 9200; c.save()
    again = cfg.load(tmp_path / "c.yaml")
    assert (again.serve_host, again.serve_port) == ("localhost", 9200)
    assert cfg.create("my-drive", path=tmp_path / "c.yaml").serve_port == 9200     # setup をやり直しても serve は引き継ぐ
    (tmp_path / "bad.yaml").write_text("drive: {remote: my-drive}\nserve: {port: 0}\n")
    with pytest.raises(SystemExit, match="serve.port"):
        cfg.load(tmp_path / "bad.yaml")


def test_overwrite_writes_unit_body_not_diff(env, monkeypatch, capsys):
    """既存 unit と差分があるとき、--yes で書かれるのは新しい本文であって unified diff ではない。"""
    from kairn import service, config as cfg
    conf = cfg.load()
    dest = env["unit_dir"] if isinstance(env, dict) and "unit_dir" in env else service.unit_dir()
    dest.mkdir(parents=True, exist_ok=True)
    opts = service.ServiceOptions(workspaces=[], enable_now=False, linger=False)
    units = service.render_units(opts, ["/usr/bin/kairn"])
    name = "kairn-serve.service"
    (dest / name).write_text(units[name].replace("RestartSec=5", "RestartSec=9"), encoding="utf-8")
    calls = []
    monkeypatch.setattr(service, "run_cmd", lambda cmd, **k: calls.append(cmd) or 0)
    monkeypatch.setattr(service, "have_systemctl", lambda: False)
    rc = service.install(conf, opts, yes=True, dest=dest, cmd=["/usr/bin/kairn"], out=lambda *a: None)
    text = (dest / name).read_text(encoding="utf-8")
    assert "+++ " not in text and "@@ " not in text and "--- " not in text
    assert text == units[name]
