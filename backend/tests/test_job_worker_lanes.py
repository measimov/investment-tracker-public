"""Worker 车道化：长任务不得饿死租约回收、周期任务与其他 job_type。

此前 worker 是单线程串行：认领到一个 ≤4h 的批量任务（security_analysis_batch
上限 4h、report_digest_batch 单轮 2-4h）就把整个 worker 堵死数小时，期间
fail_exhausted / interrupt_stale / cleanup_expired 全部停摆、周期任务（汇率
6h、基准尾部、LLM 报告 1h、分红同步）一并欠拍，其他 job_type 也无人认领
——重启恢复与租约接管的队列整体冻结。

隔离手法：整份替换 _runners 为只含随机 fake 类型的 dict，并 monkeypatch
SLOW_LANE_JOB_TYPES —— 两条车道的认领集合里没有任何真实 job_type，绝不会
捡走其他测试遗留的行。conftest 的 BACKGROUND_WORKER_ENABLED=false 只拦
start_worker()，不妨碍直接构造 JobWorker 实例。
"""

import collections
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.database import SessionLocal
from app.models.background_job import BackgroundJob
from app.services import job_worker
from app.services.background_job_store import create_or_get_active_job, update_job


def _wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _delete_jobs(*job_types):
    session = SessionLocal()
    try:
        session.query(BackgroundJob).filter(
            BackgroundJob.job_type.in_(job_types)
        ).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


@pytest.fixture
def lanes(monkeypatch):
    """两条车道各挂一个 fake runner；慢 runner 可被 Event 卡住。"""
    slow_type = f"test_slow_{uuid4().hex[:8]}"
    fast_type = f"test_fast_{uuid4().hex[:8]}"

    monkeypatch.setattr(job_worker, "SLOW_LANE_JOB_TYPES", frozenset({slow_type}))
    monkeypatch.setattr(job_worker, "_runners", {})
    monkeypatch.setattr(job_worker, "_periodic_tasks", [])

    state = SimpleNamespace(
        slow_type=slow_type,
        fast_type=fast_type,
        slow_started=threading.Event(),
        release_slow=threading.Event(),
        fast_done=threading.Event(),
    )

    def slow_runner(claimed):
        state.slow_started.set()
        # 兜底超时：即使断言失败也不会把整个测试套件挂死
        state.release_slow.wait(timeout=30)
        update_job(claimed["id"], slow_type, status="succeeded", required_status="running")

    def fast_runner(claimed):
        update_job(claimed["id"], fast_type, status="succeeded", required_status="running")
        state.fast_done.set()

    job_worker.register_runner(slow_type, slow_runner)
    job_worker.register_runner(fast_type, fast_runner)

    worker = job_worker.JobWorker(
        poll_seconds=0.05, housekeeping_interval=0.05, periodic_tick=0.05
    )
    state.worker = worker
    try:
        yield state
    finally:
        state.release_slow.set()  # 先放行再停，避免 join 白等 30s
        worker.stop(timeout=5)
        _delete_jobs(slow_type, fast_type)


def test_housekeeping_ticks_while_a_long_job_is_in_flight(lanes, monkeypatch):
    """头号回归：4h 级任务在飞时，租约回收与周期任务仍按 tick 触发。"""
    counters = collections.Counter()
    for name in ("fail_exhausted_jobs", "interrupt_stale_jobs", "cleanup_expired_jobs"):
        monkeypatch.setattr(
            job_worker.store, name,
            lambda _name=name: (counters.update([_name]), 0)[1],
        )
    periodic_ran = threading.Event()
    job_worker.register_periodic_task(periodic_ran.set, interval_seconds=0.05)

    create_or_get_active_job(lanes.slow_type, 1, {"total": 1, "completed": 0})
    lanes.worker.start()

    assert lanes.slow_started.wait(timeout=5), "慢车道未认领到任务"
    assert _wait_until(lambda: counters["interrupt_stale_jobs"] >= 2), (
        "长任务在飞期间租约回收停摆（单线程 worker 的原始缺陷）"
    )
    assert periodic_ran.wait(timeout=5), "长任务在飞期间周期任务停摆"


def test_fast_lane_claims_while_slow_lane_is_blocked(lanes):
    """其他 job_type 不得因长任务而无人认领。"""
    create_or_get_active_job(lanes.slow_type, 1, {"total": 1, "completed": 0})
    lanes.worker.start()
    assert lanes.slow_started.wait(timeout=5), "慢车道未认领到任务"

    create_or_get_active_job(lanes.fast_type, 2, {"total": 1, "completed": 0})

    assert lanes.fast_done.wait(timeout=5), "慢车道被卡住时快车道也停摆了"


def test_stop_returns_promptly_while_a_long_job_runs(lanes):
    """stop 的 timeout 是所有线程共享的总预算，不等待在飞的长任务。"""
    create_or_get_active_job(lanes.slow_type, 1, {"total": 1, "completed": 0})
    lanes.worker.start()
    assert lanes.slow_started.wait(timeout=5)

    started = time.monotonic()
    lanes.worker.stop(timeout=1)
    elapsed = time.monotonic() - started

    # 4 条线程各等 1s 会是 ~4s；共享预算下应接近 1s
    assert elapsed < 3, f"stop 耗时 {elapsed:.1f}s——timeout 被按线程累加了"
    assert lanes.worker._stop_event.is_set()


def test_lane_partitions_are_disjoint_and_total(lanes):
    """快车道是补集而非白名单：新 job_type 永远有归宿。"""
    slow = set(job_worker._lane_job_types(job_worker.LANE_SLOW))
    fast = set(job_worker._lane_job_types(job_worker.LANE_FAST))

    assert slow & fast == set(), "两条车道的认领集合必须不相交（否则同一 job 会被双重认领）"
    assert slow | fast == {lanes.slow_type, lanes.fast_type}


# 真实注册表的分区快照。新增 job_type 必须显式选车道——默认落快车道，
# 一个 4h 的新类型落在那里会原样重演 issue #126。
EXPECTED_SLOW = {
    "security_analysis",
    "security_analysis_batch",
    "report_digest_backfill",
    "report_digest_batch",
}
EXPECTED_FAST = {
    "price_refresh",
    "performance_history_sync",
    "dividend_sync",
    "llm_report",
}


def test_every_registered_job_type_has_an_explicit_lane():
    import app.main  # noqa: F401 - 触发全部 runner 注册（含懒加载的 report_digest*）

    assert set(job_worker._runners) == EXPECTED_SLOW | EXPECTED_FAST, (
        "有新的 job_type 注册进来了：请显式决定它属于慢车道还是快车道，"
        "并同步更新本用例的 EXPECTED_* 常量"
    )
    assert job_worker.SLOW_LANE_JOB_TYPES == EXPECTED_SLOW


def test_slow_lane_matches_the_enqueue_time_exclusive_set():
    """慢车道成员必须与入队互斥集合一致——两边都在说「这些不该并发」。

    job_worker 刻意不 import jobs 模块（保持领域无关），字面量的漂移由这条
    断言守住。
    """
    from app.services.security_analysis_batch_jobs import ANALYSIS_EXCLUSIVE_JOB_TYPES

    assert job_worker.SLOW_LANE_JOB_TYPES == set(ANALYSIS_EXCLUSIVE_JOB_TYPES)
