from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from ..database import Base


class CorporateActionSuggestion(Base):
    """分红公告建议表（Tushare dividend 同步产物，绝不自动入账）。

    与 corporate_actions 物理隔离：账本表被持仓重算 / FIFO / TTWR 三处重放
    无差别读取，建议的生命周期噪音（NEW/IGNORED/公告修订刷新）不应进入账本。
    用户点"接受"时才通过既有创建路径写入正式 CorporateAction。

    **每账户一条建议**：登记日权益按账户桶拆分，broker_account_id 即归属
    （NULL = 合并口径降级，归属不可拆）。接受时天然单账户入账，多账户
    权益的金额之和等于总权益，不会被错误归到单一账户。

    幂等键 (user, symbol, market, action_type, ex_date, broker_account_id)
    （NULLS NOT DISTINCT，同 holdings）：重同步 upsert——ACCEPTED/IGNORED
    不动，NEW/MATCHED 按公告修订值刷新。
    """

    __tablename__ = "corporate_action_suggestions"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    broker_account_id = Column(
        Integer,
        ForeignKey("broker_accounts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="登记日权益归属账户；NULL=合并口径（归属不可拆）",
    )

    symbol = Column(String(20), nullable=False, index=True, comment="股票代码")
    name = Column(String(100), comment="资产名称")
    market = Column(String(20), nullable=False, comment="市场")
    action_type = Column(
        String(20), nullable=False, comment="CASH_DIVIDEND / STOCK_DIVIDEND"
    )

    ann_date = Column(Date, comment="公告日")
    record_date = Column(Date, comment="股权登记日")
    ex_date = Column(Date, nullable=False, comment="除权除息日")
    pay_date = Column(Date, comment="派息日")
    currency = Column(String(10), nullable=False, default="CNY", server_default="CNY")

    cash_div_pre_tax = Column(Numeric(18, 8), comment="每股税前派息（cash_div_tax）")
    cash_div_after_tax = Column(Numeric(18, 8), comment="每股税后派息（cash_div）")
    stk_div_per_share = Column(Numeric(18, 8), comment="每股送转合计（stk_div）")

    record_date_quantity = Column(Numeric(18, 8), comment="登记日推算持仓；NULL=推算失败")
    quantity_basis = Column(
        String(20), comment="per_account / merged / unavailable"
    )
    estimated_total_dividend = Column(Numeric(18, 8), comment="税前推算总额")

    status = Column(
        String(20), nullable=False, default="NEW", server_default="NEW", index=True
    )
    matched_corporate_action_id = Column(
        Integer,
        ForeignKey("corporate_actions.id", ondelete="SET NULL"),
        nullable=True,
        comment="判重命中的既有账本记录",
    )
    created_corporate_action_id = Column(
        Integer,
        ForeignKey("corporate_actions.id", ondelete="SET NULL"),
        nullable=True,
        comment="接受后创建的账本记录",
    )
    match_detail = Column(JSON, comment="判重明细：matched_by / date_gap_days / amount_diff")
    source = Column(
        String(30), nullable=False, default="tushare-dividend",
        server_default="tushare-dividend",
    )

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "symbol", "market", "action_type", "ex_date", "broker_account_id",
            name="uq_ca_suggestions_identity",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "action_type IN ('CASH_DIVIDEND', 'STOCK_DIVIDEND')",
            name="ck_ca_suggestions_action_type",
        ),
        CheckConstraint(
            "status IN ('NEW', 'MATCHED', 'ACCEPTED', 'IGNORED')",
            name="ck_ca_suggestions_status",
        ),
    )
