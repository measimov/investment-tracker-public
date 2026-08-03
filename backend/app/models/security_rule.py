from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from ..database import Base

RULE_TYPES = (
    "EXCLUDE",
    "CASH_MANAGEMENT",
    "RELISTING",
    "NAME_OVERRIDE",
    "PRICE_GAP_EXEMPTION",
    "CMB_CASH_BUSINESS",
)


class SecurityRule(Base):
    """账本特例规则（issue #82）：手工维护、表驱动，取代散落的硬编码常量。

    单表 + rule_type 判别。symbol 列对 CMB_CASH_BUSINESS 类型存放对账单
    业务名（如"银行转存"）——单表经济性的代价，其余类型均为证券代码。
    类型特有参数放 payload：
    - RELISTING: {new_symbol, new_market, new_currency, old_currency, name}
    - NAME_OVERRIDE: {name}
    - PRICE_GAP_EXEMPTION: {start_date, end_date|null}（null=摘牌后开放至今）
    - CMB_CASH_BUSINESS: {event_type}
    - EXCLUDE / CASH_MANAGEMENT: null

    注意：EXCLUDE（只归档不入账）与 CASH_MANAGEMENT（派息按利息入账）是
    互斥政策——同一标的同时录两条时排除优先生效。
    """

    __tablename__ = "security_rules"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rule_type = Column(String(30), nullable=False, index=True)
    symbol = Column(String(50), nullable=False)
    market = Column(String(20), nullable=True)
    payload = Column(JSON, nullable=True)
    note = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "rule_type IN ('EXCLUDE', 'CASH_MANAGEMENT', 'RELISTING', "
            "'NAME_OVERRIDE', 'PRICE_GAP_EXEMPTION', 'CMB_CASH_BUSINESS')",
            name="ck_security_rules_rule_type",
        ),
        # CMB 业务映射必须 market IS NULL：唯一键含 market，放行非空市场
        # 会让同一业务名多条规则并存，读取端折字典时事件类型不确定
        CheckConstraint(
            "(rule_type != 'CMB_CASH_BUSINESS') OR (market IS NULL)",
            name="ck_security_rules_cmb_market_null",
        ),
        UniqueConstraint(
            "user_id",
            "rule_type",
            "symbol",
            "market",
            name="uq_security_rules_key",
            postgresql_nulls_not_distinct=True,
        ),
    )
