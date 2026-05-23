from app.database import SessionLocal
from app.models.user import User
from app.services.user_seed import seed_initial_users


def test_seed_initial_users_is_idempotent():
    seed_initial_users()
    created_count = seed_initial_users()

    db = SessionLocal()
    try:
        users = db.query(User).filter(User.username.in_(["admin", "demo"])).all()
        usernames = {user.username for user in users}
    finally:
        db.close()

    assert created_count == 0
    assert usernames == {"admin", "demo"}
