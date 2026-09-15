"""hkex_dayquot_reports：港交所每日行情报表的处理完成标记

Revision ID: 20260903_0017
Revises: 20260816_0016
Create Date: 2026-09-03

"该日已处理" 必须独立于价格行且可持久：跟踪集当天全部停牌 / 全部 N/A /
代码全部未匹配时报表成功解析却写入 0 行，只看价格行会每个 tick 重下同一份
25MB 并在 max_reports 预算内饿死更早日期。已有 hkex-dayquot 价格行的日期在
此回填标记（那些报表确实已处理），避免升级后把最近一个月再下一遍。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260903_0017"
down_revision: Union[str, None] = "20260816_0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "hkex_dayquot_reports",
        sa.Column("report_date", sa.Date(), nullable=False, comment="报表日期（交易日）"),
        sa.Column(
            "processed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("universe_size", sa.Integer(), nullable=False, comment="处理时的跟踪标的数"),
        sa.Column("parsed_count", sa.Integer(), nullable=False, comment="报表解析出的证券行数"),
        sa.Column("stored_count", sa.Integer(), nullable=False, comment="写入 security_prices 的行数"),
        sa.Column(
            "detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            comment="missing / suspended / unpriced / conflicts 明细",
        ),
        sa.PrimaryKeyConstraint("report_date"),
    )
    op.execute(
        """
        INSERT INTO hkex_dayquot_reports
            (report_date, universe_size, parsed_count, stored_count, detail)
        SELECT price_date, COUNT(*), 0, COUNT(*), '{"backfilled": true}'::jsonb
        FROM security_prices
        WHERE market = '港股' AND source = 'hkex-dayquot'
        GROUP BY price_date
        """
    )


def downgrade() -> None:
    op.drop_table("hkex_dayquot_reports")
