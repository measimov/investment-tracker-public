"""任务进度回写与续租：set_job_progress / job_heartbeat / 接管保护。

[评审背景] 分析类 job 原来全程只在末尾回写一次，而 update_job 是唯一的续租
途径——超过 background_job_lease_seconds(300s) 的任务会被 worker 以"running
且租约过期"接管重跑（并发双跑、重复烧 LLM token）。
"""

import threading
import time

import pytest

from app.database import SessionLocal
from app.models.background_job import BackgroundJob
from app.services.background_job_store import (
    claim_job,
    claim_next_runnable_job,
    create_or_get_active_job,
    job_heartbeat,
    set_job_progress,
    update_job,
)  # noqa: F401 - create_or_get_active_job 供参数化的内联路径用例使用

JOB_TYPE = "security_analysis"


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        session.query(BackgroundJob).filter(BackgroundJob.job_type == JOB_TYPE).delete()
        session.commit()
        yield session
        session.rollback()
        session.query(BackgroundJob).filter(BackgroundJob.job_type == JOB_TYPE).delete()
        session.commit()
    finally:
        session.close()


def _running_job(db, user_id: int = 1):
    job = create_or_get_active_job(JOB_TYPE, user_id, {"symbol": "600036", "market": "A股"})
    claimed = claim_job(job["id"], JOB_TYPE)
    assert claimed is not None
    return claimed


def _row(db, job_id: str) -> BackgroundJob:
    db.expire_all()
    return db.query(BackgroundJob).filter(BackgroundJob.id == job_id).one()


# ---------------------------------------------------------------------------
# set_job_progress
# ---------------------------------------------------------------------------


def test_set_job_progress_merges_data_and_computes_percent(db):
    claimed = _running_job(db)
    set_job_progress(claimed["id"], JOB_TYPE, stage="sync_profile", total=6, completed=1)

    row = _row(db, claimed["id"])
    assert row.data["symbol"] == "600036"  # 浅合并，原有键保留
    assert row.data["stage"] == "sync_profile"
    assert row.data["progress_percent"] == round(1 / 6 * 100, 2)

    # status/error 走列而非 data（_serialize 展平时不会与保留键撞车）
    set_job_progress(claimed["id"], JOB_TYPE, status="failed", error="boom", completed=2)
    row = _row(db, claimed["id"])
    assert row.status == "failed"
    assert row.error == "boom"
    assert "status" not in row.data and "error" not in row.data


def test_progress_renews_lease(db):
    claimed = _running_job(db)
    before = _row(db, claimed["id"]).lease_expires_at
    time.sleep(0.05)
    set_job_progress(claimed["id"], JOB_TYPE, completed=1, total=6)
    assert _row(db, claimed["id"]).lease_expires_at > before


def test_progress_is_noop_after_takeover(db):
    """[评审回归] 租约过期被接管后，旧执行的僵尸线程不得续租、也不得覆盖
    接管者写入的结果。"""
    claimed = _running_job(db)
    attempt = claimed["attempt_count"]

    # 模拟租约过期后被 worker 接管（attempt_count +1）
    row = _row(db, claimed["id"])
    row.lease_expires_at = row.lease_expires_at.replace(year=2000)
    db.commit()
    taken = claim_next_runnable_job([JOB_TYPE], owner="worker-2")
    assert taken is not None and taken["attempt_count"] == attempt + 1

    # 旧线程按自己的 attempt 回写：必须整条 no-op
    assert set_job_progress(
        claimed["id"], JOB_TYPE, required_attempt_count=attempt,
        status="succeeded", stage="persist",
    ) is None
    row = _row(db, claimed["id"])
    assert row.status == "running"  # 接管者仍在跑，未被旧线程判为成功
    assert row.data.get("stage") is None

    # 接管者用新 attempt 回写正常生效
    assert set_job_progress(
        claimed["id"], JOB_TYPE, required_attempt_count=taken["attempt_count"],
        stage="llm_analysis",
    ) is not None


def test_update_job_without_attempt_guard_keeps_old_behavior(db):
    """required_attempt_count 默认 None 时行为不变（不影响既有调用方）。"""
    claimed = _running_job(db)
    row = _row(db, claimed["id"])
    row.attempt_count = 7
    db.commit()
    assert update_job(claimed["id"], JOB_TYPE, data_updates={"x": 1}) is not None


