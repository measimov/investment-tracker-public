from sqlalchemy import Column, Integer, String, Numeric, DateTime, UniqueConstraint, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from ..database import Base


class Holding(Base):
    __tablename__ = "holdings"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 账户级持仓：同一标的在不同券商账户各持一行；NULL 表示"未指定账户"桶
    # （手工交易或按账户重放失败后的合并兜底行）。
    broker_account_id = Column(
        Integer,
        ForeignKey("broker_accounts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
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

    broker_account = relationship("BrokerAccount")

    __table_args__ = (
        # NULLS NOT DISTINCT：未指定账户桶每个 (user, symbol, market) 也只允许一行。
        UniqueConstraint(
            'user_id', 'broker_account_id', 'symbol', 'market',
            name='uix_user_account_symbol_market',
            postgresql_nulls_not_distinct=True,
        ),
    )
