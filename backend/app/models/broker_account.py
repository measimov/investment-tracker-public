from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func, text

from ..database import Base


class BrokerAccount(Base):
    __tablename__ = "broker_accounts"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    broker = Column(String(100), nullable=False, index=True)
    account_name = Column(String(100), nullable=False)
    account_number_masked = Column(String(100), nullable=True)
    base_currency = Column(String(10), nullable=False, default="CNY", server_default="CNY")
    is_active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    transactions = relationship(
        "Transaction",
        back_populates="broker_account",
        passive_deletes=True,
    )
    import_batches = relationship(
        "ImportBatch",
        back_populates="broker_account",
        passive_deletes=True,
    )
    cash_events = relationship(
        "CashEvent",
        back_populates="broker_account",
        passive_deletes=True,
    )
    reconciliation_snapshots = relationship(
        "ReconciliationSnapshot",
        back_populates="broker_account",
        passive_deletes=True,
    )
