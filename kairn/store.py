"""案件・計画の版・イベントの読み書き（docs/data-model.md）。

MCP が呼ぶ規則の実体はここ（エージェントの文章には頼らない）:
- new_plan_version(): 新版に carried_from で引き継がれなかった open タスクを superseded にする
- set_task_status(done): evidence が空なら ValueError
- validate_evidence(): 証拠の型と必須キーを検証（update_task / log_event / append_event 共通）
- すべての変更は events.jsonl に追記する（追記専用。checkout / checkin で Drive 版と行の和集合にマージされる: kairn/sync.py）
- open_case の閲覧記録は events.jsonl ではなく index/access.log（append_access_log。ローカルのみ、同期対象外）に書く。
  閲覧だけで events.jsonl に差分を作らないため（複数環境の events をマージする前提）
- ワークスペースをまたぐ参照（docs/data-model.md「跨ぎ参照」）: 対象側の access.log には cross_from=<ws>/<case> を添え、参照元の案件の
  events.jsonl には {action: "xref", workspace, case, tool} を 1 行追記する（append_xref。同じ対象は同一日に 1 回だけ）。
  case.json.related は同じワークスペースの案件 ID に加え "<ws>/<case>" を許す（parse_related / validate_related）。
  related への追記は link_related（MCP link_case / UI。event {action: "related", added}）、削除は unlink_related（UI＝人の操作のみ。
  event {action: "related", removed}）。形の検証だけを行い、案件の実在は呼び出し側（server は ToolError、UI は形だけ）が決める
- last_checkin_at / last_checkin_events: 案件単位の最終 checkin 時刻とその時点の events.jsonl 行数（case.json）。
  open_case はこれより新しいローカル変更（kairn 自身の checkin event は除く）があれば checkout を skip する
- rev / checked_in_from: checkin のたびに振り直す版マーカー（uuid4）と checkin したホスト名（case.json）。同じ rev を名前にした
  空ファイルを案件フォルダの .rev/ に 1 個だけ置く（write_rev_marker。rev を付け替えるたびに作り直す）。Drive 側の案件フォルダにも
  同じ .rev/<rev> が同期され、open_case / list_cases は rclone lsf でその名前だけを見てローカルの rev と比べる（kairn/sync.py）
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))
TASK_STATUSES = {"open", "doing", "blocked", "done", "dropped", "superseded"}
CASE_STATUSES = {"open", "closed", "suspended"}
EVENT_ACTIONS = {"opened", "plan", "started", "progress", "done", "dropped", "sendback", "comment", "decision",
                 "checkin", "status", "extract", "xref", "related"}  # 旧版が書いた "checkout" 行は読めるが、もう書かない（open_case は access.log へ）
CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
WORKSPACE_NAME_RE = CASE_ID_RE   # "<ws>/<case>" 参照で使えるワークスペース名（ディレクトリ名と同じ制約）
TASK_OWNERS = {"ai", "human"}
EVIDENCE_TYPES = {"commit", "pr", "file", "test", "url"}
EVIDENCE_REQUIRED_KEY = {"commit": "id", "pr": "id", "file": "path", "test": "cmd", "url": "url", "note": "text"}  # note は human のみ
# checkin 直後に書かれる case.json / events.jsonl の mtime は last_checkin_at（秒単位）よりわずかに後になるため、この幅は「変更なし」とみなす
CHECKIN_SLACK_SEC = 2.0
LOCAL_CHANGE_FILES = ("case.json", "events.jsonl", "worklog.md")
REV_DIR = ".rev"   # 案件フォルダ内の版マーカー置き場: <case>/.rev/<rev>（空ファイル 1 個。checkin で Drive へ同期される）
# kairn 自身が同期の記録として書く event。これだけが last_checkin_at 以後に増えた events.jsonl は「ローカル変更」とみなさない
# （checkin ツールは mark_checkin で行数を記録した後に checkin event を追記するため、その 1 行を変更と数えない）
SYNC_EVENT_ACTIONS = ("checkin",)


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def hostname() -> str:
    """checked_in_from に書くホスト名（取れなければ "unknown"）。"""
    try:
        return socket.gethostname() or "unknown"
    except OSError:
        return "unknown"


def new_rev() -> str:
    """版マーカー（uuid4）。checkin のたびに振り直す。"""
    return str(uuid.uuid4())


def parse_iso(ts: str) -> datetime | None:
    """ISO 8601（now_iso の形）→ aware datetime。tz 無しは JST。読めなければ None。"""
    try:
        t = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return t.replace(tzinfo=JST) if t.tzinfo is None else t


def validate_case_id(case_id: str) -> str:
    """案件 ID の検証（ファイルシステムに使う前に必ず通す）。不正なら ValueError。"""
    if not isinstance(case_id, str) or not CASE_ID_RE.match(case_id) or ".." in case_id:
        raise ValueError(f"invalid case id: {case_id!r}")
    return case_id


def validate_evidence(evidence: list | None, actor: str) -> list[dict]:
    """証拠の検証（docs/data-model.md「証拠の型」）。型は EVIDENCE_TYPES、`note` は actor=human のみ。
    型ごとの必須キー（commit/pr→id、file→path、test→cmd、url→url、note→text）が無ければ ValueError。"""
    if evidence is None:
        return []
    if not isinstance(evidence, list):
        raise ValueError("evidence must be a list of objects")
    for ev in evidence:
        if not isinstance(ev, dict) or not ev.get("type"):
            raise ValueError("evidence items must be objects with a 'type'")
        t = ev["type"]
        if t == "note":
            if actor != "human":
                raise ValueError("evidence type 'note' is allowed for actor=human only (ai must give commit / pr / file / test / url)")
        elif t not in EVIDENCE_TYPES:
            raise ValueError(f"unknown evidence type {t!r} (allowed: {', '.join(sorted(EVIDENCE_TYPES))}; 'note' for human only)")
        key = EVIDENCE_REQUIRED_KEY[t]
        if ev.get(key) in (None, ""):
            raise ValueError(f"evidence type {t!r} requires {key!r}")
    return evidence


def parse_related(ref: str) -> tuple[str | None, str]:
    """related の要素を (ワークスペース | None, 案件 ID) に分解する。"CASE-1" → (None, "CASE-1")、"beta/CASE-1" → ("beta", "CASE-1")。
    ワークスペース名・案件 ID の形が不正（"/" が 2 個以上、空、".." 等）なら ValueError。"""
    if not isinstance(ref, str) or not ref:
        raise ValueError(f"invalid related reference: {ref!r} (expected \"<case>\" or \"<ws>/<case>\")")
    if "/" not in ref:
        return None, validate_case_id(ref)
    ws, _, case = ref.partition("/")
    if not WORKSPACE_NAME_RE.match(ws) or ".." in ws or "/" in case:
        raise ValueError(f"invalid related reference: {ref!r} (expected \"<case>\" or \"<ws>/<case>\")")
    try:
        return ws, validate_case_id(case)
    except ValueError:
        raise ValueError(f"invalid related reference: {ref!r} (expected \"<case>\" or \"<ws>/<case>\")") from None


def validate_related(related: object) -> list[str]:
    """case.json.related の検証: 文字列のリストで、各要素は "<case>"（同じワークスペース）か "<ws>/<case>"。不正なら ValueError。"""
    if related is None:
        return []
    if not isinstance(related, list):
        raise ValueError("related must be a list of case references")
    for r in related:
        parse_related(r)
    return related


def append_access_log(path: Path, case_id: str, agent: str, cross_from: str | None = None, tool: str = "open_case") -> str:
    """open_case の閲覧記録を 1 行追記する（`<時刻>\t<案件>\t<agent>`）。置き場所はワークスペースの index/access.log
    （ローカルのみ、Drive に同期しない）。events.jsonl には書かない: 閲覧のたびに追記するとローカルの events.jsonl が
    常に Drive 版より新しくなり、他環境の events を取り込めないため。
    他のワークスペースの案件からの参照（跨ぎ参照）なら cross_from="<ws>/<case>" を受け、行末に `\tcross_from=<ws>/<case>\ttool=<tool>`
    を添える（search / find_cases のヒットもこの形で記録する）。返り値: 書いた行。"""
    line = f"{now_iso()}\t{validate_case_id(case_id)}\t{agent}"
    if cross_from:
        line += f"\tcross_from={cross_from}\ttool={tool}"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return line


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class CaseNotFound(KeyError):
    pass


class CaseStore:
    def __init__(self, cases_dir: Path):
        self.cases_dir = Path(cases_dir)

    # ---------- paths ----------
    def case_dir(self, case_id: str) -> Path:
        return self.cases_dir / validate_case_id(case_id)

    def _case_file(self, case_id: str) -> Path:
        return self.case_dir(case_id) / "case.json"

    def _plan_dir(self, case_id: str) -> Path:
        return self.case_dir(case_id) / "plan"

    def _events_file(self, case_id: str) -> Path:
        return self.case_dir(case_id) / "events.jsonl"

    # ---------- cases ----------
    def list_case_ids(self) -> list[str]:
        if not self.cases_dir.exists():
            return []
        return sorted(p.name for p in self.cases_dir.iterdir() if p.is_dir() and (p / "case.json").exists())

    def list_dirs_without_case(self) -> list[str]:
        """case.json の無い案件ディレクトリ（既存 worklog 等）。移行の対象。"""
        if not self.cases_dir.exists():
            return []
        return sorted(p.name for p in self.cases_dir.iterdir() if p.is_dir() and not (p / "case.json").exists() and not p.name.startswith("."))

    def load_case(self, case_id: str) -> dict:
        f = self._case_file(case_id)
        if not f.exists():
            raise CaseNotFound(case_id)
        return json.loads(f.read_text(encoding="utf-8"))

    def save_case(self, case: dict) -> None:
        case["updated_at"] = now_iso()
        _atomic_write(self._case_file(case["id"]), json.dumps(case, ensure_ascii=False, indent=1) + "\n")

    def create_case(self, case_id: str, title: str, workspace: str, actor: str, agent: str = "", **extra) -> dict:
        d = self.case_dir(case_id)
        if self._case_file(case_id).exists():
            raise FileExistsError(case_id)
        d.mkdir(parents=True, exist_ok=True)
        case = {"id": case_id, "title": title, "status": "open", "workspace": workspace,
                "repos": extra.get("repos", []), "tickets": extra.get("tickets", []), "prs": extra.get("prs", []),
                "related": validate_related(extra.get("related", [])), "elements": extra.get("elements", {}), "data": [],
                "created_at": now_iso(), "updated_at": now_iso(), "current_plan": 0}
        self.save_case(case)
        wl = d / "worklog.md"
        if not wl.exists():
            _atomic_write(wl, f"# {title}\n\n- **Created**: {now_iso()[:10]}\n- **Status**: open\n\n## Objective\n\n## Current State\n\n## Decision Log\n\n## Notes\n\n## Data location\n")
        self.append_event(case_id, {"actor": actor, "agent": agent, "action": "opened", "note": f"case created: {title}"})
        return case

    def mark_checkin(self, case_id: str) -> str | None:
        """checkin の版マーカー: case.json に rev（uuid4、毎回振り直す）・last_checkin_at（時刻）・checked_in_from（ホスト名）・
        last_checkin_events（その時点の events.jsonl の行数）を書く。sync.checkin は転送の**前**にこれを呼ぶ（Drive に置く case.json に
        同じ rev が入るように）。case.json が無ければ何もしない（None）。返り値: last_checkin_at。"""
        if not self._case_file(case_id).exists():
            return None
        case = self.load_case(case_id)
        case["rev"] = new_rev()
        case["last_checkin_at"] = now_iso()
        case["checked_in_from"] = hostname()
        case["last_checkin_events"] = len(self.events(case_id))
        self.save_case(case)
        self.write_rev_marker(case_id)
        return case["last_checkin_at"]

    def set_rev(self, case_id: str, rev: str) -> None:
        """case.json の rev だけを書き換える（updated_at は触らず、mtime も元に戻す。open_case の skip 判定（mtime）に影響させない）。
        .rev/ のマーカーも作り直す。"""
        f = self._case_file(case_id)
        case = self.load_case(case_id)
        mtime = f.stat().st_mtime
        case["rev"] = rev
        _atomic_write(f, json.dumps(case, ensure_ascii=False, indent=1) + "\n")
        os.utime(f, (mtime, mtime))
        self.write_rev_marker(case_id)

    def rev_dir(self, case_id: str) -> Path:
        return self.case_dir(case_id) / REV_DIR

    def rev_markers(self, case_id: str) -> list[str]:
        """.rev/ にあるマーカー名（無ければ []）。正常なら case.json の rev と同じ名前が 1 つ。"""
        d = self.rev_dir(case_id)
        if not d.is_dir():
            return []
        return sorted(p.name for p in d.iterdir() if p.is_file() and not p.is_symlink())

    def write_rev_marker(self, case_id: str) -> Path | None:
        """案件フォルダの .rev/ を case.json の rev から作り直す: 中を空にして <rev> という空ファイルを 1 個だけ置く。
        rev が無ければ .rev/ を空にして消す。case.json が無ければ何もしない（None）。返り値: 置いたマーカーのパス（無ければ None）。
        rev を付け替える経路（mark_checkin / set_rev）、checkin の転送直前、checkout（copy --update）の後に呼ぶ
        （古いマーカーが残らない。Drive 側の古いマーカーは checkin の rclone sync が消す）。"""
        if not self._case_file(case_id).exists():
            return None
        rev = self.load_case(case_id).get("rev")
        d = self.rev_dir(case_id)
        if d.is_dir():
            for p in d.iterdir():
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p)
                else:
                    p.unlink()
        if not rev:
            if d.is_dir():
                d.rmdir()
            return None
        d.mkdir(parents=True, exist_ok=True)
        marker = d / str(rev)
        marker.touch()
        return marker

    def local_changes_since_checkin(self, case_id: str) -> list[str] | None:
        """last_checkin_at より新しいローカル変更（case.json / events.jsonl / worklog.md / plan/*.json の mtime）。
        case.json が無い、または last_checkin_at 未記録なら None（判定不能＝checkout してよい）。
        events.jsonl は mtime が新しくても、checkin 時点（last_checkin_events 行）以後に増えた行が kairn 自身の同期記録
        （SYNC_EVENT_ACTIONS: checkin）だけなら変更と数えない（人／AI の実質的な変更だけを見る）。
        last_checkin_events が無い（古い case.json）場合は mtime だけで判定する。"""
        f = self._case_file(case_id)
        if not f.exists():
            return None
        case = self.load_case(case_id)
        ts = case.get("last_checkin_at")
        if not ts:
            return None
        t = parse_iso(ts)
        if t is None:
            return None
        limit = t.timestamp() + CHECKIN_SLACK_SEC
        d = self.case_dir(case_id)
        n = case.get("last_checkin_events")
        candidates = [d / n_ for n_ in LOCAL_CHANGE_FILES] + sorted(self._plan_dir(case_id).glob("v*.json"))
        changed = []
        for p in candidates:
            if not p.is_file() or p.stat().st_mtime <= limit:
                continue
            if p.name == "events.jsonl" and isinstance(n, int) and not self.substantive_events_after(case_id, n):
                continue
            changed.append(p.relative_to(d).as_posix())
        return changed

    def substantive_events_after(self, case_id: str, n: int) -> list[dict]:
        """events.jsonl の n 行目以降（checkin 時点より後に増えた行）のうち、kairn 自身の同期記録（SYNC_EVENT_ACTIONS）以外。"""
        return [e for e in self.events(case_id)[n:] if e.get("action") not in SYNC_EVENT_ACTIONS]

    def set_case_status(self, case_id: str, status: str, actor: str, agent: str = "", note: str = "") -> dict:
        """案件のステータス（open | closed | suspended）を変える。閉じる・保留する・再開するのは人の判断（docs/decisions.md 19）:
        actor="human" は人の操作（UI / CLI）、actor="ai" は人の発言を note に添えた代行（MCP set_case_status）。
        event: {action: "status", from: <前>, to: <後>, note}。同じステータスなら case.json も events も触らず changed=False。
        返り値: {case, changed, previous_status, event（変えなければ None）}。不正なステータスは ValueError。"""
        if status not in CASE_STATUSES:
            raise ValueError(f"invalid case status: {status} (allowed: {', '.join(sorted(CASE_STATUSES))})")
        case = self.load_case(case_id)
        prev = case.get("status")
        if prev == status:
            return {"case": case, "changed": False, "previous_status": prev, "event": None}
        case["status"] = status
        self.save_case(case)
        ev = self.append_event(case_id, {"actor": actor, "agent": agent, "action": "status", "from": prev, "to": status, "note": note})
        return {"case": case, "changed": True, "previous_status": prev, "event": ev}

    # ---------- related ----------
    def link_related(self, case_id: str, refs: list[str], actor: str, agent: str = "", note: str = "") -> dict:
        """case.json.related に refs（"<case>" | "<ws>/<case>"）を重複なく追記する（既存は保持、順序維持。refs 内の重複も 1 回）。
        形は validate_related で検証（不正なら ValueError。1 つでも不正なら何も書かない）。実在は検証しない（呼び出し側の責任）。
        追記があれば event {actor, agent, action: "related", added: [...], note} を 1 行。すべて既に含まれていれば case.json も events も触らず changed=False。
        返り値: {case, related, added, changed, event（無ければ None）}。"""
        refs = validate_related(list(refs))
        case = self.load_case(case_id)
        related = list(case.get("related") or [])
        added: list[str] = []
        for r in refs:
            if r not in related and r not in added:
                added.append(r)
        if not added:
            return {"case": case, "related": related, "added": [], "changed": False, "event": None}
        case["related"] = related + added
        self.save_case(case)
        ev = self.append_event(case_id, {"actor": actor, "agent": agent, "action": "related", "added": added, "note": note})
        return {"case": case, "related": case["related"], "added": added, "changed": True, "event": ev}

    def unlink_related(self, case_id: str, ref: str, actor: str, agent: str = "", note: str = "") -> dict:
        """case.json.related から ref を外す（人の操作＝UI からのみ呼ぶ。MCP には削除ツールを置かない）。文字列一致で全部外す。
        無ければ何も書かず changed=False。外したら event {actor, agent, action: "related", removed: [ref], note}。
        返り値: {case, related, removed, changed, event}。"""
        case = self.load_case(case_id)
        related = list(case.get("related") or [])
        if ref not in related:
            return {"case": case, "related": related, "removed": [], "changed": False, "event": None}
        case["related"] = [r for r in related if r != ref]
        self.save_case(case)
        ev = self.append_event(case_id, {"actor": actor, "agent": agent, "action": "related", "removed": [ref], "note": note})
        return {"case": case, "related": case["related"], "removed": [ref], "changed": True, "event": ev}

    # ---------- plans ----------
    def _plan_file(self, case_id: str, version: int) -> Path:
        return self._plan_dir(case_id) / f"v{version:04d}.json"

    def current_plan(self, case_id: str) -> dict | None:
        case = self.load_case(case_id)
        v = case.get("current_plan", 0)
        if not v:
            return None
        return json.loads(self._plan_file(case_id, v).read_text(encoding="utf-8"))

    def list_plans(self, case_id: str) -> list[dict]:
        d = self._plan_dir(case_id)
        if not d.exists():
            return []
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("v*.json"))]

    def _next_task_id(self, case_id: str) -> int:
        n = 0
        for plan in self.list_plans(case_id):
            for t in plan["tasks"]:
                n = max(n, int(t["id"][1:]))
        return n + 1

    def new_plan_version(self, case_id: str, objective: str, tasks: list[dict], reason: str, actor: str, agent: str = "") -> dict:
        """計画の新版。tasks の各要素: {title, owner?, carried_from?: "T012"}.
        carried_from で引き継がれた既存タスクは ID と履歴を保ち、引き継がれなかった open/doing/blocked は superseded。"""
        case = self.load_case(case_id)
        prev = self.current_plan(case_id)
        prev_tasks = {t["id"]: t for t in (prev["tasks"] if prev else [])}
        version = (prev["version"] if prev else 0) + 1
        next_id = self._next_task_id(case_id)
        new_tasks: list[dict] = []
        carried: set[str] = set()
        for t in tasks:
            if not isinstance(t, dict):
                raise ValueError("each task must be an object {title, owner?, carried_from?}")
            cf = t.get("carried_from")
            owner = t.get("owner")
            if owner is not None and owner not in TASK_OWNERS:
                raise ValueError(f"owner must be ai | human (got {owner!r})")
            if cf:
                if cf not in prev_tasks:
                    raise ValueError(f"carried_from refers to unknown task {cf}")
                if cf in carried:
                    raise ValueError(f"{cf} carried twice")
                base = dict(prev_tasks[cf])
                base["title"] = t.get("title") or base["title"]
                base["owner"] = owner or base.get("owner", "ai")
                base["carried_from"] = f"v{prev['version']:04d}"
                carried.add(cf)
                new_tasks.append(base)
            elif t.get("title"):
                new_tasks.append({"id": f"T{next_id:03d}", "title": t["title"], "owner": owner or "ai",
                                  "status": "open", "created_at": now_iso(), "evidence": []})
                next_id += 1
            else:
                raise ValueError("task needs title or carried_from")
        # 引き継がれなかった生きているタスクは superseded（前版のファイルに記録）
        superseded = []
        if prev:
            for tid, t in prev_tasks.items():
                if tid not in carried and t["status"] in ("open", "doing", "blocked"):
                    t["status"] = "superseded"
                    t["superseded_by"] = f"v{version:04d}"
                    superseded.append(tid)
            _atomic_write(self._plan_file(case_id, prev["version"]), json.dumps(prev, ensure_ascii=False, indent=1) + "\n")
        plan = {"version": version, "created_at": now_iso(), "actor": actor, "agent": agent, "reason": reason,
                "objective": objective, "tasks": new_tasks, "superseded": superseded}
        _atomic_write(self._plan_file(case_id, version), json.dumps(plan, ensure_ascii=False, indent=1) + "\n")
        case["current_plan"] = version
        self.save_case(case)
        self.append_event(case_id, {"actor": actor, "agent": agent, "action": "plan", "plan": version,
                                    "note": reason, "superseded": superseded, "tasks": [t["id"] for t in new_tasks]})
        return plan

    # ---------- tasks ----------
    def set_task_status(self, case_id: str, task_id: str, status: str, evidence: list[dict] | None, note: str,
                        actor: str, agent: str = "") -> dict:
        if status not in TASK_STATUSES or status == "superseded":
            raise ValueError(f"invalid task status: {status}")
        evidence = validate_evidence(evidence, actor)
        if status == "done" and not evidence:
            raise ValueError("done requires evidence (commit / pr / file / test / url)")
        plan = self.current_plan(case_id)
        if not plan:
            raise ValueError("no plan yet: call plan() first")
        task = next((t for t in plan["tasks"] if t["id"] == task_id), None)
        if not task:
            raise ValueError(f"unknown task {task_id} in plan v{plan['version']}")
        task["status"] = status
        task.setdefault("evidence", []).extend(evidence)
        task["updated_at"] = now_iso()
        if status == "done":
            task["done_at"] = now_iso()
        _atomic_write(self._plan_file(case_id, plan["version"]), json.dumps(plan, ensure_ascii=False, indent=1) + "\n")
        action = {"doing": "started", "done": "done", "dropped": "dropped"}.get(status, "progress")
        self.append_event(case_id, {"actor": actor, "agent": agent, "action": action, "task": task_id,
                                    "status": status, "note": note, "evidence": evidence})
        return task

    def open_tasks(self, case_id: str) -> list[dict]:
        plan = self.current_plan(case_id)
        return [t for t in (plan["tasks"] if plan else []) if t["status"] in ("open", "doing", "blocked")]

    # ---------- events ----------
    def append_event(self, case_id: str, event: dict) -> dict:
        if event.get("action") not in EVENT_ACTIONS:
            raise ValueError(f"invalid event action: {event.get('action')}")
        if event.get("evidence"):
            validate_evidence(event["evidence"], str(event.get("actor", "")))
        ev = {"t": now_iso(), "case": case_id, **event}
        f = self._events_file(case_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return ev

    def append_xref(self, case_id: str, workspace: str, target_case: str, tool: str, agent: str = "") -> dict | None:
        """跨ぎ参照の記録: この案件（参照元）の events.jsonl に {actor: ai, agent, action: xref, workspace, case: <対象案件>, tool} を追記する。
        同じ対象（workspace, case）への参照が同じ日（JST）に既にあれば追記せず None（閲覧のたびに events を伸ばさない）。
        ツール（open_case / search / find_cases）の違いは重複判定に含めない。"""
        today = now_iso()[:10]
        for e in self.events(case_id):
            if e.get("action") == "xref" and e.get("workspace") == workspace and e.get("case") == target_case and str(e.get("t", ""))[:10] == today:
                return None
        return self.append_event(case_id, {"actor": "ai", "agent": agent, "action": "xref", "workspace": workspace,
                                           "case": target_case, "tool": tool})

    def events(self, case_id: str, n: int | None = None) -> list[dict]:
        f = self._events_file(case_id)
        if not f.exists():
            return []
        rows = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
        return rows[-n:] if n else rows

    def last_event(self, case_id: str) -> dict | None:
        ev = self.events(case_id)
        return ev[-1] if ev else None

    # ---------- summaries ----------
    def progress(self, case_id: str) -> dict:
        plan = self.current_plan(case_id)
        tasks = plan["tasks"] if plan else []
        live = [t for t in tasks if t["status"] != "superseded"]
        return {"total": len(live), "done": sum(1 for t in live if t["status"] == "done"),
                "open": sum(1 for t in live if t["status"] in ("open", "doing", "blocked")),
                "plan": plan["version"] if plan else 0}
