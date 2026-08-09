"""In-process background job worker with DB leases and bounded retries.

The API enqueues jobs and may kick an inline fast-path execution, but this
worker is the reliability net (issue #35): it polls for runnable jobs —
including queued jobs whose enqueuing process died before executing them and
running jobs whose lease expired mid-execution — claims them atomically with
FOR UPDATE SKIP LOCKED, and retries unexpected failures with bounded
exponential backoff.

**车道化（issue #126）**：执行分两条串行车道 + 两条维护线程。此前是单线程
串行——认领到一个 ≤4h 的批量任务就把整个 worker 堵死数小时，期间租约回收
（fail_exhausted/interrupt_stale/cleanup_expired）与全部周期任务（汇率、
基准尾部、LLM 报告定时、分红同步）一并停摆，其他 job_type 也无人认领。

分区不相交 + 每车道单线程是这里的核心不变式：一个 job_type 只有唯一一条
线程会认领它，而那条线程正忙时根本不去认领——从结构上排除「同一进程内
另一个 worker 把在飞的长任务接管重跑」（并发双跑 + 重复烧 token）。
这也是不用 ThreadPoolExecutor 的原因之一；另一个原因是 TPE 的工作线程自
Python 3.9 起是非 daemon 且在解释器退出时被 join，一个 4h 任务在飞会让进程
根本退不出去（现有裸 daemon 线程没有这个问题）。
"""

import os
import socket
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from ..config import settings
from ..core.logging import get_app_logger
from . import background_job_store as store

logger = get_app_logger(__name__)

_HOUSEKEEPING_INTERVAL_SECONDS = 60
_PERIODIC_TICK_SECONDS = 60

LANE_SLOW = "slow"
LANE_FAST = "fast"

# 慢车道 = 分析家族。这四个 job_type 在入队处已经互斥
# （security_analysis_batch_jobs.ANALYSIS_EXCLUSIVE_JOB_TYPES：同时跑会对同一批
# 外部 API 双倍消耗），因此共用一条串行车道不损失任何并行度，同时保证 ≤4h 的
# 批量任务永远不冻结 price_refresh / 汇率 / 分红 / LLM 报告的队列。
# 刻意写字面量而不 import：job_worker 必须保持领域无关（不能反向依赖 jobs 模块）；
# 两边不漂移由 tests/test_job_worker_lanes.py 的分区断言守住。
SLOW_LANE_JOB_TYPES = frozenset({
    "security_analysis",
    "security_analysis_batch",
    "report_digest_backfill",
    "report_digest_batch",
})

_runners: Dict[str, Callable[[Dict[str, Any]], None]] = {}
_worker_singleton: Optional["JobWorker"] = None
_singleton_lock = threading.Lock()
# 护住注册表的读写：register_runner 可能发生在 start 之后（懒加载路由触发的
# 模块 import，见 main.py 顶部的 eager import 注释），此时车道线程正在
# list(_runners) —— 不加锁会抛 "dictionary changed size during iteration"。
_registry_lock = threading.Lock()


def register_runner(job_type: str, runner: Callable[[Dict[str, Any]], None]) -> None:
    """Register the executor for a job type; the runner receives the claimed payload."""
    with _registry_lock:
        _runners[job_type] = runner


def _lane_job_types(lane: str) -> List[str]:
    """车道的认领集合；快车道取**补集**，保证新 job_type 永远有归宿。"""
    with _registry_lock:
        registered = list(_runners)
    if lane == LANE_SLOW:
        return [job_type for job_type in registered if job_type in SLOW_LANE_JOB_TYPES]
    return [job_type for job_type in registered if job_type not in SLOW_LANE_JOB_TYPES]


# 通用周期任务钩子：跑在独立的调度线程上（与租约回收线程分开——慢的周期
# 任务不得拖延回收 tick）。领域任务（如 LLM 报告定期入队）经此注册，
# worker 保持领域无关。
_periodic_tasks: list = []  # [(fn, interval_seconds, next_due_monotonic)]


def register_periodic_task(fn: Callable[[], Any], interval_seconds: float) -> None:
    with _registry_lock:
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
        store.handle_job_failure(
            claimed["id"], claimed["job_type"], str(exc),
            # 只有本次 attempt 仍是当前 attempt 时才改写：租约过期被接管后，
            # 旧 runner 的异常不得把接管者的执行重新排队/标失败
            required_attempt_count=claimed.get("attempt_count"),
        )


