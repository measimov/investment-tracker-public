"""分红公告同步 job（结构照抄 price_refresh_jobs）。

手动触发为 V1 唯一入口；周期任务（周度）由 settings.dividend_sync_periodic_enabled
控制、默认关闭——dividend 接口有积分配额，且 A 股分红公告按年报/中报季集中，
盲目轮询收益极低。港股（披露易）不依赖 TUSHARE_TOKEN：未配置时周期入口只为
持有/交易过港股的用户入队。
"""

from typing import Any, Dict, Optional

from ..config import settings
from ..core.logging import get_app_logger
from ..database import SessionLocal
from .background_job_store import (
    JobOwnershipLostError,
    create_or_get_active_job,
    get_job,
    set_job_progress,
    update_job,
)
from .dividend_sync_service import (
    HKEX_MARKETS,
    SUPPORTED_MARKETS,
    sync_dividends_for_user,
    tushare_configured,
)
from .job_runtime import run_job_inline
from .job_worker import register_periodic_task, register_runner

logger = get_app_logger(__name__)
JOB_TYPE = "dividend_sync"

PERIODIC_INTERVAL_SECONDS = 7 * 24 * 3600


def start_dividend_sync_job(user_id: int) -> Dict[str, Any]:
    return create_or_get_active_job(JOB_TYPE, user_id, {"result": None})


def execute_dividend_sync_job(claimed: Dict[str, Any]) -> None:
    """单标的失败已在 service 层吞并记录，job 级失败只剩配置/连接类错误。

    进度逐标的（港股逐份表格下载）回写并续租：港股首次同步要下载历史表格，
    数十只标的会超过租约，不回写会被 worker 当 stale 接管重跑。
    """
    attempt = claimed.get("attempt_count")

    def progress(**fields: Any) -> None:
        if set_job_progress(
            claimed["id"], JOB_TYPE, required_attempt_count=attempt, **fields
        ) is None:
            raise JobOwnershipLostError(claimed["id"])

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
    run_job_inline(job_id, JOB_TYPE, execute_dividend_sync_job, label="Dividend sync", logger=logger)

def get_dividend_sync_job(job_id: str, user_id: int) -> Optional[Dict[str, Any]]:
    return get_job(job_id, JOB_TYPE, user_id)


def enqueue_periodic_dividend_sync() -> int:
    """周期入口（默认关闭）：为每个可能有应收分红的用户入队一次同步。

    用户全集 = 当前持有支持市场的用户 ∪ lookback 内交易过的用户——
    与服务层目标收集同一口径：登记日持有、随后卖清最后一只的用户
    仍会入队，交由登记日重放判定权益。
    开关关闭时静默返回 0；无 token 时只看港股；create_or_get_active_job 天然去重。
    """
    from datetime import date, timedelta

    if not settings.dividend_sync_periodic_enabled:
        return 0
    markets = SUPPORTED_MARKETS if tushare_configured() else HKEX_MARKETS

    from ..models.holding import Holding
    from ..models.transaction import Transaction

    lookback_start = date.today() - timedelta(days=settings.dividend_sync_lookback_days)
    db = SessionLocal()
    try:
        holding_users = {
            row[0]
            for row in db.query(Holding.user_id)
            .filter(Holding.quantity > 0, Holding.market.in_(markets))
            .distinct()
            .all()
        }
        traded_users = {
            row[0]
            for row in db.query(Transaction.user_id)
            .filter(
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


register_runner(JOB_TYPE, execute_dividend_sync_job)
register_periodic_task(enqueue_periodic_dividend_sync, PERIODIC_INTERVAL_SECONDS)
