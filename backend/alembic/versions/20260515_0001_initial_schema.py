"""Initial schema baseline.

Revision ID: 20260515_0001
Revises:
Create Date: 2026-05-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260515_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(length=50), nullable=False),
        sa.Column("email", sa.String(length=100), nullable=True),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_users_id"), "users", ["id"], unique=False)
    op.create_index(op.f("ix_users_username"), "users", ["username"], unique=True)
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)

    op.create_table(
        "exchange_rates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("from_currency", sa.String(length=10), nullable=False),
        sa.Column("to_currency", sa.String(length=10), nullable=False),
        sa.Column("rate", sa.Numeric(18, 8), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "from_currency", "to_currency", "effective_date", name="uix_currency_date"
        ),
    )
    op.create_index(op.f("ix_exchange_rates_id"), "exchange_rates", ["id"], unique=False)
    op.create_index(
        op.f("ix_exchange_rates_from_currency"), "exchange_rates", ["from_currency"], unique=False
    )
    op.create_index(
        op.f("ix_exchange_rates_to_currency"), "exchange_rates", ["to_currency"], unique=False
    )
    op.create_index(
        op.f("ix_exchange_rates_effective_date"), "exchange_rates", ["effective_date"], unique=False
    )

    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=True),
        sa.Column("market", sa.String(length=20), nullable=False),
        sa.Column("transaction_type", sa.String(length=10), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 8), nullable=False),
        sa.Column("price", sa.Numeric(18, 8), nullable=False),
        sa.Column("fee", sa.Numeric(18, 8), nullable=True),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(length=10), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_transactions_id"), "transactions", ["id"], unique=False)
    op.create_index(op.f("ix_transactions_user_id"), "transactions", ["user_id"], unique=False)
    op.create_index(op.f("ix_transactions_symbol"), "transactions", ["symbol"], unique=False)
    op.create_index(op.f("ix_transactions_market"), "transactions", ["market"], unique=False)
    op.create_index(
        op.f("ix_transactions_transaction_date"), "transactions", ["transaction_date"], unique=False
    )

    op.create_table(
        "holdings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=True),
        sa.Column("market", sa.String(length=20), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 8), nullable=False),
        sa.Column("avg_cost", sa.Numeric(18, 8), nullable=False),
        sa.Column("total_cost", sa.Numeric(18, 8), nullable=False),
        sa.Column("currency", sa.String(length=10), nullable=True),
        sa.Column("current_price", sa.Numeric(18, 8), nullable=True),
        sa.Column("price_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "symbol", "market", name="uix_user_symbol_market"),
    )
    op.create_index(op.f("ix_holdings_id"), "holdings", ["id"], unique=False)
    op.create_index(op.f("ix_holdings_user_id"), "holdings", ["user_id"], unique=False)
    op.create_index(op.f("ix_holdings_symbol"), "holdings", ["symbol"], unique=False)
    op.create_index(op.f("ix_holdings_market"), "holdings", ["market"], unique=False)

    op.create_table(
        "corporate_actions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=True),
        sa.Column("market", sa.String(length=20), nullable=False),
        sa.Column("action_type", sa.String(length=20), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=False),
        sa.Column("record_date", sa.Date(), nullable=True),
        sa.Column("payment_date", sa.Date(), nullable=True),
        sa.Column("dividend_per_share", sa.Numeric(18, 8), nullable=True),
        sa.Column("total_dividend", sa.Numeric(18, 8), nullable=True),
        sa.Column("tax_withheld", sa.Numeric(18, 8), nullable=True),
        sa.Column("tax_rate", sa.Numeric(5, 4), nullable=True),
        sa.Column("net_dividend", sa.Numeric(18, 8), nullable=True),
        sa.Column("shares_received", sa.Numeric(18, 8), nullable=True),
        sa.Column("distribution_ratio", sa.String(length=20), nullable=True),
        sa.Column("subscription_price", sa.Numeric(18, 8), nullable=True),
        sa.Column("subscription_quantity", sa.Numeric(18, 8), nullable=True),
        sa.Column("subscription_amount", sa.Numeric(18, 8), nullable=True),
        sa.Column("split_ratio", sa.String(length=20), nullable=True),
        sa.Column("new_shares", sa.Numeric(18, 8), nullable=True),
        sa.Column("cost_basis_adjustment", sa.Numeric(18, 8), nullable=True),
        sa.Column("adjusted_quantity", sa.Numeric(18, 8), nullable=True),
        sa.Column("adjusted_cost_per_share", sa.Numeric(18, 8), nullable=True),
        sa.Column("currency", sa.String(length=10), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_corporate_actions_id"), "corporate_actions", ["id"], unique=False)
    op.create_index(
        op.f("ix_corporate_actions_user_id"), "corporate_actions", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_corporate_actions_symbol"), "corporate_actions", ["symbol"], unique=False
    )
    op.create_index(
        op.f("ix_corporate_actions_market"), "corporate_actions", ["market"], unique=False
    )
    op.create_index(
        op.f("ix_corporate_actions_action_type"), "corporate_actions", ["action_type"], unique=False
    )
    op.create_index(
        op.f("ix_corporate_actions_ex_date"), "corporate_actions", ["ex_date"], unique=False
    )

    op.create_table(
        "broker_fund_flows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=True),
        sa.Column("broker", sa.String(length=50), nullable=False),
        sa.Column("row_hash", sa.String(length=64), nullable=False),
        sa.Column("source_filename", sa.String(length=255), nullable=True),
        sa.Column("source_row_number", sa.Integer(), nullable=True),
        sa.Column("security_code", sa.String(length=20), nullable=True),
        sa.Column("security_name", sa.String(length=100), nullable=True),
        sa.Column("currency", sa.String(length=10), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("trade_price", sa.Numeric(18, 8), nullable=False),
        sa.Column("trade_quantity", sa.Numeric(18, 8), nullable=False),
        sa.Column("amount", sa.Numeric(18, 8), nullable=False),
        sa.Column("cash_balance", sa.Numeric(18, 8), nullable=True),
        sa.Column("remaining_quantity", sa.Numeric(18, 8), nullable=True),
        sa.Column("contract_number", sa.String(length=50), nullable=True),
        sa.Column("serial_number", sa.String(length=50), nullable=True),
        sa.Column("business_name", sa.String(length=50), nullable=False),
        sa.Column("stamp_tax", sa.Numeric(18, 8), nullable=False),
        sa.Column("commission", sa.Numeric(18, 8), nullable=False),
        sa.Column("handling_fee", sa.Numeric(18, 8), nullable=False),
        sa.Column("management_fee", sa.Numeric(18, 8), nullable=False),
        sa.Column("settlement_fee", sa.Numeric(18, 8), nullable=False),
        sa.Column("transfer_fee", sa.Numeric(18, 8), nullable=False),
        sa.Column("other_fee", sa.Numeric(18, 8), nullable=False),
        sa.Column("shareholder_code", sa.String(length=50), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "row_hash", name="uix_broker_flow_user_hash"),
    )
    op.create_index(op.f("ix_broker_fund_flows_id"), "broker_fund_flows", ["id"], unique=False)
    op.create_index(
        op.f("ix_broker_fund_flows_user_id"), "broker_fund_flows", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_broker_fund_flows_transaction_id"),
        "broker_fund_flows",
        ["transaction_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_broker_fund_flows_broker"), "broker_fund_flows", ["broker"], unique=False
    )
    op.create_index(
        op.f("ix_broker_fund_flows_row_hash"), "broker_fund_flows", ["row_hash"], unique=False
    )
    op.create_index(
        op.f("ix_broker_fund_flows_security_code"),
        "broker_fund_flows",
        ["security_code"],
        unique=False,
    )
    op.create_index(
        op.f("ix_broker_fund_flows_trade_date"), "broker_fund_flows", ["trade_date"], unique=False
    )
    op.create_index(
        op.f("ix_broker_fund_flows_serial_number"),
        "broker_fund_flows",
        ["serial_number"],
        unique=False,
    )
    op.create_index(
        op.f("ix_broker_fund_flows_business_name"),
        "broker_fund_flows",
        ["business_name"],
        unique=False,
    )

    op.create_table(
        "ibkr_activity_flows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=True),
        sa.Column("corporate_action_id", sa.Integer(), nullable=True),
        sa.Column("broker", sa.String(length=50), nullable=False),
        sa.Column("row_hash", sa.String(length=64), nullable=False),
        sa.Column("source_filename", sa.String(length=255), nullable=True),
        sa.Column("source_row_number", sa.Integer(), nullable=False),
        sa.Column("account", sa.String(length=50), nullable=True),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("activity_type", sa.String(length=50), nullable=False),
        sa.Column("raw_symbol", sa.String(length=50), nullable=True),
        sa.Column("symbol", sa.String(length=50), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("market", sa.String(length=20), nullable=True),
        sa.Column("quantity", sa.Numeric(18, 8), nullable=True),
        sa.Column("price", sa.Numeric(18, 8), nullable=True),
        sa.Column("price_currency", sa.String(length=10), nullable=True),
        sa.Column("base_currency", sa.String(length=10), nullable=False),
        sa.Column("gross_amount", sa.Numeric(24, 10), nullable=True),
        sa.Column("commission", sa.Numeric(24, 10), nullable=True),
        sa.Column("net_amount", sa.Numeric(24, 10), nullable=True),
        sa.Column("fee_in_price_currency", sa.Numeric(18, 8), nullable=True),
        sa.Column("skip_reason", sa.String(length=100), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.ForeignKeyConstraint(
            ["corporate_action_id"], ["corporate_actions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "row_hash", name="uix_ibkr_activity_user_hash"),
    )
    op.create_index(op.f("ix_ibkr_activity_flows_id"), "ibkr_activity_flows", ["id"], unique=False)
    op.create_index(
        op.f("ix_ibkr_activity_flows_user_id"), "ibkr_activity_flows", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_ibkr_activity_flows_transaction_id"),
        "ibkr_activity_flows",
        ["transaction_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ibkr_activity_flows_corporate_action_id"),
        "ibkr_activity_flows",
        ["corporate_action_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ibkr_activity_flows_broker"), "ibkr_activity_flows", ["broker"], unique=False
    )
    op.create_index(
        op.f("ix_ibkr_activity_flows_row_hash"), "ibkr_activity_flows", ["row_hash"], unique=False
    )
    op.create_index(
        op.f("ix_ibkr_activity_flows_trade_date"),
        "ibkr_activity_flows",
        ["trade_date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ibkr_activity_flows_activity_type"),
        "ibkr_activity_flows",
        ["activity_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ibkr_activity_flows_raw_symbol"),
        "ibkr_activity_flows",
        ["raw_symbol"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ibkr_activity_flows_symbol"), "ibkr_activity_flows", ["symbol"], unique=False
    )
    op.create_index(
        op.f("ix_ibkr_activity_flows_market"), "ibkr_activity_flows", ["market"], unique=False
    )


def downgrade() -> None:
    op.drop_table("ibkr_activity_flows")
    op.drop_table("broker_fund_flows")
    op.drop_table("corporate_actions")
    op.drop_table("holdings")
    op.drop_table("transactions")
    op.drop_table("exchange_rates")
    op.drop_table("users")
