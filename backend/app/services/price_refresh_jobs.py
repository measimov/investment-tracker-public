from datetime import datetime
import logging
from threading import Lock
from typing import Any, Dict, Optional
from uuid import uuid4

from ..database import SessionLocal
from .stock_price_service import update_all_holdings_prices

logger = logging.getLogger(__name__)

_jobs: Dict[str, Dict[str, Any]] = {}
_active_job_by_user: Dict[int, str] = {}
_lock = Lock()


def _public_job(job: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in job.items() if key != "user_id"}


def start_price_refresh_job(user_id: int) -> Dict[str, Any]:
    with _lock:
        active_job_id = _active_job_by_user.get(user_id)
        if active_job_id:
            active_job = _jobs.get(active_job_id)
            if active_job and active_job["status"] in {"queued", "running"}:
                return _public_job(active_job)

        job_id = uuid4().hex
        job = {
            "id": job_id,
            "user_id": user_id,
            "status": "queued",
            "created_at": datetime.utcnow(),
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
        }
        _jobs[job_id] = job
        _active_job_by_user[user_id] = job_id
        return _public_job(job)


def run_price_refresh_job(job_id: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            logger.warning("Price refresh job %s disappeared before it could run", job_id)
            return
        user_id = job["user_id"]
        job["status"] = "running"
        job["started_at"] = datetime.utcnow()

    db = SessionLocal()
    try:
        result = update_all_holdings_prices(db, user_id)
        with _lock:
            job["status"] = "succeeded" if result.get("success") else "failed"
            job["result"] = result
            job["finished_at"] = datetime.utcnow()
    except Exception as exc:
        logger.exception("Price refresh job %s failed", job_id)
        with _lock:
            job["status"] = "failed"
            job["error"] = str(exc)
            job["finished_at"] = datetime.utcnow()
    finally:
        db.close()
        with _lock:
            if _active_job_by_user.get(user_id) == job_id:
                del _active_job_by_user[user_id]


def get_price_refresh_job(job_id: str, user_id: int) -> Optional[Dict[str, Any]]:
    with _lock:
        job = _jobs.get(job_id)
        if not job or job["user_id"] != user_id:
            return None
        return _public_job(job.copy())
