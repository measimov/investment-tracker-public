"""每周数据刷新的入队调度（周期任务，1 小时 tick）。

每个活跃用户每周两件事，都在业务时区凌晨（WINDOW_START_HOUR–WINDOW_END_HOUR）入队、由
worker 慢车道执行，周期线程只做判定与入队：

1. 数据刷新（`report_digest_batch` 的 weekly 模式）：持仓 ∪ 自选的基本面档案、美股 ADS
   换算比、最新一份年报/中报的财报摘要与港股报表抽取；
2. 观点摘要（`opinion_summary_batch`）：批量本身会跳过没有新发言的标的。

是否到期按 `scheduled_task_state` 里**每个用户各自一行**判定（重启不重跑、一个用户忙
不耽误别人）。数据刷新任务以 failed 结束（有标的的档案/ADS/摘要失败）时，之后的凌晨窗口
重试：距上次入队满 RETRY_AFTER 且重试不超过 MAX_RETRIES 次——否则一次数据源故障就让
基本面静默一周不更新。两件事与用户手动发起的分析类任务互斥（`ensure_no_conflicting_analysis_job`，
同类型也算冲突）：用户正有任务在跑就跳过、下个 tick 再试；同一 tick 里先入队数据刷新，
观点摘要要等它跑完才轮得到。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..core.logging import get_app_logger
from ..core.timeutil import business_timezone
from ..database import SessionLocal
from ..models.user import User
from .job_worker import PeriodicOutcome, periodic_outcome_task
from .llm_client import is_llm_configured
from .opinion_summary_batch_jobs import start_opinion_batch_job
from .report_digest_batch_jobs import start_weekly_refresh_job
from .scheduled_state import get_detail, get_state, is_due, mark_ran, set_detail
from .security_analysis_batch_jobs import (
    NoBatchTargetsError,
    ensure_no_conflicting_analysis_job,
)
from .security_analysis_jobs import AnalysisBusyError
from .xueqiu_opinion_source import OpinionSourceUnavailable

logger = get_app_logger(__name__)

PERIODIC_INTERVAL_SECONDS = 3600
WEEKLY_INTERVAL = timedelta(days=7)
# 业务时区凌晨窗口 [start, end)：避开白天的手动操作与交易时段的行情刷新
WINDOW_START_HOUR = 2
WINDOW_END_HOUR = 6

# 失败重试：距上次入队至少这么久（落到下一个凌晨窗口），每个周期最多重试几次
RETRY_AFTER = timedelta(hours=20)
MAX_RETRIES = 2

DATA_TASK = "weekly_data_refresh"
OPINION_TASK = "weekly_opinion_refresh"


def state_name(task: str, user_id: int) -> str:
    return f"{task}:user:{user_id}"


def in_window(now_utc: datetime) -> bool:
    local = now_utc.astimezone(business_timezone())
    return WINDOW_START_HOUR <= local.hour < WINDOW_END_HOUR


def _try_claim(db: Session, user_id: int) -> bool:
    """互斥预检：用户有任何分析类活跃任务（含同类型）即返回 False。

    预检持有事务级顾问锁，调用方入队后 commit 才释放；冲突时这里就回滚释放。"""
    try:
        ensure_no_conflicting_analysis_job(db, user_id, None)
        return True
    except AnalysisBusyError:
        db.rollback()
        return False


def retryable_job_outcome(job: Optional[Dict[str, Any]]) -> bool:
    """失败，或被中断而**不是用户主动取消**（用户终止同样写 interrupted + cancelled=True）。

    只请求了终止、还没走到下一个标的写 cancelled 就被中断（排队超时 / 进程死掉）的，同样
    是用户的意思：cancel_requested 也排除（PR #297 评审）。"""
    if not job:
        return False
    if job.get("status") == "failed":
        return True
    return (
        job.get("status") == "interrupted"
        and not job.get("cancelled")
        and not job.get("cancel_requested")
    )


def data_retry_due(db: Session, user_id: int, now: datetime) -> bool:
    """上一次入队的数据刷新任务失败或被中断（非用户取消）、距该次入队满 RETRY_AFTER、
    重试次数未用尽。

    间隔按明细里的 enqueued_at（每次入队都写，含重试）计，而不是 last_run_at：重试不推进
    last_run_at（不重置每周起点），拿它当基准的话第 2 次重试会紧跟第 1 次。"""
    from .background_job_store import get_job
    from .report_digest_batch_jobs import JOB_TYPE as DIGEST_BATCH_JOB_TYPE

    state = get_state(db, state_name(DATA_TASK, user_id))
    if state is None or state.last_run_at is None:
        return False
    detail = state.detail or {}
    job_id = detail.get("job_id")
    if not job_id or int(detail.get("retries") or 0) >= MAX_RETRIES:
        return False
    enqueued_at = detail.get("enqueued_at")
    last_enqueue = datetime.fromisoformat(enqueued_at) if enqueued_at else state.last_run_at
    if now - last_enqueue < RETRY_AFTER:
        return False
    return retryable_job_outcome(get_job(job_id, DIGEST_BATCH_JOB_TYPE, user_id))


