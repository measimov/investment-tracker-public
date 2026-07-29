from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .api import (
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
    excluded_securities,
    llm_reports,
)
from .core.logging import configure_logging, get_app_logger
from .services.background_job_store import cleanup_expired_jobs, interrupt_stale_jobs
from .services.job_worker import start_worker, stop_worker


configure_logging()
logger = get_app_logger(__name__)

# Create FastAPI app with conditional documentation
app = FastAPI(
    title="Investment Tracker API",
    description="Personal investment profit/loss tracking system",
    version=settings.app_version,
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_cors_origins_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(auth.router)
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
    excluded_securities.router,
    prefix="/api/excluded-securities",
    tags=["Excluded Securities"],
)
app.include_router(
    llm_reports.router,
    prefix="/api/llm-reports",
    tags=["LLM Reports"],
)


@app.on_event("startup")
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


@app.on_event("shutdown")
def shutdown_background_worker() -> None:
    stop_worker()


@app.get("/")
async def root():
    return {
        "message": "Investment Tracker API",
        "api_version": settings.app_version,
        "build": settings.build_sha,
        "docs": "/docs" if settings.enable_docs else None,
    }


@app.get("/health")
async def health_check(db: Session = Depends(get_db)):
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
