"""持仓现价的行情日期与来源（#217）：holdings.price_as_of / holdings.price_source

price_updated_at 是「写库时刻」而不是「行情所属交易日」：周六刷新写的是 9/26，
而 A 股收盘价实为 9/25；手工改价与接口刷新也无从区分。两列都可空、无默认值：

- price_as_of（DATE）：报价源给出的行情所属交易日（Tushare trade_date、腾讯/雪球
  行情时间）；拿不到或手工改价时为 NULL，前端退回显示「刷新于 …」。
- price_source（VARCHAR(40)）：报价源标识（tencent-quote / tushare-daily /
  xueqiu-quote … / manual）；存量行为 NULL（来源未知）。

Revision ID: 20260926_0021
Revises: 20260920_0020
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260926_0021"
down_revision: Union[str, None] = "20260920_0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("holdings", sa.Column("price_as_of", sa.Date(), nullable=True))
    op.add_column("holdings", sa.Column("price_source", sa.String(40), nullable=True))


def downgrade() -> None:
    op.drop_column("holdings", "price_source")
    op.drop_column("holdings", "price_as_of")
