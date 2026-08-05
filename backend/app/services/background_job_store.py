import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..core.logging import get_app_logger
from ..database import SessionLocal
from ..models.background_job import BackgroundJob

logger = get_app_logger(__name__)


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


def handle_job_failure(
    job_id: str,
    job_type: str,
    error: str,
    *,
    required_attempt_count: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Requeue a failed attempt with bounded exponential backoff, or fail it.

    Called for unexpected execution errors. Deterministic application-level
    failures should mark the job failed directly via update_job instead.

    required_attempt_count 与 update_job 同义且同样重要：租约过期被接管后，
    旧 attempt 的 runner 仍可能抛异常走到这里。不校验 attempt 就会把**接管者
    正在跑的那一次**重新排队或直接标失败——僵尸执行照样能改写新 owner 的任务，
    正是接管保护要消除的竞态。
    """
    now = _utcnow()
    db = SessionLocal()
    try:
        query = db.query(BackgroundJob).filter(
            BackgroundJob.id == job_id,
            BackgroundJob.job_type == job_type,
            BackgroundJob.status == "running",
        )
        if required_attempt_count is not None:
            query = query.filter(BackgroundJob.attempt_count == required_attempt_count)
        job = query.with_for_update().first()
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
    required_attempt_count: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """更新任务状态/进度；running 任务顺带续租。

    required_attempt_count：只有 attempt_count 未变（本次执行未被接管）才写入。
    租约过期后 worker 会重新认领并 attempt_count+1，此时旧执行的线程仍活着——
    没有这道判据，僵尸线程会给接管者续租、并用自己的终态覆盖接管者的结果。
    """
    now = _utcnow()
    db = SessionLocal()
    try:
        query = db.query(BackgroundJob).filter(
            BackgroundJob.id == job_id,
            BackgroundJob.job_type == job_type,
        )
        if required_status:
            query = query.filter(BackgroundJob.status == required_status)
        if required_attempt_count is not None:
            query = query.filter(BackgroundJob.attempt_count == required_attempt_count)
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
            if status == "succeeded":
                # 重试成功后不残留上一次尝试的错误信息（观感与排障噪音）
                job.error = None
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


def find_active_job_of_types(
    user_id: int,
    job_types: List[str],
    *,
    exclude_job_type: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """该用户在给定 job_type 集合中的任一活跃任务。

    partial unique index 只覆盖单一 (user_id, job_type)，因此同一用户可以同时
    跑单标的分析与批量分析——两者都在打同一批外部 API，会双倍消耗配额。
    需要跨类型互斥的调用方用本函数显式预检。
    """
    types = [t for t in job_types if t != exclude_job_type]
    if not types:
        return None
    db = SessionLocal()
    try:
        job = (
            db.query(BackgroundJob)
            .filter(
                BackgroundJob.user_id == user_id,
                BackgroundJob.job_type.in_(types),
                BackgroundJob.status.in_(ACTIVE_STATUSES),
            )
            .order_by(BackgroundJob.created_at.desc())
            .first()
        )
        return _serialize(job) if job else None
    finally:
        db.close()


def set_job_progress(
    job_id: str,
    job_type: str,
    *,
    required_attempt_count: Optional[int] = None,
    **updates: Any,
) -> Optional[Dict[str, Any]]:
    """运行中回写进度（并续租）：status/error 单独提出，其余浅合并进 data。

    调用方只需传业务字段，例如
    `set_job_progress(job_id, JOB_TYPE, completed=3, current_symbol="600036")`。
    """
    status = updates.pop("status", None)
    error = updates.pop("error", None)
    return update_job(
        job_id,
        job_type,
        data_updates=updates or None,
        status=status,
        error=error,
        calculate_progress=True,
        required_status="running",
        required_attempt_count=required_attempt_count,
    )


@contextmanager
def job_heartbeat(
    job_id: str,
    job_type: str,
    *,
    attempt_count: Optional[int] = None,
    interval_seconds: Optional[float] = None,
    max_seconds: Optional[float] = None,
):
    """守护线程周期续租，覆盖**单次调用内部**就超过租约的区间。

    阶段性进度回写解决"用户看得见的进度"，但一次 pdfplumber 解析大年报或一次
    120s 超时的 LLM 调用本身就可能吃掉整个租约；租约过期会被 worker 当作 stale
    接管重跑（并发双跑、重复烧 token）。

    - interval 默认 = 租约 / 3，确保每个租约周期内至少续租两次。
    - max_seconds 是护栏：超过后停止续租，把真正卡死的任务交还给 stale 回收，
      避免制造"永不过期的僵尸任务"。
    - attempt_count 传入后，一旦被接管（attempt_count 变化）续租即自动失效。
    """
    lease = settings.background_job_lease_seconds
    interval = interval_seconds or max(lease / 3, 5)
    deadline_seconds = (
        max_seconds
        if max_seconds is not None
        else settings.background_job_stale_minutes * 60
    )
    stop_event = threading.Event()

    def beat() -> None:
        elapsed = 0.0
        while not stop_event.wait(interval):
            elapsed += interval
            if elapsed >= deadline_seconds:
                logger.warning(
                    "任务 %s(%s) 心跳超过 %.0fs 上限，停止续租", job_id, job_type,
                    deadline_seconds,
                )
                return
            try:
                updated = update_job(
                    job_id,
                    job_type,
                    required_status="running",
                    required_attempt_count=attempt_count,
                )
                if updated is None:
                    return  # 已终态或已被接管：本次执行不再持有该任务
            except Exception as exc:  # 心跳失败不得影响业务执行
                logger.warning("任务 %s 心跳续租失败: %s", job_id, str(exc)[:150])

    thread = threading.Thread(
        target=beat, name=f"job-heartbeat-{job_id[:8]}", daemon=True
    )
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=5)


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
