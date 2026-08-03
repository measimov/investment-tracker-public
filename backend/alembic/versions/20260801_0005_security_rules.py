"""security_rules：账本特例规则表驱动（issue #82）

excluded_securities 并入为 EXCLUDE 类型后删表；原硬编码常量
（CASH_MANAGEMENT_SYMBOLS / KNOWN_RELISTINGS / KNOWN_SECURITY_NAMES /
CMB_CASH_BUSINESS_MAP / 停牌缺口豁免）按现存用户逐一播种——现状即
全局生效，按用户播种 = 行为不变。

Revision ID: 20260801_0005
Revises: 20260731_0004
Create Date: 2026-08-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260801_0005"
down_revision: Union[str, None] = "20260731_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 种子 = 迁移时点的硬编码现值。此后以表为准，这里永不再改。
SEED_CASH_MANAGEMENT = [("880013", "A股", "天添利现金管理产品，派息按利息入账")]
SEED_RELISTINGS = [
    (
        "01263",
        "港股",
        {
            "new_symbol": "PCT",
            "new_market": "新加坡股",
            "new_currency": "SGD",
            "old_currency": "HKD",
            "name": "柏能集团",
        },
        "港股退市转新加坡重新上市，导入时自动生成转换交易",
    )
]
SEED_NAME_OVERRIDES = [
    ("01263", "港股", {"name": "柏能集团"}, "Tushare 无法解析的已退市港股"),
    ("PCT", "新加坡股", {"name": "柏能集团"}, "Tushare 无 SGX 数据"),
]
SEED_PRICE_GAPS = [
    (
        "01263",
        "港股",
        {"start_date": "2026-01-09", "end_date": None},
        "停牌至摘牌，行情永久缺失（末价 2026-01-08）",
    ),
    (
        "123266",
        "A股",
        {"start_date": "2026-03-26", "end_date": "2026-04-06"},
        "可转债上市初期行情源无数据（首价 2026-04-07）",
    ),
]
SEED_CMB_CASH_BUSINESS = [
    ("银行转存", "DEPOSIT"),
    ("资管转让资金上账", "DEPOSIT"),
    ("银行转取", "WITHDRAWAL"),
    ("柜台取出", "WITHDRAWAL"),
    ("利息归本", "INTEREST"),
    ("港股通组合费收取", "FEE"),
    ("资金红冲", "FEE"),
    ("资金蓝补", "OTHER"),
    ("质押回购拆出", "TRANSFER_OUT"),
    ("拆出质押购回", "TRANSFER_IN"),
    ("产品申购确认", "TRANSFER_OUT"),
    ("产品赎回确认", "TRANSFER_IN"),
    ("新股申购", "TRANSFER_OUT"),
    ("新股申购确认缴款", "TRANSFER_OUT"),
    ("市值申购中签扣款", "TRANSFER_OUT"),
    ("申购返款", "TRANSFER_IN"),
    ("市值申购中签扣款回冲", "TRANSFER_IN"),
]


def upgrade() -> None:
    table = op.create_table(
        "security_rules",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("rule_type", sa.String(length=30), nullable=False),
        sa.Column("symbol", sa.String(length=50), nullable=False),
        sa.Column("market", sa.String(length=20), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "rule_type IN ('EXCLUDE', 'CASH_MANAGEMENT', 'RELISTING', "
            "'NAME_OVERRIDE', 'PRICE_GAP_EXEMPTION', 'CMB_CASH_BUSINESS')",
            name="ck_security_rules_rule_type",
        ),
        sa.CheckConstraint(
            "(rule_type != 'CMB_CASH_BUSINESS') OR (market IS NULL)",
            name="ck_security_rules_cmb_market_null",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "rule_type",
            "symbol",
            "market",
            name="uq_security_rules_key",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index("ix_security_rules_id", "security_rules", ["id"])
    op.create_index("ix_security_rules_user_id", "security_rules", ["user_id"])
    op.create_index("ix_security_rules_rule_type", "security_rules", ["rule_type"])

    conn = op.get_bind()

    # 1) excluded_securities 并入（保留 note 与 created_at）
    conn.execute(
        sa.text(
            "INSERT INTO security_rules (user_id, rule_type, symbol, market, note, created_at) "
            "SELECT user_id, 'EXCLUDE', symbol, market, note, created_at "
            "FROM excluded_securities"
        )
    )
    op.drop_index("ix_excluded_securities_id", table_name="excluded_securities")
    op.drop_index("ix_excluded_securities_user_id", table_name="excluded_securities")
    op.drop_table("excluded_securities")

    # 2) 硬编码现值按现存用户播种
    user_ids = [row[0] for row in conn.execute(sa.text("SELECT id FROM users"))]
    rows = []
    for user_id in user_ids:
        for symbol, market, note in SEED_CASH_MANAGEMENT:
            rows.append(
                dict(user_id=user_id, rule_type="CASH_MANAGEMENT", symbol=symbol,
                     market=market, payload=None, note=note)
            )
        for symbol, market, payload, note in SEED_RELISTINGS:
            rows.append(
                dict(user_id=user_id, rule_type="RELISTING", symbol=symbol,
                     market=market, payload=payload, note=note)
            )
        for symbol, market, payload, note in SEED_NAME_OVERRIDES:
            rows.append(
                dict(user_id=user_id, rule_type="NAME_OVERRIDE", symbol=symbol,
                     market=market, payload=payload, note=note)
            )
        for symbol, market, payload, note in SEED_PRICE_GAPS:
            rows.append(
                dict(user_id=user_id, rule_type="PRICE_GAP_EXEMPTION", symbol=symbol,
                     market=market, payload=payload, note=note)
            )
        for business_name, event_type in SEED_CMB_CASH_BUSINESS:
            rows.append(
                dict(user_id=user_id, rule_type="CMB_CASH_BUSINESS", symbol=business_name,
                     market=None, payload={"event_type": event_type},
                     note="招商对账单现金业务口径")
            )
    if rows:
        op.bulk_insert(table, rows)


def downgrade() -> None:
    op.create_table(
        "excluded_securities",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("market", sa.String(length=20), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "symbol", "market", name="uq_excluded_securities_key"),
    )
    op.create_index("ix_excluded_securities_id", "excluded_securities", ["id"])
    op.create_index("ix_excluded_securities_user_id", "excluded_securities", ["user_id"])
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO excluded_securities (user_id, symbol, market, note, created_at) "
            "SELECT user_id, symbol, market, note, created_at FROM security_rules "
            "WHERE rule_type = 'EXCLUDE' AND market IS NOT NULL"
        )
    )
    op.drop_index("ix_security_rules_rule_type", table_name="security_rules")
    op.drop_index("ix_security_rules_user_id", table_name="security_rules")
    op.drop_index("ix_security_rules_id", table_name="security_rules")
    op.drop_table("security_rules")
