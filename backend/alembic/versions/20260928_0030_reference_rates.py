"""参考利率日序列（无风险利率）

Revision ID: 20260928_0030
Revises: 20260928_0027
Create Date: 2026-09-28

#200：夏普/索提诺的无风险利率此前固定为 0。本表存 SHIBOR 3M（本币指标用）与美国 13 周
国库券利率（展示）的每日发布值，年化百分比。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0030"
down_revision: Union[str, None] = "20260928_0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reference_rates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("series", sa.String(length=32), nullable=False),
        sa.Column("rate_date", sa.Date(), nullable=False),
        sa.Column("value", sa.Numeric(12, 6), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("series", "rate_date", name="uq_reference_rates_series_date"),
    )


def downgrade() -> None:
    op.drop_table("reference_rates")
