"""案件・計画の版・イベントの読み書き（docs/data-model.md）。

MCP が呼ぶ規則の実体はここ（エージェントの文章には頼らない）:
- new_plan_version(): 新版に carried_from で引き継がれなかった open タスクを superseded にする
- set_task_status(done): evidence が空なら ValueError
- validate_evidence(): 証拠の型と必須キーを検証（update_task / log_event / append_event 共通）
- すべての変更は events.jsonl に追記する
- last_checkin_at: 案件単位の最終 checkin 時刻（case.json）。open_case はこれより新しいローカル変更があれば checkout を skip する
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))
TASK_STATUSES = {"open", "doing", "blocked", "done", "dropped", "superseded"}
CASE_STATUSES = {"open", "closed", "suspended"}
EVENT_ACTIONS = {"opened", "plan", "started", "progress", "done", "dropped", "sendback", "comment", "decision",
                 "checkin", "checkout", "status", "extract"}
CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
TASK_OWNERS = {"ai", "human"}
EVIDENCE_TYPES = {"commit", "pr", "file", "test", "url"}
EVIDENCE_REQUIRED_KEY = {"commit": "id", "pr": "id", "file": "path", "test": "cmd", "url": "url"}
# checkin 直後に書かれる case.json / events.jsonl の mtime は last_checkin_at（秒単位）よりわずかに後になるため、この幅は「変更なし」とみなす
CHECKIN_SLACK_SEC = 2.0
LOCAL_CHANGE_FILES = ("case.json", "events.jsonl", "worklog.md")


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def validate_evidence(evidence: list | None, actor: str) -> list[dict]:
    """証拠の検証（docs/data-model.md「証拠の型」）。型は EVIDENCE_TYPES、`note` は actor=human のみ。
    型ごとの必須キー（commit/pr→id、file→path、test→cmd、url→url）が無ければ ValueError。"""
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
            continue
        if t not in EVIDENCE_TYPES:
            raise ValueError(f"unknown evidence type {t!r} (allowed: {', '.join(sorted(EVIDENCE_TYPES))}; 'note' for human only)")
        key = EVIDENCE_REQUIRED_KEY[t]
        if ev.get(key) in (None, ""):
            raise ValueError(f"evidence type {t!r} requires {key!r}")
    return evidence


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
        if not CASE_ID_RE.match(case_id) or ".." in case_id:
            raise ValueError(f"invalid case id: {case_id!r}")
        return self.cases_dir / case_id

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
                "related": extra.get("related", []), "elements": extra.get("elements", {}), "data": [],
                "created_at": now_iso(), "updated_at": now_iso(), "current_plan": 0}
        self.save_case(case)
        wl = d / "worklog.md"
        if not wl.exists():
            _atomic_write(wl, f"# {title}\n\n- **Created**: {now_iso()[:10]}\n- **Status**: open\n\n## Objective\n\n## Current State\n\n## Decision Log\n\n## Notes\n\n## Data location\n")
        self.append_event(case_id, {"actor": actor, "agent": agent, "action": "opened", "note": f"case created: {title}"})
        return case

    def mark_checkin(self, case_id: str) -> str | None:
        """checkin 成功時に case.json.last_checkin_at を更新する。case.json が無ければ何もしない（None）。"""
        if not self._case_file(case_id).exists():
            return None
        case = self.load_case(case_id)
        case["last_checkin_at"] = now_iso()
        self.save_case(case)
        return case["last_checkin_at"]

    def local_changes_since_checkin(self, case_id: str) -> list[str] | None:
        """last_checkin_at より新しいローカル変更（case.json / events.jsonl / worklog.md / plan/*.json の mtime）。
        case.json が無い、または last_checkin_at 未記録なら None（判定不能＝checkout してよい）。"""
        f = self._case_file(case_id)
        if not f.exists():
            return None
        ts = self.load_case(case_id).get("last_checkin_at")
        if not ts:
            return None
        try:
            t = datetime.fromisoformat(ts)
        except ValueError:
            return None
        if t.tzinfo is None:
            t = t.replace(tzinfo=JST)
        limit = t.timestamp() + CHECKIN_SLACK_SEC
        d = self.case_dir(case_id)
        candidates = [d / n for n in LOCAL_CHANGE_FILES] + sorted(self._plan_dir(case_id).glob("v*.json"))
        return [str(p.relative_to(d)) for p in candidates if p.is_file() and p.stat().st_mtime > limit]

    def set_case_status(self, case_id: str, status: str, actor: str, agent: str = "", note: str = "") -> dict:
        if status not in CASE_STATUSES:
            raise ValueError(f"invalid case status: {status}")
        case = self.load_case(case_id)
        case["status"] = status
        self.save_case(case)
        self.append_event(case_id, {"actor": actor, "agent": agent, "action": "status", "note": f"{status}: {note}".strip(": ")})
        return case

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
