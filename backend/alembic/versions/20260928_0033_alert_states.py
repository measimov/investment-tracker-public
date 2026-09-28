"""系统告警状态表 alert_states（告警通知：Bark / Apprise）

Revision ID: 20260928_0033
Revises: 20260928_0032
Create Date: 2026-09-28

每个告警键一行，记录 active/resolved 状态、首次/最近触发、最近推送与推送次数；
状态机见 services/alert_service.py。全局表（不属于任何用户），无存量数据需要回填。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260928_0033"
down_revision: Union[str, None] = "20260928_0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "alert_states",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("alert_key", sa.String(length=200), nullable=False, comment="告警身份键"),
        sa.Column(
            "source",
            sa.String(length=50),
            nullable=False,
            comment="产生它的检查器；某检查器本轮未再触发的活动告警才会被判恢复",
        ),
        sa.Column(
            "severity", sa.String(length=20), nullable=False, comment="info / warning / critical"
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'active'"),
            nullable=False,
            comment="active / resolved",
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="本次事件首次触发时间（恢复后再触发会重置）",
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_notified_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="最近一次推送成功的时间",
        ),
        sa.Column(
            "notify_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="本次事件推送成功的次数",
        ),
        sa.Column(
            "notified_severity",
            sa.String(length=20),
            nullable=True,
            comment="最近一次推送成功时的严重度：升级推送失败时据此在下一轮重试（不等提醒间隔）",
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "resolve_notified_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="「已恢复」推送成功的时间；推送过的告警恢复后为空 = 待重试的恢复通知",
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment="检查器给出的明细 + 最近一次推送结果",
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'critical')", name="ck_alert_states_severity"
        ),
        sa.CheckConstraint("status IN ('active', 'resolved')", name="ck_alert_states_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("alert_key"),
    )
    op.create_index(
        "ix_alert_states_status_last_seen",
        "alert_states",
        ["status", "last_seen_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_alert_states_status_last_seen", table_name="alert_states")
    op.drop_table("alert_states")
