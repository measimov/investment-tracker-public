"""当日 / 本月（MTD）/ 本年（YTD）损益。

与统计页收益曲线**同一套算法**：直接复用纯内核 `build_return_curve`，分别以今天、本月 1 日、
今年 1 月 1 日为区间起点、今天为终点各跑一次。期初持仓按起点前最近一个收盘价估值（即
「相对上一交易日收盘」），区间内每个点的 `total_return_cny` = 期末市值 + 卖出/分红流出 −
（期初市值 + 买入流入），逐点相加就是区间损益；已实现与浮动、买卖、分红、汇率都在其中。

口径与 TTWR 曲线一致：**权益仓口径**（只算投入证券的资金，闲置现金与出入金不在内），
收益率是区间内的时间加权收益率——与统计页一样标注为实验口径，不是整户权威损益。

「今天」一律用业务时区（`local_today`，Asia/Shanghai）：生产容器是 UTC，用 `date.today()`
在早上 8 点前会把「今天」算成前一天。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from ...core.timeutil import local_today
from ...models.corporate_action import CorporateAction
from ...models.holding import Holding
from ...models.transaction import Transaction
from ..market_data_service import infer_price_currency
from ..dividend_tax_service import load_dividend_tax_events
from ..portfolio.curve import build_return_curve
from .analytics import build_price_maps
from .fx import DbExchangeRateLookup
from .period_receivables import add_period_receivables

PERIODS: Tuple[Tuple[str, str], ...] = (("daily", "当日"), ("mtd", "本月"), ("ytd", "本年"))
# 期初基准的新鲜度：估值价日期早于区间起点这么多天，就说明区间损益里混进了此前累积的涨跌
# （行情断更、停牌、只有很久以前的成交价）。10 天覆盖长假休市（春节/国庆 7-9 天）
OPENING_BASIS_STALE_DAYS = 10


def assess_opening_basis(
    quality: Dict[str, Any], start: date
) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """期初估值是否可靠 → (status, 缺价持仓, 陈旧基准持仓)。

    - unavailable：有期初持仓**完全没有价格**——期初市值不含它、期末却按现价计入，整笔市值会被
      当成区间收益，这个数没有意义，不给数；
    - estimated：有期初持仓的估值价早于起点 OPENING_BASIS_STALE_DAYS 天以上（或只能用成交价估），
      区间损益含此前累积涨跌，给数但标估算、列出基准日；
    - exact：其余。"""
    unpriced = list(quality.get("opening_unpriced_positions") or [])
    stale: List[Dict[str, Any]] = []
    cutoff = start - timedelta(days=OPENING_BASIS_STALE_DAYS)
    for key, basis in (quality.get("opening_price_basis") or {}).items():
        symbol, _, market = key.partition(":")
        basis_date = date.fromisoformat(basis["date"])
        if basis_date < cutoff or basis.get("source") == "transaction":
            stale.append(
                {
                    "symbol": symbol,
                    "market": market,
                    "basis_date": basis["date"],
                    "basis_source": basis.get("source") or "",
                }
            )
    if unpriced:
        return "unavailable", unpriced, stale
    if stale:
        return "estimated", unpriced, stale
    return "exact", unpriced, stale


def assess_closing_prices(
    quality: Dict[str, Any], price_dates: Dict[str, Optional[str]]
) -> List[Dict[str, Any]]:
    """期末估值价早于该持仓期初基准的错配（#267）。

    期初按起点前最近收盘估值，期末按服务端现价估值；现价停在旧日期（错过收盘后刷新）而日线
    已推进时，期末价反而比期初价还旧，两者之差会被当成区间损益。只看区间开始前就持有的持仓
    （有期初基准）；比较的是同一标的两个价格的日期，不设天数阈值——手工价与停牌标的日期虽旧，
    只要不早于期初基准就不算错配。"""
    basis_by_key = quality.get("opening_price_basis") or {}
    mismatched: List[Dict[str, Any]] = []
    for position in quality.get("terminal_positions") or []:
        key = f"{position['symbol']}:{position['market']}"
        basis = basis_by_key.get(key)
        price_date = price_dates.get(key)
        if not basis or not price_date:
            continue
        if date.fromisoformat(price_date) < date.fromisoformat(basis["date"]):
            mismatched.append(
                {
                    "symbol": position["symbol"],
                    "market": position["market"],
                    "price_date": price_date,
                    "basis_date": basis["date"],
                }
            )
    return mismatched


def period_start(period: str, today: date) -> date:
    if period == "daily":
        return today
    if period == "mtd":
        return today.replace(day=1)
    if period == "ytd":
        return today.replace(month=1, day=1)
    raise ValueError(f"未知区间: {period}")


def summarize_curve(curve: List[Dict[str, Any]], quality: Dict[str, Any]) -> Dict[str, Any]:
    """一段曲线 → 区间损益摘要（纯函数，便于单测）。"""
    if not curve:
        return {
            "pnl_cny": 0.0,
            "return_rate": None,
            "opening_market_value_cny": 0.0,
            "closing_market_value_cny": 0.0,
            "cash_in_cny": 0.0,
            "cash_out_cny": 0.0,
            "dividend_income_cny": 0.0,
            "points": 0,
            "unpriced_positions": [],
            "stale_price_positions": [],
        }
    last = curve[-1]
    has_return = any(point.get("daily_return_rate") is not None for point in curve)
    return {
        "pnl_cny": round(sum(float(point.get("total_return_cny") or 0) for point in curve), 2),
        # 曲线从区间起点开始复利，末点的累计收益率就是区间 TTWR；没有任何有效点（期初无仓、
        # 当天也没买入）时返回 None 而不是 0%，免得把「无从计算」显示成「持平」
        "return_rate": round(float(last.get("cumulative_return_rate") or 0), 4)
        if has_return
        else None,
        "opening_market_value_cny": round(float(quality.get("opening_market_value_cny") or 0), 2),
        "closing_market_value_cny": round(float(last.get("market_value_cny") or 0), 2),
        "cash_in_cny": round(sum(float(point.get("cash_in_cny") or 0) for point in curve), 2),
        "cash_out_cny": round(sum(float(point.get("cash_out_cny") or 0) for point in curve), 2),
        "dividend_income_cny": round(float(last.get("dividend_income_cny") or 0), 2),
        "points": len(curve),
        "unpriced_positions": list(last.get("unpriced_positions") or []),
        "stale_price_positions": list(last.get("stale_price_positions") or []),
    }


def calculate_period_pnl(
    db: Session,
    user_id: int,
    current_prices: Dict[str, float],
    *,
    today: Optional[date] = None,
    price_freshness: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """price_freshness：resolve_server_prices 的新鲜度映射；给出时用其中的 price_date 识别
    期末价早于期初基准的错配（POST 手工价口径不传，不做该判定）。"""
    today = today or local_today()
    price_dates = {
        key: (entry or {}).get("price_date") for key, entry in (price_freshness or {}).items()
    }
    response: Dict[str, Any] = {
        "base_currency": "CNY",
        "as_of": today.isoformat(),
        "methodology": {
            "scope": "invested_securities_only",
            "method": "ttwr_curve",
            "status": "experimental",
            "description": (
                "权益仓口径：只计投入证券的资金，闲置现金与出入金不在内；区间损益 = 期末市值 + "
                "卖出与分红流出 − 期初市值 − 买入流入，期初按区间起点前最近收盘价估值；"
                "收益率为区间时间加权收益率"
            ),
        },
        "periods": {},
        "data_quality": {"warnings": []},
    }
    transactions = (
        db.query(Transaction)
        .filter(Transaction.user_id == user_id)
        .order_by(Transaction.transaction_date, Transaction.id)
        .all()
    )
    corporate_actions = (
        db.query(CorporateAction)
        .filter(CorporateAction.user_id == user_id)
        .order_by(CorporateAction.ex_date, CorporateAction.id)
        .all()
    )
    tax_events = load_dividend_tax_events(db, user_id)
    holdings = db.query(Holding).filter(Holding.user_id == user_id).all()
    # 没有交易不等于没有持仓：期初建仓/转托管转入（OPENING_POSITION，#174）只建公司行动与持仓、
    # 不建 BUY 交易。只有交易、公司行动、持仓都没有时才是真正的空账户
    if not transactions and not corporate_actions and not holdings and not tax_events:
        for key, label in PERIODS:
            response["periods"][key] = {
                "label": label,
                "start_date": period_start(key, today).isoformat(),
                "end_date": today.isoformat(),
                "status": "exact",
                "stale_closing_prices": [],
                **summarize_curve([], {}),
            }
        add_period_receivables(db, user_id, response["periods"], corporate_actions, today)
        return response
    symbols_by_key: Dict[Tuple[str, str], str] = {}
    for txn in transactions:
        symbols_by_key[(txn.symbol, txn.market)] = txn.currency or infer_price_currency(txn.market)
    for action in corporate_actions:
        symbols_by_key.setdefault(
            (action.symbol, action.market), action.currency or infer_price_currency(action.market)
        )
    for holding in holdings:
        symbols_by_key.setdefault(
            (holding.symbol, holding.market),
            holding.currency or infer_price_currency(holding.market),
        )
    symbols = [(symbol, market, currency) for (symbol, market), currency in symbols_by_key.items()]
    rate_lookup = DbExchangeRateLookup.from_db(db)
    # 一次取最宽区间（本年）的行情：build_price_maps 另带区间起点前的最近收盘，
    # 当日/本月的期初估值都在其中，不必再查
    earliest = period_start("ytd", today)
    price_maps, _counts = build_price_maps(db, symbols, earliest, today)

    warnings: List[str] = []
    for key, label in PERIODS:
        start = period_start(key, today)
        curve, _level, quality = build_return_curve(
            transactions,
            corporate_actions,
            price_maps,
            symbols_by_key,
            current_prices,
            start,
            today,
            rate_lookup=rate_lookup,
            fallback_currency=infer_price_currency,
            today=today,
            # 期初 = 起点前一日收盘时点的本币价值：起点当天的汇率变动属于本区间的汇兑损益
            opening_fx_date=start - timedelta(days=1),
            dividend_tax_events=tax_events,
        )
        summary = summarize_curve(curve, quality)
        if quality.get("estimated_inflow_events"):
            summary["estimated_inflow_events"] = len(quality["estimated_inflow_events"])
        status, opening_unpriced, stale_basis = assess_opening_basis(quality, start)
        summary["status"] = status
        summary["opening_unpriced_positions"] = opening_unpriced
        summary["stale_opening_basis"] = stale_basis
        stale_closing = assess_closing_prices(quality, price_dates)
        summary["stale_closing_prices"] = stale_closing
        if stale_closing and status == "exact":
            status = summary["status"] = "estimated"
        if status == "unavailable":
            # 不给数：此时的 pnl 会把缺价持仓的整笔市值当成收益
            summary["pnl_cny"] = None
            summary["return_rate"] = None
            names = "、".join(p["symbol"] for p in opening_unpriced[:5])
            warnings.append(
                f"{label}损益无法计算：{len(opening_unpriced)} 只持仓在 {start.isoformat()} 前没有任何价格"
                f"（{names}），期初市值无从估值"
            )
        elif status == "estimated":
            if stale_basis:
                names = "、".join(
                    f"{p['symbol']}（{p['basis_date']}{'成交价' if p['basis_source'] == 'transaction' else '收盘'}）"
                    for p in stale_basis[:5]
                )
                warnings.append(
                    f"{label}损益为估算：{len(stale_basis)} 只持仓的期初价早于 {start.isoformat()}，"
                    f"区间损益含此前累积涨跌：{names}"
                )
            if stale_closing:
                names = "、".join(
                    f"{p['symbol']}（现价 {p['price_date']}，期初 {p['basis_date']}）"
                    for p in stale_closing[:5]
                )
                warnings.append(
                    f"{label}损益为估算：{len(stale_closing)} 只持仓的估值价早于期初基准，"
                    f"期末按旧价计：{names}"
                )
        response["periods"][key] = {
            "label": label,
            "start_date": start.isoformat(),
            "end_date": today.isoformat(),
            **summary,
        }
        missing_tax_rates = quality.get("missing_tax_rate_currencies", [])
        if missing_tax_rates:
            response["periods"][key].update(
                status="unavailable",
                pnl_cny=None,
                return_rate=None,
                missing_tax_rate_currencies=missing_tax_rates,
            )
            warnings.append(
                f"{label}损益无法完整计算：股息税扣款日缺少汇率（{'、'.join(missing_tax_rates)}），"
                "未将原币税款混入人民币收益。"
            )
        if summary["unpriced_positions"] and key == "daily":
            names = "、".join(p["symbol"] for p in summary["unpriced_positions"][:5])
            warnings.append(
                f"{len(summary['unpriced_positions'])} 只持仓无可用价格，未计入市值：{names}"
            )
        if summary["stale_price_positions"] and key == "daily":
            names = "、".join(p["symbol"] for p in summary["stale_price_positions"][:5])
            warnings.append(
                f"{len(summary['stale_price_positions'])} 只持仓今日无最新价，按最近收盘估值：{names}"
            )
    add_period_receivables(
        db, user_id, response["periods"], corporate_actions, today, rate_lookup=rate_lookup
    )
    response["data_quality"]["warnings"] = warnings
    return response
