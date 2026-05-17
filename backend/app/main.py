from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .config import settings
from .database import SessionLocal
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
from .models.user import User
from .core.security import get_password_hash
import logging
from logging.handlers import RotatingFileHandler
import os
from sqlalchemy.exc import IntegrityError

# Create logs directory
os.makedirs("logs", exist_ok=True)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        RotatingFileHandler("logs/app.log", maxBytes=10485760, backupCount=10),  # 10MB
        logging.StreamHandler(),
    ],
)

# Set up authentication logger
auth_logger = logging.getLogger("investment_tracker.auth")
auth_logger.setLevel(logging.INFO)
auth_handler = RotatingFileHandler("logs/auth.log", maxBytes=10485760, backupCount=10)  # 10MB
auth_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
auth_logger.addHandler(auth_handler)


def seed_initial_users() -> None:
    """Create initial users when deploying on a fresh database."""
    db = SessionLocal()
    try:
        created = False

        admin = db.query(User).filter(User.username == "admin").first()
        if not admin:
            db.add(
                User(
                    username="admin",
                    email=None,
                    hashed_password=get_password_hash(settings.admin_initial_password),
                    is_active=True,
                    is_admin=True,
                )
            )
            created = True

        demo = db.query(User).filter(User.username == "demo").first()
        if not demo:
            db.add(
                User(
                    username="demo",
                    email=None,
                    hashed_password=get_password_hash(settings.demo_initial_password),
                    is_active=True,
                    is_admin=False,
                )
            )
            created = True

        if created:
            db.commit()
            logging.info("Seeded initial users: admin, demo")
    except IntegrityError:
        db.rollback()
        logging.warning("Initial user seeding skipped due to existing records")
    finally:
        db.close()


seed_initial_users()

# Create FastAPI app with conditional documentation
app = FastAPI(
    title="Investment Tracker API",
    description="Personal investment profit/loss tracking system",
    version="1.0.0",
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
    return {"message": "Investment Tracker API", "version": "1.0.0", "docs": "/docs"}


@app.get("/health")
async def health_check():
    return {"status": "healthy"}
