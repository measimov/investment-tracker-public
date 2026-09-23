"""期初建仓（OPENING_POSITION，#174）：holdings.unknown_cost_quantity + 回填存量归档的转托转入行

场外→场内「转托转入」/「转存管转入」在解析器里此前没有映射，走通用归档（三个链接列全
NULL、skip_reason NULL），持仓从不建立，后续卖出撞账户预检整批被拒。本版把这类行
入账为 OPENING_POSITION 公司行动（数量/日期/账户来自对账单，成本未知则标记）。

两件事：
1. holdings.unknown_cost_quantity NOT NULL DEFAULT 0：桶内成本未知的份额，重放派生、
   落库供 GET /holdings 展示「成本未知」而不必再重放。
2. 回填存量归档的转入行 skip_reason='unbooked_opening_position'：get_existing_hashes
   刻意放行带 skip_reason 的行，重导同一对账单时它们会在原行上转正为公司行动
   （与 0011 未归属税行同一套三段式）。只认「确实是转入业务、且确实没入账」的行；
   幂等跨 downgrade 成立（notes 追加按内容判重，照抄 0011）。

Revision ID: 20260920_0020
Revises: 20260904_0019
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260920_0020"
down_revision: Union[str, None] = "20260904_0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 与 broker_import_common.UNBOOKED_OPENING_POSITION / 招商导入器常量对应。
# 迁移刻意不 import 应用代码：迁移必须能在任意历史代码版本下重放。
UNBOOKED_OPENING_POSITION = "unbooked_opening_position"
OPENING_POSITION_BUSINESS_NAMES = ("转托转入", "转存管转入")
BACKFILL_NOTE = "backfilled as unbooked opening position by migration 20260920_0020"


def upgrade() -> None:
    op.add_column(
        "holdings",
        sa.Column(
            "unknown_cost_quantity",
            sa.Numeric(18, 8),
            nullable=False,
            server_default="0",
        ),
    )
    connection = op.get_bind()
    for business_name in OPENING_POSITION_BUSINESS_NAMES:
        connection.execute(
            sa.text(
                """
                UPDATE broker_fund_flows
                SET skip_reason = :marker,
                    notes = CASE
                        WHEN COALESCE(notes, '') LIKE '%' || :note || '%' THEN notes
                        WHEN COALESCE(notes, '') = '' THEN :note
                        ELSE notes || '; ' || :note
                    END
                WHERE broker = '招商证券'
                  AND business_name = :business_name
                  AND COALESCE(security_code, '') <> ''
                  AND transaction_id IS NULL
                  AND corporate_action_id IS NULL
                  AND cash_event_id IS NULL
                  AND skip_reason IS NULL
                """
            ),
            {"marker": UNBOOKED_OPENING_POSITION, "business_name": business_name, "note": BACKFILL_NOTE},
        )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("UPDATE broker_fund_flows SET skip_reason = NULL WHERE skip_reason = :marker"),
        {"marker": UNBOOKED_OPENING_POSITION},
    )
    op.drop_column("holdings", "unknown_cost_quantity")
