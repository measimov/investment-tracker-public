from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func, text

from ..database import Base


class ReconciliationSnapshot(Base):
    __tablename__ = "reconciliation_snapshots"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    broker_account_id = Column(
        Integer,
        ForeignKey("broker_accounts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    import_batch_id = Column(
        Integer,
        ForeignKey("import_batches.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    snapshot_date = Column(Date, nullable=False, index=True)
    status = Column(
        String(20), nullable=False, default="PENDING", server_default="PENDING", index=True
    )
    source_filename = Column(String(255), nullable=True)
    statement_scope = Column(String(30), nullable=True)
    cash_balances = Column(JSON, nullable=False, default=dict, server_default=text("'{}'"))
    positions = Column(JSON, nullable=False, default=list, server_default=text("'[]'"))
    # 自动比对结果：diff 明细与比对时间（status 由比对写入 MATCHED/MISMATCHED）
    diff_detail = Column(JSON, nullable=True)
    compared_at = Column(DateTime(timezone=True), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    broker_account = relationship(
        "BrokerAccount",
        back_populates="reconciliation_snapshots",
    )
    import_batch = relationship(
        "ImportBatch",
        back_populates="reconciliation_snapshots",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'MATCHED', 'MISMATCHED')",
            name="ck_reconciliation_snapshots_status",
        ),
    )
