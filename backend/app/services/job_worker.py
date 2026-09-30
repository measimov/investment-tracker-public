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
from dataclasses import dataclass
from datetime import datetime, timezone
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
SLOW_LANE_JOB_TYPES = frozenset(
    {
        "security_analysis",
        "security_analysis_batch",
        "report_digest_backfill",
        "report_digest_batch",
        "opinion_summary",
        "opinion_summary_batch",
    }
)

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
# [fn, interval_seconds, next_due_monotonic, name, group]；group 缺省（4 元素）= DEFAULT_GROUP
_periodic_tasks: list = []

# 周期任务分组：每组一条调度线程，组内串行（#273）。此前 14 个任务挂在同一条线程上，一个慢任务
# （目录/行业同步、港交所日报下载）就会推迟行情刷新与告警检查；某个外呼卡住时连告警检查本身也停。
DEFAULT_GROUP = "default"
ALERTS_GROUP = "alerts"
QUOTES_GROUP = "quotes"
# 单个任务耗时超过该秒数记 warning（进程内记录最近一次耗时，供排查）
SLOW_PERIODIC_TASK_SECONDS = 300
_periodic_durations: Dict[str, float] = {}


def task_group(task) -> str:
    return task[4] if len(task) > 4 else DEFAULT_GROUP


def periodic_task_groups() -> List[str]:
    """已注册周期任务的全部分组（告警检查按组核对调度心跳）。"""
    return sorted({task_group(task) for task in list(_periodic_tasks)})


def scheduler_heartbeat_name(group: str) -> str:
    """scheduled_task_state 里调度线程心跳行的名字；alert_checks 据此判停摆。"""
    return f"scheduler:{group}"


PERIODIC_SUCCEEDED = "succeeded"
PERIODIC_FAILED = "failed"
PERIODIC_SKIPPED = "skipped"


@dataclass(frozen=True)
class PeriodicOutcome:
    """周期任务的结果契约（告警依据）。

    多数领域入口为了不拖垮调度线程会**自吞异常**并返回 0——只看「有没有抛异常」的话，
    数据源持续宕机会被记成成功：失败计数清零、已有告警被误判恢复（PR #255 评审 P1）。
    所以注册的周期任务一律返回本类型，把真实结果交代清楚：
    - succeeded：跑完了且没有错误（写入 0 行的正常空跑也算）；
    - failed(reason)：数据源/解析/写库出错——计入连续失败；
    - skipped(reason)：开关关闭、未到期、无配置、另一轮在跑——**不计失败也不算恢复**。
    抛异常等同 failed。`periodic_outcome_task` 装饰器标记遵守本契约的入口，
    `tests/test_periodic_outcomes.py` 断言每个注册的任务都带这个标记。
    """

    status: str
    reason: str = ""
    count: int = 0

    @classmethod
    def succeeded(cls, count: int = 0, reason: str = "") -> "PeriodicOutcome":
        return cls(PERIODIC_SUCCEEDED, reason, count)

    @classmethod
    def failed(cls, reason: str, count: int = 0) -> "PeriodicOutcome":
        return cls(PERIODIC_FAILED, reason, count)

    @classmethod
    def skipped(cls, reason: str = "", count: int = 0) -> "PeriodicOutcome":
        return cls(PERIODIC_SKIPPED, reason, count)


def periodic_outcome_task(fn: Callable[[], Any]) -> Callable[[], Any]:
    """标记：该入口按 PeriodicOutcome 契约报告结果（自吞的错误不会被当成成功）。"""
    fn.periodic_outcome_contract = True  # type: ignore[attr-defined]
    return fn


def register_periodic_task(
    fn: Callable[[], Any],
    interval_seconds: float,
    *,
    name: Optional[str] = None,
    group: str = DEFAULT_GROUP,
) -> None:
    """name 缺省取函数名；它是告警键 `periodic:<name>` 的一部分，改名等于换一个告警。

    同名重复注册是 no-op（注册集中在 periodic_registry，允许被多次调用）。"""
    task_name = name or periodic_task_name(fn)
    with _registry_lock:
        if any(task[3] == task_name for task in _periodic_tasks):
            return
        _periodic_tasks.append([fn, interval_seconds, 0.0, task_name, group])


