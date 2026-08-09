"""服务端估值定价：Holding 现价优先、历史收盘兜底，附来源与新鲜度标记。"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from ...core.timeutil import local_today, to_local_date
from ...models.holding import Holding
from ...models.security_price import SecurityPrice

# 估值价格超过该天数未更新视为"陈价"（stale）：参与计算但前端/快照必须可见地标记。
PRICE_STALE_DAYS = 7


def resolve_server_prices(
    db: Session, user_id: int
) -> Tuple[Dict[str, float], Dict[str, str], Dict[str, Dict[str, Any]]]:
    """Build the valuation price map from server-side authority (issue #46).

    Prefers Holding.current_price, falls back to the latest cached SecurityPrice
    close. Keys are market-qualified ("symbol:market") so the same symbol held in
    two markets resolves independently. Returns (prices, sources, freshness):
    sources maps each key to "holding" / "latest_history" / "missing"; freshness
    maps each key to {"price_as_of": iso|None, "stale": bool}（价格新鲜度，
    路线图 #6：PCT 等手动维护标的的陈价必须可见）。
    """
    holdings = (
        db.query(Holding.symbol, Holding.market, Holding.current_price, Holding.price_updated_at)
        .filter(Holding.user_id == user_id)
        .all()
    )

    prices: Dict[str, float] = {}
    sources: Dict[str, str] = {}
    price_as_of: Dict[str, Any] = {}
    # 账户级持仓下同一证券可能多行（每账户一行）；任何一行有价即视为已定价，
    # 全部行都无价才回退历史收盘。价格与其更新时间是同一候选，必须一起取舍：
    # 只保留更新时间最晚的 (price, updated_at) 对（None 视为最旧），避免查询
    # 顺序决定结果、或出现"旧价格配新时间戳"的撕裂。
    _oldest = datetime.min.replace(tzinfo=timezone.utc)

    def _as_of_sort_value(value):
        if value is None:
            return _oldest
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    best_holding_price: Dict[str, Tuple[float, Any]] = {}
    for symbol, market, current_price, updated_at in holdings:
        key = f"{symbol}:{market}"
        if current_price is not None and float(current_price) > 0:
            current_best = best_holding_price.get(key)
            if current_best is None or _as_of_sort_value(updated_at) > _as_of_sort_value(
                current_best[1]
            ):
                best_holding_price[key] = (float(current_price), updated_at)
            sources[key] = "holding"
        else:
            sources.setdefault(key, "missing")
    for key, (price_value, updated_at) in best_holding_price.items():
        prices[key] = price_value
        price_as_of[key] = updated_at
    missing: List[Tuple[str, str]] = [
        (symbol, market)
        for symbol, market, _, _ in holdings
        if sources.get(f"{symbol}:{market}") == "missing"
    ]
    missing = list(dict.fromkeys(missing))

    if missing:
        # One batched query: latest close per missing (symbol, market).
        rows = (
            db.query(
                SecurityPrice.symbol,
                SecurityPrice.market,
                SecurityPrice.close_price,
                SecurityPrice.price_date,
            )
            .filter(tuple_(SecurityPrice.symbol, SecurityPrice.market).in_(missing))
            .order_by(SecurityPrice.symbol, SecurityPrice.market, SecurityPrice.price_date.desc())
            .all()
        )
        seen = set()
        for symbol, market, close_price, price_date in rows:
            pair = (symbol, market)
            if pair in seen:
                continue
            seen.add(pair)
            if close_price is not None and float(close_price) > 0:
                key = f"{symbol}:{market}"
                prices[key] = float(close_price)
                sources[key] = "latest_history"
                price_as_of[key] = price_date

    freshness: Dict[str, Dict[str, Any]] = {}
    # "今天"与时间戳换算必须来自**同一业务时区**（不能一边把 as_of 转成东八区
    # 日期、一边用 UTC 容器的 date.today() 当今天——生产容器就是 UTC，天数差
    # 会错一天，刚刷新的价格被推近陈价边界）。
    today = local_today()
    for key, source in sources.items():
        as_of = price_as_of.get(key)
        as_of_date = to_local_date(as_of) if isinstance(as_of, datetime) else as_of
        stale = (
            source == "missing"
            or as_of_date is None
            or (today - as_of_date).days > PRICE_STALE_DAYS
        )
        freshness[key] = {
            "source": source,
            "price_as_of": as_of.isoformat() if as_of is not None else None,
            "stale": stale,
        }

    return prices, sources, freshness
