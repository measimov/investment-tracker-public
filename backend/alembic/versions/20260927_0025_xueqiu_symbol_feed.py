"""雪球按标的监控落库：公告/讨论、热帖快照、组合调仓、组合名单 + 每日轮次状态

Revision ID: 20260927_0025
Revises: 20260927_0024
Create Date: 2026-09-27

原仓库 xueqiu-timeline-archiver 的 monitor_symbols 只写 Markdown；收纳后改为落库：
- `xueqiu_symbol_posts`：身份键是本仓 (symbol, market)，**不存雪球 symbol**；
  唯一键 (symbol, market, kind, post_id)——同一讨论帖提及两只被跟踪标的时各存一行。
- `xueqiu_hot_posts`：(scope, post_id) 唯一，rank/snapshot_at 为最近一次上榜。
- `xueqiu_cube_rebalancing`：(cube_id, rebalancing_id) 唯一。
- `xueqiu_collector_cubes`：组合跟踪名单（作者名单的兄弟表），空表起步。
- `xueqiu_collector_state` 追加每日按标的轮次的状态列（与作者轮次分开记）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260927_0025"
down_revision: Union[str, None] = "20260927_0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMPTY_JSONB = sa.text("'{}'::jsonb")


def _seen_columns():
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "xueqiu_symbol_posts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "symbol",
            sa.String(length=20),
            nullable=False,
            comment="本仓标的代码（裸码），不是雪球 symbol",
        ),
        sa.Column(
            "market",
            sa.String(length=20),
            nullable=False,
            comment="本仓市场（A股/B股/港股/美股）",
        ),
        sa.Column(
            "kind",
            sa.String(length=20),
            nullable=False,
            comment="announcement=公告 / discussion=讨论",
        ),
        sa.Column("post_id", sa.Text(), nullable=False, comment="雪球帖子 ID"),
        sa.Column(
            "created_at_ms",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
            comment="发帖时间（毫秒）",
        ),
        sa.Column("title", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "text", sa.Text(), server_default=sa.text("''"), nullable=False, comment="纯文本正文"
        ),
        sa.Column("author_id", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("author_name", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "url", sa.Text(), server_default=sa.text("''"), nullable=False, comment="雪球原帖链接"
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=EMPTY_JSONB,
            nullable=False,
            comment="裁剪后的原始字段（互动计数、公告附件链接等）",
        ),
        *_seen_columns(),
        sa.CheckConstraint(
            "kind IN ('announcement', 'discussion')", name="ck_xueqiu_symbol_posts_kind"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "symbol", "market", "kind", "post_id", name="uq_xueqiu_symbol_posts_identity"
        ),
    )
    op.create_index(
        "ix_xueqiu_symbol_posts_feed",
        "xueqiu_symbol_posts",
        ["symbol", "market", "kind", sa.text("created_at_ms DESC")],
    )

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
            server_default=EMPTY_JSONB,
            nullable=False,
        ),
        *_seen_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope", "post_id", name="uq_xueqiu_hot_posts_scope_post"),
    )
    op.create_index(
        "ix_xueqiu_hot_posts_snapshot",
        "xueqiu_hot_posts",
        ["scope", sa.text("snapshot_at DESC"), "rank"],
    )

    op.create_table(
        "xueqiu_cube_rebalancing",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("cube_id", sa.String(length=32), nullable=False, comment="组合代号（ZH 开头）"),
        sa.Column("rebalancing_id", sa.Text(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=EMPTY_JSONB,
            nullable=False,
        ),
        *_seen_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cube_id", "rebalancing_id", name="uq_xueqiu_cube_rebalancing_identity"
        ),
    )
    op.create_index(
        "ix_xueqiu_cube_rebalancing_cube_created",
        "xueqiu_cube_rebalancing",
        ["cube_id", sa.text("created_at_ms DESC")],
    )

    op.create_table(
        "xueqiu_collector_cubes",
        sa.Column("cube_id", sa.String(length=32), nullable=False, comment="组合代号，如 ZH000001"),
        sa.Column("display_name", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("note", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("last_message", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.CheckConstraint(
            "cube_id ~ '^[A-Z]{2}[0-9]{1,20}$'", name="ck_xueqiu_collector_cubes_id"
        ),
        sa.PrimaryKeyConstraint("cube_id"),
    )

    op.add_column(
        "xueqiu_collector_state",
        sa.Column(
            "symbols_run_requested_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="管理员请求立即跑一轮按标的采集",
        ),
    )
    op.add_column(
        "xueqiu_collector_state",
        sa.Column("symbols_last_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "xueqiu_collector_state",
        sa.Column("symbols_last_finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "xueqiu_collector_state",
        sa.Column("symbols_last_status", sa.Text(), server_default=sa.text("''"), nullable=False),
    )
    op.add_column(
        "xueqiu_collector_state",
        sa.Column("symbols_last_message", sa.Text(), server_default=sa.text("''"), nullable=False),
    )
    op.add_column(
        "xueqiu_collector_state",
        sa.Column(
            "symbols_last_business_date",
            sa.Date(),
            nullable=True,
            comment="上一次每日一轮所属的业务日（同一业务日不重跑）",
        ),
    )
    op.add_column(
        "xueqiu_collector_state",
        sa.Column(
            "symbols_last_stats",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=EMPTY_JSONB,
            nullable=False,
            comment="上一轮计数",
        ),
    )
    op.add_column(
        "xueqiu_collector_state",
        sa.Column(
            "symbols_pending",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="当日待重试：{date, attempts, items|null}；items=null 表示整轮重跑",
        ),
    )


def downgrade() -> None:
    for column in (
        "symbols_pending",
        "symbols_last_stats",
        "symbols_last_business_date",
        "symbols_last_message",
        "symbols_last_status",
        "symbols_last_finished_at",
        "symbols_last_started_at",
        "symbols_run_requested_at",
    ):
        op.drop_column("xueqiu_collector_state", column)
    op.drop_table("xueqiu_collector_cubes")
    op.drop_index("ix_xueqiu_cube_rebalancing_cube_created", table_name="xueqiu_cube_rebalancing")
    op.drop_table("xueqiu_cube_rebalancing")
    op.drop_index("ix_xueqiu_hot_posts_snapshot", table_name="xueqiu_hot_posts")
    op.drop_table("xueqiu_hot_posts")
    op.drop_index("ix_xueqiu_symbol_posts_feed", table_name="xueqiu_symbol_posts")
    op.drop_table("xueqiu_symbol_posts")