def periodic_task_durations() -> Dict[str, float]:
    """各周期任务最近一次的耗时（秒）快照。"""
    with _failures_lock:
        return dict(_periodic_durations)


# 周期任务的连续失败计数（进程内）：告警检查器（alert_checks.check_periodic_tasks）读取，
# 连续 N 次失败即告警，成功一次清零，skipped 不动。重启清零是可接受的——重启本身就是一次
# 恢复尝试，真正持续的故障重启后会再次累积。
_periodic_failures: Dict[str, Dict[str, Any]] = {}
# 本进程启动以来至少成功过一次的任务：重启清零了失败计数，但「清零」不等于「恢复」——
# 检查器据此区分「重启后还没跑到」与「跑成功了」，不对前者误发「已恢复」
_periodic_succeeded: set = set()
_failures_lock = threading.Lock()


def periodic_task_name(fn: Callable[[], Any]) -> str:
    return getattr(fn, "__name__", None) or repr(fn)


def as_periodic_outcome(result: Any) -> PeriodicOutcome:
    """任务返回值 → 结果；未遵守契约的旧式返回值（int/None）按成功处理。"""
    if isinstance(result, PeriodicOutcome):
        return result
    return PeriodicOutcome.succeeded()


def record_periodic_outcome(name: str, outcome: PeriodicOutcome) -> None:
    with _failures_lock:
        if outcome.status == PERIODIC_SKIPPED:
            return
        if outcome.status == PERIODIC_SUCCEEDED:
            _periodic_failures.pop(name, None)
            _periodic_succeeded.add(name)
            return
        entry = _periodic_failures.setdefault(name, {"consecutive_failures": 0})
        entry["consecutive_failures"] += 1
        entry["last_error"] = (outcome.reason or "未知错误")[:300]
        entry["last_failed_at"] = datetime.now(timezone.utc).isoformat()


def record_periodic_result(name: str, error: Optional[BaseException] = None) -> None:
    """异常形态的便捷入口：None = 成功，异常 = 失败。"""
    if error is None:
        record_periodic_outcome(name, PeriodicOutcome.succeeded())
    else:
        record_periodic_outcome(
            name, PeriodicOutcome.failed(f"{type(error).__name__}: {str(error)[:300]}")
        )


def periodic_task_failures() -> Dict[str, Dict[str, Any]]:
    """{任务名: {consecutive_failures, last_error, last_failed_at}} 的快照。"""
    with _failures_lock:
        return {name: dict(entry) for name, entry in _periodic_failures.items()}


