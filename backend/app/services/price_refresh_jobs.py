from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ..config import settings
from ..core.logging import get_app_logger
from ..database import SessionLocal
from .background_job_store import (
    create_or_get_active_job,
    get_job,
    update_job,
)
from .job_runtime import run_job_inline
from .job_worker import PeriodicOutcome, periodic_outcome_task, register_runner
from .market_sessions import open_markets
from .stock_price_service import refresh_quotes, update_all_holdings_prices

logger = get_app_logger(__name__)
JOB_TYPE = "price_refresh"


def start_price_refresh_job(user_id: int) -> Dict[str, Any]:
    return create_or_get_active_job(
        JOB_TYPE,
        user_id,
        {"result": None},
    )


def execute_price_refresh_job(claimed: Dict[str, Any]) -> None:
    """Execute an already-claimed price refresh job.

    Unexpected exceptions propagate to the caller, which routes them through the
    retry/backoff path; an unsuccessful result is a deterministic failure.
    """
    db = SessionLocal()
    try:
        result = update_all_holdings_prices(db, claimed["user_id"])
        update_job(
            claimed["id"],
            JOB_TYPE,
            status="succeeded" if result.get("success") else "failed",
            data_updates={"result": result},
            required_status="running",
            # 接管者的状态同样是 running，只校验 status 挡不住僵尸线程改写终态
            required_attempt_count=claimed.get("attempt_count"),
        )
    finally:
        db.close()


def run_price_refresh_job(job_id: str) -> None:
    run_job_inline(
        job_id, JOB_TYPE, execute_price_refresh_job, label="Price refresh", logger=logger
    )


def get_price_refresh_job(job_id: str, user_id: int) -> Optional[Dict[str, Any]]:
    return get_job(job_id, JOB_TYPE, user_id)


register_runner(JOB_TYPE, execute_price_refresh_job)


# 交易时段自动刷新：每 15 分钟一次，只刷当前处于交易时段的市场（各市场时段在收盘后
# 留约 30 分钟余量、且周期刷新不看新鲜度窗口，保证每天至少有一次落在收盘之后）
PERIODIC_INTERVAL_SECONDS = 15 * 60


@periodic_outcome_task
def periodic_refresh_quotes() -> PeriodicOutcome:
    """周期入口（注册名 refresh_quotes）：全体活跃用户的持仓（数量 > 0）∪ 自选。

    休市/全部市场收盘 → skipped；报价全部失败 → failed；部分失败仍算 succeeded
    （单只标的的报价源问题不代表任务坏了，失败明细在日志里）。刷完把有昨收的持仓
    交给价格异动提醒；提醒出错只记日志，不影响本次刷新的结果。
    """
    if not settings.quote_auto_refresh_enabled:
        return PeriodicOutcome.skipped("QUOTE_AUTO_REFRESH_ENABLED=false")
    markets = open_markets(datetime.now(timezone.utc))
    if not markets:
        return PeriodicOutcome.skipped("当前没有市场处于交易时段")
    db = SessionLocal()
    try:
        result = refresh_quotes(db, markets=markets, only_open=True, ignore_freshness=True)
        if not result.get("success"):
            return PeriodicOutcome.failed(str(result.get("error") or "报价写库失败"))
        moves = result.get("moves") or []
        if moves:
            try:
                from .event_notifications import notify_price_moves

                notify_price_moves(db, moves)
            except Exception:  # noqa: BLE001 - 提醒失败不拖累报价刷新
                db.rollback()
                logger.exception("价格异动提醒失败")
        success, failed = result["success_count"], result["failed_count"]
        if failed and not success:
            first = (result.get("failed_list") or [{}])[0].get("error") or "未知错误"
            return PeriodicOutcome.failed(f"{failed} 个标的报价全部失败：{first[:200]}", failed)
        return PeriodicOutcome.succeeded(
            success, f"{'/'.join(markets)}：成功 {success}、失败 {failed}"
        )
    finally:
        db.close()
