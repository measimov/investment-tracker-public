"""标的行业分类：security_industries 全局表 + security_rules 加 INDUSTRY

Revision ID: 20260928_0031
Revises: 20260928_0030
Create Date: 2026-09-28

issue #235 第 3 项。行业按来源各存一行（官方 Tushare/EDGAR 为主、东方财富 F10 补缺），
读取时与用户规则 INDUSTRY（payload {industry}）按优先级合成。
CheckConstraint 变更 alembic autogenerate 检测不到，规则白名单手写（模板同 0023）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260928_0031"
down_revision: Union[str, None] = "20260928_0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RULE_TYPES = (
    "'EXCLUDE', 'CASH_MANAGEMENT', 'RELISTING', "
    "'NAME_OVERRIDE', 'PRICE_GAP_EXEMPTION', 'CMB_CASH_BUSINESS', 'ADS_RATIO'"
)
_ADDED_RULE_TYPE = "'INDUSTRY'"


def _replace_rule_check(rule_types: str) -> None:
    op.drop_constraint("ck_security_rules_rule_type", "security_rules", type_="check")
    op.create_check_constraint(
        "ck_security_rules_rule_type", "security_rules", f"rule_type IN ({rule_types})"
    )


def upgrade() -> None:
    op.create_table(
        "security_industries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "symbol",
            sa.String(length=20),
            nullable=False,
            comment="账本口径代码（同 holdings.symbol）",
        ),
        sa.Column(
            "market", sa.String(length=20), nullable=False, comment="市场：A股/B股/港股/美股"
        ),
        sa.Column(
            "source", sa.String(length=20), nullable=False, comment="tushare / edgar / eastmoney"
        ),
        sa.Column("industry", sa.String(length=100), nullable=False, comment="中文行业名"),
        sa.Column(
            "raw",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
            comment="来源原始字段：SIC 码与英文描述 / EM2016 三级分类 / 证监会分类 …",
        ),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="最近拉取时间",
        ),
        sa.CheckConstraint(
            "source IN ('tushare', 'edgar', 'eastmoney')",
            name="ck_security_industries_source",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol", "market", "source", name="uq_security_industries_key"),
    )
    op.create_index(op.f("ix_security_industries_id"), "security_industries", ["id"], unique=False)
    _replace_rule_check(f"{_RULE_TYPES}, {_ADDED_RULE_TYPE}")


def downgrade() -> None:
    # 先删再收紧：留着新值的行会让 create_check_constraint 直接失败
    op.execute(f"DELETE FROM security_rules WHERE rule_type IN ({_ADDED_RULE_TYPE})")
    _replace_rule_check(_RULE_TYPES)
    op.drop_index(op.f("ix_security_industries_id"), table_name="security_industries")
    op.drop_table("security_industries")
