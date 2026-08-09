"""broker_fund_flows.skip_reason：未归属红利税行的可恢复标记 + 回填历史孤儿

招商/东财此前找不到唯一股息时，把税行无链接归档并把 row_hash 记入判重；
之后即使补齐了股息、重新导入同一对账单，也会被 hash 判重跳过——tax_withheld
永远缺失且没有任何补救通道。IBKR 早有解法（skip_reason="unattributed_tax"
+ 重导再归属），但 broker_fund_flows 缺这个标记位。

**必须回填**：只加列不回填的话，既有孤儿行升级后仍是 skip_reason=NULL，
get_existing_hashes 继续把它们当成已入账并在重导时跳过——等于只修了上线后
新产生的孤儿，issue #132 描述的存量失联数据一条都没修。实测部署库有 36 条
招商未归属税行。

回填**有严格约束**，只认"确实是税业务、且确实没入账"的行：
  - broker + business_name 精确匹配两家的税业务名（其余业务的无链接行不动——
    误标会让它们在重导时被反复重复入账）；
  - 三个链接列全为 NULL；
  - skip_reason 仍为 NULL（幂等：重复执行不覆盖已有标记）。

**幂等要跨 downgrade 成立**：downgrade 直接删列，skip_reason 随之全部归 NULL，
但 notes 里的回填痕迹留了下来——只靠 `skip_reason IS NULL` 守卫的话，
downgrade → upgrade 会把同一句审计备注追加第二遍。故备注追加另按 notes
内容判重。

Revision ID: 20260806_0011
Revises: 20260805_0010
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260806_0011"
down_revision: Union[str, None] = "20260805_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 与 broker_import_common.UNATTRIBUTED_TAX / 两个导入器的业务名常量对应。
# 迁移刻意不 import 应用代码：迁移必须能在任意历史代码版本下重放。
UNATTRIBUTED_TAX = "unattributed_tax"
TAX_BUSINESS_BY_BROKER = {
    "招商证券": "股息红利税补缴",
    "东方财富证券": "股息红利差异扣税",
}
BACKFILL_NOTE = "backfilled as unattributed tax by migration 20260806_0011"


def upgrade() -> None:
    op.add_column(
        "broker_fund_flows",
        sa.Column("skip_reason", sa.String(length=100), nullable=True),
    )
    # 判重要按"是否已入账"过滤这一列，未归属税行的查询会命中它
    op.create_index(
        "ix_broker_fund_flows_skip_reason",
        "broker_fund_flows",
        ["skip_reason"],
        unique=False,
        postgresql_where=sa.text("skip_reason IS NOT NULL"),
    )

    connection = op.get_bind()
    for broker, business_name in TAX_BUSINESS_BY_BROKER.items():
        connection.execute(
            sa.text(
                """
                UPDATE broker_fund_flows
                SET skip_reason = :marker,
                    notes = CASE
                        -- 备注只追加一次：downgrade 会删列、skip_reason 随之
                        -- 归 NULL，但 notes 里的痕迹还在；只靠 skip_reason IS NULL
                        -- 做幂等守卫的话，downgrade → upgrade 会重复追加。
                        WHEN COALESCE(notes, '') LIKE '%' || :note || '%' THEN notes
                        WHEN COALESCE(notes, '') = '' THEN :note
                        ELSE notes || '; ' || :note
                    END
                WHERE broker = :broker
                  AND business_name = :business_name
                  AND transaction_id IS NULL
                  AND corporate_action_id IS NULL
                  AND cash_event_id IS NULL
                  AND skip_reason IS NULL
                """
            ),
            {
                "marker": UNATTRIBUTED_TAX,
                "broker": broker,
                "business_name": business_name,
                "note": BACKFILL_NOTE,
            },
        )


def downgrade() -> None:
    # 列一并删除，回填标记随之消失，无需单独回滚 UPDATE
    op.drop_index("ix_broker_fund_flows_skip_reason", table_name="broker_fund_flows")
    op.drop_column("broker_fund_flows", "skip_reason")
