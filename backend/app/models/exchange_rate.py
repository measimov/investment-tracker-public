from sqlalchemy import Column, Integer, String, Numeric, Date, DateTime, Boolean, UniqueConstraint
from sqlalchemy.sql import func
from ..database import Base


class ExchangeRate(Base):
    __tablename__ = "exchange_rates"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    from_currency = Column(String(10), nullable=False, index=True)  # 源币种
    to_currency = Column(String(10), nullable=False, index=True)  # 目标币种
    rate = Column(Numeric(18, 8), nullable=False)  # 汇率
    effective_date = Column(Date, nullable=False, index=True)  # 生效日期
    source = Column(String(50), default='manual')  # 来源：manual/api/system
    is_active = Column(Boolean, default=True)  # 是否启用
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint('from_currency', 'to_currency', 'effective_date', name='uix_currency_date'),
    )
