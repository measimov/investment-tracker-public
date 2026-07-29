from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.sql import func

from ..database import Base


class AuthSession(Base):
    """Server-side record for an issued JWT, keyed by its jti claim.

    Enables real revocation (logout, password change, deactivation) instead of
    waiting for token expiry (issue #36).
    """

    __tablename__ = "auth_sessions"

    id = Column(String(32), primary_key=True)  # the JWT jti claim
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_auth_sessions_expires_at", "expires_at"),)
