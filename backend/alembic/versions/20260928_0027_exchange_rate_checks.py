"""官方汇率源：官方中间价与第三方报价的逐日比对表

Revision ID: 20260928_0027
Revises: 20260927_0026
Create Date: 2026-09-28

#200：人民币汇率中间价（中国货币网）成为 CNY 汇率主源，frankfurter / open.er-api 降为
比对与兜底。官方源成功时第三方报价不再写入 exchange_rates，只在本表留一行差异。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0027"
down_revision: Union[str, None] = "20260927_0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "exchange_rate_checks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("from_currency", sa.String(length=10), nullable=False),
        sa.Column("to_currency", sa.String(length=10), nullable=False),
        sa.Column("check_date", sa.Date(), nullable=False),
        sa.Column("official_date", sa.Date(), nullable=False),
        sa.Column("official_source", sa.String(length=50), nullable=False),
        sa.Column("official_rate", sa.Numeric(18, 8), nullable=False),
        sa.Column("reference_source", sa.String(length=50), nullable=False),
        sa.Column("reference_rate", sa.Numeric(18, 8), nullable=False),
        sa.Column("diff_pct", sa.Numeric(10, 4), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "from_currency", "to_currency", "check_date", name="uq_exchange_rate_checks_pair_date"
        ),
    )


def downgrade() -> None:
    op.drop_table("exchange_rate_checks")
