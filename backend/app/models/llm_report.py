from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from ..database import Base


class LlmReport(Base):
    """LLM 投资复盘报告（产品目的③）。

    只在生成成功时落行：进行中/失败状态由 background_jobs 承担，
    列表永远不出现半成品。input_payload 保存生成时的压缩输入，
    追问对话复用它做上下文（不重算、可复现）。
    """

    __tablename__ = "llm_reports"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title = Column(String(120), nullable=False)
    content = Column(Text, nullable=False)
    model = Column(String(50), nullable=False)
    trigger_source = Column(String(20), nullable=False)
    input_payload = Column(JSON, nullable=False)
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "trigger_source IN ('manual', 'scheduled')",
            name="ck_llm_reports_trigger_source",
        ),
        Index("ix_llm_reports_user_created", "user_id", "created_at"),
    )


class LlmReportMessage(Base):
    """报告追问对话消息（user/assistant 成对落库，仅在 LLM 成功后写入）。"""

    __tablename__ = "llm_report_messages"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    report_id = Column(
        Integer,
        ForeignKey("llm_reports.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = Column(String(10), nullable=False)
    content = Column(Text, nullable=False)
    total_tokens = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="ck_llm_report_messages_role"),
        Index("ix_llm_report_messages_report", "report_id", "id"),
    )


class LlmReportSchedule(Base):
    """每用户的定期生成节奏配置（off / weekly / monthly）。"""

    __tablename__ = "llm_report_schedules"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cadence = Column(String(10), nullable=False, default="off", server_default="off")
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_llm_report_schedules_user"),
        CheckConstraint(
            "cadence IN ('off', 'weekly', 'monthly')",
            name="ck_llm_report_schedules_cadence",
        ),
    )
