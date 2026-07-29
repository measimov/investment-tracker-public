from sqlalchemy import Column, Date, DateTime, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.sql import func

from ..database import Base


class SecurityPrice(Base):
    __tablename__ = "security_prices"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    symbol = Column(String(50), nullable=False, index=True)
    market = Column(String(20), nullable=False, index=True)
    ts_code = Column(String(50), nullable=True, index=True)
    price_date = Column(Date, nullable=False, index=True)
    currency = Column(String(10), nullable=False, default="CNY")
    open_price = Column(Numeric(18, 8), nullable=True)
    high_price = Column(Numeric(18, 8), nullable=True)
    low_price = Column(Numeric(18, 8), nullable=True)
    close_price = Column(Numeric(18, 8), nullable=False)
    pre_close_price = Column(Numeric(18, 8), nullable=True)
    adj_factor = Column(Numeric(18, 8), nullable=True)
    adj_close_price = Column(Numeric(18, 8), nullable=True)
    source = Column(String(50), nullable=False, default="tushare")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("symbol", "market", "price_date", name="uix_security_price_symbol_market_date"),
    )
