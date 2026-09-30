"""服务端估值定价：持仓现价与最新收盘按行情日期择优，附来源与新鲜度标记。"""

from datetime import date, datetime, timezone
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from ...core.timeutil import business_timezone, local_today, to_local_date
from ...models.holding import Holding
from ...models.security_price import SecurityPrice
from ..market_sessions import market_timezone

# 估值价格超过该天数未更新视为"陈价"（stale）：参与计算但前端/快照必须可见地标记。
PRICE_STALE_DAYS = 7

MANUAL_PRICE_SOURCE = "manual"


def quote_market_date(
    market: str, price_as_of: Optional[date], price_updated_at: Optional[datetime]
) -> Optional[date]:
    """持仓/自选现价对应的**市场本地**行情日期。

    price_as_of 由报价源给出，本就是市场本地日期；手工价与拿不到日期的报价为 NULL，
    退回写库时刻按该市场时区换算——美股按北京时间换算会把纽约的周五算成周六。"""
    if price_as_of is not None:
        return price_as_of
    if price_updated_at is None:
        return None
    if price_updated_at.tzinfo is None:
        price_updated_at = price_updated_at.replace(tzinfo=timezone.utc)
    return price_updated_at.astimezone(market_timezone(market) or business_timezone()).date()


def latest_closes(db: Session, keys) -> Dict[Tuple[str, str], Tuple[float, date, str]]:
    """每个 (symbol, market) 最新一根收盘 → (close, price_date, source)，一次 DISTINCT ON。"""
    keys = list(dict.fromkeys(keys))
    if not keys:
        return {}
    rows = (
        db.query(
            SecurityPrice.symbol,
            SecurityPrice.market,
            SecurityPrice.close_price,
            SecurityPrice.price_date,
            SecurityPrice.source,
        )
        .filter(
            tuple_(SecurityPrice.symbol, SecurityPrice.market).in_(keys),
            SecurityPrice.close_price > 0,
        )
        .distinct(SecurityPrice.symbol, SecurityPrice.market)
        .order_by(SecurityPrice.symbol, SecurityPrice.market, SecurityPrice.price_date.desc())
        .all()
    )
    return {
        (symbol, market): (float(close), price_date, source or "")
        for symbol, market, close, price_date, source in rows
    }


def history_close_wins(
    close_date: Optional[date], quote_date: Optional[date], quote_source: Optional[str]
) -> bool:
    """最新收盘是否应取代持仓现价（#267）。

    行情日期较新者胜；同一交易日收盘胜——security_prices 的当天行只在收盘后写入，是终值，
    而持仓现价可能是没赶上收盘那次刷新的盘中价。唯一例外是手工价：用户当天手填的价格
    同日胜出（下一次报价照常覆盖它，与刷新口径一致）。"""
    if close_date is None:
        return False
    if quote_date is None:
        return True
    if close_date != quote_date:
        return close_date > quote_date
    return quote_source != MANUAL_PRICE_SOURCE


def resolve_server_prices(
    db: Session, user_id: int
) -> Tuple[Dict[str, float], Dict[str, str], Dict[str, Dict[str, Any]]]:
    """Build the valuation price map from server-side authority (issue #46).

    持仓现价（Holding.current_price）与该标的最新缓存收盘（SecurityPrice）按市场本地行情
    日期择优（`history_close_wins`）：持仓现价停在旧日期、而日线尾部已推进时，必须用更新的
    收盘——否则期末按旧价、期初按新收盘，差额会被当成当日损益（#267）。Keys are
    market-qualified ("symbol:market"). Returns (prices, sources, freshness): sources maps
    each key to "holding" / "latest_history" / "missing"; freshness maps each key to
    {"source", "price_as_of", "price_date", "stale", "name"}——price_date 是所选价格的市场本地行情
    日期（区间损益据此识别期末价早于期初基准的错配）。
    """
    holdings = (
        db.query(
            Holding.symbol,
            Holding.market,
            Holding.name,
            Holding.current_price,
            Holding.price_updated_at,
            Holding.price_as_of,
            Holding.price_source,
        )
        .filter(Holding.user_id == user_id)
        .all()
    )

    prices: Dict[str, float] = {}
    sources: Dict[str, str] = {}
    price_as_of: Dict[str, Any] = {}
    price_dates: Dict[str, Optional[date]] = {}
    # 账户级持仓下同一证券可能多行（每账户一行）；任何一行有价即视为已定价。
    # 价格与其更新时间是同一候选，必须一起取舍：只保留更新时间最晚的一行
    # （None 视为最旧），避免查询顺序决定结果、或出现"旧价格配新时间戳"的撕裂。
    _oldest = datetime.min.replace(tzinfo=timezone.utc)

    def _as_of_sort_value(value):
        if value is None:
            return _oldest
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    best_holding_price: Dict[str, Tuple] = {}
    market_by_key: Dict[str, Tuple[str, str]] = {}
    names: Dict[str, Optional[str]] = {}
    for symbol, market, name, current_price, updated_at, as_of_date, price_source in holdings:
        key = f"{symbol}:{market}"
        market_by_key[key] = (symbol, market)
        if name and not names.get(key):
            names[key] = name
        if current_price is not None and float(current_price) > 0:
            current_best = best_holding_price.get(key)
            if current_best is None or _as_of_sort_value(updated_at) > _as_of_sort_value(
                current_best[1]
            ):
                best_holding_price[key] = (
                    float(current_price),
                    updated_at,
                    as_of_date,
                    price_source,
                )
            sources[key] = "holding"
        else:
            sources.setdefault(key, "missing")

    closes = latest_closes(db, market_by_key.values())
    for key, (symbol, market) in market_by_key.items():
        candidate = best_holding_price.get(key)
        close = closes.get((symbol, market))
        quote_date = quote_market_date(market, candidate[2], candidate[1]) if candidate else None
        if close is not None and (
            candidate is None or history_close_wins(close[1], quote_date, candidate[3])
        ):
            prices[key] = close[0]
            sources[key] = "latest_history"
            price_as_of[key] = close[1]
            price_dates[key] = close[1]
        elif candidate is not None:
            prices[key] = candidate[0]
            price_as_of[key] = candidate[1]
            price_dates[key] = quote_date

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
        price_date = price_dates.get(key)
        freshness[key] = {
            "source": source,
            "price_as_of": as_of.isoformat() if as_of is not None else None,
            "price_date": price_date.isoformat() if price_date is not None else None,
            "stale": stale,
            # 展示用（陈价/缺价提示列名称而不是只列代码，#286）
            "name": names.get(key),
        }

    return prices, sources, freshness
