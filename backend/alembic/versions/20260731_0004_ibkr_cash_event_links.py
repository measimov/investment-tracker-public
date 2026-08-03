"""ibkr_activity_flows cash event links

IBKR 现金入账：现金业务行（存款/利息）链接一个 CashEvent；外汇兑换行
一行产生两条现金腿（基础币/对价币）及可能的佣金，各自独立链接，
使不可变守卫能覆盖每一个由导入生成的现金事件。

Revision ID: 20260731_0004
Revises: 20260730_0003
Create Date: 2026-07-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260731_0004"
down_revision: Union[str, None] = "20260730_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = ("cash_event_id", "fx_quote_cash_event_id", "fx_fee_cash_event_id")


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column(
            "ibkr_activity_flows",
            sa.Column(column, sa.Integer(), nullable=True),
        )
        op.create_foreign_key(
            f"fk_ibkr_activity_flows_{column}",
            "ibkr_activity_flows",
            "cash_events",
            [column],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(
            f"ix_ibkr_activity_flows_{column}",
            "ibkr_activity_flows",
            [column],
        )


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.drop_index(f"ix_ibkr_activity_flows_{column}", table_name="ibkr_activity_flows")
        op.drop_constraint(
            f"fk_ibkr_activity_flows_{column}", "ibkr_activity_flows", type_="foreignkey"
        )
        op.drop_column("ibkr_activity_flows", column)
