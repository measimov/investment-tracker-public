from sqlalchemy import Column, Date, DateTime, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.sql import func

from ..database import Base


class ReferenceRate(Base):
    """参考利率日序列（#200，全局表）：无风险利率等，年化百分比。

    series：`SHIBOR_3M`（中国货币网，本币风险指标用）/ `UST_3M`（美国财政部 13 周国库券，
    只展示）。只存发布日；非发布日由读取方按前一个发布日向前填充。
    """

    __tablename__ = "reference_rates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    series = Column(String(32), nullable=False)
    rate_date = Column(Date, nullable=False)
    value = Column(Numeric(12, 6), nullable=False)  # 年化 %
    source = Column(String(50), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("series", "rate_date", name="uq_reference_rates_series_date"),
    )
