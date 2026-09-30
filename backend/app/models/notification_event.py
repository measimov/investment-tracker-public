"""一次性事件提醒（`services/event_notifications.py`）：新分红建议、除净日临近、价格异动。

与 `alert_states` 的区别：告警是「状态」（持续未恢复会定期再提醒、恢复时发「已恢复」），
事件是「发生过一次」——一个事件键只推送一次。先写本表去重再发送；发送失败保留
pending，下一轮重试（有上限）。
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from ..database import Base


class NotificationEvent(Base):
    __tablename__ = "notification_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'skipped')",
            name="ck_notification_events_status",
        ),
        Index("ix_notification_events_status_created", "status", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_key = Column(String(200), nullable=False, unique=True, comment="事件身份键（去重）")
    kind = Column(String(40), nullable=False, comment="dividend_suggestion / ex_date / price_move")
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, comment="所属用户"
    )
    title = Column(Text, nullable=False, server_default=sa_text("''"))
    message = Column(Text, nullable=False, server_default=sa_text("''"))
    payload = Column(JSONB, nullable=False, server_default=sa_text("'{}'::jsonb"))
    status = Column(
        String(20),
        nullable=False,
        server_default=sa_text("'pending'"),
        comment="pending 待发送 / sent / failed 重试用尽 / skipped 未配置渠道或已关闭",
    )
    attempts = Column(Integer, nullable=False, server_default=sa_text("0"))
    last_error = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    sent_at = Column(DateTime(timezone=True))
