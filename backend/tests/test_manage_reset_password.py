"""manage.py reset-password：按 API 同一口令规则改密并吊销该用户全部会话（#277）。"""

from uuid import uuid4

import manage
from app.core.security import get_password_hash, verify_password
from app.database import SessionLocal
from app.models.auth_session import AuthSession
from app.models.user import User
from app.services.auth_session_service import issue_session


def _make_user(db):
    user = User(
        username=f"reset_{uuid4().hex[:8]}",
        email=f"reset_{uuid4().hex[:8]}@example.com",
        hashed_password=get_password_hash("old-password-123"),
        is_active=True,
    )
    db.add(user)
    db.commit()
    return user


def test_reset_password_sets_hash_and_revokes_sessions(monkeypatch):
    db = SessionLocal()
    try:
        user = _make_user(db)
        issue_session(db, user)
        monkeypatch.setenv("RESET_PW_TEST", "brand-new-password-456")

        assert manage.reset_password(user.username, "RESET_PW_TEST") == 0

        db.expire_all()
        refreshed = db.get(User, user.id)
        assert verify_password("brand-new-password-456", refreshed.hashed_password)
        live = (
            db.query(AuthSession)
            .filter(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
            .count()
        )
        assert live == 0
    finally:
        db.query(User).filter(User.id == user.id).delete()
        db.commit()
        db.close()


def test_reset_password_rejects_weak_and_unknown(monkeypatch):
    db = SessionLocal()
    try:
        user = _make_user(db)
        old_hash = user.hashed_password

        monkeypatch.setenv("RESET_PW_TEST", "short")
        assert manage.reset_password(user.username, "RESET_PW_TEST") == 1
        monkeypatch.setenv("RESET_PW_TEST", "汉" * 25)  # 75 字节，超过 bcrypt 上限
        assert manage.reset_password(user.username, "RESET_PW_TEST") == 1
        monkeypatch.delenv("RESET_PW_TEST")
        assert manage.reset_password(user.username, "RESET_PW_TEST") == 1

        db.expire_all()
        assert db.get(User, user.id).hashed_password == old_hash

        monkeypatch.setenv("RESET_PW_TEST", "brand-new-password-456")
        assert manage.reset_password("no-such-user-xyz", "RESET_PW_TEST") == 1
    finally:
        db.query(User).filter(User.id == user.id).delete()
        db.commit()
        db.close()
