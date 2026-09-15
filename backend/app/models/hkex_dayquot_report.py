from sqlalchemy import Column, Date, DateTime, Integer
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from ..database import Base


class HkexDayquotReport(Base):
    """港交所《每日行情报表》处理完成标记（全局表，按报表日期一行）。

    "该日已处理" 不能用当天是否存在 hkex-dayquot 价格行来表示：跟踪集当天全部
    停牌 / 全部 N/A / 代码全部未匹配时，报表已成功下载解析但写入 0 行，下一 tick
    会再下同一份 25MB，且在 max_reports 预算内反复耗尽、饿死更早的日期。标记
    随报表写入同一事务落库，进程重启后仍有效。
    """

    __tablename__ = "hkex_dayquot_reports"

    report_date = Column(Date, primary_key=True, comment="报表日期（交易日）")
    processed_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    universe_size = Column(Integer, nullable=False, comment="处理时的跟踪标的数")
    parsed_count = Column(Integer, nullable=False, comment="报表解析出的证券行数")
    stored_count = Column(Integer, nullable=False, comment="写入 security_prices 的行数")
    detail = Column(
        JSONB, nullable=False, comment="missing / suspended / unpriced / conflicts 明细"
    )
