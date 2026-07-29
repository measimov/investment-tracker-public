"""LLM 报告定期生成的到期检查（由 job_worker 周期任务钩子调用）。

到期语义：cadence ≠ off 且该用户最新一份报告（**任意 trigger 都算**——
手动报告抑制周期内的自动生成，避免重复花费）早于周期长度。幂等性由两层
保证：报告落库即不再到期；create_or_get_active_job 的"每用户单活跃任务"
唯一约束吞掉并发入队。宕机恢复后每用户恰好补一份，无回填风暴。
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func

from ..core.logging import get_app_logger
from ..database import SessionLocal
from ..models.background_job import BackgroundJob
from ..models.llm_report import LlmReport, LlmReportSchedule
from .background_job_store import ACTIVE_STATUSES, create_or_get_active_job
from .llm_client import is_llm_configured

logger = get_app_logger(__name__)

CADENCE_DAYS = {"weekly": 7, "monthly": 30}


def enqueue_due_scheduled_reports(now: Optional[datetime] = None) -> int:
    """为所有到期用户入队 llm_report 任务；返回入队数。"""
    if not is_llm_configured():
        return 0
    now = now or datetime.now(timezone.utc)

    db = SessionLocal()
    try:
        schedules = (
            db.query(LlmReportSchedule)
            .filter(LlmReportSchedule.cadence.in_(list(CADENCE_DAYS)))
            .all()
        )
        if not schedules:
            return 0

        latest_by_user = dict(
            db.query(LlmReport.user_id, func.max(LlmReport.created_at))
            .filter(LlmReport.user_id.in_([s.user_id for s in schedules]))
            .group_by(LlmReport.user_id)
            .all()
        )
        users_with_active_job = {
            row[0]
            for row in db.query(BackgroundJob.user_id).filter(
                BackgroundJob.job_type == "llm_report",
                BackgroundJob.status.in_(ACTIVE_STATUSES),
            )
        }
    finally:
        db.close()

    enqueued = 0
    for schedule in schedules:
        if schedule.user_id in users_with_active_job:
            continue  # 已有进行中的报告任务（含手动触发）
        latest = latest_by_user.get(schedule.user_id)
        if latest is not None and latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        due_before = now - timedelta(days=CADENCE_DAYS[schedule.cadence])
        if latest is None or latest < due_before:
            create_or_get_active_job(
                "llm_report",
                schedule.user_id,
                {"trigger": "scheduled", "report_id": None},
            )
            enqueued += 1
            logger.info(
                "Scheduled LLM report enqueued for user=%s cadence=%s",
                schedule.user_id,
                schedule.cadence,
            )
    return enqueued
