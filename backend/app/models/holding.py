from decimal import Decimal

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
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
    # 成本未知的份额（期初建仓/转托管转入，#174）：这部分按 0 成本并入 avg_cost，
    # 持仓成本与已实现盈亏对它们是估计值；>0 时前端打「成本未知」标签、转仓被拒
    unknown_cost_quantity = Column(
        Numeric(18, 8), nullable=False, default=Decimal("0"), server_default="0"
    )
    currency = Column(String(10), default="CNY")
    current_price = Column(Numeric(18, 8), nullable=True)  # 当前股价
    price_updated_at = Column(DateTime(timezone=True), nullable=True)  # 股价写库时刻
    # 行情所属交易日（#217）：报价源给出的日期（Tushare trade_date、腾讯/雪球行情时间），
    # 与 price_updated_at（写库时刻）不同——周六刷新到的是周五收盘。拿不到/手工价为 NULL。
    price_as_of = Column(Date, nullable=True)
    # 报价来源：tencent-quote / tushare-daily / xueqiu-quote … / manual（手工改价）；
    # 存量行为 NULL（来源未知）。前端据此给手工价打「手工」标。
    price_source = Column(String(40), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    broker_account = relationship("BrokerAccount")

    __table_args__ = (
        # NULLS NOT DISTINCT：未指定账户桶每个 (user, symbol, market) 也只允许一行。
        UniqueConstraint(
            "user_id",
            "broker_account_id",
            "symbol",
            "market",
            name="uix_user_account_symbol_market",
            postgresql_nulls_not_distinct=True,
        ),
    )
