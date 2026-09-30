"""官方公告表 security_announcements（#306）

Revision ID: 20260929_0038
Revises: 20260929_0037
Create Date: 2026-09-29

巨潮 / 披露易 / EDGAR 三地官方公告的全局表：一行一份文件，按 (source, source_id) 唯一；
分类与重要性由 announcement_classifier 纯函数给出，group_key 把同日同类文件合成一个事件。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260929_0038"
down_revision: Union[str, None] = "20260929_0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "security_announcements",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False, comment="本仓标的代码"),
        sa.Column(
            "market", sa.String(length=20), nullable=False, comment="本仓市场（A股/B股/港股/美股）"
        ),
        sa.Column(
            "source", sa.String(length=20), nullable=False, comment="cninfo / hkexnews / edgar"
        ),
        sa.Column(
            "source_id",
            sa.String(length=64),
            nullable=False,
            comment="来源内唯一 ID（公告 ID/NEWS_ID/accession）",
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False, comment="发布时间"),
        sa.Column("ann_date", sa.Date(), nullable=False, comment="公告日（业务时区）"),
        sa.Column("title", sa.Text(), nullable=False, comment="公告标题（交易所原文）"),
        sa.Column(
            "url", sa.Text(), server_default=sa.text("''"), nullable=False, comment="原文链接"
        ),
        sa.Column(
            "category_raw",
            sa.Text(),
            server_default=sa.text("''"),
            nullable=False,
            comment="官方分类原文（巨潮 announcementType / 披露易 LONG_TEXT / EDGAR form|items）",
        ),
        sa.Column(
            "category",
            sa.String(length=30),
            nullable=False,
            comment="归类（见 announcement_classifier.CATEGORIES）",
        ),
        sa.Column(
            "importance", sa.String(length=10), nullable=False, comment="major / normal / minor"
        ),
        sa.Column(
            "rule_id",
            sa.String(length=60),
            server_default=sa.text("''"),
            nullable=False,
            comment="命中的分类规则",
        ),
        sa.Column(
            "classifier_version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="分类器版本",
        ),
        sa.Column(
            "group_key",
            sa.String(length=120),
            nullable=False,
            comment="symbol|market|ann_date|category：同日同类合并为一个事件",
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment="来源原始字段（裁剪）",
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "importance IN ('major', 'normal', 'minor')",
            name="ck_security_announcements_importance",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "symbol",
            "market",
            "source",
            "source_id",
            name="uq_security_announcements_symbol_source_id",
        ),
    )
    op.create_index(
        "ix_security_announcements_symbol_published",
        "security_announcements",
        ["symbol", "market", sa.text("published_at DESC")],
    )
    op.create_index("ix_security_announcements_group_key", "security_announcements", ["group_key"])
    op.create_index(
        "ix_security_announcements_importance_date",
        "security_announcements",
        ["importance", "ann_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_security_announcements_importance_date", table_name="security_announcements")
    op.drop_index("ix_security_announcements_group_key", table_name="security_announcements")
    op.drop_index("ix_security_announcements_symbol_published", table_name="security_announcements")
    op.drop_table("security_announcements")
