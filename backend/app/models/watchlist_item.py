from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from ..database import Base


class WatchlistItem(Base):
    """观察清单条目（用户域）：纳入观察但未持仓的标的。

    标的档案/分析/事件都是全局表，观察标的的详情页能力零成本复用；本表只
    承载"谁在观察什么 + 为什么 + 当前价快照"。正式的买入/卖出/观察论点
    记录在 security_theses（PR-D），note 只是列表页一眼可见的一句话理由。

    current_price/price_updated_at 与 Holding 同模式：一键刷新价格的目标集
    为 持仓 ∪ 观察（PR-C），价格写回各自行——security_prices 只存日线收盘，
    盘中快照没有别的家。
    """

    __tablename__ = "watchlist_items"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    symbol = Column(String(20), nullable=False, index=True, comment="股票代码")
    market = Column(String(20), nullable=False, comment="市场")
    name = Column(String(100), comment="资产名称（可选，便于列表辨认）")
    note = Column(String(500), comment="观察理由一句话（正式论点见 security_theses）")

    current_price = Column(Numeric(20, 8), comment="最近刷新价（与 Holding 同语义）")
    price_updated_at = Column(DateTime(timezone=True), comment="价格刷新时间")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "symbol", "market", name="uq_watchlist_user_symbol"),
    )
