"""系统告警状态（`services/alert_service.py` 的状态机落库）。

一行 = 一个告警键（如 `xueqiu:cookie`、`periodic:refresh_rates_if_stale`、`backup`）的
**当前**状态：active / resolved。重新触发时复用同一行（first_seen_at 重置为新一次
事件的开始），因此表的大小只随告警键的种类增长，不随时间增长。全局表，不属于任何用户。
"""

from sqlalchemy import CheckConstraint, Column, DateTime, Index, Integer, String, Text
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from ..database import Base


class AlertState(Base):
    __tablename__ = "alert_states"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('info', 'warning', 'critical')", name="ck_alert_states_severity"
        ),
        CheckConstraint("status IN ('active', 'resolved')", name="ck_alert_states_status"),
        Index("ix_alert_states_status_last_seen", "status", "last_seen_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    alert_key = Column(String(200), nullable=False, unique=True, comment="告警身份键")
    source = Column(
        String(50),
        nullable=False,
        comment="产生它的检查器；某检查器本轮未再触发的活动告警才会被判恢复",
    )
    severity = Column(String(20), nullable=False, comment="info / warning / critical")
    status = Column(
        String(20), nullable=False, server_default=sa_text("'active'"), comment="active / resolved"
    )
    title = Column(Text, nullable=False)
    message = Column(Text, nullable=False, server_default=sa_text("''"))
    first_seen_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        comment="本次事件首次触发时间（恢复后再触发会重置）",
    )
    last_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_notified_at = Column(DateTime(timezone=True), comment="最近一次推送成功的时间")
    notify_count = Column(
        Integer, nullable=False, server_default=sa_text("0"), comment="本次事件推送成功的次数"
    )
    notified_severity = Column(
        String(20),
        comment="最近一次推送成功时的严重度：升级推送失败时据此在下一轮重试（不等提醒间隔）",
    )
    resolved_at = Column(DateTime(timezone=True))
    resolve_notified_at = Column(
        DateTime(timezone=True),
        comment="「已恢复」推送成功的时间；推送过的告警恢复后为空 = 待重试的恢复通知",
    )
    payload = Column(
        JSONB, nullable=False, server_default=sa_text("'{}'::jsonb"),
        comment="检查器给出的明细 + 最近一次推送结果",
    )
