"""Add dated dividend tax facts and explicit allocations (no historical data writes)."""

from alembic import op
import sqlalchemy as sa

revision = "20261001_0041"
down_revision = "20260930_0040"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("cash_events", sa.Column("tax_kind", sa.String(20), nullable=True))
    op.create_index("ix_cash_events_tax_kind", "cash_events", ["tax_kind"])
    op.create_check_constraint(
        "ck_cash_events_tax_kind",
        "cash_events",
        "tax_kind IS NULL OR (tax_kind = 'DIVIDEND' AND event_type = 'TAX')",
    )
    op.create_table(
        "dividend_tax_allocations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "cash_event_id",
            sa.Integer(),
            sa.ForeignKey("cash_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "corporate_action_id",
            sa.Integer(),
            sa.ForeignKey("corporate_actions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(24, 8), nullable=False),
        sa.UniqueConstraint(
            "cash_event_id", "corporate_action_id", name="uq_dividend_tax_allocation"
        ),
        sa.CheckConstraint("amount > 0", name="ck_dividend_tax_allocation_positive"),
    )
    for column in ("user_id", "cash_event_id", "corporate_action_id"):
        op.create_index(
            f"ix_dividend_tax_allocations_{column}", "dividend_tax_allocations", [column]
        )


def downgrade():
    # 调用方必须先导出/还原税款事实；降级会移除归属信息。
    op.drop_table("dividend_tax_allocations")
    op.drop_constraint("ck_cash_events_tax_kind", "cash_events", type_="check")
    op.drop_index("ix_cash_events_tax_kind", table_name="cash_events")
    op.drop_column("cash_events", "tax_kind")
