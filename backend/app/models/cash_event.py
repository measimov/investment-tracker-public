from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..database import Base


class CashEvent(Base):
    __tablename__ = "cash_events"

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
    event_type = Column(String(20), nullable=False, index=True)
    amount = Column(Numeric(24, 8), nullable=False)
    currency = Column(
        String(10), nullable=False, default="CNY", server_default="CNY", index=True
    )
    event_date = Column(Date, nullable=False, index=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    broker_account = relationship("BrokerAccount", back_populates="cash_events")

    __table_args__ = (
        CheckConstraint(
            "event_type IN ("
            "'DEPOSIT', 'WITHDRAWAL', 'INTEREST', 'FEE', 'TAX', "
            "'TRANSFER_IN', 'TRANSFER_OUT', 'FX_IN', 'FX_OUT', 'OTHER'"
            ")",
            name="ck_cash_events_type",
        ),
        CheckConstraint("amount > 0", name="ck_cash_events_positive_amount"),
    )
