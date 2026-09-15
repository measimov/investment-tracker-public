"""security_opinion_summaries：雪球观点摘要（全局追加产物）

Revision ID: 20260816_0016
Revises: 20260814_0015
Create Date: 2026-08-31

输入来自 xueqiu-timeline-archiver 写入同库的 xueqiu_archiver_utterances 表，
本应用**只读**该表——本迁移不建也不引用它（表不存在是合法形态，运行时显式
降级）。本表只承载 LLM 产物，克隆 security_analyses 的追加式语义。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260816_0016"
down_revision: Union[str, None] = "20260814_0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "security_opinion_summaries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False, comment="股票代码"),
        sa.Column("market", sa.String(length=20), nullable=False, comment="市场"),
        sa.Column("name", sa.String(length=100), nullable=True, comment="资产名称（公共元数据，可空）"),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=False, comment="观点标签数组（白名单）"),
        sa.Column(
            "author_stances", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            comment="逐作者立场 [{author, stance, recent_change, evidence}]",
        ),
        sa.Column("summary", sa.String(length=300), nullable=False, comment="一句话观点概括"),
        sa.Column("content", sa.Text(), nullable=False, comment="Markdown 全文"),
        sa.Column("model", sa.String(length=50), nullable=False, comment="生成模型"),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "input_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            comment="生成时的压缩输入（可复现）",
        ),
        sa.Column("recent_days", sa.Integer(), nullable=False, comment="近期窗口天数（生成时值）"),
        sa.Column("lookback_days", sa.Integer(), nullable=False, comment="回看深度天数（生成时值）"),
        sa.Column("utterance_count", sa.Integer(), nullable=False, comment="纳入的发言总数"),
        sa.Column("recent_utterance_count", sa.Integer(), nullable=False, comment="其中近期窗口内条数"),
        sa.Column("latest_utterance_at", sa.DateTime(timezone=True), nullable=True, comment="纳入的最新发言时间"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_security_opinion_summaries_id"), "security_opinion_summaries", ["id"], unique=False
    )
    op.create_index(
        op.f("ix_security_opinion_summaries_symbol"),
        "security_opinion_summaries", ["symbol"], unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_security_opinion_summaries_symbol"), table_name="security_opinion_summaries")
    op.drop_index(op.f("ix_security_opinion_summaries_id"), table_name="security_opinion_summaries")
    op.drop_table("security_opinion_summaries")
