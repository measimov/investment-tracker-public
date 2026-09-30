"""删除 xueqiu_hot_posts（市场热帖快照，#282）

Revision ID: 20260929_0035
Revises: 20260928_0034
Create Date: 2026-09-29

热帖采集与展示已于 2026-09-28 下线，表在 app/ 里再无读取方，只剩旧 Markdown 导入写入。
这里连同存量快照一并删除；旧 Markdown 导入改为跳过 `market-hots-*` 文件。
downgrade 按迁移 0025 的原 DDL 重建空表（数据不恢复）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260929_0035"
down_revision: Union[str, None] = "20260928_0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_xueqiu_hot_posts_snapshot", table_name="xueqiu_hot_posts")
    op.drop_table("xueqiu_hot_posts")


def downgrade() -> None:
    op.create_table(
        "xueqiu_hot_posts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False, comment="热帖口径（day / week）"),
        sa.Column("post_id", sa.Text(), nullable=False),
        sa.Column(
            "rank",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="快照内名次（1 起）",
        ),
        sa.Column(
            "snapshot_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="最近一次出现在热帖榜上的快照时间",
        ),
        sa.Column("created_at_ms", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("title", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("text", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("author_id", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("author_name", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("url", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope", "post_id", name="uq_xueqiu_hot_posts_scope_post"),
    )
    op.create_index(
        "ix_xueqiu_hot_posts_snapshot",
        "xueqiu_hot_posts",
        ["scope", sa.text("snapshot_at DESC"), "rank"],
    )
