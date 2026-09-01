"""案件・計画の版・イベントの読み書き（docs/data-model.md）。

ここに置く規則（MCP から呼ばれる。エージェントの文章には頼らない）:
- plan.new_version(): 新版に carried_from で引き継がれなかった open タスクを superseded にする
- task.set_status(done): evidence が空なら ValueError
"""
from __future__ import annotations

from pathlib import Path


class CaseStore:
    def __init__(self, cases_dir: Path):
        self.cases_dir = cases_dir

    # --- case ---
    def load_case(self, case_id: str) -> dict:
        raise NotImplementedError

    def save_case(self, case: dict) -> None:
        raise NotImplementedError

    # --- plan versions ---
    def current_plan(self, case_id: str) -> dict:
        raise NotImplementedError

    def new_plan_version(self, case_id: str, objective: str, tasks: list[dict], reason: str, actor: str) -> dict:
        """新版を保存し、引き継がれなかった open タスクを superseded にして返す。"""
        raise NotImplementedError

    # --- tasks ---
    def set_task_status(self, case_id: str, task_id: str, status: str, evidence: list[dict], note: str, actor: str) -> dict:
        if status == "done" and not evidence:
            raise ValueError("done requires evidence (commit / pr / file / test)")
        raise NotImplementedError

    # --- events ---
    def append_event(self, case_id: str, event: dict) -> None:
        raise NotImplementedError

    def recent_events(self, case_id: str, n: int = 20) -> list[dict]:
        raise NotImplementedError
