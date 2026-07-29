from sqlalchemy import (
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
from sqlalchemy.sql import func

from ..database import Base


class ImportBatch(Base):
    __tablename__ = "import_batches"

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
    broker = Column(String(100), nullable=False, index=True)
    source_type = Column(String(50), nullable=False, index=True)
    source_filename = Column(String(255), nullable=True)
    source_sha256 = Column(String(64), nullable=True, index=True)
    period_start = Column(Date, nullable=True)
    period_end = Column(Date, nullable=True)
    status = Column(
        String(20), nullable=False, default="PENDING", server_default="PENDING", index=True
    )
    row_count = Column(Integer, nullable=False, default=0, server_default="0")
    archived_count = Column(Integer, nullable=False, default=0, server_default="0")
    imported_count = Column(Integer, nullable=False, default=0, server_default="0")
    duplicate_count = Column(Integer, nullable=False, default=0, server_default="0")
    skipped_count = Column(Integer, nullable=False, default=0, server_default="0")
    error_count = Column(Integer, nullable=False, default=0, server_default="0")
    parser_name = Column(String(100), nullable=True)
    parser_version = Column(String(50), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    broker_account = relationship("BrokerAccount", back_populates="import_batches")
    transactions = relationship(
        "Transaction",
        back_populates="import_batch",
        passive_deletes=True,
    )
    reconciliation_snapshots = relationship(
        "ReconciliationSnapshot",
        back_populates="import_batch",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'COMPLETED', 'PARTIAL', 'FAILED')",
            name="ck_import_batches_status",
        ),
    )
