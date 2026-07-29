from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..database import SessionLocal
from ..models.background_job import BackgroundJob


ACTIVE_STATUSES = {"queued", "running"}
TERMINAL_STATUSES = {"succeeded", "failed", "interrupted"}
MAX_RETRY_DELAY_SECONDS = 900


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _lease_deadline(now: datetime, lease_seconds: Optional[int] = None) -> datetime:
    return now + timedelta(seconds=lease_seconds or settings.background_job_lease_seconds)


def _claim_payload(job: BackgroundJob) -> Dict[str, Any]:
    return {
        "id": job.id,
        "job_type": job.job_type,
        "user_id": job.user_id,
        "data": dict(job.data or {}),
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
    }


def _serialize(job: BackgroundJob) -> Dict[str, Any]:
    result = {
        "id": job.id,
        "type": job.job_type,
        "status": job.status,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "error": job.error,
    }
    result.update(job.data or {})
    return result


def cleanup_expired_jobs(
    *,
    now: Optional[datetime] = None,
    retention: Optional[timedelta] = None,
) -> int:
    current_time = now or _utcnow()
    keep_for = retention or timedelta(hours=settings.background_job_retention_hours)
    cutoff = current_time - keep_for
    db = SessionLocal()
    try:
        deleted = (
            db.query(BackgroundJob)
            .filter(
                BackgroundJob.status.in_(TERMINAL_STATUSES),
                BackgroundJob.finished_at.is_not(None),
                BackgroundJob.finished_at < cutoff,
            )
            .delete(synchronize_session=False)
        )
        db.commit()
        return deleted
    finally:
        db.close()


