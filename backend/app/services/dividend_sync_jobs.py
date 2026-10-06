"""分红公告同步 job（结构照抄 price_refresh_jobs）。

手动触发之外，周期任务（每日）由 settings.dividend_sync_periodic_enabled
控制、默认关闭——开启前确认 Tushare 积分配额，沿用现有接口限速与配额退避。
港股（披露易）不依赖 TUSHARE_TOKEN：未配置时周期入口只为
持有/交易过港股的用户入队。

周期入口每小时 tick 一次，是否满一天按 scheduled_task_state 里的上次入队时间判定，
避免每次重启/部署后重复同步。
"""

from datetime import timedelta
from typing import Any, Dict, Optional

from ..config import settings
from ..core.timeutil import local_today
from ..core.logging import get_app_logger
from ..database import SessionLocal
from .background_job_store import (
    create_or_get_active_job,
    get_job,
    update_job,
)
from .dividend_sync_service import (
    HKEX_MARKETS,
    SUPPORTED_MARKETS,
    sync_dividends_for_user,
)
from .job_runtime import make_batch_progress, run_job_inline
from .stock_price_service import tushare_configured
from .job_worker import (
    PeriodicOutcome,
    periodic_outcome_task,
    register_runner,
)

logger = get_app_logger(__name__)
JOB_TYPE = "dividend_sync"

# 每小时 tick；真正的周期（每日一次）按库内上次入队时间判定
PERIODIC_INTERVAL_SECONDS = 3600
PERIODIC_TASK_NAME = "enqueue_periodic_dividend_sync"
PERIODIC_EVERY = timedelta(days=1)


def start_dividend_sync_job(user_id: int) -> Dict[str, Any]:
    return create_or_get_active_job(JOB_TYPE, user_id, {"result": None})


def execute_dividend_sync_job(claimed: Dict[str, Any]) -> None:
    """单标的失败已在 service 层吞并记录，job 级失败只剩配置/连接类错误。

    进度逐标的（港股逐份表格下载）回写并续租：港股首次同步要下载历史表格，
    数十只标的会超过租约，不回写会被 worker 当 stale 接管重跑。
    """
    progress = make_batch_progress(claimed["id"], JOB_TYPE, claimed.get("attempt_count"))

    db = SessionLocal()
    try:
        result = sync_dividends_for_user(db, claimed["user_id"], progress=progress)
        update_job(
            claimed["id"],
            JOB_TYPE,
            status="succeeded",
            data_updates={"result": result},
            required_status="running",
            # 接管者的状态同样是 running，只校验 status 挡不住僵尸线程改写终态
            required_attempt_count=claimed.get("attempt_count"),
        )
    finally:
        db.close()


def run_dividend_sync_job(job_id: str) -> None:
    run_job_inline(
        job_id, JOB_TYPE, execute_dividend_sync_job, label="Dividend sync", logger=logger
    )


def get_dividend_sync_job(job_id: str, user_id: int) -> Optional[Dict[str, Any]]:
    return get_job(job_id, JOB_TYPE, user_id)


def enqueue_periodic_dividend_sync() -> int:
    """周期入口（默认关闭）：为每个可能有应收分红的用户入队一次同步。

    用户全集 = 当前持有支持市场的活跃用户 ∪ lookback 内交易过的活跃用户——
    与服务层目标收集同一口径：登记日持有、随后卖清最后一只的用户
    仍会入队，交由登记日重放判定权益。停用的用户不入队。
    开关关闭时静默返回 0；无 token 时只看港股；create_or_get_active_job 天然去重。
    """

    if not settings.dividend_sync_periodic_enabled:
        return 0
    markets = SUPPORTED_MARKETS if tushare_configured() else HKEX_MARKETS

    from ..models.holding import Holding
    from ..models.transaction import Transaction
    from ..models.user import User

    lookback_start = local_today() - timedelta(days=settings.dividend_sync_lookback_days)
    db = SessionLocal()
    try:
        holding_users = {
            row[0]
            for row in db.query(Holding.user_id)
            .join(User, User.id == Holding.user_id)
            .filter(
                User.is_active.is_(True),
                Holding.quantity > 0,
                Holding.market.in_(markets),
            )
            .distinct()
            .all()
        }
        traded_users = {
            row[0]
            for row in db.query(Transaction.user_id)
            .join(User, User.id == Transaction.user_id)
            .filter(
                User.is_active.is_(True),
                Transaction.market.in_(markets),
                Transaction.transaction_date >= lookback_start,
            )
            .distinct()
            .all()
        }
        user_ids = sorted(holding_users | traded_users)
    finally:
        db.close()

    for user_id in user_ids:
        start_dividend_sync_job(user_id)
    return len(user_ids)


@periodic_outcome_task
def periodic_enqueue_dividend_sync() -> PeriodicOutcome:
    """周期任务入口（以名字 enqueue_periodic_dividend_sync 注册，每小时 tick）：
    开关关闭或距上次入队不足一天为 skipped；查询/入队出错照常上抛（worker 记失败，
    且不记入队时间，下个 tick 重试）；同步任务本身的失败由后台任务检查器报告。"""
    from . import scheduled_state

    if not settings.dividend_sync_periodic_enabled:
        return PeriodicOutcome.skipped("DIVIDEND_SYNC_PERIODIC_ENABLED=false")
    db = SessionLocal()
    try:
        if not scheduled_state.is_due(db, PERIODIC_TASK_NAME, PERIODIC_EVERY):
            return PeriodicOutcome.skipped("距上次入队不足 24 小时")
        enqueued = enqueue_periodic_dividend_sync()
        scheduled_state.mark_ran(db, PERIODIC_TASK_NAME, detail={"enqueued_users": enqueued})
    finally:
        db.close()
    return PeriodicOutcome.succeeded(enqueued)


register_runner(JOB_TYPE, execute_dividend_sync_job)
