"""雪球发言采集器收纳：五张 xueqiu_archiver_* 表纳入 Alembic + 作者名单 + 进程状态

Revision ID: 20260927_0024
Revises: 20260927_0023
Create Date: 2026-09-27

- 五张 `xueqiu_archiver_*` 表与两个索引：DDL 与 xueqiu-timeline-archiver
  `scan_user_replies.ensure_database_schema` **逐字一致**，全部 `IF NOT EXISTS`——
  生产库里它们早已存在且有存量数据，这一步是零改动；新库（测试/公开部署）则新建。
- `xueqiu_archiver_scan_runs` 追加 status / error_message / waf_hit / utterance_count
  （`ADD COLUMN IF NOT EXISTS` + 默认值，只增不改；原表从未写入过）。
- 新表 `xueqiu_collector_authors`（关注作者名单，取代 config/monitor_users.txt）与
  `xueqiu_collector_state`（单行：心跳、上一轮结果、WAF 冷却、立即运行请求）。

downgrade 只撤销新表与追加列，**不删**五张原有表：它们在生产上先于本迁移存在，
回退迁移不能顺手删掉存量采集数据。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260927_0024"
down_revision: Union[str, None] = "20260927_0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 公开的雪球用户 ID 与昵称（不是私人账本数据）。
# **公开镜像同步时必须把这个列表置空**（PUBLICATION 规则：不带个人关注名单）。
SEED_AUTHORS: tuple[tuple[str, str], ...] = ()

# ---- 以下 DDL 逐字取自 xueqiu-timeline-archiver（勿改：生产表由它建出） ----
ARCHIVER_DDL = (
    """
            create table if not exists xueqiu_archiver_posts (
                post_id text primary key,
                url text not null,
                title text not null default '',
                author_id text not null default '',
                author_name text not null default '',
                text text not null default '',
                created_at text not null default '',
                created_at_ms bigint not null default 0,
                source text not null default '',
                detail_enriched boolean not null default false,
                first_seen_at timestamptz not null default now(),
                last_seen_at timestamptz not null default now(),
                last_detail_fetch_at timestamptz
            )
            """,
    """
            create table if not exists xueqiu_archiver_replies (
                reply_key text primary key,
                target_user_id text not null,
                post_id text not null references xueqiu_archiver_posts(post_id) on delete cascade,
                post_url text not null,
                comment_id text not null default '',
                created_at text not null default '',
                author_id text not null default '',
                author_name text not null default '',
                text text not null default '',
                like_count integer not null default 0,
                created_at_ms bigint not null default 0,
                reply_to text not null default '',
                first_seen_at timestamptz not null default now(),
                last_seen_at timestamptz not null default now()
            )
            """,
    "create index if not exists idx_xar_replies_target_created on xueqiu_archiver_replies(target_user_id, created_at_ms desc)",
    "create index if not exists idx_xar_replies_post on xueqiu_archiver_replies(post_id)",
    """
            create table if not exists xueqiu_archiver_post_scan_state (
                target_user_id text not null,
                post_id text not null references xueqiu_archiver_posts(post_id) on delete cascade,
                last_scanned_at timestamptz,
                last_max_page integer not null default 0,
                last_checked_page_count integer not null default 0,
                last_waf_at timestamptz,
                primary key (target_user_id, post_id)
            )
            """,
    """
            create table if not exists xueqiu_archiver_scan_runs (
                run_id bigserial primary key,
                target_user_id text not null,
                author_user_id text,
                started_at timestamptz not null default now(),
                finished_at timestamptz,
                candidate_count integer not null default 0,
                reply_count integer not null default 0,
                stopped_early boolean not null default false
            )
            """,
    """
            create table if not exists xueqiu_archiver_utterances (
                utterance_key text primary key,
                target_user_id text not null,
                source text not null,
                source_id text not null default '',
                kind text not null,
                post_id text not null default '',
                post_url text not null default '',
                created_at text not null default '',
                created_at_ms bigint not null default 0,
                author_id text not null default '',
                author_name text not null default '',
                text text not null default '',
                context_post_id text not null default '',
                context_url text not null default '',
                context_author_name text not null default '',
                context_text text not null default '',
                first_seen_at timestamptz not null default now(),
                last_seen_at timestamptz not null default now()
            )
            """,
    "create index if not exists idx_xar_utterances_target_created on xueqiu_archiver_utterances(target_user_id, created_at_ms desc)",
)
# ---- 原 DDL 结束 ----

SCAN_RUN_ADDED_COLUMNS = (
    ("status", "text not null default ''"),
    ("error_message", "text not null default ''"),
    ("waf_hit", "boolean not null default false"),
    ("utterance_count", "integer not null default 0"),
)


def upgrade() -> None:
    for statement in ARCHIVER_DDL:
        op.execute(statement)
    for name, ddl in SCAN_RUN_ADDED_COLUMNS:
        op.execute(f"ALTER TABLE xueqiu_archiver_scan_runs ADD COLUMN IF NOT EXISTS {name} {ddl}")

    op.create_table(
        "xueqiu_collector_authors",
        sa.Column("xueqiu_user_id", sa.Text(), nullable=False, comment="雪球用户数字 ID"),
        sa.Column(
            "display_name",
            sa.Text(),
            server_default=sa.text("''"),
            nullable=False,
            comment="展示名",
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("note", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_run_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="最近一次采集结束时间",
        ),
        sa.Column(
            "last_status",
            sa.Text(),
            server_default=sa.text("''"),
            nullable=False,
            comment="ok / failed / waf / interrupted",
        ),
        sa.Column("last_message", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.CheckConstraint(
            "xueqiu_user_id ~ '^[0-9]{1,20}$'", name="ck_xueqiu_collector_authors_user_id"
        ),
        sa.PrimaryKeyConstraint("xueqiu_user_id"),
    )
    op.create_table(
        "xueqiu_collector_state",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_cycle_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_cycle_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_cycle_status", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("last_cycle_message", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("last_waf_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_xueqiu_collector_state_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("INSERT INTO xueqiu_collector_state (id) VALUES (1) ON CONFLICT DO NOTHING")

    authors = sa.table(
        "xueqiu_collector_authors",
        sa.column("xueqiu_user_id", sa.Text()),
        sa.column("display_name", sa.Text()),
    )
    if SEED_AUTHORS:
        op.bulk_insert(
            authors,
            [{"xueqiu_user_id": uid, "display_name": name} for uid, name in SEED_AUTHORS],
        )


def downgrade() -> None:
    op.drop_table("xueqiu_collector_state")
    op.drop_table("xueqiu_collector_authors")
    for name, _ in reversed(SCAN_RUN_ADDED_COLUMNS):
        op.execute(f"ALTER TABLE xueqiu_archiver_scan_runs DROP COLUMN IF EXISTS {name}")
