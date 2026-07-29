from sqlalchemy import Column, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.sql import func

from ..database import Base


class IbkrActivityFlow(Base):
    __tablename__ = "ibkr_activity_flows"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    transaction_id = Column(Integer, ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True, index=True)
    corporate_action_id = Column(Integer, ForeignKey("corporate_actions.id", ondelete="SET NULL"), nullable=True, index=True)
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

    broker = Column(String(50), nullable=False, default="IBKR", index=True)
    row_hash = Column(String(64), nullable=False, index=True)
    source_filename = Column(String(255), nullable=True)
    source_row_number = Column(Integer, nullable=False)

    account = Column(String(50), nullable=True)
    trade_date = Column(Date, nullable=False, index=True)
    description = Column(Text, nullable=True)
    activity_type = Column(String(50), nullable=False, index=True)
    raw_symbol = Column(String(50), nullable=True, index=True)
    symbol = Column(String(50), nullable=True, index=True)
    name = Column(String(255), nullable=True)
    market = Column(String(20), nullable=True, index=True)

    quantity = Column(Numeric(18, 8), nullable=True)
    price = Column(Numeric(18, 8), nullable=True)
    price_currency = Column(String(10), nullable=True)
    base_currency = Column(String(10), nullable=False, default="USD")
    gross_amount = Column(Numeric(24, 10), nullable=True)
    commission = Column(Numeric(24, 10), nullable=True)
    net_amount = Column(Numeric(24, 10), nullable=True)
    fee_in_price_currency = Column(Numeric(18, 8), nullable=True)

    skip_reason = Column(String(100), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "row_hash", name="uix_ibkr_activity_user_hash"),
    )