def interrupt_stale_jobs(
    *,
    now: Optional[datetime] = None,
    stale_after: Optional[timedelta] = None,
) -> int:
    current_time = now or _utcnow()
    timeout = stale_after or timedelta(minutes=settings.background_job_stale_minutes)
    cutoff = current_time - timeout
    db = SessionLocal()
    try:
        interrupted = (
            db.query(BackgroundJob)
            .filter(
                BackgroundJob.status.in_(ACTIVE_STATUSES),
                BackgroundJob.heartbeat_at < cutoff,
            )
            .update(
                {
                    BackgroundJob.status: "interrupted",
                    BackgroundJob.error: "任务执行进程已中断，请重新启动任务。",
                    BackgroundJob.finished_at: current_time,
                    BackgroundJob.heartbeat_at: current_time,
                    BackgroundJob.updated_at: current_time,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return interrupted
    finally:
        db.close()


def create_or_get_active_job(
    job_type: str,
    user_id: int,
    data: Dict[str, Any],
) -> Dict[str, Any]:
    # Global maintenance (stale interruption, TTL cleanup) lives in the startup
    # hook and the worker's periodic housekeeping — not on this hot path, which
    # previously opened three sessions per call (issue #49).
    db = SessionLocal()
    try:
        existing = (
            db.query(BackgroundJob)
            .filter(
                BackgroundJob.user_id == user_id,
                BackgroundJob.job_type == job_type,
                BackgroundJob.status.in_(ACTIVE_STATUSES),
            )
            .order_by(BackgroundJob.created_at.desc())
            .first()
        )
        if existing:
            return _serialize(existing)

        now = _utcnow()
        job = BackgroundJob(
            id=uuid4().hex,
            user_id=user_id,
            job_type=job_type,
            status="queued",
            data=jsonable_encoder(data),
            heartbeat_at=now,
        )
        db.add(job)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = (
                db.query(BackgroundJob)
                .filter(
                    BackgroundJob.user_id == user_id,
                    BackgroundJob.job_type == job_type,
                    BackgroundJob.status.in_(ACTIVE_STATUSES),
                )
                .order_by(BackgroundJob.created_at.desc())
                .first()
            )
            if existing:
                return _serialize(existing)
            raise
        db.refresh(job)
        return _serialize(job)
    finally:
        db.close()


def claim_job(
    job_id: str,
    job_type: str,
    *,
    owner: str = "inline",
    lease_seconds: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    now = _utcnow()
    db = SessionLocal()
    try:
        claimed = (
            db.query(BackgroundJob)
            .filter(
                BackgroundJob.id == job_id,
                BackgroundJob.job_type == job_type,
                BackgroundJob.status == "queued",
            )
            .update(
                {
                    BackgroundJob.status: "running",
                    BackgroundJob.started_at: now,
                    BackgroundJob.heartbeat_at: now,
                    BackgroundJob.updated_at: now,
                    BackgroundJob.lease_owner: owner,
                    BackgroundJob.lease_expires_at: _lease_deadline(now, lease_seconds),
                    BackgroundJob.attempt_count: BackgroundJob.attempt_count + 1,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        if claimed != 1:
            return None
        job = db.query(BackgroundJob).filter(BackgroundJob.id == job_id).one()
        return _claim_payload(job)
    finally:
        db.close()


def claim_next_runnable_job(
    job_types: List[str],
    *,
    owner: str,
    lease_seconds: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Atomically claim one runnable job with FOR UPDATE SKIP LOCKED.

    Runnable means either a queued job whose next_attempt_at has passed (or is
    unset), or a running job whose lease expired with attempts remaining — the
    takeover path that recovers work from a dead process (issue #35).
    """
    if not job_types:
        return None
    current_time = now or _utcnow()
    db = SessionLocal()
    try:
        job = (
            db.query(BackgroundJob)
            .filter(
                BackgroundJob.job_type.in_(job_types),
                or_(
                    (BackgroundJob.status == "queued")
                    & (
                        BackgroundJob.next_attempt_at.is_(None)
                        | (BackgroundJob.next_attempt_at <= current_time)
                    ),
                    (BackgroundJob.status == "running")
                    & (BackgroundJob.lease_expires_at.is_not(None))
                    & (BackgroundJob.lease_expires_at < current_time)
                    & (BackgroundJob.attempt_count < BackgroundJob.max_attempts),
                ),
            )
            .order_by(BackgroundJob.created_at)
            .with_for_update(skip_locked=True)
            .first()
        )
        if job is None:
            return None
        job.status = "running"
        if job.started_at is None:
            job.started_at = current_time
        job.heartbeat_at = current_time
        job.updated_at = current_time
        job.lease_owner = owner
        job.lease_expires_at = _lease_deadline(current_time, lease_seconds)
        job.attempt_count = (job.attempt_count or 0) + 1
        db.commit()
        db.refresh(job)
        return _claim_payload(job)
    finally:
        db.close()


def handle_job_failure(job_id: str, job_type: str, error: str) -> Optional[Dict[str, Any]]:
    """Requeue a failed attempt with bounded exponential backoff, or fail it.

    Called for unexpected execution errors. Deterministic application-level
    failures should mark the job failed directly via update_job instead.
    """
    now = _utcnow()
    db = SessionLocal()
    try:
        job = (
            db.query(BackgroundJob)
            .filter(
                BackgroundJob.id == job_id,
                BackgroundJob.job_type == job_type,
                BackgroundJob.status == "running",
            )
            .with_for_update()
            .first()
        )
        if job is None:
            return None
        attempts = job.attempt_count or 0
        if attempts >= (job.max_attempts or settings.background_job_max_attempts):
            job.status = "failed"
            job.error = error
            job.finished_at = now
        else:
            delay = min(
                settings.background_job_retry_base_seconds * (2 ** max(attempts - 1, 0)),
                MAX_RETRY_DELAY_SECONDS,
            )
            job.status = "queued"
            job.error = error
            job.next_attempt_at = now + timedelta(seconds=delay)
            job.lease_owner = None
            job.lease_expires_at = None
        job.heartbeat_at = now
        job.updated_at = now
        db.commit()
        db.refresh(job)
        return _serialize(job)
    finally:
        db.close()


def fail_exhausted_jobs(*, now: Optional[datetime] = None) -> int:
    """Terminal-fail running jobs whose lease expired with no attempts left."""
    current_time = now or _utcnow()
    db = SessionLocal()
    try:
        failed = (
            db.query(BackgroundJob)
            .filter(
                BackgroundJob.status == "running",
                BackgroundJob.lease_expires_at.is_not(None),
                BackgroundJob.lease_expires_at < current_time,
                BackgroundJob.attempt_count >= BackgroundJob.max_attempts,
            )
            .update(
                {
                    BackgroundJob.status: "failed",
                    BackgroundJob.error: "任务多次执行中断，已停止重试。",
                    BackgroundJob.finished_at: current_time,
                    BackgroundJob.heartbeat_at: current_time,
                    BackgroundJob.updated_at: current_time,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return failed
    finally:
        db.close()


def update_job(
    job_id: str,
    job_type: str,
    *,
    data_updates: Optional[Dict[str, Any]] = None,
    status: Optional[str] = None,
    error: Optional[str] = None,
    calculate_progress: bool = False,
    required_status: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    now = _utcnow()
    db = SessionLocal()
    try:
        query = db.query(BackgroundJob).filter(
            BackgroundJob.id == job_id,
            BackgroundJob.job_type == job_type,
        )
        if required_status:
            query = query.filter(BackgroundJob.status == required_status)
        job = query.with_for_update().first()
        if not job:
            return None
        if data_updates:
            merged_data = {**(job.data or {}), **jsonable_encoder(data_updates)}
            if calculate_progress:
                total = merged_data.get("total") or 0
                completed = merged_data.get("completed") or 0
                merged_data["progress_percent"] = (
                    round(completed / total * 100, 2) if total else 100
                )
            job.data = merged_data
        if status:
            job.status = status
            if status in TERMINAL_STATUSES:
                job.finished_at = now
        if error is not None:
            job.error = error
        job.heartbeat_at = now
        job.updated_at = now
        # Progress updates from a live worker renew its lease.
        if job.status == "running" and job.lease_expires_at is not None:
            job.lease_expires_at = _lease_deadline(now)
        db.commit()
        db.refresh(job)
        return _serialize(job)
    finally:
        db.close()


def get_job(job_id: str, job_type: str, user_id: int) -> Optional[Dict[str, Any]]:
    db = SessionLocal()
    try:
        job = (
            db.query(BackgroundJob)
            .filter(
                BackgroundJob.id == job_id,
                BackgroundJob.job_type == job_type,
                BackgroundJob.user_id == user_id,
            )
            .first()
        )
        return _serialize(job) if job else None
    finally:
        db.close()
