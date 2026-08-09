"""财报摘要回填 job：每次最多补 4 份 digest，可重复触发续跑至补齐十年。

结构照抄五函数模板；跨标的活跃任务校验同 security_analysis_jobs 的
AnalysisBusyError 模式（每用户单活跃任务、目标必须匹配）。
"""

from typing import Any, Dict, Optional

from ..core.logging import get_app_logger
from .background_job_store import (
    create_or_get_active_job,
    get_job,
    job_heartbeat,
    update_job,
)
from ..database import SessionLocal
from .job_runtime import run_job_inline
from .job_worker import register_runner
from .report_digest_service import ensure_report_digests
from .security_analysis_jobs import AnalysisBusyError

logger = get_app_logger(__name__)
JOB_TYPE = "report_digest_backfill"

# 单次回填上限（成本护栏）：十年年报 ≈ 3 次点击补齐
BACKFILL_BATCH_SIZE = 4


def start_report_backfill_job(user_id: int, symbol: str, market: str) -> Dict[str, Any]:
    job = create_or_get_active_job(
        JOB_TYPE,
        user_id,
        {"symbol": symbol, "market": market, "result": None},
    )
    if job.get("symbol") != symbol or job.get("market") != market:
        raise AnalysisBusyError(
            f"已有针对 {job.get('symbol')}（{job.get('market')}）的报告摘要回填任务"
            "进行中，请等待其完成后再发起。",
            active_job=job,
        )
    return job


def execute_report_backfill_job(claimed: Dict[str, Any]) -> None:
    job_id = claimed["id"]
    attempt = claimed.get("attempt_count")
    symbol = claimed["data"]["symbol"]
    market = claimed["data"]["market"]

    db = SessionLocal()
    try:
        from .report_digest_service import REPORT_MARKETS

        if market not in REPORT_MARKETS:
            update_job(
                job_id, JOB_TYPE, status="failed",
                error=f"{market} 暂不支持财报摘要（支持：{'/'.join(REPORT_MARKETS)}）",
                required_status="running", required_attempt_count=attempt,
            )
            return
        # 单次最多 4 份年报（PDF 下载 + 解析 + LLM），远超 5 分钟租约：
        # 无心跳会被 worker 当作 stale 接管重跑，重复下载与烧 token
        with job_heartbeat(job_id, JOB_TYPE, attempt_count=attempt):
            result = ensure_report_digests(db, symbol, market, max_new=BACKFILL_BATCH_SIZE)
        update_job(
            job_id, JOB_TYPE, status="succeeded",
            data_updates={"result": result},
            required_status="running", required_attempt_count=attempt,
        )
    finally:
        db.close()


def run_report_backfill_job(job_id: str) -> None:
    run_job_inline(job_id, JOB_TYPE, execute_report_backfill_job, label="Report backfill", logger=logger)

def get_report_backfill_job(job_id: str, user_id: int) -> Optional[Dict[str, Any]]:
    return get_job(job_id, JOB_TYPE, user_id)


register_runner(JOB_TYPE, execute_report_backfill_job)
