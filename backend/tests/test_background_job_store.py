from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.database import SessionLocal
from app.models.background_job import BackgroundJob
from app.services.background_job_store import (
    claim_job,
    claim_next_runnable_job,
    cleanup_expired_jobs,
    create_or_get_active_job,
    fail_exhausted_jobs,
    get_job,
    handle_job_failure,
    interrupt_stale_jobs,
    update_job,
)
from app.services import performance_history_jobs, price_refresh_jobs


@pytest.fixture
def job_type():
    # setup 先清空整张表（易失状态表，别的文件的 fixture 只清理各自的 job_type）：
    # 本文件有对 interrupt_stale_jobs / cleanup_expired_jobs 的**全局计数**断言，
    # 任何前序测试残留一行 heartbeat 过期的 background_jobs 都会让计数 +1——
    # 全量跑偶发红、单跑必绿的经典形态（#105，本地长期复用 test 库时更易触发）。
    db = SessionLocal()
    try:
        db.query(BackgroundJob).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    value = f"test_{uuid4().hex[:12]}"
    yield value
    db = SessionLocal()
    try:
        db.query(BackgroundJob).filter(BackgroundJob.job_type == value).delete(
            synchronize_session=False
        )
        db.commit()
    finally:
        db.close()


def test_job_state_is_persistent_deduplicated_and_atomically_claimed(job_type):
    first = create_or_get_active_job(job_type, 2, {"total": 2, "completed": 0})
    duplicate = create_or_get_active_job(job_type, 2, {"total": 999})

    assert duplicate["id"] == first["id"]
    assert duplicate["total"] == 2
    assert claim_job(first["id"], job_type)["user_id"] == 2
    assert claim_job(first["id"], job_type) is None

    updated = update_job(
        first["id"],
        job_type,
        data_updates={
            "completed": 1,
            "value": Decimal("12.50"),
            "observed_at": datetime(2026, 7, 11, tzinfo=timezone.utc),
        },
        calculate_progress=True,
        required_status="running",
    )
    assert updated["progress_percent"] == 50
    assert updated["value"] == 12.5
    assert updated["observed_at"] == "2026-07-11T00:00:00+00:00"

    # A fresh database session can read the same state, while another user cannot.
    assert get_job(first["id"], job_type, 2)["completed"] == 1
    assert get_job(first["id"], job_type, 1) is None

    update_job(
        first["id"],
        job_type,
        status="succeeded",
        required_status="running",
    )
    next_job = create_or_get_active_job(job_type, 2, {"total": 1})
    assert next_job["id"] != first["id"]


def test_stale_jobs_are_interrupted_and_terminal_jobs_expire(job_type):
    job = create_or_get_active_job(job_type, 2, {"result": None})
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        row = db.query(BackgroundJob).filter(BackgroundJob.id == job["id"]).one()
        row.heartbeat_at = now - timedelta(hours=2)
        db.commit()
    finally:
        db.close()

    assert interrupt_stale_jobs(now=now, stale_after=timedelta(hours=1)) == 1
    interrupted = get_job(job["id"], job_type, 2)
    assert interrupted["status"] == "interrupted"
    assert interrupted["finished_at"] is not None
    assert (
        update_job(
            job["id"],
            job_type,
            status="succeeded",
            required_status="running",
        )
        is None
    )

    db = SessionLocal()
    try:
        row = db.query(BackgroundJob).filter(BackgroundJob.id == job["id"]).one()
        row.finished_at = now - timedelta(hours=2)
        db.commit()
    finally:
        db.close()

    assert cleanup_expired_jobs(now=now, retention=timedelta(hours=1)) == 1
    assert get_job(job["id"], job_type, 2) is None


def test_orphaned_queued_job_is_picked_up_by_worker_claim(job_type):
    """Issue #35: a job enqueued by a process that died before executing it is
    still claimable through the worker's runnable-job scan."""
    job = create_or_get_active_job(job_type, 2, {"result": None})

    claimed = claim_next_runnable_job([job_type], owner="worker-a")

    assert claimed is not None
    assert claimed["id"] == job["id"]
    assert claimed["job_type"] == job_type
    assert claimed["attempt_count"] == 1
    # Second scan finds nothing runnable (job is running with a live lease).
    assert claim_next_runnable_job([job_type], owner="worker-b") is None


def test_expired_lease_is_taken_over_with_attempts_remaining(job_type):
    """Issue #35: a running job whose lease expired is re-claimed by another worker."""
    job = create_or_get_active_job(job_type, 2, {"result": None})
    assert claim_next_runnable_job([job_type], owner="worker-a") is not None

    # Simulate the worker dying: expire its lease.
    db = SessionLocal()
    try:
        row = db.query(BackgroundJob).filter(BackgroundJob.id == job["id"]).one()
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()

    takeover = claim_next_runnable_job([job_type], owner="worker-b")
    assert takeover is not None
    assert takeover["id"] == job["id"]
    assert takeover["attempt_count"] == 2

    stored = get_job(job["id"], job_type, 2)
    assert stored["status"] == "running"


