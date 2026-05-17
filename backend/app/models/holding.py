from sqlalchemy import Column, Integer, String, Numeric, DateTime, UniqueConstraint, ForeignKey
from sqlalchemy.sql import func
from ..database import Base


class Holding(Base):
    __tablename__ = "holdings"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    name = Column(String(100))
    market = Column(String(20), nullable=False, index=True)
    quantity = Column(Numeric(18, 8), nullable=False)
    avg_cost = Column(Numeric(18, 8), nullable=False)
    total_cost = Column(Numeric(18, 8), nullable=False)
    currency = Column(String(10), default="CNY")
    current_price = Column(Numeric(18, 8), nullable=True)  # 当前股价
    price_updated_at = Column(DateTime(timezone=True), nullable=True)  # 股价更新时间
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint('user_id', 'symbol', 'market', name='uix_user_symbol_market'),
    )
