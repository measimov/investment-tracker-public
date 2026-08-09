"""模型层欠账：NOT NULL 列补 server_default，transactions.fee/currency 收紧 NOT NULL

CLAUDE.md 约定 server_default 必须写在列定义上（不能只有 Python 侧 default），
broker_accounts / cash_events / import_batches 早已照做，这里补齐掉队的一批：
绕过 ORM 的写入（修数 SQL、COPY、conftest 的裸 INSERT）不该在这些列上报错
或落 NULL。

transactions.fee / currency 服务层早已保证有值（真实账本零 NULL 存量），DB 侧
同步收紧为 NOT NULL；SET NOT NULL 之前防御性回填——本库是零行 no-op，但迁移
必须能在任意历史数据上重放。

注意 alembic autogenerate **不比对 server_default**（compare_server_default
默认关闭），本迁移手写；也正因如此，"自动生成出空 diff"证明不了默认值一致
——真正的校验在 tests/test_model_server_defaults.py：inspector 逐列核对实际
schema，迁移漏列/写错值会直接红。

Revision ID: 20260807_0012
Revises: 20260806_0011
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260807_0012"
down_revision: Union[str, None] = "20260806_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NUMERIC = sa.Numeric(18, 8)

# (表, 列, existing_type, server_default)
SERVER_DEFAULTS = [
    ("users", "is_active", sa.Boolean(), sa.text("true")),
    ("users", "is_admin", sa.Boolean(), sa.text("false")),
    ("background_jobs", "status", sa.String(20), "queued"),
    ("background_jobs", "data", sa.JSON(), sa.text("'{}'")),
    ("reconciliation_snapshots", "cash_balances", sa.JSON(), sa.text("'{}'")),
    ("reconciliation_snapshots", "positions", sa.JSON(), sa.text("'[]'")),
    ("security_prices", "currency", sa.String(10), "CNY"),
    ("security_prices", "source", sa.String(50), "tushare"),
    ("broker_fund_flows", "broker", sa.String(50), "招商证券"),
    ("broker_fund_flows", "currency", sa.String(10), "CNY"),
    ("broker_fund_flows", "trade_price", NUMERIC, "0"),
    ("broker_fund_flows", "trade_quantity", NUMERIC, "0"),
    ("broker_fund_flows", "amount", NUMERIC, "0"),
    ("broker_fund_flows", "stamp_tax", NUMERIC, "0"),
    ("broker_fund_flows", "commission", NUMERIC, "0"),
    ("broker_fund_flows", "handling_fee", NUMERIC, "0"),
    ("broker_fund_flows", "management_fee", NUMERIC, "0"),
    ("broker_fund_flows", "settlement_fee", NUMERIC, "0"),
    ("broker_fund_flows", "transfer_fee", NUMERIC, "0"),
    ("broker_fund_flows", "other_fee", NUMERIC, "0"),
    ("ibkr_activity_flows", "broker", sa.String(50), "IBKR"),
    ("ibkr_activity_flows", "base_currency", sa.String(10), "USD"),
    ("transactions", "fee", NUMERIC, "0"),
    ("transactions", "currency", sa.String(10), "CNY"),
]


def upgrade() -> None:
    for table, column, existing_type, default in SERVER_DEFAULTS:
        op.alter_column(table, column, existing_type=existing_type, server_default=default)

    # 收紧 NOT NULL 前防御性回填（本库零行；迁移要能在任意历史数据上重放）
    op.execute("UPDATE transactions SET fee = 0 WHERE fee IS NULL")
    op.execute("UPDATE transactions SET currency = 'CNY' WHERE currency IS NULL")
    op.alter_column("transactions", "fee", existing_type=NUMERIC, nullable=False)
    op.alter_column("transactions", "currency", existing_type=sa.String(10), nullable=False)


def downgrade() -> None:
    op.alter_column("transactions", "currency", existing_type=sa.String(10), nullable=True)
    op.alter_column("transactions", "fee", existing_type=NUMERIC, nullable=True)
    for table, column, existing_type, _default in SERVER_DEFAULTS:
        op.alter_column(table, column, existing_type=existing_type, server_default=None)
