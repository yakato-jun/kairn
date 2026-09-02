"""環境ローカル設定（~/.config/kairn/config.yaml）。人は編集しない。kairn のコマンドが書く。

  drive:      {remote: <rclone remote>, root: ws}          kairn setup --remote が書く
  extract:    {agent: claude|codex|opencode|antigravity, timeout: 600}   kairn setup --agent / --extract-timeout（秒）
  workspaces: {<name>: {repos: [<abs path> | {path: <abs path>} | {glob: <pattern>} | {exclude: <abs path>} ...]}}
              kairn attach / detach が書く（glob: は展開時にディレクトリだけ採る。exclude: は glob: の展開から外す（detach が書く）。
              未知のキー・空文字は拒否）。repos は「cwd がどのワークスペースに属するか」を決めるためだけの対応表で、
              リポジトリ側には何も作らない（案件は DATA_ROOT/<name>/cases/ にだけある）
  serve:      {host: 127.0.0.1, port: 8765}               kairn install-service が書く。kairn ensure が /mcp の応答確認と起動に使う
  rules:      同期・退避規則（既定値あり）。kairn rules set / add-exclude / remove-exclude / add-raw-ext / remove-raw-ext
              （または UI の /ui/settings）が書く（set_rule / add_exclude / … → Config.save()。他のキーは壊さない）

規則:
- drive.remote が無ければ起動しない。設定済みの remote 以外は決して使わない。
- workspaces.<name>.repos に登録されたパス以外に対して kairn は何もしない。
- workspaces/（データ）が git に追跡されていたら、または git リポジトリ内にあるのに ignore されていなければ起動しない。
"""
from __future__ import annotations

import copy
import glob as _glob
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("KAIRN_DATA_ROOT", ROOT / "workspaces"))
USER_CONFIG_PATH = Path(os.environ.get("KAIRN_CONFIG", os.path.expanduser("~/.config/kairn/config.yaml")))
EXAMPLE_CONFIG_PATH = ROOT / "config" / "config.example.yaml"

# exclude は rclone のフィルタ規則。先頭に / の無い `target/**` は任意の階層の target/ に一致する（`**/target/**` はルート直下に一致しない）
DEFAULT_RULES = {
    "exclude": ["target/**", "build/**", "__pycache__/**", "node_modules/**", ".venv/**", "*.o", "*.rlib", "*.pyc"],
    "raw_data": {"extensions": ["bag", "zst", "pgm", "npz", "zip", "gz", "tar", "active", "pcd", "mp4"], "min_size": "50M", "min_age": "14d"},
    "bag_to_zst": True,
}
DEFAULT_EXTRACT_TIMEOUT_SEC = 600  # extract の子エージェントのタイムアウト（秒）。extract.timeout
DEFAULT_EXTRACT_AGENT = "claude"
DEFAULT_SERVE_HOST = "127.0.0.1"
DEFAULT_SERVE_PORT = 8765


@dataclass
class Workspace:
    name: str
    description: str = ""
    repos: list[Path] = field(default_factory=list)            # 展開済み（glob: はディレクトリに展開）
    repo_specs: list = field(default_factory=list)             # 設定ファイルに書く形（文字列 / {path} / {glob} / {exclude}）
    data_root: Path | None = None  # None なら DATA_ROOT（KAIRN_DATA_ROOT）

    def add_repo(self, repo: Path) -> None:
        if repo not in self.repos:
            self.repos.append(repo)
            self.repo_specs.append(str(repo))

    def remove_repo(self, repo: Path) -> None:
        """path 指定の登録を外す。glob: で拾われたものは `{exclude: <path>}` を追記して展開から外す（glob 行は残す）。"""
        keep = []
        via_glob = False
        for spec in self.repo_specs:
            if isinstance(spec, dict) and "glob" in spec:
                if any(r.resolve() == repo for r in _expand_repo_spec(spec, self.name)):
                    via_glob = True
                keep.append(spec)
            elif isinstance(spec, dict) and "exclude" in spec:
                keep.append(spec)
            elif Path(os.path.expanduser(spec if isinstance(spec, str) else spec["path"])).resolve() == repo:
                continue
            else:
                keep.append(spec)
        if via_glob and not any(isinstance(sp, dict) and "exclude" in sp and Path(os.path.expanduser(sp["exclude"])).resolve() == repo for sp in keep):
            keep.append({"exclude": str(repo)})
        self.repo_specs = keep
        self.repos = [r for r in self.repos if r.resolve() != repo]

    @property
    def data_dir(self) -> Path:
        return (self.data_root or DATA_ROOT) / self.name

    @property
    def cases_dir(self) -> Path:
        return self.data_dir / "cases"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"


