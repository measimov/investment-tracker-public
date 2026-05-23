from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..core.logging import get_app_logger
from ..core.security import get_password_hash
from ..database import SessionLocal
from ..models.user import User


logger = get_app_logger(__name__)


def seed_initial_users() -> int:
    """Create the default users for a fresh deployment."""
    with SessionLocal() as db:
        try:
            created_count = 0

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
                created_count += 1

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
                created_count += 1

            if created_count:
                db.commit()
                logger.info("Seeded %s initial users", created_count)
            else:
                logger.info("Initial users already exist; no seed changes made")

            return created_count
        except IntegrityError:
            db.rollback()
            logger.warning("Initial user seeding skipped due to existing records")
            return 0
