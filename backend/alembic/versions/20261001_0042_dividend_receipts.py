"""Separate dividend receipt confirmation from announcements.

Revision ID: 20261001_0042
Revises: 20261001_0041
"""

from alembic import op
import sqlalchemy as sa

revision = "20261001_0042"
down_revision = "20261001_0041"
branch_labels = None
depends_on = None


def upgrade():
    # Preserve old arithmetic until the separately reviewed historical upgrade.
    op.add_column(
        "corporate_actions",
        sa.Column("receipt_status", sa.String(20), nullable=False, server_default="RECEIVED"),
    )
    op.add_column(
        "corporate_actions",
        sa.Column("amount_basis", sa.String(20), nullable=False, server_default="LEGACY"),
    )
    op.add_column(
        "corporate_actions", sa.Column("dividend_suggestion_id", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        "fk_corporate_actions_dividend_suggestion_id",
        "corporate_actions",
        "corporate_action_suggestions",
        ["dividend_suggestion_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_corporate_actions_dividend_suggestion_id",
        "corporate_actions",
        ["dividend_suggestion_id"],
    )
    op.create_check_constraint(
        "ck_ca_receipt_status", "corporate_actions", "receipt_status IN ('RECEIVED', 'UNVERIFIED')"
    )
    op.create_check_constraint(
        "ck_ca_amount_basis",
        "corporate_actions",
        "amount_basis IN ('LEGACY', 'GROSS_NET', 'NET_ONLY')",
    )
    op.add_column(
        "corporate_action_suggestions",
        sa.Column("receipt_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column("corporate_action_suggestions", "receipt_complete")
    op.drop_constraint("ck_ca_amount_basis", "corporate_actions")
    op.drop_constraint("ck_ca_receipt_status", "corporate_actions")
    op.drop_index("ix_corporate_actions_dividend_suggestion_id", "corporate_actions")
    op.drop_constraint(
        "fk_corporate_actions_dividend_suggestion_id", "corporate_actions", type_="foreignkey"
    )
    op.drop_column("corporate_actions", "dividend_suggestion_id")
    op.drop_column("corporate_actions", "amount_basis")
    op.drop_column("corporate_actions", "receipt_status")
