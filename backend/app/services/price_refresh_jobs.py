from typing import Any, Dict, Optional

from ..core.logging import get_app_logger
from ..database import SessionLocal
from .background_job_store import (
    claim_job,
    create_or_get_active_job,
    get_job,
    handle_job_failure,
    update_job,
)
from .job_worker import register_runner
from .stock_price_service import update_all_holdings_prices

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
        )
    finally:
        db.close()


def run_price_refresh_job(job_id: str) -> None:
    """Inline fast path: claim by id and execute; retries on unexpected errors."""
    claimed = claim_job(job_id, JOB_TYPE)
    if not claimed:
        logger.info("Price refresh job %s was already claimed or no longer queued", job_id)
        return
    try:
        execute_price_refresh_job(claimed)
    except Exception as exc:
        logger.exception("Price refresh job %s failed", job_id)
        handle_job_failure(
            job_id, JOB_TYPE, str(exc),
            required_attempt_count=claimed.get("attempt_count"),
        )


def get_price_refresh_job(job_id: str, user_id: int) -> Optional[Dict[str, Any]]:
    return get_job(job_id, JOB_TYPE, user_id)


register_runner(JOB_TYPE, execute_price_refresh_job)
