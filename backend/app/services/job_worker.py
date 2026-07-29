"""In-process background job worker with DB leases and bounded retries.

The API enqueues jobs and may kick an inline fast-path execution, but this
worker is the reliability net (issue #35): it polls for runnable jobs —
including queued jobs whose enqueuing process died before executing them and
running jobs whose lease expired mid-execution — claims them atomically with
FOR UPDATE SKIP LOCKED, and retries unexpected failures with bounded
exponential backoff.
"""

import os
import socket
import threading
import time
from typing import Any, Callable, Dict, Optional

from ..config import settings
from ..core.logging import get_app_logger
from . import background_job_store as store

logger = get_app_logger(__name__)

_HOUSEKEEPING_INTERVAL_SECONDS = 60

_runners: Dict[str, Callable[[Dict[str, Any]], None]] = {}
_worker_singleton: Optional["JobWorker"] = None
_singleton_lock = threading.Lock()


def register_runner(job_type: str, runner: Callable[[Dict[str, Any]], None]) -> None:
    """Register the executor for a job type; the runner receives the claimed payload."""
    _runners[job_type] = runner


# 通用周期任务钩子：挂在 worker 的 housekeeping tick 上（无独立调度器）。
# 领域任务（如 LLM 报告定期入队）经此注册，worker 保持领域无关。
_periodic_tasks: list = []  # [(fn, interval_seconds, next_due_monotonic)]


def register_periodic_task(fn: Callable[[], Any], interval_seconds: float) -> None:
    _periodic_tasks.append([fn, interval_seconds, 0.0])


def execute_claimed_job(claimed: Dict[str, Any]) -> None:
    """Run a claimed job, routing unexpected errors into the retry path."""
    runner = _runners.get(claimed["job_type"])
    if runner is None:
        store.handle_job_failure(
            claimed["id"], claimed["job_type"], f"未注册的任务类型: {claimed['job_type']}"
        )
        return
    try:
        runner(claimed)
    except Exception as exc:  # noqa: BLE001 - the retry path needs every failure
        logger.exception(
            "Background job %s (%s) attempt %s failed",
            claimed["id"],
            claimed["job_type"],
            claimed.get("attempt_count"),
        )
        store.handle_job_failure(claimed["id"], claimed["job_type"], str(exc))


class JobWorker:
    def __init__(self, poll_seconds: Optional[int] = None):
        self.poll_seconds = poll_seconds or settings.background_job_poll_seconds
        self.owner = f"{socket.gethostname()}:{os.getpid()}"
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._housekeeping_due = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="background-job-worker", daemon=True
        )
        self._thread.start()
        logger.info("Background job worker started (owner=%s)", self.owner)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        logger.info("Background job worker stopped (owner=%s)", self.owner)

    def _loop(self) -> None:

        while not self._stop_event.is_set():
            try:
                now = time.monotonic()
                if now >= self._housekeeping_due:
                    self._housekeep()
                    self._housekeeping_due = now + _HOUSEKEEPING_INTERVAL_SECONDS

                claimed = store.claim_next_runnable_job(
                    list(_runners.keys()), owner=self.owner
                )
                if claimed is not None:
                    execute_claimed_job(claimed)
                    continue  # drain runnable jobs without sleeping
            except Exception:  # noqa: BLE001 - the worker loop must survive anything
                logger.exception("Background job worker iteration failed")
            self._stop_event.wait(self.poll_seconds)

    def _housekeep(self) -> None:
        now = time.monotonic()
        for task in _periodic_tasks:
            fn, interval, due = task
            if now >= due:
                task[2] = now + interval
                try:
                    fn()
                except Exception:  # noqa: BLE001 - 周期任务失败不拖垮 worker
                    logger.exception("Periodic task %s failed", getattr(fn, "__name__", fn))
        failed = store.fail_exhausted_jobs()
        interrupted = store.interrupt_stale_jobs()
        deleted = store.cleanup_expired_jobs()
        if failed or interrupted or deleted:
            logger.info(
                "Background job housekeeping: exhausted=%s, interrupted=%s, deleted=%s",
                failed,
                interrupted,
                deleted,
            )


def start_worker() -> Optional[JobWorker]:
    """Start (or return) the process-wide worker; honors background_worker_enabled."""
    global _worker_singleton
    if not settings.background_worker_enabled:
        return None
    with _singleton_lock:
        if _worker_singleton is None:
            _worker_singleton = JobWorker()
        _worker_singleton.start()
        return _worker_singleton


def stop_worker() -> None:
    with _singleton_lock:
        if _worker_singleton is not None:
            _worker_singleton.stop()
