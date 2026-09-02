"""プロセス内のジョブ機構（rclone の長い転送を MCP の呼び出しから切り離す）。

MCP の checkin / open_case の取り寄せは rclone を同期的に待つと、大きな案件（数百 MB・数百ファイル）で
MCP クライアント側の呼び出しタイムアウト（300 秒程度）に当たる。サーバー側は最後まで走るがクライアントは失敗扱いになる。
そこで転送はデーモンスレッドのジョブにし、ツールは job_id を即座に返す。状態は job_status で見る。

- JobTable: job_id → Job（queued / running / done / failed、開始・終了時刻、進捗テキスト、結果、エラー）
- 同一案件に対する同種（kind）のジョブが queued / running なら新規に作らず既存の Job を返す（submit の created=False）
- **同一案件（workspace, case）のジョブは種類を問わず直列**: (workspace, case) ごとの FIFO キューで 1 つずつ実行する
  （checkin 実行中に checkout（open_case の取り寄せ）が来たら queued で待ち、先行が done / failed になってから走る）。
  別の案件のジョブは並走する
- 完了したジョブは MAX_DONE 件（既定 200）または TTL_SEC（既定 24 時間）で捨てる（prune。submit / get / active のたびに呼ぶ）
- 進捗は fn に渡す progress(text) コールバックで更新する（sync._run が rclone の stderr を行単位で流す。最新行だけ保持）
- テーブルはプロセス内のメモリだけ。サーバーが再起動するとジョブは消える（設計上許容。docs/mcp-tools.md に明記）。
  消えた job_id を job_status に渡すと「unknown job」になるが、転送自体の結果は case.json.last_checkin_at と checkin event に残る
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .store import now_iso

JOB_STATUSES = ("queued", "running", "done", "failed")
ACTIVE_STATUSES = ("queued", "running")
MAX_DONE = 200
TTL_SEC = 24 * 3600


@dataclass
class Job:
    id: str
    kind: str
    workspace: str
    case: str
    status: str = "queued"
    created_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    progress: str = ""
    result: Any = None
    error: str | None = None
    _created_mono: float = field(default_factory=time.monotonic, repr=False)
    _started_mono: float | None = field(default=None, repr=False)
    _finished_mono: float | None = field(default=None, repr=False)
    _done: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def elapsed_sec(self) -> float:
        """running なら開始からの経過秒、終了済みなら開始→終了、未開始なら 0。"""
        if self._started_mono is None:
            return 0.0
        end = self._finished_mono if self._finished_mono is not None else time.monotonic()
        return round(end - self._started_mono, 1)

    def wait(self, timeout: float | None = None) -> bool:
        """終了（done / failed）まで待つ。テストと CLI 用。返り値: 終了したか。"""
        return self._done.wait(timeout)

    def to_dict(self) -> dict[str, Any]:
        """job_status の返り値。queued は同じ案件の先行ジョブが終わるのを待っている（種類を問わず直列）。"""
        return {"job_id": self.id, "kind": self.kind, "workspace": self.workspace, "case": self.case, "status": self.status,
                "created_at": self.created_at, "started_at": self.started_at, "finished_at": self.finished_at,
                "elapsed_sec": self.elapsed_sec(), "progress": self.progress, "result": self.result, "error": self.error}


class JobTable:
    """プロセス内のジョブ表。fn(progress) をデーモンスレッドで実行する。"""

    def __init__(self, max_done: int = MAX_DONE, ttl_sec: float = TTL_SEC):
        self.max_done = max_done
        self.ttl_sec = ttl_sec
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._running: dict[tuple[str, str], Job] = {}                                   # (workspace, case) → 実行中のジョブ
        self._queues: dict[tuple[str, str], deque[tuple[Job, Callable]]] = {}            # (workspace, case) → 待ち行列（FIFO）

    def submit(self, kind: str, workspace: str, case: str, fn: Callable[[Callable[[str], None]], Any]) -> tuple[Job, bool]:
        """ジョブを登録する。同じ (kind, workspace, case) が queued / running なら既存を返す（created=False）。
        同じ (workspace, case) のジョブが実行中なら（種類を問わず）queued のままキューに入れ、先行が終わってから開始する。"""
        key = (workspace, case)
        with self._lock:
            self._prune_locked()
            for j in self._jobs.values():
                if j.active and (j.kind, j.workspace, j.case) == (kind, workspace, case):
                    return j, False
            job = Job(id=uuid.uuid4().hex[:12], kind=kind, workspace=workspace, case=case)
            self._jobs[job.id] = job
            if key in self._running:
                self._queues.setdefault(key, deque()).append((job, fn))
                return job, True
            self._running[key] = job
        self._start(job, fn)
        return job, True

    def _start(self, job: Job, fn: Callable[[Callable[[str], None]], Any]) -> None:
        """ジョブをデーモンスレッドで走らせる（テストはここを差し替えて同期実行にできる）。"""
        threading.Thread(target=self._run, args=(job, fn), name=f"kairn-job-{job.kind}-{job.case}", daemon=True).start()

    def _run(self, job: Job, fn: Callable[[Callable[[str], None]], Any]) -> None:
        def progress(text: str) -> None:
            text = (text or "").strip()
            if text:
                job.progress = text[-400:]

        with self._lock:
            job.status = "running"
            job.started_at = now_iso()
            job._started_mono = time.monotonic()
        try:
            result = fn(progress)
        except Exception as e:  # 失敗の理由は文字列で残す（呼び出し側のエージェントが job_status で読む）
            with self._lock:
                job.status = "failed"
                job.error = f"{type(e).__name__}: {e}"[-800:]
        else:
            with self._lock:
                job.status = "done"
                job.result = result
        finally:
            key = (job.workspace, job.case)
            with self._lock:
                job.finished_at = now_iso()
                job._finished_mono = time.monotonic()
                nxt = self._queues[key].popleft() if self._queues.get(key) else None   # 同じ案件の次のジョブ（FIFO）
                if nxt is None:
                    self._running.pop(key, None); self._queues.pop(key, None)
                else:
                    self._running[key] = nxt[0]
            job._done.set()
            if nxt is not None:
                self._start(*nxt)   # ロックの外で開始（テストの同期実行でも再入しない）

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            self._prune_locked()
            return self._jobs.get(job_id)

    def active(self, workspace: str | None = None, case: str | None = None) -> list[Job]:
        """queued / running のジョブ（古い順）。workspace / case で絞る。"""
        with self._lock:
            self._prune_locked()
            return [j for j in self._jobs.values() if j.active
                    and (workspace is None or j.workspace == workspace) and (case is None or j.case == case)]

    def latest(self, kind: str, workspace: str, case: str) -> Job | None:
        """同じ (kind, workspace, case) のうち最後に登録されたジョブ（状態を問わない）。無ければ None。"""
        with self._lock:
            self._prune_locked()
            hits = [j for j in self._jobs.values() if (j.kind, j.workspace, j.case) == (kind, workspace, case)]
            return max(hits, key=lambda j: j._created_mono) if hits else None

    def running_snapshot(self) -> list[Job]:
        """running のジョブ（ロックを取らない）。停止シグナルのハンドラから呼ぶ用: ハンドラはメインスレッドで走るので、
        メインスレッドがロック中に割り込むと通常の active() では固まる。list() のコピーは CPython では GIL の下で一気に行われる。"""
        return [j for j in list(self._jobs.values()) if j.status == "running"]

    def all(self) -> list[Job]:
        with self._lock:
            self._prune_locked()
            return list(self._jobs.values())

    def prune(self) -> int:
        with self._lock:
            return self._prune_locked()

    def _prune_locked(self) -> int:
        """終了済みのうち TTL を過ぎたもの、および max_done を超えた分（古い順）を捨てる。返り値: 捨てた数。"""
        now = time.monotonic()
        finished = [j for j in self._jobs.values() if not j.active and j._finished_mono is not None]
        drop = [j for j in finished if now - j._finished_mono > self.ttl_sec]
        keep = [j for j in finished if j not in drop]
        if len(keep) > self.max_done:
            keep.sort(key=lambda j: j._finished_mono)
            drop += keep[: len(keep) - self.max_done]
        for j in drop:
            del self._jobs[j.id]
        return len(drop)
