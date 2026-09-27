"""标的分析：风险等级按市场下限上调的记录列

Revision ID: 20260927_0026
Revises: 20260927_0025
Create Date: 2026-09-27

港股 risk_level 低于下限（medium）时此前整份分析被拒绝（02313 连续两次失败、重试结果
相同）；改为上调并记录 {from, to, reason}，前端在风险标签旁提示。未上调为 NULL，存量行
不回填。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260927_0026"
down_revision: Union[str, None] = "20260927_0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "security_analyses",
        sa.Column(
            "risk_level_adjusted",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="风险等级按市场下限上调的记录 {from,to,reason}；未上调为 NULL",
        ),
    )


def downgrade() -> None:
    op.drop_column("security_analyses", "risk_level_adjusted")
