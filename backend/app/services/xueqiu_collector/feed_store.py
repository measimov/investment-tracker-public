"""按标的监控的落库与读取：公告/讨论、热帖快照、组合调仓（迁移 0025 的三张表）。

写入全部是按唯一键的幂等 upsert（重跑同一天不产生重复行，只刷新 last_seen_at 与
可变字段）；新文本为空时保留库里已有的非空值——接口偶发返回空正文不该抹掉旧内容。
读取供 API 使用（标的详情「雪球公告 / 讨论」）。热帖快照的采集与展示已于 2026-09-28
下线，`upsert_hot_posts` 只剩旧 Markdown 导入（`archive_import`）在用，存量行保留。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ...models.xueqiu_collector import (
    XueqiuCubeRebalancing,
    XueqiuHotPost,
    XueqiuSymbolPost,
)
from .feed_parsing import FeedPost, RebalancingRecord

TEXT_FIELDS = ("title", "text", "author_id", "author_name", "url")


def _keep_nonempty(table, stmt, name: str):
    """新值为空串时保留旧值。"""
    return func.coalesce(func.nullif(getattr(stmt.excluded, name), ""), getattr(table, name))


def upsert_symbol_posts(
    db: Session, symbol: str, market: str, kind: str, posts: Sequence[FeedPost],
    *, overwrite: bool = True,
) -> Tuple[int, int]:
    """幂等写入一只标的某一类的帖子；返回 (新增, 已存在)。不 commit。

    symbol / market 必须是本仓身份键（调用方从持仓/自选取来），这里不做雪球格式转换。
    """
    unique: Dict[str, FeedPost] = {}
    for post in posts:
        unique.setdefault(post.post_id, post)
    if not unique:
        return 0, 0
    existing = {
        row[0]
        for row in db.query(XueqiuSymbolPost.post_id).filter(
            XueqiuSymbolPost.symbol == symbol,
            XueqiuSymbolPost.market == market,
            XueqiuSymbolPost.kind == kind,
            XueqiuSymbolPost.post_id.in_(list(unique)),
        )
    }
    table = XueqiuSymbolPost.__table__
    stmt = insert(table).values([
        {
            "symbol": symbol,
            "market": market,
            "kind": kind,
            "post_id": post.post_id,
            "created_at_ms": post.created_at_ms,
            "title": post.title,
            "text": post.text,
            "author_id": post.author_id,
            "author_name": post.author_name,
            "url": post.url,
            "payload": post.payload,
        }
        for post in unique.values()
    ])
    update = {name: _keep_nonempty(table.c, stmt, name) for name in TEXT_FIELDS}
    update.update({
        "created_at_ms": func.greatest(stmt.excluded.created_at_ms, table.c.created_at_ms),
        "payload": stmt.excluded.payload,
        "last_seen_at": func.now(),
    })
    if not overwrite:  # 旧产物导入：已有行（采集器写的更完整）一律不动
        db.execute(stmt.on_conflict_do_nothing(constraint="uq_xueqiu_symbol_posts_identity"))
        return len(unique) - len(existing), len(existing)
    db.execute(
        stmt.on_conflict_do_update(constraint="uq_xueqiu_symbol_posts_identity", set_=update)
    )
    return len(unique) - len(existing), len(existing)


def upsert_hot_posts(
    db: Session, scope: str, posts: Sequence[FeedPost], snapshot_at: datetime,
    *, overwrite: bool = True,
) -> Tuple[int, int]:
    """写入一张热帖快照：名次按列表顺序（1 起），同帖取首次出现的名次。不 commit。"""
    ranked: Dict[str, Tuple[int, FeedPost]] = {}
    for index, post in enumerate(posts, start=1):
        ranked.setdefault(post.post_id, (index, post))
    if not ranked:
        return 0, 0
    existing = {
        row[0]
        for row in db.query(XueqiuHotPost.post_id).filter(
            XueqiuHotPost.scope == scope, XueqiuHotPost.post_id.in_(list(ranked))
        )
    }
    table = XueqiuHotPost.__table__
    stmt = insert(table).values([
        {
            "scope": scope,
            "post_id": post.post_id,
            "rank": rank,
            "snapshot_at": snapshot_at,
            "created_at_ms": post.created_at_ms,
            "title": post.title,
            "text": post.text,
            "author_id": post.author_id,
            "author_name": post.author_name,
            "url": post.url,
            "payload": post.payload,
        }
        for rank, post in ranked.values()
    ])
    update = {name: _keep_nonempty(table.c, stmt, name) for name in TEXT_FIELDS}
    update.update({
        "rank": stmt.excluded.rank,
        "snapshot_at": stmt.excluded.snapshot_at,
        "created_at_ms": func.greatest(stmt.excluded.created_at_ms, table.c.created_at_ms),
        "payload": stmt.excluded.payload,
        "last_seen_at": func.now(),
    })
    if not overwrite:
        db.execute(stmt.on_conflict_do_nothing(constraint="uq_xueqiu_hot_posts_scope_post"))
        return len(ranked) - len(existing), len(existing)
    db.execute(
        stmt.on_conflict_do_update(constraint="uq_xueqiu_hot_posts_scope_post", set_=update)
    )
    return len(ranked) - len(existing), len(existing)


def upsert_rebalancing(
    db: Session, cube_id: str, records: Sequence[RebalancingRecord]
) -> Tuple[int, int]:
    unique: Dict[str, RebalancingRecord] = {}
    for record in records:
        unique.setdefault(record.rebalancing_id, record)
    if not unique:
        return 0, 0
    existing = {
        row[0]
        for row in db.query(XueqiuCubeRebalancing.rebalancing_id).filter(
            XueqiuCubeRebalancing.cube_id == cube_id,
            XueqiuCubeRebalancing.rebalancing_id.in_(list(unique)),
        )
    }
    table = XueqiuCubeRebalancing.__table__
    stmt = insert(table).values([
        {
            "cube_id": cube_id,
            "rebalancing_id": record.rebalancing_id,
            "created_at_ms": record.created_at_ms,
            "payload": record.payload,
        }
        for record in unique.values()
    ])
    db.execute(
        stmt.on_conflict_do_update(
            constraint="uq_xueqiu_cube_rebalancing_identity",
            set_={
                "created_at_ms": stmt.excluded.created_at_ms,
                "payload": stmt.excluded.payload,
                "last_seen_at": func.now(),
            },
        )
    )
    return len(unique) - len(existing), len(existing)


# --------------------------------------------------------------------------- #
# 读取（API）
# --------------------------------------------------------------------------- #
def post_to_dict(row: Any) -> Dict[str, Any]:
    payload = row.payload or {}
    return {
        "post_id": row.post_id,
        "created_at_ms": int(row.created_at_ms or 0),
        "title": row.title,
        "text": row.text,
        "author_id": row.author_id,
        "author_name": row.author_name,
        "url": row.url,
        "reply_count": _int_or_none(payload.get("reply_count")),
        "like_count": _int_or_none(payload.get("like_count")),
        "links": [str(link) for link in payload.get("links") or []],
        "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at,
    }


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def symbol_feed(
    db: Session, symbol: str, market: str, kinds: Iterable[str], limit: int
) -> Dict[str, List[Dict[str, Any]]]:
    """每类各取最新 limit 条（按发帖时间倒序）。"""
    result: Dict[str, List[Dict[str, Any]]] = {}
    for kind in kinds:
        rows = (
            db.query(XueqiuSymbolPost)
            .filter(
                XueqiuSymbolPost.symbol == symbol,
                XueqiuSymbolPost.market == market,
                XueqiuSymbolPost.kind == kind,
            )
            .order_by(XueqiuSymbolPost.created_at_ms.desc(), XueqiuSymbolPost.id.desc())
            .limit(limit)
            .all()
        )
        result[kind] = [post_to_dict(row) for row in rows]
    return result