@dataclass
class Config:
    remote: str
    drive_root: str
    extract_agent: str
    rules: dict
    workspaces: dict[str, Workspace]
    path: Path = USER_CONFIG_PATH
    extract_timeout: int = DEFAULT_EXTRACT_TIMEOUT_SEC
    serve_host: str = DEFAULT_SERVE_HOST
    serve_port: int = DEFAULT_SERVE_PORT

    def drive_path(self, ws: str, *parts: str) -> str:
        return f"{self.remote}:{'/'.join([self.drive_root, ws, *parts]).strip('/')}"

    def workspace_for_path(self, path: Path) -> Workspace | None:
        """パスが属するワークスペース（登録リポジトリの配下）。登録外なら None（＝何もしない）。"""
        p = path.resolve()
        for ws in self.workspaces.values():
            for repo in ws.repos:
                try:
                    p.relative_to(repo.resolve())
                    return ws
                except ValueError:
                    continue
        return None

    def to_dict(self) -> dict:
        return {
            "drive": {"remote": self.remote, "root": self.drive_root},
            "extract": {"agent": self.extract_agent, "timeout": self.extract_timeout},
            "serve": {"host": self.serve_host, "port": self.serve_port},
            "rules": self.rules,
            "workspaces": {
                n: {"description": w.description, "repos": list(w.repo_specs)}
                for n, w in self.workspaces.items()
            },
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("# kairn 環境ローカル設定。kairn のコマンドが書く（手で編集しない・コミットしない）\n"
                       + yaml.safe_dump(self.to_dict(), allow_unicode=True, sort_keys=False), encoding="utf-8")
        os.replace(tmp, self.path)


# ---------------------------------------------------------------------------
# rules の編集（kairn rules … / UI の設定ページ）。値は検証し、不正なら ValueError（保存しない）
# ---------------------------------------------------------------------------

RULE_KEYS = ("raw_data.min_size", "raw_data.min_age", "bag_to_zst", "bwlimit")
_TRUE = ("true", "yes", "on", "1")
_FALSE = ("false", "no", "off", "0")


def _rules_mut(conf: Config) -> dict:
    """編集用に rules を深いコピーにしてから返す（DEFAULT_RULES や他の Config と入れ子のリストを共有しない）。"""
    conf.rules = copy.deepcopy(conf.rules)
    conf.rules.setdefault("exclude", [])
    conf.rules.setdefault("raw_data", {}).setdefault("extensions", [])
    return conf.rules


def rules_view(conf: Config) -> dict:
    """表示用: {raw_data.min_size, raw_data.min_age, bag_to_zst, bwlimit, exclude: [...], raw_data.extensions: [...]}"""
    raw = conf.rules.get("raw_data") or {}
    return {"raw_data.min_size": raw.get("min_size"), "raw_data.min_age": raw.get("min_age"),
            "bag_to_zst": bool(conf.rules.get("bag_to_zst", True)), "bwlimit": conf.rules.get("bwlimit"),
            "exclude": list(conf.rules.get("exclude") or []), "raw_data.extensions": list(raw.get("extensions") or [])}


def set_rule(conf: Config, key: str, value: str) -> object:
    """rules の単一値を検証して書き、保存する。返り値は保存した値。
    raw_data.min_size: rclone の SizeSuffix（50M 等）、raw_data.min_age: Duration（14d 等）、bag_to_zst: true/false、
    bwlimit: rclone の --bwlimit 表記（'off' は制限なし＝キーを消す）。"""
    from . import sync  # 循環 import を避ける（sync が config を読む）
    if key not in RULE_KEYS:
        raise ValueError(f"unknown rule {key!r} (expected one of {', '.join(RULE_KEYS)})")
    v = str(value).strip()
    if not v:
        raise ValueError(f"{key}: value is required")
    rules = _rules_mut(conf)
    if key == "raw_data.min_size":
        sync.parse_size(v); rules["raw_data"]["min_size"] = v; out = v
    elif key == "raw_data.min_age":
        sync.parse_age(v); rules["raw_data"]["min_age"] = v; out = v
    elif key == "bag_to_zst":
        if v.lower() in _TRUE:
            out = True
        elif v.lower() in _FALSE:
            out = False
        else:
            raise ValueError(f"bag_to_zst: expected true or false, got {v!r}")
        rules["bag_to_zst"] = out
    else:  # bwlimit
        out = sync.parse_bwlimit(v)
        if out == "off":
            rules.pop("bwlimit", None); out = None
        else:
            rules["bwlimit"] = out
    conf.save()
    return out


def _pattern(v: str, what: str) -> str:
    v = str(v).strip()
    if not v or any(ch.isspace() for ch in v):
        raise ValueError(f"{what}: must be a non-empty string without whitespace, got {v!r}")
    return v


def add_exclude(conf: Config, pattern: str) -> bool:
    """rules.exclude にパターンを足して保存する。既にあれば何もしない（返り値 False）。"""
    pat = _pattern(pattern, "exclude pattern")
    rules = _rules_mut(conf)
    if pat in rules["exclude"]:
        return False
    rules["exclude"].append(pat); conf.save()
    return True


def remove_exclude(conf: Config, pattern: str) -> None:
    """rules.exclude からパターンを外して保存する。無ければ ValueError。"""
    pat = _pattern(pattern, "exclude pattern")
    rules = _rules_mut(conf)
    if pat not in rules["exclude"]:
        raise ValueError(f"exclude pattern {pat!r} is not set (have: {rules['exclude']})")
    rules["exclude"].remove(pat); conf.save()


def _ext(v: str) -> str:
    e = _pattern(v, "extension").lstrip(".").lower()
    if not e or "/" in e or "*" in e:
        raise ValueError(f"extension: expected e.g. bag or .bag, got {v!r}")
    return e


def add_raw_ext(conf: Config, ext: str) -> bool:
    """rules.raw_data.extensions に拡張子（先頭の . は外す・小文字）を足して保存する。既にあれば何もしない（返り値 False）。"""
    e = _ext(ext)
    rules = _rules_mut(conf)
    if e in rules["raw_data"]["extensions"]:
        return False
    rules["raw_data"]["extensions"].append(e); conf.save()
    return True


def remove_raw_ext(conf: Config, ext: str) -> None:
    """rules.raw_data.extensions から拡張子を外して保存する。無ければ ValueError。"""
    e = _ext(ext)
    rules = _rules_mut(conf)
    if e not in rules["raw_data"]["extensions"]:
        raise ValueError(f"extension {e!r} is not set (have: {rules['raw_data']['extensions']})")
    rules["raw_data"]["extensions"].remove(e); conf.save()


def _expand_repo_spec(spec, ws_name: str) -> list[Path]:
    """repos の 1 要素を Path のリストに。文字列 / {path: ...} はそのまま 1 件、{glob: ...} は一致するディレクトリだけ、
    {exclude: ...} は空（除外は expand_repos で引く）。未知のキー・複数キー・空文字は SystemExit
    （Path("") がカレントディレクトリに化けるのを防ぐ）。"""
    where = f"workspaces.{ws_name}.repos"
    if isinstance(spec, str):
        spec = {"path": spec}
    if not isinstance(spec, dict) or len(spec) != 1:
        raise SystemExit(f"kairn: {where}: each entry must be a path string, {{path: ...}}, {{glob: ...}} or {{exclude: ...}} (got {spec!r})")
    (key, val), = spec.items()
    if key not in ("path", "glob", "exclude"):
        raise SystemExit(f"kairn: {where}: unknown key {key!r} (expected path, glob or exclude)")
    if not isinstance(val, str) or not val.strip():
        raise SystemExit(f"kairn: {where}: {key} must be a non-empty string (got {val!r})")
    if key == "path":
        return [Path(os.path.expanduser(val))]
    if key == "exclude":
        return []
    return [Path(p) for p in sorted(_glob.glob(os.path.expanduser(val))) if os.path.isdir(p)]


def expand_repos(specs: list, ws_name: str) -> list[Path]:
    """repos 全体を展開する（重複なし）。{exclude: <path>} に一致するものは glob: の展開から外す（path: の明示登録は外さない）。"""
    excluded = set()
    for spec in specs:
        if isinstance(spec, dict) and len(spec) == 1 and "exclude" in spec:
            _expand_repo_spec(spec, ws_name)  # 検証のみ
            excluded.add(Path(os.path.expanduser(spec["exclude"])).resolve())
    repos: list[Path] = []
    for spec in specs:
        is_glob = isinstance(spec, dict) and "glob" in spec
        for r in _expand_repo_spec(spec, ws_name):
            if is_glob and r.resolve() in excluded:
                continue
            if r not in repos:
                repos.append(r)
    return repos


def _parse(raw: dict, path: Path) -> Config:
    drive = raw.get("drive") or {}
    remote = drive.get("remote")
    if not remote or not isinstance(remote, str):
        raise SystemExit("kairn: drive.remote is not set. run: kairn setup --remote <rclone remote name>")
    wss: dict[str, Workspace] = {}
    for name, w in (raw.get("workspaces") or {}).items():
        w = w or {}
        specs = list(w.get("repos") or [])
        repos = expand_repos(specs, name)
        wss[name] = Workspace(name=name, description=w.get("description", ""), repos=repos, repo_specs=specs)
    rules = {**DEFAULT_RULES, **(raw.get("rules") or {})}
    ext = raw.get("extract") or {}
    timeout = ext.get("timeout", DEFAULT_EXTRACT_TIMEOUT_SEC)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise SystemExit(f"kairn: extract.timeout must be a positive integer (seconds), got {timeout!r}")
    serve = raw.get("serve") or {}
    port = serve.get("port", DEFAULT_SERVE_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise SystemExit(f"kairn: serve.port must be an integer 1..65535, got {port!r}")
    host = serve.get("host", DEFAULT_SERVE_HOST)
    if not isinstance(host, str) or not host.strip():
        raise SystemExit(f"kairn: serve.host must be a non-empty string, got {host!r}")
    return Config(remote=remote, drive_root=drive.get("root", "ws"), extract_agent=ext.get("agent", DEFAULT_EXTRACT_AGENT),
                  rules=rules, workspaces=wss, path=path, extract_timeout=timeout, serve_host=host, serve_port=port)


def load(path: Path | None = None) -> Config:
    path = path or USER_CONFIG_PATH
    if not path.exists():
        raise SystemExit(f"kairn: no config at {path}. run: kairn setup --remote <rclone remote name>")
    return _parse(yaml.safe_load(path.read_text(encoding="utf-8")) or {}, path)


def create(remote: str, agent: str | None = None, path: Path | None = None, drive_root: str = "ws",
           extract_timeout: int | None = None) -> Config:
    """設定を書く（既存の rules / workspaces / extract.agent / extract.timeout / serve は引き継ぐ）。
    agent=None / extract_timeout=None なら既存値（無ければ既定 claude / 600）。"""
    path = path or USER_CONFIG_PATH
    existing = load(path) if path.exists() else None
    if agent is None:
        agent = existing.extract_agent if existing else DEFAULT_EXTRACT_AGENT
    if extract_timeout is None:
        extract_timeout = existing.extract_timeout if existing else DEFAULT_EXTRACT_TIMEOUT_SEC
    if extract_timeout <= 0:
        raise SystemExit(f"kairn: extract timeout must be a positive integer (seconds), got {extract_timeout!r}")
    conf = Config(remote=remote, drive_root=drive_root, extract_agent=agent, rules=dict(existing.rules if existing else DEFAULT_RULES),
                  workspaces=dict(existing.workspaces if existing else {}), path=path, extract_timeout=extract_timeout,
                  serve_host=existing.serve_host if existing else DEFAULT_SERVE_HOST,
                  serve_port=existing.serve_port if existing else DEFAULT_SERVE_PORT)
    conf.save()
    return conf


def _git_top(path: Path) -> str | None:
    """path を含む git リポジトリのトップ。リポジトリ外・存在しないなら None。"""
    probe = path if path.is_dir() else path.parent
    if not probe.exists():
        return None
    top = subprocess.run(["git", "-C", str(probe), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return top.stdout.strip() if top.returncode == 0 else None


def _tracked_in_git(path: Path, top: str | None = None) -> bool:
    """path（またはその配下）が、path を含む git リポジトリで追跡されているか。リポジトリ外・存在しないなら False。"""
    top = top or _git_top(path)
    if not top:
        return False
    r = subprocess.run(["git", "-C", top, "ls-files", "--", str(path)], capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def _ignored_in_git(path: Path, top: str) -> bool:
    """path が .gitignore 等で無視されているか（git check-ignore -q）。"""
    return subprocess.run(["git", "-C", top, "check-ignore", "-q", "--", str(path)], capture_output=True, text=True).returncode == 0


def assert_data_not_tracked(data_root: Path | None = None) -> None:
    """安全弁: workspaces/（リポジトリ内）と KAIRN_DATA_ROOT が git に追跡されている、または git リポジトリ内にあるのに
    ignore されていない（次の `git add` で入ってしまう）なら起動を拒否する。リポジトリ外なら何もしない。"""
    data_root = data_root or DATA_ROOT
    for p in {ROOT / "workspaces", data_root}:
        top = _git_top(p)
        if not top:
            continue
        if _tracked_in_git(p, top):
            raise SystemExit(f"kairn: refusing to run — {p} is tracked by git (data must never be committed)")
        if not _ignored_in_git(p, top):
            raise SystemExit(f"kairn: refusing to run — {p} is inside the git repository {top} but not ignored "
                             f"(add it to .gitignore; data must never be committed)")


def rclone_remotes() -> list[str]:
    r = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True)
    return [x.rstrip(":") for x in r.stdout.split()] if r.returncode == 0 else []
