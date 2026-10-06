from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .config import settings
from .core.proxy_headers import ProxyHeadersMiddleware
from .database import get_db
from .api import (
    announcements,
    transactions,
    holdings,
    statistics,
    import_export,
    corporate_actions,
    exchange_rates,
    auth,
    users,
    broker_accounts,
    import_batches,
    cash_events,
    reconciliation_snapshots,
    security_rules,
    llm_reports,
    notifications,
    security_catalog,
    security_profiles,
    watchlist,
    xueqiu_collector,
    capabilities,
)
from .core.logging import configure_logging, get_app_logger
from .services.background_job_store import cleanup_expired_jobs, interrupt_stale_jobs

# 后台任务 runner 显式注册：worker 只认领 _runners 里已注册的 job_type，
# 而注册发生在各 jobs 模块的 import 副作用里。只被路由函数体**懒加载**的模块
# （report_digest_jobs / report_digest_batch_jobs）在冷启动时不会被导入——
# 进程若在任务入队后重启，且没有用户再访问对应路由，queued/租约过期的任务
# 永远不会被接管，只能被 stale 清理中断。这与 job 模块宣称的恢复语义相反。
from .services import opinion_summary_batch_jobs as _opinion_summary_batch_jobs  # noqa: F401
from .services import opinion_summary_jobs as _opinion_summary_jobs  # noqa: F401
from .services import report_digest_batch_jobs as _report_digest_batch_jobs  # noqa: F401
from .services import report_digest_jobs as _report_digest_jobs  # noqa: F401
from .services.job_worker import start_worker, stop_worker
from .services.periodic_registry import register_all as register_periodic_tasks

# 周期任务的唯一注册处（#273）：全部任务、间隔与调度分组都在 services/periodic_registry.py
register_periodic_tasks()


configure_logging()
logger = get_app_logger(__name__)


# Create FastAPI app with conditional documentation
@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动：中断失联任务、清理过期任务、启动 worker；关闭：停 worker。
    （@app.on_event 已被 FastAPI 标记废弃，#273）"""
    reconcile_background_jobs()
    try:
        yield
    finally:
        stop_worker()


app = FastAPI(
    title="Investment Tracker API",
    description="Personal investment profit/loss tracking system",
    version=settings.app_version,
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
    lifespan=lifespan,
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_cors_origins_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# 必须**最后**添加 = 栈的最外层（add_middleware 是 insert(0)）：scheme 要在
# 路由之前定下来，否则 Starlette 的 slash redirect 会用 http 生成绝对 Location，
# 把 HTTPS 客户端的 POST body 引到明文端口上。详见 core/proxy_headers.py。
app.add_middleware(ProxyHeadersMiddleware, settings=settings)

# Include routers
app.include_router(auth.router)
app.include_router(capabilities.router)
app.include_router(users.router)
app.include_router(transactions.router, prefix="/api/transactions", tags=["Transactions"])
app.include_router(holdings.router, prefix="/api/holdings", tags=["Holdings"])
app.include_router(statistics.router, prefix="/api/statistics", tags=["Statistics"])
app.include_router(import_export.router, prefix="/api", tags=["Import/Export"])
app.include_router(
    corporate_actions.router, prefix="/api/corporate-actions", tags=["Corporate Actions"]
)
app.include_router(exchange_rates.router, prefix="/api", tags=["Exchange Rates"])
app.include_router(
    broker_accounts.router,
    prefix="/api/broker-accounts",
    tags=["Broker Accounts"],
)
app.include_router(
    import_batches.router,
    prefix="/api/import-batches",
    tags=["Import Batches"],
)
app.include_router(
    cash_events.router,
    prefix="/api/cash-events",
    tags=["Cash Events"],
)
app.include_router(
    reconciliation_snapshots.router,
    prefix="/api/reconciliation-snapshots",
    tags=["Reconciliation Snapshots"],
)
app.include_router(
    security_rules.router,
    prefix="/api/security-rules",
    tags=["Security Rules"],
)
app.include_router(
    llm_reports.router,
    prefix="/api/llm-reports",
    tags=["LLM Reports"],
)
app.include_router(
    security_profiles.router,
    prefix="/api/securities",
    tags=["Security Profiles"],
)
app.include_router(
    security_catalog.router,
    prefix="/api/securities",
    tags=["Security Catalog"],
)
app.include_router(
    watchlist.router,
    prefix="/api",
    tags=["Watchlist"],
)
app.include_router(xueqiu_collector.router)
app.include_router(notifications.router)
app.include_router(announcements.router)


def reconcile_background_jobs() -> None:
    interrupted = interrupt_stale_jobs()
    deleted = cleanup_expired_jobs()
    if interrupted or deleted:
        logger.info(
            "Background job reconciliation completed: interrupted=%s, deleted=%s",
            interrupted,
            deleted,
        )
    # Reliability net for background jobs (leases, takeover, retries).
    start_worker()


@app.get("/")
def root():
    return {
        "message": "Investment Tracker API",
        "api_version": settings.app_version,
        "build": settings.build_sha,
        "docs": "/docs" if settings.enable_docs else None,
    }


@app.get("/health")
def health_check(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.exception("Health check database probe failed")
        raise HTTPException(
            status_code=503,
            detail={
                "status": "unhealthy",
                "database": "unreachable",
                "error": exc.__class__.__name__,
            },
        ) from exc

    return {
        "status": "healthy",
        "database": "reachable",
        "api_version": settings.app_version,
        "build": settings.build_sha,
    }