def _enqueue_data_refresh(db: Session, user_id: int, now: datetime, *, retry: bool = False) -> str:
    """返回 enqueued / busy / no_targets。retry=True 是失败后的重试：只更新明细，
    不推进 last_run_at（不重置每周起点）。"""
    if not _try_claim(db, user_id):
        return "busy"
    name = state_name(DATA_TASK, user_id)
    try:
        job = start_weekly_refresh_job(db, user_id)
    except NoBatchTargetsError:
        db.rollback()
        mark_ran(db, name, detail={"status": "no_targets", "job_id": None}, now=now)
        return "no_targets"
    db.commit()  # 释放顾问锁
    detail = {"status": "enqueued", "job_id": job.get("id"), "enqueued_at": now.isoformat()}
    if retry:
        detail["retries"] = int(get_detail(db, name).get("retries") or 0) + 1
        set_detail(db, name, detail)
    else:
        mark_ran(db, name, detail={**detail, "retries": 0}, now=now)
    return "enqueued"


def _enqueue_opinion_refresh(db: Session, user_id: int, now: datetime) -> str:
    """返回 enqueued / busy / no_targets / unavailable。"""
    if not _try_claim(db, user_id):
        return "busy"
    name = state_name(OPINION_TASK, user_id)
    try:
        job = start_opinion_batch_job(db, user_id)
    except NoBatchTargetsError:
        db.rollback()
        mark_ran(db, name, detail={"status": "no_targets"}, now=now)
        return "no_targets"
    except OpinionSourceUnavailable:
        # 采集器还没跑出数据：不记已跑，下个 tick 再看
        db.rollback()
        return "unavailable"
    db.commit()
    mark_ran(db, name, detail={"status": "enqueued", "job_id": job.get("id")}, now=now)
    return "enqueued"


def enqueue_weekly_refresh(db: Session, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """对每个活跃用户判定两件每周任务是否到期并入队；返回各状态计数。"""
    now = now or datetime.now(timezone.utc)
    llm_ready = is_llm_configured()
    counts: Dict[str, int] = {}
    errors: Dict[int, str] = {}
    user_ids = [
        row[0] for row in db.query(User.id).filter(User.is_active.is_(True)).order_by(User.id).all()
    ]
    for user_id in user_ids:
        try:
            due = is_due(db, state_name(DATA_TASK, user_id), WEEKLY_INTERVAL, now=now)
            if due or data_retry_due(db, user_id, now):
                status = _enqueue_data_refresh(db, user_id, now, retry=not due)
                if not due:
                    status = f"retry_{status}" if status != "enqueued" else status
                    counts["data_retry"] = counts.get("data_retry", 0) + 1
                counts[f"data_{status}"] = counts.get(f"data_{status}", 0) + 1
                if status == "enqueued":
                    continue  # 观点摘要与之互斥：等数据刷新跑完，下个 tick 再入队
            if llm_ready and is_due(
                db, state_name(OPINION_TASK, user_id), WEEKLY_INTERVAL, now=now
            ):
                status = _enqueue_opinion_refresh(db, user_id, now)
                counts[f"opinion_{status}"] = counts.get(f"opinion_{status}", 0) + 1
        except Exception as exc:  # 单用户失败不影响其他用户
            db.rollback()
            logger.warning("每周数据刷新入队失败 user=%s: %s", user_id, str(exc)[:200])
            errors[user_id] = str(exc)[:200]
    return {"counts": counts, "errors": errors, "users": len(user_ids)}


@periodic_outcome_task
def periodic_enqueue_weekly_data_refresh() -> PeriodicOutcome:
    """周期任务入口（以名字 enqueue_weekly_data_refresh 注册）。"""
    if not settings.weekly_data_refresh_enabled:
        return PeriodicOutcome.skipped("WEEKLY_DATA_REFRESH_ENABLED=false")
    now = datetime.now(timezone.utc)
    if not in_window(now):
        return PeriodicOutcome.skipped("不在凌晨入队窗口")
    db = SessionLocal()
    try:
        result = enqueue_weekly_refresh(db, now=now)
    finally:
        db.close()
    enqueued = sum(value for key, value in result["counts"].items() if key.endswith("_enqueued"))
    if result["errors"]:
        return PeriodicOutcome.failed(
            f"{len(result['errors'])} 个用户入队失败：" + "；".join(result["errors"].values()),
            count=enqueued,
        )
    if enqueued:
        return PeriodicOutcome.succeeded(enqueued)
    return PeriodicOutcome.skipped("无到期任务或用户有任务在跑", count=0)
