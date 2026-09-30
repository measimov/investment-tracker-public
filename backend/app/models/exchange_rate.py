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
    source = Column(
        String(50), default="manual"
    )  # 来源：manual / cfets-ccpr（官方中间价）/ api-ecb / api-backup
    is_active = Column(Boolean, default=True)  # 是否启用
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "from_currency", "to_currency", "effective_date", name="uix_currency_date"
        ),
    )


class ExchangeRateCheck(Base):
    """官方中间价与第三方报价的逐日比对（#200）。

    官方源成功时第三方报价**不入 exchange_rates**，只在这里留一行差异，供汇率页与
    数据质量告警使用；一个币对每个比对日一行（重复刷新覆盖）。
    """

    __tablename__ = "exchange_rate_checks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    from_currency = Column(String(10), nullable=False)
    to_currency = Column(String(10), nullable=False)
    check_date = Column(Date, nullable=False)  # 第三方报价的抓取日（业务时区）
    official_date = Column(Date, nullable=False)  # 参与比对的官方中间价日期（最近发布日）
    official_source = Column(String(50), nullable=False)
    official_rate = Column(Numeric(18, 8), nullable=False)
    reference_source = Column(String(50), nullable=False)
    reference_rate = Column(Numeric(18, 8), nullable=False)
    diff_pct = Column(Numeric(10, 4), nullable=False)  # (第三方 / 官方 − 1) × 100
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "from_currency", "to_currency", "check_date", name="uq_exchange_rate_checks_pair_date"
        ),
    )
