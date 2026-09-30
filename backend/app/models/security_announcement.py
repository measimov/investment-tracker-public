"""官方公告（全局表，与 SecurityPrice 同定位，不分用户）。

来源：巨潮（A/B 股，B 股按 orgId 对应的 A 股代码检索）/ 披露易全部类别（港股）/ EDGAR
submissions（美股）。一行一份公告文件；同一标的同一天同一类别的多份文件由 group_key
合并成一个「事件」（安琪酵母 2026-09-24 可转债预案 15 份文件 → 1 组）。分类是纯函数
`announcement_classifier`，改规则 bump `ANNOUNCEMENT_CLASSIFIER_VERSION` 后
`manage.py reclassify-announcements` 零外呼重算（#306）。
"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from ..database import Base


class SecurityAnnouncement(Base):
    __tablename__ = "security_announcements"
    __table_args__ = (
        # 唯一键带上标的（PR #309 评审 P2-2）：B 股按对应 A 股的 orgId 检索、EDGAR 同一 CIK 的
        # 多股别（GOOG/GOOGL）共用同一批公告 ID——只按 (source, source_id) 唯一时后同步的
        # 那只 ON CONFLICT 一行也写不进，读取方按 (symbol, market) 取数永远为空
        UniqueConstraint(
            "symbol",
            "market",
            "source",
            "source_id",
            name="uq_security_announcements_symbol_source_id",
        ),
        CheckConstraint(
            "importance IN ('major', 'normal', 'minor')",
            name="ck_security_announcements_importance",
        ),
        Index(
            "ix_security_announcements_symbol_published",
            "symbol",
            "market",
            sa_text("published_at DESC"),
        ),
        Index("ix_security_announcements_group_key", "group_key"),
        Index("ix_security_announcements_importance_date", "importance", "ann_date"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, comment="本仓标的代码")
    market = Column(String(20), nullable=False, comment="本仓市场（A股/B股/港股/美股）")
    source = Column(String(20), nullable=False, comment="cninfo / hkexnews / edgar")
    source_id = Column(
        String(64), nullable=False, comment="来源内唯一 ID（公告 ID/NEWS_ID/accession）"
    )
    published_at = Column(DateTime(timezone=True), nullable=False, comment="发布时间")
    ann_date = Column(Date, nullable=False, comment="公告日（业务时区）")
    title = Column(Text, nullable=False, comment="公告标题（交易所原文）")
    url = Column(Text, nullable=False, server_default=sa_text("''"), comment="原文链接")
    category_raw = Column(
        Text,
        nullable=False,
        server_default=sa_text("''"),
        comment="官方分类原文（巨潮 announcementType / 披露易 LONG_TEXT / EDGAR form|items）",
    )
    category = Column(
        String(30), nullable=False, comment="归类（见 announcement_classifier.CATEGORIES）"
    )
    importance = Column(String(10), nullable=False, comment="major / normal / minor")
    rule_id = Column(
        String(60), nullable=False, server_default=sa_text("''"), comment="命中的分类规则"
    )
    classifier_version = Column(
        Integer, nullable=False, server_default=sa_text("0"), comment="分类器版本"
    )
    group_key = Column(
        String(120),
        nullable=False,
        comment="symbol|market|ann_date|category：同日同类合并为一个事件",
    )
    payload = Column(
        JSONB, nullable=False, server_default=sa_text("'{}'::jsonb"), comment="来源原始字段（裁剪）"
    )
    first_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
