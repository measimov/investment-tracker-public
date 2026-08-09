"""Server-side session management backing JWT revocation (issue #36)."""

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from uuid import uuid4

from sqlalchemy.orm import Session

from ..config import settings
from ..core.security import ACCESS_TOKEN_EXPIRE_MINUTES, create_access_token
from ..models.auth_session import AuthSession
from ..models.user import User


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _absolute_deadline(session: AuthSession) -> Optional[datetime]:
    """会话自首次登录起的绝对截止点；created_at 缺失时返回 None。

    created_at 由迁移基线建表时就带上，但历史行可能为 NULL（不同来源的补写），
    读不到就当作没有截止点——宁可放行也不要把在用会话一次性全踢下线。
    列无时区、存的是 UTC，比较前补上 tzinfo。
    """
    created_at = getattr(session, "created_at", None)
    if created_at is None:
        return None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at + timedelta(hours=settings.session_absolute_max_hours)


def _clamped_expiry(deadline: Optional[datetime], now: datetime, lifetime: timedelta) -> datetime:
    """有效期截止时刻：绝对截止点之内。

    只在续期入口比一次"现在有没有到顶"是不够的：**截止前一瞬**刷新仍会把
    expires_at 与 JWT 的 exp 一起推到 now+lifetime，于是会话实际比绝对上限
    多活一个 lifetime（默认 30 分钟，配得更长就越界更久）。单次 lifetime 本身
    大于绝对上限时，签发那一下就已经越界。所以两个入口都按截止点钳一次。
    """
    expires_at = now + lifetime
    if deadline is not None and deadline < expires_at:
        expires_at = deadline
    return expires_at


def issue_session(
    db: Session,
    user: User,
    expires_delta: Optional[timedelta] = None,
) -> Tuple[str, str]:
    """Create a session row and its JWT. Returns (token, jti)."""
    lifetime = expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    jti = uuid4().hex
    now = _utcnow()
    # 单次 lifetime 大于绝对上限时，不钳的话第一张令牌就已经越界。
    deadline = now + timedelta(hours=settings.session_absolute_max_hours)
    expires_at = _clamped_expiry(deadline, now, lifetime)
    db.add(
        AuthSession(
            id=jti,
            user_id=user.id,
            created_at=now,
            expires_at=expires_at,
        )
    )
    db.commit()
    # 传绝对时刻而非 delta：helper 若在更晚的 t1 重新取 utcnow() 再加 delta，
    # JWT 的 exp 会变成 deadline + (t1 − t0)，钳位等于没做（中间还隔着 commit）。
    token = create_access_token(
        data={"sub": user.username, "jti": jti},
        expires_at=expires_at,
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
    deadline = _absolute_deadline(session)
    if deadline is not None and deadline <= now:
        # 绝对生命周期到顶：只靠"每次续期都往后推 30 分钟"的话，被窃取的
        # cookie 只要按时打一次 /refresh 就能永久续命——续期链再长也只有
        # 一个 jti，用户端不会有任何察觉。到顶即吊销，强制重新登录。
        session.revoked_at = now
        db.commit()
        return None
    # 钳到截止点：截止前一瞬刷新也不能把会话推到上限之外（见 _clamped_expiry）。
    expires_at = _clamped_expiry(deadline, now, lifetime)
    session.expires_at = expires_at
    db.commit()
    return create_access_token(
        data={"sub": user.username, "jti": jti},
        expires_at=expires_at,
    )


def is_session_valid(db: Session, jti: str, user_id: int) -> bool:
    session = db.query(AuthSession).filter(AuthSession.id == jti).first()
    if session is None or session.user_id != user_id:
        return False
    if session.revoked_at is not None:
        return False
    now = _utcnow()
    # 绝对截止点在这里兜底：钳位负责不越界，这一条负责"即使某条路径漏钳、
    # 或上限被调小，已越界的会话也立刻失效"。普通请求走的就是这里。
    deadline = _absolute_deadline(session)
    if deadline is not None and deadline <= now:
        return False
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > now


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
