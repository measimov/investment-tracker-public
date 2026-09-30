"""自选条目「加入以来涨跌幅」基准价的存量回填。

加入 1 小时内取到报价的条目直接用那次报价（`stock_price_service.added_price_basis_for`，
口径 quote）。其余条目（含本功能上线前的存量）按**加入日（业务时区）或之前最近一根日线
收盘**补，口径 close_on_add；那根收盘不得早于加入日 7 天。

找不到时分两种情况：
- 加入日之前的历史还没探过（日线尾部同步会把自选的历史回补到加入日 − 7 天）→ 先不动，
  下一轮再看，避免历史到位前抢先用了更晚的价格；
- 已经探过（`sync_price_tails` 的 head_probed 记录、覆盖起点已早于加入日 − 7 天，或港股等
  不做头部回补的市场）→ 取加入日之后的第一根收盘（close_after_add）；连这也没有，就把
  口径标成 pending_quote，下一次成功报价写入基准并记为 first_quote。

所以每个条目最终都会有基准价，不会永久为空。幂等：只填空值。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..core.timeutil import to_local_date
from ..models.security_price import SecurityPrice
from ..models.watchlist_item import WatchlistItem

CLOSE_ON_ADD_MAX_GAP_DAYS = 7
PENDING_QUOTE = "pending_quote"


def _head_probed_map() -> Dict[str, str]:
    from .price_tail_sync import TAIL_TASK_NAME
    from .scheduled_state import get_detail
    from ..database import SessionLocal

    session = SessionLocal()
    try:
        return get_detail(session, TAIL_TASK_NAME).get("head_probed") or {}
    finally:
        session.close()


def history_probed(
    db: Session,
    item: WatchlistItem,
    added_on: date,
    head_probed: Dict[str, str],
) -> bool:
    """加入日 − 7 天之前的历史是否已确认拉取过（之后再找不到收盘就是真没有）。"""
    from .price_tail_sync import TAIL_MARKETS, WATCHLIST_LEAD_DAYS, format_price_key

    if item.market not in TAIL_MARKETS:
        return True  # 港股等不做头部回补的市场：库里有什么就是什么
    from datetime import timedelta

    lead_start = added_on - timedelta(days=WATCHLIST_LEAD_DAYS)
    probed = head_probed.get(format_price_key((item.symbol, item.market)))
    if probed and probed <= lead_start.isoformat():
        return True
    earliest = (
        db.query(func.min(SecurityPrice.price_date))
        .filter(SecurityPrice.symbol == item.symbol, SecurityPrice.market == item.market)
        .scalar()
    )
    return earliest is not None and earliest <= lead_start


def backfill_added_prices(
    db: Session,
    *,
    dry_run: bool = False,
    head_probed: Optional[Dict[str, str]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    from datetime import timedelta

    from .stock_price_service import ADDED_PRICE_QUOTE_WINDOW, ensure_utc

    moment = now or datetime.now(timezone.utc)
    probed_map = head_probed if head_probed is not None else _head_probed_map()
    items = (
        db.query(WatchlistItem)
        .filter(WatchlistItem.added_price.is_(None))
        .order_by(WatchlistItem.id)
        .all()
    )
    filled: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []
    for item in items:
        added_on = to_local_date(item.created_at)
        entry = {"id": item.id, "symbol": item.symbol, "market": item.market, "added_on": added_on}
        if added_on is None:
            missing.append(entry)
            continue
        prices = db.query(SecurityPrice.price_date, SecurityPrice.close_price).filter(
            SecurityPrice.symbol == item.symbol,
            SecurityPrice.market == item.market,
            SecurityPrice.close_price > 0,
        )
        row = (
            prices.filter(
                SecurityPrice.price_date <= added_on,
                SecurityPrice.price_date >= added_on - timedelta(days=CLOSE_ON_ADD_MAX_GAP_DAYS),
            )
            .order_by(SecurityPrice.price_date.desc())
            .first()
        )
        created = ensure_utc(item.created_at)
        if created is not None and moment - created <= ADDED_PRICE_QUOTE_WINDOW:
            # 刚加入：加入时的报价比上一根收盘更贴近加入时的价格，先让报价口径去填
            missing.append(entry)
            continue
        basis = "close_on_add"
        if row is None:
            if not history_probed(db, item, added_on, probed_map):
                # 加入日之前的历史还没探过：先等，不抢先用更晚的价格
                missing.append(entry)
                continue
            row = (
                prices.filter(SecurityPrice.price_date > added_on)
                .order_by(SecurityPrice.price_date.asc())
                .first()
            )
            basis = "close_after_add"
        if row is None:
            pending.append(entry)
            if not dry_run and item.added_price_basis != PENDING_QUOTE:
                item.added_price_basis = PENDING_QUOTE
            continue
        filled.append(
            {**entry, "price_date": row.price_date, "close": row.close_price, "basis": basis}
        )
        if not dry_run:
            item.added_price = row.close_price
            item.added_price_date = row.price_date
            item.added_price_basis = basis
    if dry_run:
        db.rollback()
    else:
        db.commit()
    return {
        "candidates": len(items),
        "filled": filled,
        "missing": missing,
        "pending_quote": pending,
        "dry_run": dry_run,
    }