# ---------------------------------------------------------------------------
# job_heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_renews_lease_during_long_body(db):
    """业务体长时间不回写进度时，心跳线程仍持续续租。"""
    claimed = _running_job(db)
    before = _row(db, claimed["id"]).lease_expires_at

    with job_heartbeat(claimed["id"], JOB_TYPE, attempt_count=claimed["attempt_count"],
                       interval_seconds=0.05):
        time.sleep(0.3)  # 期间零业务回写
    after = _row(db, claimed["id"]).lease_expires_at
    assert after > before


def test_heartbeat_thread_stops_after_context_exit(db):
    claimed = _running_job(db)
    before_threads = threading.active_count()
    with job_heartbeat(claimed["id"], JOB_TYPE, interval_seconds=0.05):
        time.sleep(0.1)
    time.sleep(0.2)
    assert threading.active_count() <= before_threads  # 无线程泄漏


def test_heartbeat_stops_at_max_seconds(db):
    """护栏：超过上限即停止续租，把真正卡死的任务交还 stale 回收，
    不制造"永不过期的僵尸"。"""
    claimed = _running_job(db)
    with job_heartbeat(claimed["id"], JOB_TYPE, interval_seconds=0.05, max_seconds=0.1):
        time.sleep(0.25)
        stopped_at = _row(db, claimed["id"]).lease_expires_at
        time.sleep(0.3)
        assert _row(db, claimed["id"]).lease_expires_at == stopped_at


def test_heartbeat_stops_when_job_reaches_terminal_state(db):
    claimed = _running_job(db)
    with job_heartbeat(claimed["id"], JOB_TYPE, interval_seconds=0.05):
        update_job(claimed["id"], JOB_TYPE, status="succeeded")
        time.sleep(0.15)
        finished = _row(db, claimed["id"]).lease_expires_at
        time.sleep(0.2)
        assert _row(db, claimed["id"]).lease_expires_at == finished


# ---------------------------------------------------------------------------
# [评审回归] 异常重试路径同样需要接管保护
# ---------------------------------------------------------------------------


def test_failure_path_is_noop_after_takeover(db):
    """租约过期被接管后，旧 attempt 的 runner 抛异常不得改写接管者的执行。

    handle_job_failure 原来只筛 status=="running"，会把**接管者正在跑的那一次**
    重新排队或直接标失败——僵尸执行照样能覆盖新 owner。
    """
    from app.services.background_job_store import handle_job_failure

    claimed = _running_job(db)
    old_attempt = claimed["attempt_count"]

    row = _row(db, claimed["id"])
    row.lease_expires_at = row.lease_expires_at.replace(year=2000)
    db.commit()
    taken = claim_next_runnable_job([JOB_TYPE], owner="worker-2")
    assert taken is not None and taken["attempt_count"] == old_attempt + 1

    # 旧 runner 事后抛异常
    assert handle_job_failure(
        claimed["id"], JOB_TYPE, "旧执行的异常",
        required_attempt_count=old_attempt,
    ) is None

    row = _row(db, claimed["id"])
    assert row.status == "running"  # 接管者仍在跑
    assert row.error is None  # 未被旧执行写入错误
    assert row.attempt_count == old_attempt + 1

    # 接管者自己的失败照常走退避重试
    assert handle_job_failure(
        claimed["id"], JOB_TYPE, "接管者的异常",
        required_attempt_count=taken["attempt_count"],
    ) is not None
    assert _row(db, claimed["id"]).error == "接管者的异常"


def test_worker_passes_claimed_attempt_to_failure_path(db, monkeypatch):
    """job_worker 与内联 run_* 都必须把 claimed attempt 传进失败路径。"""
    from app.services import job_worker

    captured: dict = {}

    def spy(job_id, job_type, error, *, required_attempt_count=None):
        captured["attempt"] = required_attempt_count
        return None

    monkeypatch.setattr(job_worker.store, "handle_job_failure", spy)

    def boom(claimed):
        raise RuntimeError("runner 挂了")

    monkeypatch.setitem(job_worker._runners, JOB_TYPE, boom)
    claimed = _running_job(db)
    job_worker.execute_claimed_job(claimed)
    assert captured["attempt"] == claimed["attempt_count"]


