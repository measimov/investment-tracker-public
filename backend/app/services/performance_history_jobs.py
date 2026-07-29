from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..core.logging import get_app_logger
from ..database import SessionLocal
from ..models.holding import Holding
from ..models.transaction import Transaction
from .market_data_service import (
    fetch_and_store_security_price_history_incremental,
    infer_price_currency,
)
from .background_job_store import (
    claim_job,
    create_or_get_active_job,
    get_job,
    handle_job_failure,
    update_job,
)
from .job_worker import register_runner


logger = get_app_logger(__name__)
MAX_CONSECUTIVE_FAILURES = 5
JOB_TYPE = "performance_history_sync"


def _set_job_progress(job_id: str, **updates) -> None:
    status = updates.pop("status", None)
    error = updates.pop("error", None)
    updates.pop("finished_at", None)
    update_job(
        job_id,
        JOB_TYPE,
        data_updates=updates,
        status=status,
        error=error,
        calculate_progress=True,
        required_status="running",
    )


def _default_history_sync_end_date() -> date:
    return date.today() - timedelta(days=1)


def get_history_sync_targets(
    db: Session,
    user_id: int,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> Dict[str, Any]:
    transactions = (
        db.query(Transaction)
        .filter(Transaction.user_id == user_id)
        .order_by(Transaction.transaction_date, Transaction.id)
        .all()
    )
    holdings = db.query(Holding).filter(Holding.user_id == user_id).all()

    if not transactions:
        sync_end = end_date or _default_history_sync_end_date()
        return {
            "start_date": start_date or sync_end,
            "end_date": sync_end,
            "targets": [],
        }

    sync_start = start_date or min(txn.transaction_date for txn in transactions)
    sync_end = end_date or _default_history_sync_end_date()
    if sync_end < sync_start:
        sync_end = sync_start
    symbols_by_key = {}
    for txn in transactions:
        key = (txn.symbol, txn.market)
        symbols_by_key[key] = txn.currency or infer_price_currency(txn.market)
    for holding in holdings:
        key = (holding.symbol, holding.market)
        symbols_by_key.setdefault(key, holding.currency or infer_price_currency(holding.market))

    return {
        "start_date": sync_start,
        "end_date": sync_end,
        "targets": [
            {"symbol": symbol, "market": market, "currency": currency}
            for (symbol, market), currency in sorted(symbols_by_key.items())
        ],
    }


def start_performance_history_sync_job(
    user_id: int,
    *,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> Dict[str, Any]:
    return create_or_get_active_job(
        JOB_TYPE,
        user_id,
        {
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
            "total": 0,
            "completed": 0,
            "success_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "progress_percent": 0,
            "current_symbol": None,
            "current_market": None,
            "results": [],
        },
    )


def execute_performance_history_sync_job(claimed: Dict[str, Any]) -> None:
    """Execute an already-claimed history sync job.

    Unexpected exceptions propagate to the caller for retry/backoff; the
    consecutive-failure early stop remains a deterministic terminal failure.
    The per-symbol fetch is incremental (already-stored rows are skipped), so a
    retried attempt is idempotent.
    """
    job_id = claimed["id"]
    job_data = claimed["data"]
    user_id = claimed["user_id"]
    requested_start = (
        date.fromisoformat(job_data["start_date"]) if job_data.get("start_date") else None
    )
    requested_end = date.fromisoformat(job_data["end_date"]) if job_data.get("end_date") else None

    db = SessionLocal()
    try:
        target_info = get_history_sync_targets(
            db,
            user_id,
            start_date=requested_start,
            end_date=requested_end,
        )
        targets: List[Dict[str, Any]] = target_info["targets"]
        _set_job_progress(
            job_id,
            total=len(targets),
            start_date=target_info["start_date"].isoformat(),
            end_date=target_info["end_date"].isoformat(),
        )

        success_count = 0
        failed_count = 0
        skipped_count = 0
        consecutive_failures = 0
        results = []

        if not targets:
            _set_job_progress(job_id, status="succeeded", completed=0)
            return

        for index, target in enumerate(targets, start=1):
            _set_job_progress(
                job_id,
                current_symbol=target["symbol"],
                current_market=target["market"],
            )
            result = fetch_and_store_security_price_history_incremental(
                db,
                symbol=target["symbol"],
                market=target["market"],
                start_date=target_info["start_date"],
                end_date=target_info["end_date"],
                currency=target["currency"],
            )
            results.append(result)
            if result.get("success"):
                success_count += 1
                consecutive_failures = 0
                if result.get("skipped"):
                    skipped_count += 1
            else:
                failed_count += 1
                consecutive_failures += 1

            _set_job_progress(
                job_id,
                completed=index,
                success_count=success_count,
                failed_count=failed_count,
                skipped_count=skipped_count,
                results=results[-50:],
            )

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                _set_job_progress(
                    job_id,
                    status="failed",
                    current_symbol=None,
                    current_market=None,
                    error=(
                        f"连续 {MAX_CONSECUTIVE_FAILURES} 个标的同步失败，已提前停止。"
                        "请稍后重试或检查 Tushare 服务状态。"
                    ),
                )
                return

        _set_job_progress(
            job_id,
            status="succeeded" if failed_count == 0 else "failed",
            current_symbol=None,
            current_market=None,
        )
    finally:
        db.close()


def run_performance_history_sync_job(job_id: str) -> None:
    """Inline fast path: claim by id and execute; retries on unexpected errors."""
    claimed = claim_job(job_id, JOB_TYPE)
    if not claimed:
        logger.info("Performance history job %s was already claimed or no longer queued", job_id)
        return
    try:
        execute_performance_history_sync_job(claimed)
    except Exception as exc:
        logger.exception("Performance history job %s failed", job_id)
        handle_job_failure(job_id, JOB_TYPE, str(exc))


def get_performance_history_sync_job(job_id: str, user_id: int) -> Optional[Dict[str, Any]]:
    return get_job(job_id, JOB_TYPE, user_id)


register_runner(JOB_TYPE, execute_performance_history_sync_job)
