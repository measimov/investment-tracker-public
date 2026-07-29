"""LLM 复盘报告后台任务（五函数模板，对齐 price_refresh_jobs）。"""

from datetime import date
from typing import Any, Dict, Optional

from ..core.logging import get_app_logger
from ..database import SessionLocal
from ..models.llm_report import LlmReport
from .background_job_store import (
    claim_job,
    create_or_get_active_job,
    get_job,
    handle_job_failure,
    update_job,
)
from .job_worker import register_periodic_task, register_runner
from .llm_client import LLMClientError, LLMNotConfiguredError, chat_completion
from .llm_report_input import build_llm_report_input
from .llm_report_prompts import build_report_messages

logger = get_app_logger(__name__)
JOB_TYPE = "llm_report"


def start_llm_report_job(user_id: int, trigger: str = "manual") -> Dict[str, Any]:
    return create_or_get_active_job(
        JOB_TYPE,
        user_id,
        {"trigger": trigger, "report_id": None},
    )


def execute_llm_report_job(claimed: Dict[str, Any]) -> None:
    """构建输入 → 调 LLM → 成功才落 llm_reports 行。

    确定性失败（未配置 key、LLM 4xx）直接置 failed 不烧重试；
    5xx/超时/意外异常向上抛，由调用方走有界重试路径。
    """
    db = SessionLocal()
    try:
        input_payload = build_llm_report_input(db, claimed["user_id"])
        try:
            completion = chat_completion(build_report_messages(input_payload))
        except LLMNotConfiguredError as exc:
            update_job(
                claimed["id"], JOB_TYPE,
                status="failed",
                error=str(exc),
                required_status="running",
            )
            return
        except LLMClientError as exc:
            if exc.status_code is not None and 400 <= exc.status_code < 500:
                update_job(
                    claimed["id"], JOB_TYPE,
                    status="failed",
                    error=str(exc),
                    required_status="running",
                )
                return
            raise

        usage = completion.get("usage") or {}
        report = LlmReport(
            user_id=claimed["user_id"],
            title=f"投资复盘 {date.today().isoformat()}",
            content=completion["content"],
            model=completion["model"],
            trigger_source=claimed.get("data", {}).get("trigger", "manual"),
            input_payload=input_payload,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
        )
        db.add(report)
        db.commit()
        db.refresh(report)
        update_job(
            claimed["id"], JOB_TYPE,
            status="succeeded",
            data_updates={"report_id": report.id},
            required_status="running",
        )
    finally:
        db.close()


def run_llm_report_job(job_id: str) -> None:
    """Inline fast path: claim by id and execute; retries on unexpected errors."""
    claimed = claim_job(job_id, JOB_TYPE)
    if not claimed:
        logger.info("LLM report job %s was already claimed or no longer queued", job_id)
        return
    try:
        execute_llm_report_job(claimed)
    except Exception as exc:
        logger.exception("LLM report job %s failed", job_id)
        handle_job_failure(job_id, JOB_TYPE, str(exc))


def get_llm_report_job(job_id: str, user_id: int) -> Optional[Dict[str, Any]]:
    return get_job(job_id, JOB_TYPE, user_id)


register_runner(JOB_TYPE, execute_llm_report_job)

# 定期自动生成：无独立调度器，挂在 worker housekeeping tick 上、小时级节流。
from .llm_report_scheduler import enqueue_due_scheduled_reports  # noqa: E402

register_periodic_task(enqueue_due_scheduled_reports, interval_seconds=3600)
