"""security_catalog / security_catalog_syncs：标的全集参考数据集与每源同步状态

Revision ID: 20260904_0018
Revises: 20260903_0017
Create Date: 2026-09-04

全集是**参考数据集**（名称/拼音/类型/上市状态），账本（交易/持仓/自选）仍是
用户自己那几行的权威——搜索时账本行排前、目录只补账本缺的字段，所以不会成为
"第二个漂移源"。两张表都是全局表、无 user_id。永不删行：退市只翻 list_status，
某来源一次拉取失败保留旧行并在 security_catalog_syncs 记 failed。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260904_0018"
down_revision: Union[str, None] = "20260903_0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "security_catalog",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "symbol", sa.String(length=20), nullable=False,
            comment="账本口径代码：大写，港股 5 位（同 normalize_manual_symbol）",
        ),
        sa.Column("market", sa.String(length=20), nullable=False, comment="市场：A股/B股/港股/美股"),
        sa.Column("name", sa.String(length=200), nullable=True, comment="简体中文名"),
        sa.Column("name_en", sa.String(length=200), nullable=True, comment="英文名"),
        sa.Column("name_trad", sa.String(length=200), nullable=True, comment="繁体名（港交所證券名單）"),
        sa.Column("pinyin", sa.String(length=50), nullable=True, comment="拼音首字母缩写（大写）"),
        sa.Column("currency", sa.String(length=10), nullable=True, comment="交易币种"),
        sa.Column(
            "currency_source", sa.String(length=20), nullable=True,
            comment="币种来源：tushare/inferred/hkex-dayquot/tencent-quote",
        ),
        sa.Column(
            "security_type", sa.String(length=20), server_default="unknown", nullable=False,
            comment="stock/etf/fund/reit/adr/pref/gdr/unknown",
        ),
        sa.Column("board", sa.String(length=30), nullable=True, comment="板块：主板/创业板/科创板/北交所/…"),
        sa.Column("exchange", sa.String(length=20), nullable=True, comment="交易所：SSE/SZSE/BSE/HKEX/US"),
        sa.Column(
            "list_status", sa.String(length=20), server_default="listed", nullable=False,
            comment="listed/delisted/unknown",
        ),
        sa.Column("list_date", sa.Date(), nullable=True),
        sa.Column("delist_date", sa.Date(), nullable=True),
        sa.Column("source", sa.String(length=40), nullable=False, comment="最后写入该行的来源（loader 名）"),
        sa.Column(
            "detail", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"),
            nullable=False, comment="来源特有字段：fund_type / 次分類 / classify / isin …",
        ),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "security_type IN ('stock','etf','fund','reit','adr','pref','gdr','unknown')",
            name="ck_security_catalog_security_type",
        ),
        sa.CheckConstraint(
            "list_status IN ('listed','delisted','unknown')", name="ck_security_catalog_list_status"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol", "market", name="uq_security_catalog_symbol_market"),
    )
    op.create_index(op.f("ix_security_catalog_id"), "security_catalog", ["id"], unique=False)
    op.create_index("ix_security_catalog_market", "security_catalog", ["market"], unique=False)
    op.create_index(
        "ix_security_catalog_symbol_pattern", "security_catalog", ["symbol"], unique=False,
        postgresql_ops={"symbol": "varchar_pattern_ops"},
    )
    op.create_index(
        "ix_security_catalog_pinyin_pattern", "security_catalog", ["pinyin"], unique=False,
        postgresql_ops={"pinyin": "varchar_pattern_ops"},
    )

    op.create_table(
        "security_catalog_syncs",
        sa.Column("source", sa.String(length=40), nullable=False, comment="loader 名，如 tushare-stock_basic"),
        sa.Column(
            "markets", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'"),
            nullable=False, comment="该源覆盖的市场",
        ),
        sa.Column("status", sa.String(length=20), nullable=False, comment="ok/failed/skipped/running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_success_at", sa.DateTime(timezone=True), nullable=True,
            comment="最近一次成功时间（失败不刷新）",
        ),
        sa.Column("rows_seen", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rows_upserted", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "detail", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'"),
            nullable=False, comment="跳过原因等",
        ),
        sa.PrimaryKeyConstraint("source"),
    )


def downgrade() -> None:
    op.drop_table("security_catalog_syncs")
    op.drop_index("ix_security_catalog_pinyin_pattern", table_name="security_catalog")
    op.drop_index("ix_security_catalog_symbol_pattern", table_name="security_catalog")
    op.drop_index("ix_security_catalog_market", table_name="security_catalog")
    op.drop_index(op.f("ix_security_catalog_id"), table_name="security_catalog")
    op.drop_table("security_catalog")
