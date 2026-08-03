from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from ..database import Base


class SecurityEvent(Base):
    """标的事件表（全局数据，与 SecurityPrice 同定位，不分用户）。

    来源：分红同步 job 顺带落库——财报披露计划（disclosure_date.pre_date）、
    分红预案/股东大会通过阶段的除权/派息日（dividend 非"实施"行）、限售解禁
    （share_float.float_date）。过期事件保留：历史事件是标的档案 LLM 分析的输入。
    """

    __tablename__ = "security_events"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True, comment="股票代码")
    market = Column(String(20), nullable=False, comment="市场")
    event_type = Column(String(30), nullable=False, comment="事件类型")
    event_date = Column(Date, nullable=False, index=True, comment="事件日期")
    source = Column(String(30), nullable=False, comment="数据来源接口")
    payload = Column(JSON, comment="类型特有参数（每股金额、解禁数量等）")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "symbol", "market", "event_type", "event_date",
            name="uq_security_events_identity",
        ),
        CheckConstraint(
            "event_type IN ('EARNINGS_DISCLOSURE', 'DIVIDEND_PLAN', 'SHARE_UNLOCK')",
            name="ck_security_events_type",
        ),
    )
