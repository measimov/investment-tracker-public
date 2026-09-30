"""官方公告的读取侧（#306 后半）：把 security_announcements 的文件行按 group_key 合成「事件」，
供 API（详情页时间线、持仓/自选徽标）、重大公告推送与分析输入共用——组的口径只定义一处。

组 = 同一标的、同一公告日、同一类别的全部文件；组的重要性取组内最高，代表标题按
`representative_index`（预案/方案 > 提示性公告 > 论证报告 > 其余）。
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from ..models.holding import Holding
from ..models.security_announcement import SecurityAnnouncement
from ..models.user import User
from ..models.watchlist_item import WatchlistItem
from .announcement_classifier import CATEGORIES, IMPORTANCE_ORDER, representative_index

Key = Tuple[str, str]
IMPORTANCE_LEVELS = {
    "major": ("major",),
    "normal": ("major", "normal"),
    "all": ("major", "normal", "minor"),
}


def _group_rows(rows: Sequence[SecurityAnnouncement]) -> List[Dict[str, Any]]:
    """已按 published_at 倒序的文件行 → 组（保持首个文件出现的顺序，即组按最新文件倒序）。"""
    buckets: "OrderedDict[str, List[SecurityAnnouncement]]" = OrderedDict()
    for row in rows:
        buckets.setdefault(row.group_key, []).append(row)
    groups = []
    for key, items in buckets.items():
        # 代表标题同级取最早发布（与 classifier 约定一致）：items 是倒序，按升序传入
        # ——否则全落在「其余」档的组会被后发的补充/更正公告当代表（PR #311 评审 P3-3）
        ascending = sorted(items, key=lambda item: (item.published_at, item.id))
        rep = ascending[representative_index([item.title for item in ascending])]
        importance = max(
            (item.importance for item in items), key=lambda level: IMPORTANCE_ORDER[level]
        )
        groups.append(
            {
                "group_key": key,
                "symbol": rep.symbol,
                "market": rep.market,
                "ann_date": rep.ann_date,
                "category": rep.category,
                "category_label": CATEGORIES.get(rep.category, rep.category),
                "importance": importance,
                "title": rep.title,
                "url": rep.url,
                "source": rep.source,
                "document_count": len(items),
                "first_seen_at": min(item.first_seen_at for item in items),
                "latest_published_at": max(item.published_at for item in items),
                "sec_name": (rep.payload or {}).get("sec_name")
                or (rep.payload or {}).get("stock_name"),
                "documents": [
                    {
                        "title": item.title,
                        "url": item.url,
                        "published_at": item.published_at,
                        "importance": item.importance,
                        "category": item.category,
                    }
                    for item in items
                ],
            }
        )
    return groups


def _select_group_keys(
    db: Session,
    *,
    keys: Optional[List[Key]],
    since: Optional[date],
    before: Optional[date],
    levels: Tuple[str, ...],
    category: Optional[str],
    limit: int,
    complete_days: bool,
) -> Tuple[List[str], bool]:
    base = db.query(SecurityAnnouncement.group_key, SecurityAnnouncement.ann_date)
    if keys is not None:
        base = base.filter(
            tuple_(SecurityAnnouncement.symbol, SecurityAnnouncement.market).in_(keys)
        )
    if since is not None:
        base = base.filter(SecurityAnnouncement.ann_date >= since)
    if before is not None:
        base = base.filter(SecurityAnnouncement.ann_date < before)
    if category:
        base = base.filter(SecurityAnnouncement.category == category)
    base = base.filter(SecurityAnnouncement.importance.in_(levels))
    # ann_date 是 published_at 的业务时区日期，按 published_at 倒序即按公告日倒序
    ordered: List[str] = []
    seen = set()
    last_day: Optional[date] = None
    rows = base.order_by(
        SecurityAnnouncement.published_at.desc(), SecurityAnnouncement.id.desc()
    ).all()
    for group_key, ann_date in rows:
        if group_key in seen:
            continue
        if len(ordered) >= limit and (not complete_days or ann_date != last_day):
            return ordered, True
        seen.add(group_key)
        ordered.append(group_key)
        last_day = ann_date
    return ordered, False


def load_groups_page(
    db: Session,
    *,
    keys: Optional[Iterable[Key]] = None,
    since: Optional[date] = None,
    before: Optional[date] = None,
    importance: str = "all",
    category: Optional[str] = None,
    limit: int = 50,
    complete_days: bool = False,
) -> Tuple[List[Dict[str, Any]], bool]:
    """按条件取组，返回 (组列表, 是否还有更多)。

    重要性按「组内最高」判定：先取命中级别的文件所在的组，再取这些组的**全部**文件合成，
    避免只拿到组内一部分文件。`complete_days=True` 时到达 limit 后把同一公告日的组取完——
    翻页用 `before=最后一组的公告日`，不会漏掉被截在半天里的组。"""
    levels = IMPORTANCE_LEVELS.get(importance, IMPORTANCE_LEVELS["all"])
    key_list = list(keys) if keys is not None else None
    if key_list is not None and not key_list:
        return [], False
    ordered_keys, has_more = _select_group_keys(
        db,
        keys=key_list,
        since=since,
        before=before,
        levels=levels,
        category=category,
        limit=limit,
        complete_days=complete_days,
    )
    if not ordered_keys:
        return [], False
    rows = (
        db.query(SecurityAnnouncement)
        .filter(SecurityAnnouncement.group_key.in_(ordered_keys))
        .order_by(SecurityAnnouncement.published_at.desc(), SecurityAnnouncement.id.desc())
        .all()
    )
    groups = {group["group_key"]: group for group in _group_rows(rows)}
    return [groups[key] for key in ordered_keys if key in groups], has_more


def load_groups(db: Session, **kwargs: Any) -> List[Dict[str, Any]]:
    return load_groups_page(db, **kwargs)[0]


def user_scope(db: Session, user_id: Optional[int] = None) -> Dict[int, Dict[Key, Optional[str]]]:
    """活跃用户 → {(symbol, market): 用户录入的名称}：持仓（数量>0）∪ 自选。"""
    scope: Dict[int, Dict[Key, Optional[str]]] = {}
    holdings = (
        db.query(Holding.user_id, Holding.symbol, Holding.market, Holding.name)
        .join(User, User.id == Holding.user_id)
        .filter(User.is_active.is_(True), Holding.quantity > 0)
    )
    watch = (
        db.query(
            WatchlistItem.user_id, WatchlistItem.symbol, WatchlistItem.market, WatchlistItem.name
        )
        .join(User, User.id == WatchlistItem.user_id)
        .filter(User.is_active.is_(True))
    )
    if user_id is not None:
        holdings = holdings.filter(Holding.user_id == user_id)
        watch = watch.filter(WatchlistItem.user_id == user_id)
    for uid, symbol, market, name in list(holdings.all()) + list(watch.all()):
        entries = scope.setdefault(uid, {})
        if not entries.get((symbol, market)):
            entries[(symbol, market)] = name
    return scope


def compact_for_analysis(groups: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """分析输入用的精简组：只给日期、类别、重要性、代表标题与文件数（标题是交易所原文）。"""
    return [
        {
            "date": group["ann_date"].isoformat()
            if isinstance(group["ann_date"], date)
            else group["ann_date"],
            "category": group["category_label"],
            "importance": group["importance"],
            "title": group["title"],
            "documents": group["document_count"],
        }
        for group in groups
    ]
