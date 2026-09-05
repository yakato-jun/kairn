"""常駐の登録（kairn install-service）と保険起動（kairn ensure）。

install-service: systemd user unit をコード内テンプレートから生成し、~/.config/systemd/user/ に書いて登録する。
  kairn-serve.service         MCP + UI の常駐（kairn serve --host <h> --port <p>）。Restart=on-failure、TimeoutStopSec=15（停止が 90 秒待たないため）
  kairn-daily@.service        日次同期のテンプレート unit（%i = ワークスペース名）
  kairn-daily@<ws>.timer      ワークスペースごとの timer（Persistent=true、RandomizedDelaySec=10m）
  ExecStart には install-service を実行した kairn 自身の実行パス（sys.argv[0] を resolve）を埋める
  （uv tool install なら ~/.local/bin/kairn、venv 実行なら <repo>/.venv/bin/kairn）。

ensure: 設定（serve.host / serve.port）の /mcp に応答が無ければ `kairn serve` を切り離して起動し（start_new_session、
  出力は ~/.local/state/kairn/serve.log）、応答が出るまで待つ。service が止まっていた時の保険。

unit の生成先（XDG_CONFIG_HOME）・ログ先（XDG_STATE_HOME）・外部コマンド（run_cmd）・対話（input）は
関数単位で差し替えられるようにしてある（tests/test_service.py はすべて一時ディレクトリとモックで動かす）。
"""
from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg

SERVE_UNIT = "kairn-serve.service"
DAILY_SERVICE = "kairn-daily@.service"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_DAILY_TIME = "12:30"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def daily_timer(ws: str) -> str:
    return f"kairn-daily@{ws}.timer"


def daily_service(ws: str) -> str:
    return f"kairn-daily@{ws}.service"


# ---- 環境（テストで差し替える） -------------------------------------------------------------

def unit_dir() -> Path:
    """systemd user unit の置き場（$XDG_CONFIG_HOME/systemd/user、既定 ~/.config/systemd/user）。"""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "systemd" / "user"


def state_dir() -> Path:
    """ワークスペース非依存の状態置き場（$XDG_STATE_HOME/kairn、Windows は %LOCALAPPDATA%/kairn、既定 ~/.local/state/kairn）。serve.log はここ。"""
    if os.environ.get("XDG_STATE_HOME"):
        base = os.environ["XDG_STATE_HOME"]
    elif sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "kairn"
    else:
        base = os.path.expanduser("~/.local/state")
    return Path(base) / "kairn"


def self_command() -> list[str]:
    """今動いている kairn 自身を起動するコマンド。console script（~/.local/bin/kairn / .venv/bin/kairn）なら
    その絶対パス 1 要素。`python -m kairn.cli` で動いている時は [<python>, -m, kairn.cli]。"""
    argv0 = sys.argv[0] if sys.argv else ""
    p = Path(argv0).resolve() if argv0 else None
    if p and p.is_file() and p.suffix != ".py":
        return [str(p)]
    return [sys.executable, "-m", "kairn.cli"]