# 全部内联 run_* 路径：(模块名, run 函数名, execute 函数名, job_type)
#
# 曾经这里只抽查 security_analysis 一条，docstring 还写着"六个"——而实际有八条，
# 于是 security_analysis_batch 与 report_digest_batch 两条漏传 attempt 的路径
# 一直没被发现。恰恰是这两条最长寿（2-4h）、最容易被接管，危害最大。
# 参数化到全部八条，并由下面的 test_every_inline_run_path_is_covered 保证
# 新增 job 家族时这份清单不会漏。
INLINE_RUN_PATHS = [
    ("price_refresh_jobs", "run_price_refresh_job", "execute_price_refresh_job"),
    ("dividend_sync_jobs", "run_dividend_sync_job", "execute_dividend_sync_job"),
    ("llm_report_jobs", "run_llm_report_job", "execute_llm_report_job"),
    (
        "performance_history_jobs",
        "run_performance_history_sync_job",
        "execute_performance_history_sync_job",
    ),
    ("security_analysis_jobs", "run_security_analysis_job", "execute_security_analysis_job"),
    ("report_digest_jobs", "run_report_backfill_job", "execute_report_backfill_job"),
    (
        "security_analysis_batch_jobs",
        "run_batch_analysis_job",
        "execute_batch_analysis_job",
    ),
    ("report_digest_batch_jobs", "run_digest_batch_job", "execute_digest_batch_job"),
]


@pytest.mark.parametrize("module_name,run_name,execute_name", INLINE_RUN_PATHS)
def test_inline_run_paths_pass_attempt(db, monkeypatch, module_name, run_name, execute_name):
    """八个内联 run_* 路径的同一契约：失败路径必须带 claimed attempt。

    不带守卫时，僵尸线程的异常会把**接管者正在跑的那一次**重新排队或标失败。
    """
    import importlib

    jobs = importlib.import_module(f"app.services.{module_name}")
    job_type = jobs.JOB_TYPE

    captured: dict = {}
    # 打桩目标是 job_runtime——八条内联路径的失败处置收敛到那里之后（#134），
    # 各 job 模块不再 import handle_job_failure。断言的契约没变（失败必须带
    # claimed attempt），只是换成盯着真正实现它的那一处；模块里若哪天又冒出
    # 一份自己的失败处置，这里会因为拦不到而立刻红。
    from app.services import job_runtime

    monkeypatch.setattr(
        job_runtime, "handle_job_failure",
        lambda job_id, jt, error, *, required_attempt_count=None: captured.update(
            attempt=required_attempt_count
        ),
    )
    monkeypatch.setattr(
        jobs, execute_name,
        lambda claimed: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    session = SessionLocal()
    try:
        session.query(BackgroundJob).filter(BackgroundJob.job_type == job_type).delete()
        session.commit()
    finally:
        session.close()

    job = create_or_get_active_job(job_type, 1, {"symbol": "600036", "market": "A股"})
    try:
        getattr(jobs, run_name)(job["id"])
        assert captured.get("attempt") == 1, (
            f"{module_name}.{run_name} 未把 claimed attempt 传进 handle_job_failure"
        )
    finally:
        session = SessionLocal()
        try:
            session.query(BackgroundJob).filter(BackgroundJob.job_type == job_type).delete()
            session.commit()
        finally:
            session.close()


def test_every_inline_run_path_is_covered():
    """新增 job 家族却忘了加进 INLINE_RUN_PATHS 时，这条会红。"""
    import importlib
    import pkgutil

    import app.services as services_pkg

    discovered = set()
    for module_info in pkgutil.iter_modules(services_pkg.__path__):
        if not module_info.name.endswith("_jobs"):
            continue
        module = importlib.import_module(f"app.services.{module_info.name}")
        for attr in dir(module):
            if attr.startswith("run_") and attr.endswith("_job") and callable(
                getattr(module, attr)
            ):
                discovered.add((module_info.name, attr))

    covered = {(m, r) for m, r, _ in INLINE_RUN_PATHS}
    assert discovered == covered, (
        f"内联 run_* 路径清单与实际不一致：缺 {discovered - covered}，多 {covered - discovered}"
    )
