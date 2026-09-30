"""标的分析：解析层对模型输出的调整记录列

Revision ID: 20260929_0037
Revises: 20260929_0036
Create Date: 2026-09-29

#287：近义标签、白名单外/本市场禁用标签、超过 4 个标签此前整份拒绝，改为归一/丢弃/截断
并记录；缺免责声明时补上并记录。记录落此列（无调整为 NULL），前端在风险标签旁提示。
存量行不回填。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260929_0037"
down_revision: Union[str, None] = "20260929_0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "security_analyses",
        sa.Column(
            "output_adjustments",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="解析层对模型输出的调整记录（标签归一/丢弃/截断、补免责声明等）；无调整为 NULL",
        ),
    )


def downgrade() -> None:
    op.drop_column("security_analyses", "output_adjustments")
