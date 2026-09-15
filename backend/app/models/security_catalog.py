from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from ..database import Base

SECURITY_TYPES = ("stock", "etf", "fund", "reit", "adr", "pref", "gdr", "unknown")
LIST_STATUSES = ("listed", "delisted", "unknown")


class SecurityCatalogEntry(Base):
    """标的全集（全局参考数据集，按 (symbol, market) 一行）。

    只提供名称 / 拼音 / 类型 / 上市状态等**参考信息**，账本（交易/持仓/自选）
    仍是用户自己那几行的权威：搜索时账本行排前，目录只补账本没有的字段。
    永不删行——退市只翻 list_status；某来源一次拉取失败不影响已有行。
    """

    __tablename__ = "security_catalog"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    symbol = Column(
        String(20), nullable=False, comment="账本口径代码：大写，港股 5 位（同 normalize_manual_symbol）"
    )
    market = Column(String(20), nullable=False, comment="市场：A股/B股/港股/美股")
    name = Column(String(200), nullable=True, comment="简体中文名")
    name_en = Column(String(200), nullable=True, comment="英文名")
    name_trad = Column(String(200), nullable=True, comment="繁体名（港交所證券名單）")
    pinyin = Column(String(50), nullable=True, comment="拼音首字母缩写（大写）")
    currency = Column(String(10), nullable=True, comment="交易币种")
    currency_source = Column(
        String(20), nullable=True, comment="币种来源：tushare/inferred/hkex-dayquot/tencent-quote"
    )
    security_type = Column(
        String(20),
        nullable=False,
        default="unknown",
        server_default="unknown",
        comment="stock/etf/fund/reit/adr/pref/gdr/unknown",
    )
    board = Column(String(30), nullable=True, comment="板块：主板/创业板/科创板/北交所/…")
    exchange = Column(String(20), nullable=True, comment="交易所：SSE/SZSE/BSE/HKEX/US")
    list_status = Column(
        String(20),
        nullable=False,
        default="listed",
        server_default="listed",
        comment="listed/delisted/unknown",
    )
    list_date = Column(Date, nullable=True)
    delist_date = Column(Date, nullable=True)
    source = Column(String(40), nullable=False, comment="最后写入该行的来源（loader 名）")
    detail = Column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
        comment="来源特有字段：fund_type / 次分類 / classify / isin …",
    )
    first_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    synced_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("symbol", "market", name="uq_security_catalog_symbol_market"),
        CheckConstraint(
            "security_type IN ('stock','etf','fund','reit','adr','pref','gdr','unknown')",
            name="ck_security_catalog_security_type",
        ),
        CheckConstraint(
            "list_status IN ('listed','delisted','unknown')",
            name="ck_security_catalog_list_status",
        ),
        Index("ix_security_catalog_market", "market"),
        # 默认 collation 的 B-tree 吃不到 LIKE 'q%'；pattern_ops 让代码/拼音前缀走索引
        Index(
            "ix_security_catalog_symbol_pattern",
            "symbol",
            postgresql_ops={"symbol": "varchar_pattern_ops"},
        ),
        Index(
            "ix_security_catalog_pinyin_pattern",
            "pinyin",
            postgresql_ops={"pinyin": "varchar_pattern_ops"},
        ),
    )


class SecurityCatalogSync(Base):
    """每个目录来源一行的同步状态——显式降级的载体：某源失败时 status=failed、
    error 有值、last_success_at 不动，前端据此提示"目录未就绪/部分来源失败"。"""

    __tablename__ = "security_catalog_syncs"

    source = Column(String(40), primary_key=True, comment="loader 名，如 tushare-stock_basic")
    markets = Column(
        JSONB, nullable=False, default=list, server_default=text("'[]'"), comment="该源覆盖的市场"
    )
    status = Column(String(20), nullable=False, comment="ok/failed/skipped/running")
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    last_success_at = Column(
        DateTime(timezone=True), nullable=True, comment="最近一次成功时间（失败不刷新）"
    )
    rows_seen = Column(Integer, nullable=False, default=0, server_default="0")
    rows_upserted = Column(Integer, nullable=False, default=0, server_default="0")
    error = Column(Text, nullable=True)
    detail = Column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'"), comment="跳过原因等"
    )