def test_failure_requeues_with_backoff_then_fails_at_max_attempts(job_type):
    """Issue #35: unexpected failures retry with a future next_attempt_at until
    max_attempts, then reach a terminal failed state keeping the last error."""
    job = create_or_get_active_job(job_type, 2, {"result": None})

    for attempt in range(1, 4):
        claimed = claim_next_runnable_job(
            [job_type],
            owner="worker-a",
            # Past the backoff horizon so each retry is immediately runnable.
            now=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        assert claimed is not None, f"attempt {attempt} should be claimable"
        assert claimed["attempt_count"] == attempt
        outcome = handle_job_failure(job["id"], job_type, f"boom {attempt}")
        if attempt < 3:
            assert outcome["status"] == "queued"
        else:
            assert outcome["status"] == "failed"

    stored = get_job(job["id"], job_type, 2)
    assert stored["status"] == "failed"
    assert stored["error"] == "boom 3"
    # Terminal: nothing left to claim even far in the future.
    assert (
        claim_next_runnable_job(
            [job_type], owner="worker-a", now=datetime.now(timezone.utc) + timedelta(days=1)
        )
        is None
    )


def test_retry_backoff_delays_next_attempt(job_type):
    job = create_or_get_active_job(job_type, 2, {"result": None})
    assert claim_next_runnable_job([job_type], owner="worker-a") is not None
    handle_job_failure(job["id"], job_type, "transient")

    # Not yet runnable: next_attempt_at is in the future.
    assert claim_next_runnable_job([job_type], owner="worker-a") is None
    # Runnable once the backoff has elapsed.
    later = datetime.now(timezone.utc) + timedelta(minutes=30)
    assert claim_next_runnable_job([job_type], owner="worker-a", now=later) is not None


def test_exhausted_expired_lease_job_is_terminally_failed(job_type):
    job = create_or_get_active_job(job_type, 2, {"result": None})
    db = SessionLocal()
    try:
        row = db.query(BackgroundJob).filter(BackgroundJob.id == job["id"]).one()
        row.status = "running"
        row.attempt_count = 3
        row.max_attempts = 3
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()

    assert fail_exhausted_jobs() == 1
    stored = get_job(job["id"], job_type, 2)
    assert stored["status"] == "failed"
    assert stored["finished_at"] is not None


@pytest.fixture
def clear_service_jobs():
    job_types = {price_refresh_jobs.JOB_TYPE, performance_history_jobs.JOB_TYPE}
    db = SessionLocal()
    try:
        db.query(BackgroundJob).filter(
            BackgroundJob.user_id == 2,
            BackgroundJob.job_type.in_(job_types),
        ).delete(synchronize_session=False)
        db.commit()
        yield
        db.query(BackgroundJob).filter(
            BackgroundJob.user_id == 2,
            BackgroundJob.job_type.in_(job_types),
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_price_refresh_runner_persists_json_safe_result(monkeypatch, clear_service_jobs):
    monkeypatch.setattr(
        price_refresh_jobs,
        "update_all_holdings_prices",
        lambda db, user_id: {
            "success": True,
            "price": Decimal("8.88"),
            "updated_at": datetime(2026, 7, 11, tzinfo=timezone.utc),
        },
    )
    job = price_refresh_jobs.start_price_refresh_job(2)

    price_refresh_jobs.run_price_refresh_job(job["id"])

    stored = price_refresh_jobs.get_price_refresh_job(job["id"], 2)
    assert stored["status"] == "succeeded"
    assert stored["result"]["price"] == 8.88
    assert stored["result"]["updated_at"] == "2026-07-11T00:00:00+00:00"


def test_history_runner_persists_progress_and_result(monkeypatch, clear_service_jobs):
    monkeypatch.setattr(
        performance_history_jobs,
        "get_history_sync_targets",
        lambda db, user_id, start_date, end_date: {
            "start_date": datetime(2026, 7, 1).date(),
            "end_date": datetime(2026, 7, 10).date(),
            "targets": [{"symbol": "TEST", "market": "美股", "currency": "USD"}],
        },
    )
    monkeypatch.setattr(
        performance_history_jobs,
        "fetch_and_store_security_price_history_incremental",
        lambda *args, **kwargs: {
            "symbol": kwargs["symbol"],
            "market": kwargs["market"],
            "success": True,
            "rows": 3,
        },
    )
    job = performance_history_jobs.start_performance_history_sync_job(2)

    performance_history_jobs.run_performance_history_sync_job(job["id"])

    stored = performance_history_jobs.get_performance_history_sync_job(job["id"], 2)
    assert stored["status"] == "succeeded"
    assert stored["completed"] == 1
    assert stored["progress_percent"] == 100
    assert stored["results"][0]["rows"] == 3
