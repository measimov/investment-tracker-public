from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from ..database import Base

INDUSTRY_SOURCES = ("tushare", "edgar", "eastmoney")


class SecurityIndustry(Base):
    """标的行业分类（全局参考数据，按 (symbol, market, source) 一行）。

    每个来源各存一行，读取时按优先级合成：用户规则 INDUSTRY（security_rules，
    用户域）> 官方（A股 Tushare stock_basic / 美股 EDGAR SIC）> 东方财富 F10
    （非官方，补缺：港股、B股、官方缺失的 A股/美股）。
    只存取到的行业——取不到/请求失败不写空行（显式缺口由同步结果报告）。
    """

    __tablename__ = "security_industries"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, comment="账本口径代码（同 holdings.symbol）")
    market = Column(String(20), nullable=False, comment="市场：A股/B股/港股/美股")
    source = Column(String(20), nullable=False, comment="tushare / edgar / eastmoney")
    industry = Column(String(100), nullable=False, comment="中文行业名")
    raw = Column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
        comment="来源原始字段：SIC 码与英文描述 / EM2016 三级分类 / 证监会分类 …",
    )
    fetched_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), comment="最近拉取时间"
    )

    # 唯一键 (symbol, market, source) 的前缀即覆盖按 (symbol, market) 的查询，不另建索引
    __table_args__ = (
        UniqueConstraint("symbol", "market", "source", name="uq_security_industries_key"),
        CheckConstraint(
            "source IN ('tushare', 'edgar', 'eastmoney')",
            name="ck_security_industries_source",
        ),
    )
