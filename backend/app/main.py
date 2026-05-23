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
)
from .core.logging import configure_logging, get_app_logger


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
