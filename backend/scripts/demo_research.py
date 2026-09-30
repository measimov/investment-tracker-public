"""演示库的标的研究数据：由系统从公开数据源**真实**抓取与生成，不手写。

标的详情页展示的是一家真实公司的档案、公告与 AI 分析——手写或编造会误导读者，所以这里
只调用系统自己的任务：Tushare 档案同步、巨潮公告同步、年报摘要回填、AI 分析。需要真实
凭证（TUSHARE_TOKEN、LLM_REPORT_API_KEY），因此从持有 .env 的 backend 目录运行，并用
环境变量把库指向演示库：

    cd <带 .env 的 backend 目录>
    DATABASE_URL=postgresql://…/investment_demo python <本仓>/backend/scripts/demo_research.py

费用：年报摘要最多 4 份 + 1 次分析（DeepSeek 约几毛钱）；只写演示库（库名须含 demo）。
seed_demo.py --keep-research 重灌账本时保留这些数据，无需重复生成。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

for _flag in ("BACKGROUND_WORKER_ENABLED", "PERIODIC_TASKS_ENABLED", "EVENT_NOTIFICATIONS_ENABLED"):
    os.environ[_flag] = "false"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import make_url  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402
from app.services import announcement_sync  # noqa: E402
from app.services import report_digest_jobs, security_analysis_jobs  # noqa: E402
from app.services.background_job_store import get_job  # noqa: E402

SYMBOL, MARKET = "600900", "A股"


def main() -> None:
    name = make_url(settings.database_url).database or ""
    if "demo" not in name:
        raise SystemExit(f"拒绝写入：库名 {name!r} 不含 demo")
    if not settings.tushare_token or not settings.llm_report_api_key:
        raise SystemExit(
            "需要 TUSHARE_TOKEN 与 LLM_REPORT_API_KEY（从带 .env 的 backend 目录运行）"
        )

    db = SessionLocal()
    try:
        user_id = db.query(User).filter_by(username="demo").one().id
        result = announcement_sync.sync_announcements(
            db, keys=[(SYMBOL, MARKET)], force_window_days=365
        )
        print("公告同步：", {k: result.get(k) for k in ("synced", "new", "failed", "unsupported")})
    finally:
        db.close()

    for label, module, start, run in (
        (
            "年报摘要回填",
            report_digest_jobs,
            "start_report_backfill_job",
            "run_report_backfill_job",
        ),
        (
            "AI 分析",
            security_analysis_jobs,
            "start_security_analysis_job",
            "run_security_analysis_job",
        ),
    ):
        job = getattr(module, start)(user_id, SYMBOL, MARKET)
        getattr(module, run)(job["id"])
        final = get_job(job["id"], module.JOB_TYPE, user_id) or {}
        print(f"{label}：{final.get('status')} {final.get('error') or ''}".rstrip())


if __name__ == "__main__":
    main()