def run_cmd(argv: list[str]) -> tuple[int, str]:
    """外部コマンド（systemctl / loginctl）を実行し (returncode, stdout+stderr) を返す。テストではモックする。"""
    r = subprocess.run(argv, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def interactive() -> bool:
    return sys.stdin.isatty()


def have_systemctl() -> bool:
    return shutil.which("systemctl") is not None


# ---- unit テンプレート ------------------------------------------------------------------------

@dataclass
class ServiceOptions:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    workspaces: list[str] = field(default_factory=list)   # 日次ジョブを回すワークスペース名
    daily_time: str = DEFAULT_DAILY_TIME                  # HH:MM
    enable_now: bool = True                                # systemctl --user enable --now
    linger: bool = False                                   # loginctl enable-linger <user>


def _quote(arg: str) -> str:
    """systemd の ExecStart 用。空白や引用符を含む要素だけ二重引用符で囲む。"""
    if re.search(r'[\s"\\]', arg):
        return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return arg


def exec_line(cmd: list[str], *args: str) -> str:
    return " ".join(_quote(x) for x in [*cmd, *args])


def render_units(opts: ServiceOptions, cmd: list[str]) -> dict[str, str]:
    """{unit ファイル名: 内容}。cmd は ExecStart に埋める kairn 自身の起動コマンド（self_command()）。"""
    if not _TIME_RE.match(opts.daily_time):
        raise ValueError(f"daily time must be HH:MM, got {opts.daily_time!r}")
    hh, mm = (int(x) for x in opts.daily_time.split(":"))
    units = {
        SERVE_UNIT: "\n".join([
            "# kairn の MCP サーバー＋ローカル UI の常駐。kairn install-service が生成した（手で編集せず、再実行して作り直す）。",
            f"# MCP http://{opts.host}:{opts.port}/mcp   UI http://{opts.host}:{opts.port}/ui   ログ: journalctl --user -u kairn-serve",
            "[Unit]",
            "Description=kairn MCP server and local UI (kairn serve)",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={exec_line(cmd, 'serve', '--host', opts.host, '--port', str(opts.port))}",
            "Restart=on-failure",
            "RestartSec=5",
            "KillSignal=SIGTERM",
            "TimeoutStopSec=15",   # kairn serve は SIGTERM 後、開いている MCP 接続を最大 5 秒しか待たない（server.GRACEFUL_SHUTDOWN_SEC）
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]),
        DAILY_SERVICE: "\n".join([
            "# kairn 日次同期（bag2zst -> checkin -> raw-move -> drive-index -> index）のテンプレート unit。%i はワークスペース名。",
            "# kairn install-service が生成した。起動は kairn-daily@<ws>.timer から。ログ: journalctl --user -u 'kairn-daily@*' と <data>/<ws>/index/daily.log",
            "[Unit]",
            "Description=kairn daily sync for workspace %i",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=oneshot",
            f"ExecStart={exec_line(cmd, 'daily', '%i')}",
            "",
        ]),
    }
    for ws in opts.workspaces:
        units[daily_timer(ws)] = "\n".join([
            f"# kairn 日次同期のタイマー（ワークスペース {ws}、毎日 {opts.daily_time} ± 10 分。停止中だった分は次回起動時に実行）。kairn install-service が生成した。",
            "[Unit]",
            f"Description=Run kairn daily sync for workspace {ws} at {opts.daily_time}",
            "",
            "[Timer]",
            f"OnCalendar=*-*-* {hh:02d}:{mm:02d}:00",
            "Persistent=true",
            "RandomizedDelaySec=10m",
            f"Unit={daily_service(ws)}",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ])
    return units


# ---- 対話 --------------------------------------------------------------------------------------

def _ask(prompt: str, default: str, yes: bool) -> str:
    """1 項目の入力。--yes か非対話なら既定値。"""
    if yes or not interactive():
        return default
    ans = input(f"{prompt} [{default}]: ").strip()
    return ans or default


def _ask_bool(prompt: str, default: bool, yes: bool) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        ans = _ask(prompt, d, yes)
        if ans == d:
            return default
        if ans.lower() in ("y", "yes"):
            return True
        if ans.lower() in ("n", "no"):
            return False
        print("y か n で答えてください")


def interview(conf: cfg.Config, yes: bool) -> ServiceOptions:
    """対話で ServiceOptions を組み立てる。yes=True（または stdin が端末でない）なら既定値: 127.0.0.1:8765、
    日次は設定にある全ワークスペース 12:30、enable --now する、linger はしない。"""
    names = list(conf.workspaces)
    opts = ServiceOptions(workspaces=list(names))
    while True:
        host = _ask("バインドするアドレス（127.0.0.1 = この機械の中だけ）", DEFAULT_HOST, yes)
        if host in LOCAL_HOSTS:
            break
        print(f"注意: {host} にバインドすると MCP と UI がネットワークに公開されます（認証はありません。docs/ui.md）。")
        if _ask_bool("それでも公開しますか", False, yes):
            break
    opts.host = host
    while True:
        port = _ask("ポート", str(DEFAULT_PORT), yes)
        if port.isdigit() and 0 < int(port) < 65536:
            opts.port = int(port); break
        print("1〜65535 の数を入れてください")
    if names:
        print("日次同期（kairn daily）を回すワークスペース（設定にあるもの）:")
        for i, n in enumerate(names, 1):
            print(f"  {i}. {n}")
        while True:
            ans = _ask("番号か名前をカンマ区切りで。all = 全部、none = 日次同期を登録しない", "all", yes)
            if ans == "all":
                opts.workspaces = list(names); break
            if ans == "none":
                opts.workspaces = []; break
            picked, bad = [], []
            for tok in (t.strip() for t in ans.split(",") if t.strip()):
                if tok.isdigit() and 1 <= int(tok) <= len(names):
                    tok = names[int(tok) - 1]
                (picked if tok in names else bad).append(tok)
            if bad:
                print(f"設定に無いワークスペース: {bad}"); continue
            opts.workspaces = list(dict.fromkeys(picked)); break
        if opts.workspaces:
            while True:
                t = _ask("日次同期の時刻（HH:MM）", DEFAULT_DAILY_TIME, yes)
                if _TIME_RE.match(t):
                    opts.daily_time = t; break
                print("HH:MM の形で入れてください")
    else:
        print("設定にワークスペースが無いので日次同期は登録しません（kairn attach / kairn ws create の後に再実行）")
        opts.workspaces = []
    opts.enable_now = _ask_bool("今すぐ起動して有効化しますか（systemctl --user enable --now）", True, yes)
    opts.linger = _ask_bool("ログインしていなくても起動しますか（loginctl enable-linger。管理者認証を求められることがあります）", False, yes)
    return opts


# ---- 書き込みと登録 ----------------------------------------------------------------------------

def plan_writes(units: dict[str, str], dest: Path) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """{新規: 内容}, {変更あり: unified diff}, [変更なし] に分ける。"""
    new, changed, same = {}, {}, []
    for name, body in units.items():
        p = dest / name
        if not p.exists():
            new[name] = body
        elif p.read_text(encoding="utf-8") == body:
            same.append(name)
        else:
            changed[name] = "".join(difflib.unified_diff(
                p.read_text(encoding="utf-8").splitlines(True), body.splitlines(True),
                fromfile=f"{p} (current)", tofile=f"{p} (new)"))
    return new, changed, same


def write_units(units: dict[str, str], dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name, body in units.items():
        p = dest / name
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, p)


def install(conf: cfg.Config, opts: ServiceOptions, *, yes: bool, dest: Path | None = None, cmd: list[str] | None = None,
            out=print) -> int:
    """unit を書いて登録する。戻り値は終了コード。
    既存 unit と差分があれば表示し、--yes なら上書き、対話なら確認、非対話（端末でない）なら拒否して何も書かない。
    systemctl が無ければ unit を書くだけにして案内を出す。設定（serve.host / serve.port）は ensure が使うので保存する。"""
    dest = dest or unit_dir()
    units = render_units(opts, cmd or self_command())
    new, changed, same = plan_writes(units, dest)
    for name in same:
        out(f"unchanged: {dest / name}")
    if changed:
        for name, diff in changed.items():
            out(f"--- {dest / name} は既にあり、内容が異なります:")
            out(diff.rstrip("\n"))
        if not yes:
            if not interactive():
                out(f"kairn: {len(changed)} 個の既存 unit を上書きしません（確認できないため）。上書きするなら --yes を付けて再実行")
                return 1
            if not _ask_bool(f"{len(changed)} 個の既存 unit を上書きしますか", False, yes):
                out("kairn: 中止しました（何も書いていません）")
                return 1
    write_units({name: units[name] for name in [*new, *changed]}, dest)  # changed は diff なので本文は units から取る
    for name in new:
        out(f"wrote: {dest / name}")
    for name in changed:
        out(f"overwrote: {dest / name}")
    conf.serve_host, conf.serve_port = opts.host, opts.port
    conf.save()
    out(f"config: serve.host={opts.host} serve.port={opts.port} -> {conf.path}（kairn ensure が使う）")

    timers = [daily_timer(ws) for ws in opts.workspaces]
    if not have_systemctl():
        out("systemctl が見つかりません。unit は書きましたが登録していません。systemd のある環境で:")
        out(f"  systemctl --user daemon-reload && systemctl --user enable {'--now ' if opts.enable_now else ''}{' '.join([SERVE_UNIT, *timers])}")
        return 0
    rc = _run_report(["systemctl", "--user", "daemon-reload"], out)
    if rc != 0:
        return rc
    enable = ["systemctl", "--user", "enable", *(["--now"] if opts.enable_now else []), SERVE_UNIT, *timers]
    rc = _run_report(enable, out)
    if rc != 0:
        return rc
    if opts.linger:
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        out("loginctl enable-linger は管理者認証（polkit）を求めることがあります。失敗しても unit の登録には影響しません。")
        lrc = _run_report(["loginctl", "enable-linger", *([user] if user else [])], out)
        if lrc != 0:
            out("警告: enable-linger に失敗しました。ログアウト中は service が止まります（後で手で: loginctl enable-linger <user>）")
    for unit in [SERVE_UNIT, *timers]:
        _, text = run_cmd(["systemctl", "--user", "is-active", unit])
        out(f"{unit}: {text or '(no output)'}")
    if opts.host not in LOCAL_HOSTS or opts.port != DEFAULT_PORT:
        out(f"各エージェントの MCP 登録 URL を http://{opts.host}:{opts.port}/mcp に合わせてください（README「各エージェントへの適用」）")
    return 0


def _run_report(argv: list[str], out) -> int:
    rc, text = run_cmd(argv)
    out(f"$ {' '.join(argv)}" + (f"\n{text}" if text else ""))
    if rc != 0:
        out(f"kairn: {argv[0]} が失敗しました (exit {rc})")
    return rc


# ---- ensure -------------------------------------------------------------------------------------

def is_alive(host: str, port: int, timeout: float = 1.0) -> bool:
    """/mcp が HTTP で応答するか。ステータスは問わない（MCP の GET は 4xx を返すが、それでも動いている）。"""
    url = f"http://{host}:{port}/mcp"
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout).close()
        return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError):
        return False


