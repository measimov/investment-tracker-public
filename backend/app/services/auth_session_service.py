"""Server-side session management backing JWT revocation (issue #36)."""

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from uuid import uuid4

from sqlalchemy.orm import Session

from ..core.security import ACCESS_TOKEN_EXPIRE_MINUTES, create_access_token
from ..models.auth_session import AuthSession
from ..models.user import User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def issue_session(
    db: Session,
    user: User,
    expires_delta: Optional[timedelta] = None,
) -> Tuple[str, str]:
    """Create a session row and its JWT. Returns (token, jti)."""
    lifetime = expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    jti = uuid4().hex
    now = _utcnow()
    db.add(
        AuthSession(
            id=jti,
            user_id=user.id,
            created_at=now,
            expires_at=now + lifetime,
        )
    )
    db.commit()
    token = create_access_token(
        data={"sub": user.username, "jti": jti},
        expires_delta=lifetime,
    )
    return token, jti


def renew_session(
    db: Session,
    user: User,
    jti: str,
    expires_delta: Optional[timedelta] = None,
) -> Optional[str]:
    """滑动续期：延长现有会话行并按同一 jti 重签 JWT。

    刻意不新建会话：整条续期链共享一个 jti，登出吊销该 jti 即同时终止
    链上所有 JWT（含已泄露的旧令牌）；旧 JWT 自带的 exp 不变，在途请求
    也不会因轮换被误判 401。会话无效/已过期时返回 None。
    """
    lifetime = expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    now = _utcnow()
    session = (
        db.query(AuthSession)
        .filter(
            AuthSession.id == jti,
            AuthSession.user_id == user.id,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
        )
        .first()
    )
    if session is None:
        return None
    session.expires_at = now + lifetime
    db.commit()
    return create_access_token(
        data={"sub": user.username, "jti": jti},
        expires_delta=lifetime,
    )


def is_session_valid(db: Session, jti: str, user_id: int) -> bool:
    session = db.query(AuthSession).filter(AuthSession.id == jti).first()
    if session is None or session.user_id != user_id:
        return False
    if session.revoked_at is not None:
        return False
    return session.expires_at > _utcnow()


def revoke_session(db: Session, jti: str) -> bool:
    """Revoke a single session (browser logout). Returns True if newly revoked."""
    updated = (
        db.query(AuthSession)
        .filter(AuthSession.id == jti, AuthSession.revoked_at.is_(None))
        .update({AuthSession.revoked_at: _utcnow()}, synchronize_session=False)
    )
    db.commit()
    return updated > 0


def revoke_user_sessions(db: Session, user_id: int) -> int:
    """Revoke every live session of a user (password change, admin action)."""
    revoked = (
        db.query(AuthSession)
        .filter(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .update({AuthSession.revoked_at: _utcnow()}, synchronize_session=False)
    )
    db.commit()
    return revoked


def cleanup_expired_sessions(db: Session, user_id: Optional[int] = None) -> int:
    """Delete sessions past expiry; scoped per user on login to bound growth."""
    query = db.query(AuthSession).filter(AuthSession.expires_at < _utcnow())
    if user_id is not None:
        query = query.filter(AuthSession.user_id == user_id)
    deleted = query.delete(synchronize_session=False)
    db.commit()
    return deleted
