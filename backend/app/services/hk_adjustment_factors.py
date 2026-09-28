"""港股复权因子：由披露易现金股息公告的除净日推导，写回 security_prices.adj_factor。

口径与 A股 Tushare `adj_factor` 一致——**后复权因子**、`adj_close_price = close ×
adj_factor`、随时间单调不减：每个除净日 E（该日若干笔股息先合并）贡献

    r_E = (P − D) / P，P = E 之前最后一个收盘价，D = 该日每股股息折成价格币种

价格日 t 的因子 = Π_{E ≤ t} 1 / r_E。区别只在锚点：Tushare 以上市首日为 1，本模块以
**该标的库内最早一根价格**为 1（锚点之前的股息没有意义；日期间比值与锚点无关）。
股息折价格币种（港股收盘价是 HKD）：派发币种即价格币种直接用；宣派币种是价格币种
用宣派金额；否则按除净日（含）之前最近一条汇率换算（汇率表存 X→CNY，经 CNY 交叉；
不回退到未来汇率）。缺汇率或 D ≥ P 的事件判 unresolved：**该日及之后**的因子写 NULL
（无法确定），不猜。

覆盖边界：披露易 EF001 现金股息表格自 2022 年起才有，更早的除净日不在输入里，
那一段因子恒为 1（未复权）；以股代息/实物分派不计（表格只有现金股息）。
当前没有任何读取方——因子为回测/前复权展示预留；价格刷新（Tushare/腾讯 upsert）
会把重叠行的因子覆盖成 NULL，下次分红同步或 `manage.py recompute-hk-adj-factors`
重算即恢复。
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from ..core.logging import get_app_logger
from .hkex_dividend_source import (
    load_cached_entries,
    resolve_current_dividends,
    resolve_dividend_resolution,
)

logger = get_app_logger(__name__)

MARKET = "港股"
_QUANT = Decimal("0.00000001")

RateFn = Callable[[str, str, date], Optional[Decimal]]


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def dividend_amount_in(
    component: Dict[str, Any], price_currency: str, ex_date: date, rate_fn: RateFn
) -> Optional[Decimal]:
    """单笔股息 → 价格币种每股金额（派发币种 > 宣派币种 > 汇率换算）。"""
    for key in ("payment", "declared"):
        leg = component.get(key)
        if leg and leg.get("currency") == price_currency:
            return Decimal(str(leg["amount"]))
    leg = component.get("payment") or component.get("declared")
    if not leg:
        return None
    rate = rate_fn(leg["currency"], price_currency, ex_date)
    if rate is None:
        return None
    return Decimal(str(leg["amount"])) * rate


def compute_backward_adj_factors(
    prices: Sequence[Tuple[date, Decimal, str]],
    dividends: Iterable[Dict[str, Any]],
    rate_fn: RateFn,
) -> Dict[str, Any]:
    """(price_date, close, currency) 升序序列 + 股息 [{ex_date, payment, declared}]
    → {"factors": {price_date: Decimal|None}, "events": [...]}。

    events 每个除净日一条：status = applied / unresolved（缺汇率或股息 ≥ 前收）/
    before_series（早于首根价格，不影响）/ pending（晚于末根价格，尚未生效）。
    """
    ordered = sorted(prices, key=lambda row: row[0])
    by_ex_date: Dict[date, List[Dict[str, Any]]] = defaultdict(list)
    for dividend in dividends:
        by_ex_date[dividend["ex_date"]].append(dividend)

    events: List[Dict[str, Any]] = []
    ratios: Dict[date, Optional[Decimal]] = {}
    for ex_date in sorted(by_ex_date):
        event: Dict[str, Any] = {"ex_date": ex_date, "components": len(by_ex_date[ex_date])}
        previous = [row for row in ordered if row[0] < ex_date]
        if not ordered or not previous:
            event["status"] = "before_series"
            events.append(event)
            continue
        if ex_date > ordered[-1][0]:
            event["status"] = "pending"
            events.append(event)
            continue
        prev_date, prev_close, price_currency = previous[-1]
        prev_close = Decimal(str(prev_close))
        amounts = [
            dividend_amount_in(component, price_currency, ex_date, rate_fn)
            for component in by_ex_date[ex_date]
        ]
        event.update({"prev_date": prev_date, "prev_close": prev_close,
                      "price_currency": price_currency})
        if any(amount is None for amount in amounts):
            event.update(status="unresolved", reason="missing_fx_rate")
            ratios[ex_date] = None
        else:
            total = sum(amounts, Decimal("0"))
            event["dividend"] = total
            if prev_close <= 0 or total >= prev_close:
                event.update(status="unresolved", reason="dividend_not_below_prev_close")
                ratios[ex_date] = None
            else:
                ratio = (prev_close - total) / prev_close
                event.update(status="applied", ratio=ratio)
                ratios[ex_date] = ratio
        events.append(event)

    factors: Dict[date, Optional[Decimal]] = {}
    running: Optional[Decimal] = Decimal("1")
    pending = sorted(ratios)
    index = 0
    for price_date, _close, _currency in ordered:
        while index < len(pending) and pending[index] <= price_date:
            ratio = ratios[pending[index]]
            running = None if (running is None or ratio is None) else running / ratio
            index += 1
        factors[price_date] = running
    return {"factors": factors, "events": events}


def dividends_from_entries(entries: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """缓存表格 → 现行股息（取代关系已解析、撤回已剔除）。"""
    return [
        {
            "ex_date": entry["form"]["ex_date"],
            "payment": entry["form"].get("payment"),
            "declared": entry["form"].get("declared"),
        }
        for entry in resolve_current_dividends(entries)
    ]


def cross_rate_fn(lookup) -> RateFn:
    """ExchangeRateLookup → (from, to, day) 汇率：直接/反向，否则经 CNY 交叉；只取当日及之前。"""

    def rate(from_currency: str, to_currency: str, on_date: date) -> Optional[Decimal]:
        if from_currency == to_currency:
            return Decimal("1")
        direct = lookup.get_rate_strictly_on_or_before(from_currency, to_currency, on_date)
        if direct is not None:
            return direct
        from_cny = lookup.get_rate_strictly_on_or_before(from_currency, "CNY", on_date)
        to_cny = lookup.get_rate_strictly_on_or_before(to_currency, "CNY", on_date)
        if from_cny is None or not to_cny:
            return None
        return from_cny / to_cny

    return rate


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------


def recompute_hk_adj_factors(
    db: Session, symbol: str, *, rate_lookup=None, commit: bool = False
) -> Dict[str, Any]:
    """按缓存的披露易表格重算单标的全部价格行的 adj_factor / adj_close_price（零网络）。

    只写发生变化的行；返回 {symbol, rows, updated, events}。
    """
    from ..models.security_price import SecurityPrice

    if rate_lookup is None:
        from .statistics.fx import DbExchangeRateLookup

        rate_lookup = DbExchangeRateLookup.from_db(db)
    rows = (
        db.query(SecurityPrice)
        .filter(SecurityPrice.symbol == symbol, SecurityPrice.market == MARKET)
        .order_by(SecurityPrice.price_date)
        .all()
    )
    entries = load_cached_entries(db, symbol, MARKET)
    resolution = resolve_dividend_resolution(entries)
    if resolution.blocked or resolution.unscoped:
        # 有公告认不出：少算或多算一笔派息都会让该日之前的全部因子失真，保持现值不动
        logger.warning(
            "港股复权因子 %s：%d 笔股息的最新公告无法解析、%d 份公告无法归属，本次不重算",
            symbol, len(resolution.blocked), len(resolution.unscoped),
        )
        return {"symbol": symbol, "rows": 0, "updated": 0, "events": [], "skipped": "blocked"}
    dividends = dividends_from_entries(entries)
    computed = compute_backward_adj_factors(
        [(row.price_date, Decimal(str(row.close_price)), row.currency or "HKD") for row in rows],
        dividends,
        cross_rate_fn(rate_lookup),
    )
    updated = 0
    for row in rows:
        factor = computed["factors"].get(row.price_date)
        factor = factor.quantize(_QUANT) if factor is not None else None
        adj_close = (
            (Decimal(str(row.close_price)) * factor).quantize(_QUANT)
            if factor is not None
            else None
        )
        current_factor = Decimal(str(row.adj_factor)) if row.adj_factor is not None else None
        current_close = (
            Decimal(str(row.adj_close_price)) if row.adj_close_price is not None else None
        )
        if current_factor == factor and current_close == adj_close:
            continue
        row.adj_factor = factor
        row.adj_close_price = adj_close
        updated += 1
    if commit:
        db.commit()
    unresolved = [e for e in computed["events"] if e["status"] == "unresolved"]
    if unresolved:
        logger.warning(
            "港股复权因子 %s：%d 个除净日无法确定（%s），其后因子置空",
            symbol, len(unresolved),
            ", ".join(f"{e['ex_date']}:{e.get('reason')}" for e in unresolved),
        )
    return {
        "symbol": symbol,
        "rows": len(rows),
        "updated": updated,
        "events": computed["events"],
    }