def ensure(conf: cfg.Config, *, timeout: float = 15.0, cmd: list[str] | None = None, log_path: Path | None = None,
           popen=subprocess.Popen, alive=None, sleep=time.sleep, out=print) -> int:
    """設定の serve.host / serve.port で /mcp が応答しなければ `kairn serve` を切り離して起動し、応答まで最大 timeout 秒待つ。
    既に動いていれば何もしない。戻り値は終了コード（0 = 応答あり、1 = 起動できない / タイムアウト）。"""
    alive = alive or is_alive
    host, port = conf.serve_host, conf.serve_port
    if alive(host, port):
        out(f"kairn serve is running: http://{host}:{port}/mcp")
        return 0
    log_path = log_path or (state_dir() / "serve.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [*(cmd or self_command()), "serve", "--host", host, "--port", str(port)]
    with open(log_path, "ab") as log:
        log.write(f"\n--- kairn ensure {time.strftime('%Y-%m-%d %H:%M:%S')}: {' '.join(argv)}\n".encode())
        log.flush()
        kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        else:
            kwargs["start_new_session"] = True
        proc = popen(argv, **kwargs)
    out(f"started: {' '.join(argv)} (pid {proc.pid}, log {log_path})")
    deadline = time.monotonic() + timeout
    while True:
        if alive(host, port):
            out(f"kairn serve is up: http://{host}:{port}/mcp")
            return 0
        rc = proc.poll()
        if rc is not None:
            out(f"kairn: serve exited with {rc} before answering. see {log_path}")
            return 1
        if time.monotonic() >= deadline:
            out(f"kairn: no answer from http://{host}:{port}/mcp after {timeout:g}s (pid {proc.pid} still running). see {log_path}")
            return 1
        sleep(0.25)
