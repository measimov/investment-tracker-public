"""数据自动刷新：scheduled_task_state、notification_events、自选现价与加入价列

Revision ID: 20260928_0034
Revises: 20260928_0033
Create Date: 2026-09-28

- scheduled_task_state：周期任务的持久化运行状态（重启不重跑每周/每日任务）。
- notification_events：一次性事件提醒（新分红建议、除净日临近、价格异动）的去重与发送记录。
- watchlist_items：行情日期/来源与「加入以来涨跌幅」的基准价。存量条目的基准价由
  `manage.py backfill-watchlist-added-price` 按加入日收盘补，迁移本身不回填。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260928_0034"
down_revision: Union[str, None] = "20260928_0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scheduled_task_state",
        sa.Column(
            "name",
            sa.String(length=100),
            nullable=False,
            comment="任务名（与周期任务注册名一致）",
        ),
        sa.Column(
            "last_run_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="最近一次运行（含失败）的时间",
        ),
        sa.Column(
            "last_success_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="最近一次成功的时间",
        ),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment="任务自定的明细，如已处理到的交易日",
        ),
        sa.PrimaryKeyConstraint("name"),
    )

    op.create_table(
        "notification_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_key", sa.String(length=200), nullable=False, comment="事件身份键（去重）"),
        sa.Column(
            "kind",
            sa.String(length=40),
            nullable=False,
            comment="dividend_suggestion / ex_date / price_move",
        ),
        sa.Column("user_id", sa.Integer(), nullable=True, comment="所属用户"),
        sa.Column("title", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("message", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'pending'"),
            nullable=False,
            comment="pending 待发送 / sent / failed 重试用尽 / skipped 未配置渠道或已关闭",
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'skipped')",
            name="ck_notification_events_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_key"),
    )
    op.create_index(
        "ix_notification_events_status_created",
        "notification_events",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_events_user_id"),
        "notification_events",
        ["user_id"],
        unique=False,
    )

    op.add_column(
        "watchlist_items",
        sa.Column(
            "price_as_of",
            sa.Date(),
            nullable=True,
            comment="行情所属交易日（报价源拿不到日期时为空）",
        ),
    )
    op.add_column(
        "watchlist_items",
        sa.Column("price_source", sa.String(length=40), nullable=True, comment="报价来源"),
    )
    op.add_column(
        "watchlist_items",
        sa.Column(
            "added_price", sa.Numeric(20, 8), nullable=True, comment="加入时价格（涨跌幅基准）"
        ),
    )
    op.add_column(
        "watchlist_items",
        sa.Column("added_price_date", sa.Date(), nullable=True, comment="基准价所属交易日"),
    )
    op.add_column(
        "watchlist_items",
        sa.Column(
            "added_price_basis",
            sa.String(length=20),
            nullable=True,
            comment="quote 加入时报价 / close_on_add 加入日收盘 / first_quote 加入后首次报价",
        ),
    )


def downgrade() -> None:
    for column in (
        "added_price_basis",
        "added_price_date",
        "added_price",
        "price_source",
        "price_as_of",
    ):
        op.drop_column("watchlist_items", column)
    op.drop_index(op.f("ix_notification_events_user_id"), table_name="notification_events")
    op.drop_index("ix_notification_events_status_created", table_name="notification_events")
    op.drop_table("notification_events")
    op.drop_table("scheduled_task_state")