class JobWorker:
    def __init__(
        self,
        poll_seconds: Optional[int] = None,
        housekeeping_interval: Optional[float] = None,
        periodic_tick: Optional[float] = None,
    ):
        self.poll_seconds = poll_seconds or settings.background_job_poll_seconds
        # 间隔可注入纯粹为了可测：真实 tick 是 60s，否则测试要么睡 60s
        # 要么改全局常量（会污染并发跑的其他用例）。
        self.housekeeping_interval = housekeeping_interval or _HOUSEKEEPING_INTERVAL_SECONDS
        self.periodic_tick = periodic_tick or _PERIODIC_TICK_SECONDS
        self.owner = f"{socket.gethostname()}:{os.getpid()}"
        self._stop_event = threading.Event()
        self._threads: List[threading.Thread] = []

    def start(self) -> None:
        if any(thread.is_alive() for thread in self._threads):
            return
        self._stop_event.clear()
        self._threads = [
            threading.Thread(
                target=self._reconcile_loop, name="background-job-reconciler", daemon=True
            ),
            threading.Thread(
                target=self._periodic_loop, name="background-job-scheduler", daemon=True
            ),
            *(
                threading.Thread(
                    target=self._lane_loop, args=(lane,),
                    name=f"background-job-lane-{lane}", daemon=True,
                )
                for lane in (LANE_SLOW, LANE_FAST)
            ),
        ]
        for thread in self._threads:
            thread.start()
        logger.info(
            "Background job worker started (owner=%s, threads=%s)",
            self.owner, len(self._threads),
        )

    def stop(self, timeout: float = 10.0) -> None:
        """停止认领并尽力回收空闲线程；**不等待在飞的长任务**。

        timeout 是所有线程共享的**总**预算：stop_worker() 在
        @app.on_event("shutdown") 里同步调用，逐线程各等 timeout 会把最坏
        shutdown 从 10s 拉到 40s，阻塞 uvicorn 的事件循环。

        在飞的长任务不发 cancel 信号：线程是 daemon，随进程消亡；租约
        300s 后过期，下次启动的 interrupt_stale_jobs + 认领会按
        completed_keys 续跑，不会重烧已完成的标的。
        """
        self._stop_event.set()
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            if not thread.is_alive():
                continue
            thread.join(timeout=max(deadline - time.monotonic(), 0.0))
        stuck = [thread.name for thread in self._threads if thread.is_alive()]
        if stuck:
            logger.warning(
                "Background job worker stop timed out with in-flight lanes: %s "
                "(daemon 线程随进程消亡；租约 %ss 后过期并在下次启动时被接管)",
                stuck, settings.background_job_lease_seconds,
            )
        logger.info("Background job worker stopped (owner=%s)", self.owner)

    def _lane_loop(self, lane: str) -> None:
        """一条车道 = 一条串行执行流。

        分区不相交保证同一个 job 不会被本进程的另一条线程接管重跑。
        """
        while not self._stop_event.is_set():
            try:
                claimed = store.claim_next_runnable_job(
                    _lane_job_types(lane), owner=self.owner
                )
                if claimed is not None:
                    execute_claimed_job(claimed)
                    continue  # drain runnable jobs without sleeping
            except Exception:  # noqa: BLE001 - the worker loop must survive anything
                logger.exception("Background job lane %s iteration failed", lane)
            self._stop_event.wait(self.poll_seconds)

    def _reconcile_loop(self) -> None:
        """租约回收：独立线程，任何业务执行都不得延迟这个 tick。"""
        while not self._stop_event.is_set():
            try:
                self._housekeep()
            except Exception:  # noqa: BLE001
                logger.exception("Background job housekeeping iteration failed")
            self._stop_event.wait(self.housekeeping_interval)

    def _periodic_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._run_due_periodic_tasks()
            except Exception:  # noqa: BLE001
                logger.exception("Background job periodic tick failed")
            self._stop_event.wait(self.periodic_tick)

    def _housekeep(self) -> None:
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

    def _run_due_periodic_tasks(self) -> None:
        now = time.monotonic()
        with _registry_lock:
            due = [task for task in _periodic_tasks if now >= task[2]]
        # 绝不持锁调 fn：一个慢周期任务会卡住所有注册
        for task in due:
            fn, interval, _ = task
            task[2] = now + interval  # next_due 只有本线程写
            try:
                fn()
            except Exception:  # noqa: BLE001 - 周期任务失败不拖垮 worker
                logger.exception("Periodic task %s failed", getattr(fn, "__name__", fn))


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
