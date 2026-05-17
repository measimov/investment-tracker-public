from sqlalchemy import Column, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.sql import func

from ..database import Base


class BrokerFundFlow(Base):
    __tablename__ = "broker_fund_flows"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    transaction_id = Column(Integer, ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True, index=True)
    broker = Column(String(50), nullable=False, default="招商证券", index=True)
    row_hash = Column(String(64), nullable=False, index=True)
    source_filename = Column(String(255), nullable=True)
    source_row_number = Column(Integer, nullable=True)

    security_code = Column(String(20), nullable=True, index=True)
    security_name = Column(String(100), nullable=True)
    currency = Column(String(10), nullable=False, default="CNY")
    trade_date = Column(Date, nullable=False, index=True)
    trade_price = Column(Numeric(18, 8), nullable=False, default=0)
    trade_quantity = Column(Numeric(18, 8), nullable=False, default=0)
    amount = Column(Numeric(18, 8), nullable=False, default=0)
    cash_balance = Column(Numeric(18, 8), nullable=True)
    remaining_quantity = Column(Numeric(18, 8), nullable=True)
    contract_number = Column(String(50), nullable=True)
    serial_number = Column(String(50), nullable=True, index=True)
    business_name = Column(String(50), nullable=False, index=True)

    stamp_tax = Column(Numeric(18, 8), nullable=False, default=0)
    commission = Column(Numeric(18, 8), nullable=False, default=0)
    handling_fee = Column(Numeric(18, 8), nullable=False, default=0)
    management_fee = Column(Numeric(18, 8), nullable=False, default=0)
    settlement_fee = Column(Numeric(18, 8), nullable=False, default=0)
    transfer_fee = Column(Numeric(18, 8), nullable=False, default=0)
    other_fee = Column(Numeric(18, 8), nullable=False, default=0)
    shareholder_code = Column(String(50), nullable=True)
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "row_hash", name="uix_broker_flow_user_hash"),
    )
