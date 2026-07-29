from sqlalchemy import Column, Integer, String, Numeric, Date, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from ..database import Base


class CorporateAction(Base):
    """
    公司行动记录表 - 用于记录股息、红股、配股等事件

    支持的公司行动类型:
    - CASH_DIVIDEND: 现金股息
    - STOCK_DIVIDEND: 红股/股票股息
    - RIGHTS_ISSUE: 配股
    - STOCK_SPLIT: 拆股
    - REVERSE_SPLIT: 合股
    - BONUS_ISSUE: 送股
    - SPIN_OFF: 拆分
    - MERGER: 合并
    """
    __tablename__ = "corporate_actions"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
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

    # 基本信息
    symbol = Column(String(20), nullable=False, index=True, comment="股票代码")
    name = Column(String(100), comment="资产名称")
    market = Column(String(20), nullable=False, index=True, comment="市场")

    # 公司行动类型
    action_type = Column(String(20), nullable=False, index=True, comment="行动类型")

    # 日期
    ex_date = Column(Date, nullable=False, index=True, comment="除权除息日")
    record_date = Column(Date, comment="登记日")
    payment_date = Column(Date, comment="支付日/到账日")

    # 现金股息相关（CASH_DIVIDEND）
    dividend_per_share = Column(Numeric(18, 8), comment="每股股息金额")
    total_dividend = Column(Numeric(18, 8), comment="股息总额")

    # 税务相关
    tax_withheld = Column(Numeric(18, 8), default=0, comment="预扣税金额")
    tax_rate = Column(Numeric(5, 4), comment="税率（如0.10表示10%）")
    net_dividend = Column(Numeric(18, 8), comment="税后净股息")

    # 股票股息/红股相关（STOCK_DIVIDEND, BONUS_ISSUE）
    shares_received = Column(Numeric(18, 8), comment="获得的股票数量")
    distribution_ratio = Column(String(20), comment="分配比例（如10:3表示每10股送3股）")

    # 配股相关（RIGHTS_ISSUE）
    subscription_price = Column(Numeric(18, 8), comment="认购价格")
    subscription_quantity = Column(Numeric(18, 8), comment="认购数量")
    subscription_amount = Column(Numeric(18, 8), comment="认购金额")

    # 拆股/合股相关（STOCK_SPLIT, REVERSE_SPLIT）
    split_ratio = Column(String(20), comment="拆股比例（如1:2表示1股拆2股）")
    new_shares = Column(Numeric(18, 8), comment="拆股后的股数")

    # 成本基础调整
    cost_basis_adjustment = Column(Numeric(18, 8), comment="成本基础调整金额")
    adjusted_quantity = Column(Numeric(18, 8), comment="调整后的持股数量")
    adjusted_cost_per_share = Column(Numeric(18, 8), comment="调整后的每股成本")

    # 其他
    currency = Column(String(10), default="CNY", comment="币种")
    notes = Column(Text, comment="备注")

    # 时间戳
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    broker_account = relationship("BrokerAccount")
    import_batch = relationship("ImportBatch")