def periodic_task_succeeded_since_start() -> set:
    with _failures_lock:
        return set(_periodic_succeeded)


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
            claimed["id"],
            claimed["job_type"],
            str(exc),
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
        if not settings.periodic_tasks_enabled:
            # 告警检查本身也是周期任务：关掉之后没有任何告警源能报出「周期任务全停了」
            logger.warning(
                "PERIODIC_TASKS_ENABLED=false：周期任务（含告警检查）全部停用，仅供 E2E/测试"
            )
        # 总开关只决定是否起调度线程；_run_due_periodic_tasks 本身不看它（测试直接驱动）。
        # 每个任务分组一条线程（#273）：告警检查、行情刷新不再排在慢任务后面
        with _registry_lock:
            groups = sorted({task_group(task) for task in _periodic_tasks} | {DEFAULT_GROUP})
        scheduler = (
            [
                threading.Thread(
                    target=self._periodic_loop,
                    args=(group,),
                    name=(
                        "background-job-scheduler"
                        if group == DEFAULT_GROUP
                        else f"background-job-scheduler-{group}"
                    ),
                    daemon=True,
                )
                for group in groups
            ]
            if settings.periodic_tasks_enabled
            else []
        )
        self._threads = [
            threading.Thread(
                target=self._reconcile_loop, name="background-job-reconciler", daemon=True
            ),
            *scheduler,
            *(
                threading.Thread(
                    target=self._lane_loop,
                    args=(lane,),
                    name=f"background-job-lane-{lane}",
                    daemon=True,
                )
                for lane in (LANE_SLOW, LANE_FAST)
            ),
        ]
        for thread in self._threads:
            thread.start()
        logger.info(
            "Background job worker started (owner=%s, threads=%s)",
            self.owner,
            len(self._threads),
        )

    def stop(self, timeout: float = 10.0) -> None:
        """停止认领并尽力回收空闲线程；**不等待在飞的长任务**。

        timeout 是所有线程共享的**总**预算：stop_worker() 在
        main.py lifespan 的 finally 里同步调用，逐线程各等 timeout 会把最坏
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
                stuck,
                settings.background_job_lease_seconds,
            )
        logger.info("Background job worker stopped (owner=%s)", self.owner)

    def _lane_loop(self, lane: str) -> None:
        """一条车道 = 一条串行执行流。

        分区不相交保证同一个 job 不会被本进程的另一条线程接管重跑。
        """
        while not self._stop_event.is_set():
            try:
                claimed = store.claim_next_runnable_job(_lane_job_types(lane), owner=self.owner)
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

    def _periodic_loop(self, group: str = DEFAULT_GROUP) -> None:
        while not self._stop_event.is_set():
            try:
                self._run_due_periodic_tasks(group=group)
            except Exception:  # noqa: BLE001
                logger.exception("Background job periodic tick failed (group=%s)", group)
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

    def _run_due_periodic_tasks(self, group: Optional[str] = None) -> None:
        """跑到期的周期任务；group=None 跑全部分组（测试直接驱动），否则只跑该组。

        每个 tick 与每个任务开跑前都写一次该组的心跳（含当前任务名），alert_checks 据此发现
        调度线程停摆或卡在某个任务里——此前线程卡住没有任何出口能看见（#273）。"""
        now = time.monotonic()
        with _registry_lock:
            due = [
                task
                for task in _periodic_tasks
                if now >= task[2] and (group is None or task_group(task) == group)
            ]
        if group is not None:
            _write_scheduler_heartbeat(group, current_task=None)
        # 绝不持锁调 fn：一个慢周期任务会卡住所有注册
        for task in due:
            fn, interval, _, name = task[:4]
            task[2] = now + interval  # next_due 只有本组线程写
            if group is not None:
                _write_scheduler_heartbeat(group, current_task=name)
            started = time.monotonic()
            try:
                outcome = as_periodic_outcome(fn())
            except Exception as exc:  # noqa: BLE001 - 周期任务失败不拖垮 worker
                logger.exception("Periodic task %s failed", name)
                record_periodic_result(name, exc)
                continue
            finally:
                elapsed = time.monotonic() - started
                with _failures_lock:
                    _periodic_durations[name] = round(elapsed, 3)
                if elapsed > SLOW_PERIODIC_TASK_SECONDS:
                    logger.warning(
                        "Periodic task %s took %.0fs (group=%s)", name, elapsed, task_group(task)
                    )
            if outcome.status == PERIODIC_FAILED:
                logger.warning("Periodic task %s reported failure: %s", name, outcome.reason)
            record_periodic_outcome(name, outcome)
        if group is not None and due:
            _write_scheduler_heartbeat(group, current_task=None)


def _write_scheduler_heartbeat(group: str, *, current_task: Optional[str]) -> None:
    """调度线程心跳（scheduled_task_state 的 scheduler:<group> 行）；写失败只记日志。"""
    try:
        from ..database import SessionLocal
        from . import scheduled_state

        db = SessionLocal()
        try:
            scheduled_state.mark_ran(
                db,
                scheduler_heartbeat_name(group),
                detail={
                    "current_task": current_task,
                    "current_task_started_at": (
                        datetime.now(timezone.utc).isoformat() if current_task else None
                    ),
                },
            )
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - 心跳失败不影响任务本身
        logger.exception("Scheduler heartbeat write failed (group=%s)", group)


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
