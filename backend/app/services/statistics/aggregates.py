"""聚合统计：概览/分市场/分时段/成本分布、持仓表现、已实现盈亏、股息与收益卡片。

查 DB → 喂 portfolio 纯内核 → 组响应；重放/FIFO/指标计算全部在内核中。
"""

from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from ...models.corporate_action import CorporateAction
from ...models.holding import Holding
from ...models.transaction import Transaction
from .. import exchange_rate_service
from ..portfolio.curve import get_current_price
from ..portfolio.fifo import empty_fifo_result, fifo_data_quality
from ..portfolio.fx import ExchangeRateLookup
from ..portfolio.metrics import xirr
from ..portfolio.semantics import cash_dividend_amounts
from .fifo_results import fifo_results_for_user
from .fx import (
    DbExchangeRateLookup,
    missing_rate_warning,
    to_cny_on_date,
    to_cny_or_track_missing,
    to_usd_or_zero,
    txn_signed_cash_flow,
)


def _empty_cost_bucket() -> Dict[str, Any]:
    return {
        "total_cost_cny": Decimal("0"),
        "total_cost_by_currency": {},
        "security_keys": set(),
        "missing_rates": set(),
    }


def _holdings_cost_buckets(
    db: Session, holdings: List[Holding], *, group_by_market: bool
) -> Dict[Optional[str], Dict[str, Any]]:
    """summary 与 by-market 的共同核心（issue #136：此前是同一段逻辑的两份拷贝）。

    按币种记录明细、折 CNY（缺汇率剔除并记录）、按证券去重计数；
    group_by_market=False 时归入单一 None 桶。
    """
    buckets: Dict[Optional[str], Dict[str, Any]] = {}
    for holding in holdings:
        bucket_key = holding.market if group_by_market else None
        bucket = buckets.get(bucket_key)
        if bucket is None:
            bucket = _empty_cost_bucket()
            buckets[bucket_key] = bucket

        currency = holding.currency or "CNY"
        amount = Decimal(str(holding.total_cost))

        if currency not in bucket["total_cost_by_currency"]:
            bucket["total_cost_by_currency"][currency] = Decimal("0")
        bucket["total_cost_by_currency"][currency] += amount

        bucket["total_cost_cny"] += to_cny_or_track_missing(
            db, amount, currency, bucket["missing_rates"]
        )
        bucket["security_keys"].add((holding.symbol, holding.market))
    return buckets


def get_summary_statistics(db: Session, user_id: int) -> Dict[str, Any]:
    """Get overall summary statistics with multi-currency support."""
    holdings = db.query(Holding).filter(Holding.user_id == user_id).all()
    bucket = (
        _holdings_cost_buckets(db, holdings, group_by_market=False).get(None)
        or _empty_cost_bucket()
    )
    total_invested_cny = bucket["total_cost_cny"]
    total_invested_by_currency = bucket["total_cost_by_currency"]

    # Total transactions（转仓对是内部迁移，不计入用户可见交易数）
    total_transactions = (
        db.query(Transaction)
        .filter(
            Transaction.user_id == user_id,
            Transaction.transaction_type.in_(["BUY", "SELL"]),
        )
        .count()
    )

    total_invested_usd = to_usd_or_zero(db, total_invested_cny)

    # 获取使用的汇率
    exchange_rates_used = {}
    for currency in total_invested_by_currency.keys():
        if currency != "CNY":
            try:
                rate_info = exchange_rate_service.get_rate_info(db, currency, "CNY")
                if rate_info:
                    exchange_rates_used[currency] = float(rate_info["rate"])
            except ValueError:
                pass

    return {
        "total_invested_cny": round(float(total_invested_cny), 2),
        "total_invested_usd": round(float(total_invested_usd), 2),
        "total_invested": round(float(total_invested_cny), 2),  # 保持向后兼容
        "total_invested_by_currency": {
            k: round(float(v), 2) for k, v in total_invested_by_currency.items()
        },
        "total_holdings": len(bucket["security_keys"]),
        "total_transactions": total_transactions,
        # 注意：按 Transaction 的 distinct market 计数（含已清仓市场），与其余
        # 基于 Holding 的"当前持仓"语义错位——issue #136 已指出，但改它就是改
        # 用户看到的数字，不属于本次零差异重构，留待单独决策。
        "markets_count": (
            db.query(Transaction.market).filter(Transaction.user_id == user_id).distinct().count()
        ),
        "base_currency": "CNY",
        "exchange_rates_used": exchange_rates_used,
        "missing_rate_currencies": sorted(bucket["missing_rates"]),
    }


def get_statistics_by_market(db: Session, user_id: int) -> List[Dict[str, Any]]:
    """Get statistics grouped by market with multi-currency support."""
    holdings = db.query(Holding).filter(Holding.user_id == user_id).all()
    buckets = _holdings_cost_buckets(db, holdings, group_by_market=True)

    result = []
    for market, bucket in buckets.items():
        result.append(
            {
                "market": market,
                "total_cost_cny": round(float(bucket["total_cost_cny"]), 2),
                "total_cost_usd": round(float(to_usd_or_zero(db, bucket["total_cost_cny"])), 2),
                "total_cost": round(float(bucket["total_cost_cny"]), 2),  # 向后兼容
                "total_cost_by_currency": {
                    k: round(float(v), 2) for k, v in bucket["total_cost_by_currency"].items()
                },
                "holdings_count": len(bucket["security_keys"]),
                "missing_rate_currencies": sorted(bucket["missing_rates"]),
            }
        )

    return result


def get_statistics_by_time(
    db: Session, user_id: int, group_by: str = "month"
) -> List[Dict[str, Any]]:
    """Transaction statistics grouped by time period (month or year).

    Amounts are converted to CNY at the transaction-date exchange rate so
    multi-currency accounts are not summed across raw currencies (issue #44).
    """
    if group_by not in ("month", "year"):
        raise ValueError(f"Unsupported group_by '{group_by}'. Use 'month' or 'year'.")

    transactions = db.query(Transaction).filter(Transaction.user_id == user_id).all()
    rate_lookup = DbExchangeRateLookup.from_db(db)

    time_stats: Dict[str, Dict[str, Any]] = {}
    for txn in transactions:
        # 转仓对用户级统计透明：不建 bucket、不计数（否则仅有转仓的月份
        # 会出现全零分组）。
        if txn.transaction_type not in ("BUY", "SELL"):
            continue
        txn_date = txn.transaction_date
        if group_by == "month":
            key = f"{txn_date.year}-{txn_date.month:02d}"
        else:
            key = f"{txn_date.year}"

        bucket = time_stats.get(key)
        if bucket is None:
            bucket = {
                "period": key,
                "buy_count": 0,
                "sell_count": 0,
                "buy_amount": 0.0,
                "sell_amount": 0.0,
                "buy_amount_cny": 0.0,
                "sell_amount_cny": 0.0,
            }
            time_stats[key] = bucket

        gross = Decimal(str(txn.quantity)) * Decimal(str(txn.price))
        amount_cny = float(to_cny_on_date(gross, txn.currency, txn_date, rate_lookup))

        if txn.transaction_type == "BUY":
            bucket["buy_count"] += 1
            bucket["buy_amount_cny"] += amount_cny
        elif txn.transaction_type == "SELL":
            bucket["sell_count"] += 1
            bucket["sell_amount_cny"] += amount_cny
        # Other transaction types (e.g. corporate-action bookkeeping) are ignored
        # here rather than silently overwriting SELL figures.

    ordered = sorted(time_stats.values(), key=lambda b: b["period"])
    for bucket in ordered:
        bucket["buy_amount_cny"] = round(bucket["buy_amount_cny"], 2)
        bucket["sell_amount_cny"] = round(bucket["sell_amount_cny"], 2)
        # Keep buy_amount/sell_amount as the chart's (now CNY) series.
        bucket["buy_amount"] = bucket["buy_amount_cny"]
        bucket["sell_amount"] = bucket["sell_amount_cny"]

    return ordered


def get_holdings_cost_breakdown(db: Session, user_id: int) -> List[Dict[str, Any]]:
    """Current holdings sorted by total cost; feeds the cost-distribution chart.

    Renamed from get_profit_loss_analysis (issue #48): it never contained any
    P&L fields, only the cost composition of current holdings.
    """
    holdings = db.query(Holding).filter(Holding.user_id == user_id).all()

    analysis = []
    for holding in holdings:
        analysis.append(
            {
                "symbol": holding.symbol,
                "name": holding.name,
                "market": holding.market,
                "quantity": float(holding.quantity),
                "avg_cost": float(holding.avg_cost),
                "total_cost": float(holding.total_cost),
                "currency": holding.currency,
            }
        )

    # Sort by total cost descending
    analysis.sort(key=lambda x: x["total_cost"], reverse=True)

    return analysis


def calculate_current_holdings_performance(
    db: Session,
    user_id: int,
    current_prices: Dict[str, float],
    fifo_results: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """计算当前持仓表现（基于 FIFO 剩余批次）——多币种，缺价持仓剔除并可见。"""
    # 获取所有持仓的symbol列表（包含currency信息）。
    # 账户级持仓下同一 (symbol, market) 可能有多行（每账户一行）；FIFO 结果
    # 目前是用户级的，这里按证券去重，避免逐账户行重复累加同一份 FIFO 队列。
    holding_rows = (
        db.query(Holding.symbol, Holding.market, Holding.name, Holding.currency)
        .filter(Holding.user_id == user_id)
        .all()
    )
    seen_keys = set()
    holdings = []
    for row in holding_rows:
        key = (row[0], row[1])
        if key not in seen_keys:
            seen_keys.add(key)
            holdings.append(row)
    if fifo_results is None:
        fifo_results = fifo_results_for_user(
            db, user_id, {(symbol, market) for symbol, market, _, _ in holdings}
        )

    total_unrealized_pnl_cny = Decimal(0)
    total_holdings_cost_cny = Decimal(0)
    total_market_value_cny = Decimal(0)
    holdings_detail = []
    unpriced_positions = []
    missing_rates: Set[str] = set()

    for symbol, market, name, currency in holdings:
        currency = currency or "CNY"  # 默认CNY

        fifo_result = fifo_results.get((symbol, market), empty_fifo_result(symbol, market))

        # Market-aware price lookup (supports "symbol:market"/"market:symbol"/symbol)
        # so two markets sharing a symbol resolve independently (issue #45).
        current_price = get_current_price(current_prices, symbol, market)
        if current_price is None:
            # A holding without a usable price is excluded from both cost and market
            # value (keeping the ratio self-consistent) but recorded and surfaced,
            # rather than silently dropped as it was before (issue #45).
            if Decimal(str(fifo_result.get("current_holdings_cost", 0))) > 0:
                unpriced_positions.append(
                    {
                        "symbol": symbol,
                        "market": market,
                        "name": name,
                    }
                )
            continue

        buy_queue = fifo_result["buy_queue"]
        holdings_cost = Decimal(str(fifo_result["current_holdings_cost"]))

        # 基于FIFO剩余批次计算未实现盈亏
        unrealized_pnl = Decimal(0)
        total_qty = Decimal(0)

        for batch in buy_queue:
            batch_pnl = (Decimal(str(current_price)) - Decimal(str(batch["price"]))) * Decimal(
                str(batch["quantity"])
            )
            unrealized_pnl += batch_pnl
            total_qty += Decimal(str(batch["quantity"]))

        market_value = Decimal(str(current_price)) * total_qty

        holdings_cost_cny = to_cny_or_track_missing(db, holdings_cost, currency, missing_rates)
        unrealized_pnl_cny = to_cny_or_track_missing(db, unrealized_pnl, currency, missing_rates)
        market_value_cny = to_cny_or_track_missing(db, market_value, currency, missing_rates)

        total_unrealized_pnl_cny += unrealized_pnl_cny
        total_holdings_cost_cny += holdings_cost_cny
        total_market_value_cny += market_value_cny

        holdings_detail.append(
            {
                "symbol": symbol,
                "name": name,
                "market": market,
                "currency": currency,
                "quantity": float(total_qty),
                "current_price": current_price,
                "holdings_cost": float(holdings_cost),
                "holdings_cost_cny": float(holdings_cost_cny),
                "market_value": float(market_value),
                "market_value_cny": float(market_value_cny),
                "unrealized_pnl": float(unrealized_pnl),
                "unrealized_pnl_cny": float(unrealized_pnl_cny),
                "unrealized_pnl_rate": (
                    float(unrealized_pnl / holdings_cost * 100) if holdings_cost > 0 else 0
                ),
            }
        )

    # 计算总收益率
    unrealized_pnl_rate = Decimal(0)
    if total_holdings_cost_cny > 0:
        unrealized_pnl_rate = total_unrealized_pnl_cny / total_holdings_cost_cny * Decimal(100)

    # 转换为USD
    total_unrealized_pnl_usd = to_usd_or_zero(db, total_unrealized_pnl_cny)
    total_holdings_cost_usd = to_usd_or_zero(db, total_holdings_cost_cny)
    total_market_value_usd = to_usd_or_zero(db, total_market_value_cny)

    return {
        "unrealized_pnl_cny": float(total_unrealized_pnl_cny),
        "unrealized_pnl_usd": float(total_unrealized_pnl_usd),
        "unrealized_pnl": float(total_unrealized_pnl_cny),  # 向后兼容
        "current_holdings_cost_cny": float(total_holdings_cost_cny),
        "current_holdings_cost_usd": float(total_holdings_cost_usd),
        "current_holdings_cost": float(total_holdings_cost_cny),  # 向后兼容
        "unrealized_pnl_rate": float(unrealized_pnl_rate),
        "current_market_value_cny": float(total_market_value_cny),
        "current_market_value_usd": float(total_market_value_usd),
        "current_market_value": float(total_market_value_cny),  # 向后兼容
        "holdings_detail": holdings_detail,
        "unpriced_positions": unpriced_positions,
        "base_currency": "CNY",
        "missing_rate_currencies": sorted(missing_rates),
        "data_quality": _current_holdings_data_quality(
            fifo_results, unpriced_positions, missing_rates
        ),
    }


def _current_holdings_data_quality(
    fifo_results: Dict[Tuple[str, str], Dict[str, Any]],
    unpriced_positions: List[Dict[str, Any]],
    missing_rates: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """FIFO data quality plus the list of holdings excluded for lack of a price."""
    data_quality = fifo_data_quality(fifo_results)
    data_quality["unpriced_positions"] = unpriced_positions
    data_quality["unpriced_position_count"] = len(unpriced_positions)
    if unpriced_positions:
        data_quality.setdefault("warnings", []).append(
            "部分当前持仓缺少可用估值价格，其成本与市值未计入汇总。"
        )
    data_quality["missing_rate_currencies"] = sorted(missing_rates or set())
    warning = missing_rate_warning(missing_rates or set())
    if warning:
        data_quality.setdefault("warnings", []).append(warning)
    return data_quality


def calculate_realized_pnl_fifo(
    db: Session,
    user_id: int,
    fifo_results: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """计算已实现盈亏（FIFO 方法）——多币种。"""
    # 按 (symbol, market) 去重——FIFO 结果就是按这个键索引的。
    # 曾经这里 distinct 的是 (symbol, market, currency) 三元组，于是同一标的的
    # currency 值不一致时（Transaction.currency 只有 Python 侧 default、无
    # server_default，手工录入与券商导入混合就会出现 NULL 与 "CNY" 并存），
    # 同一份 FIFO 队列被迭代多次 → 已实现盈亏与 sold_cost 翻倍、closed_trades
    # 重复进而污染 trade_skill 的胜率样本。
    # currency 只是展示字段，单独解析：取时间序最后一个非空值，与持仓重放
    # （holding_service 逐事件覆盖 state['currency']）同口径。
    symbol_rows = (
        db.query(
            Transaction.symbol,
            Transaction.market,
            Transaction.currency,
        )
        .filter(Transaction.user_id == user_id)
        .order_by(
            Transaction.transaction_date.asc(),
            Transaction.id.asc(),
        )
        .all()
    )

    security_keys: List[Tuple[str, str]] = []
    currency_by_key: Dict[Tuple[str, str], str] = {}
    for symbol, market, row_currency in symbol_rows:
        key = (symbol, market)
        if key not in currency_by_key:
            security_keys.append(key)
            currency_by_key[key] = "CNY"  # 全为 NULL 时的兜底
        if row_currency:
            currency_by_key[key] = row_currency

    if fifo_results is None:
        fifo_results = fifo_results_for_user(db, user_id, set(security_keys))

    total_realized_pnl_cny = Decimal(0)
    total_sold_cost_cny = Decimal(0)
    trades_detail = []
    closed_trades = []
    missing_rates: Set[str] = set()

    for symbol, market in security_keys:
        currency = currency_by_key[(symbol, market)]

        result = fifo_results.get((symbol, market), empty_fifo_result(symbol, market))

        realized_pnl = Decimal(str(result["realized_pnl"]))
        sold_cost = Decimal(str(result["sold_cost"]))

        if realized_pnl != 0 or sold_cost != 0:
            realized_pnl_cny = to_cny_or_track_missing(db, realized_pnl, currency, missing_rates)
            sold_cost_cny = to_cny_or_track_missing(db, sold_cost, currency, missing_rates)

            total_realized_pnl_cny += realized_pnl_cny
            total_sold_cost_cny += sold_cost_cny

            # Per-closing-lot detail, with pnl/cost in CNY, for per-trade metrics.
            for lot in result.get("closed_trades", []):
                lot_pnl = Decimal(str(lot["realized_pnl"]))
                lot_cost = Decimal(str(lot["matched_cost"]))
                lot_pnl_cny = to_cny_or_track_missing(db, lot_pnl, currency, missing_rates)
                lot_cost_cny = to_cny_or_track_missing(db, lot_cost, currency, missing_rates)
                closed_trades.append(
                    {
                        **lot,
                        "currency": currency,
                        "realized_pnl_cny": float(lot_pnl_cny),
                        "matched_cost_cny": float(lot_cost_cny),
                    }
                )

            trades_detail.append(
                {
                    "symbol": symbol,
                    "market": market,
                    "currency": currency,
                    "realized_pnl": float(realized_pnl),
                    "realized_pnl_cny": float(realized_pnl_cny),
                    "sold_cost": float(sold_cost),
                    "sold_cost_cny": float(sold_cost_cny),
                    "realized_pnl_rate": (
                        float(realized_pnl / sold_cost * 100) if sold_cost > 0 else 0
                    ),
                }
            )

    # 计算总收益率
    realized_pnl_rate = Decimal(0)
    if total_sold_cost_cny > 0:
        realized_pnl_rate = total_realized_pnl_cny / total_sold_cost_cny * Decimal(100)

    # 转换为USD
    total_realized_pnl_usd = to_usd_or_zero(db, total_realized_pnl_cny)
    total_sold_cost_usd = to_usd_or_zero(db, total_sold_cost_cny)

    return {
        "realized_pnl_cny": float(total_realized_pnl_cny),
        "realized_pnl_usd": float(total_realized_pnl_usd),
        "realized_pnl": float(total_realized_pnl_cny),  # 向后兼容
        "sold_cost_cny": float(total_sold_cost_cny),
        "sold_cost_usd": float(total_sold_cost_usd),
        "sold_cost": float(total_sold_cost_cny),  # 向后兼容
        "realized_pnl_rate": float(realized_pnl_rate),
        "trades_detail": trades_detail,
        "closed_trades": closed_trades,
        "base_currency": "CNY",
        "missing_rate_currencies": sorted(missing_rates),
        "data_quality": _realized_data_quality(fifo_results, missing_rates),
    }


def _realized_data_quality(
    fifo_results: Dict[Tuple[str, str], Dict[str, Any]],
    missing_rates: Set[str],
) -> Dict[str, Any]:
    data_quality = fifo_data_quality(fifo_results)
    data_quality["missing_rate_currencies"] = sorted(missing_rates)
    warning = missing_rate_warning(missing_rates)
    if warning:
        data_quality.setdefault("warnings", []).append(warning)
    return data_quality


def get_dividend_summary(
    db: Session,
    user_id: int,
    *,
    dividend_actions: Optional[List[CorporateAction]] = None,
) -> Dict[str, Any]:
    """股息统计摘要（独立模块，不混入盈亏）——多币种。"""
    if dividend_actions is None:
        dividend_actions = (
            db.query(CorporateAction)
            .filter(
                CorporateAction.user_id == user_id,
                CorporateAction.action_type == "CASH_DIVIDEND",
            )
            .all()
        )
    dividends = dividend_actions

    total_gross_cny = Decimal(0)
    total_tax_cny = Decimal(0)
    total_net_cny = Decimal(0)
    missing_rate_currencies: set = set()

    by_symbol = {}

    for div in dividends:
        symbol = div.symbol
        currency = div.currency or "CNY"  # 获取股息的货币

        gross, tax, net = cash_dividend_amounts(div)

        # 转换为CNY。缺汇率时不得把外币原值混进 CNY 总额（假装是人民币），
        # 而是从折算总额中剔除并记录币种，由调用方/前端提示。
        try:
            gross_cny = exchange_rate_service.convert_to_cny(db, gross, currency)
            tax_cny = exchange_rate_service.convert_to_cny(db, tax, currency)
            net_cny = exchange_rate_service.convert_to_cny(db, net, currency)
        except ValueError:
            missing_rate_currencies.add(currency)
            gross_cny = Decimal(0)
            tax_cny = Decimal(0)
            net_cny = Decimal(0)

        total_gross_cny += gross_cny
        total_tax_cny += tax_cny
        total_net_cny += net_cny

        # 键必须带 market：同一代码在两个市场（转板 RELISTING 正是此场景）
        # 原本会被并进一个桶，market/currency/name 取首条股息的值，原币
        # total_gross 还会跨币种相加。与全项目"price map 键 market-qualified"
        # 的约定一致。
        bucket_key = (symbol, div.market)
        if bucket_key not in by_symbol:
            by_symbol[bucket_key] = {
                "symbol": symbol,
                "name": div.name,
                "market": div.market,
                "total_gross": Decimal(0),
                "total_gross_cny": Decimal(0),
                "total_tax": Decimal(0),
                "total_tax_cny": Decimal(0),
                "total_net": Decimal(0),
                "total_net_cny": Decimal(0),
                "count": 0,
                "currency": currency,
            }

        by_symbol[bucket_key]["total_gross"] += gross
        by_symbol[bucket_key]["total_gross_cny"] += gross_cny
        by_symbol[bucket_key]["total_tax"] += tax
        by_symbol[bucket_key]["total_tax_cny"] += tax_cny
        by_symbol[bucket_key]["total_net"] += net
        by_symbol[bucket_key]["total_net_cny"] += net_cny
        by_symbol[bucket_key]["count"] += 1

    # 转换为USD
    total_gross_usd = to_usd_or_zero(db, total_gross_cny)
    total_tax_usd = to_usd_or_zero(db, total_tax_cny)
    total_net_usd = to_usd_or_zero(db, total_net_cny)

    # 转换为列表并格式化
    by_symbol_list = [
        {
            "symbol": v["symbol"],
            "name": v["name"],
            "market": v["market"],
            "currency": v["currency"],
            "total_gross": float(v["total_gross"]),
            "total_gross_cny": float(v["total_gross_cny"]),
            "total_tax": float(v["total_tax"]),
            "total_tax_cny": float(v["total_tax_cny"]),
            "total_net": float(v["total_net"]),
            "total_net_cny": float(v["total_net_cny"]),
            "count": v["count"],
        }
        for v in by_symbol.values()
    ]

    return {
        "total_dividend_gross_cny": float(total_gross_cny),
        "total_dividend_gross_usd": float(total_gross_usd),
        "total_dividend_gross": float(total_gross_cny),  # 向后兼容
        "total_tax_cny": float(total_tax_cny),
        "total_tax_usd": float(total_tax_usd),
        "total_tax": float(total_tax_cny),  # 向后兼容
        "total_dividend_net_cny": float(total_net_cny),
        "total_dividend_net_usd": float(total_net_usd),
        "total_dividend_net": float(total_net_cny),  # 向后兼容
        "by_symbol": by_symbol_list,
        "missing_rate_currencies": sorted(missing_rate_currencies),
        "base_currency": "CNY",
    }


def _compose_total_realized_return(
    realized: Dict[str, Any], dividends: Dict[str, Any]
) -> Dict[str, Any]:
    realized_trading_pnl_cny = Decimal(str(realized.get("realized_pnl_cny", 0)))
    realized_trading_pnl_usd = Decimal(str(realized.get("realized_pnl_usd", 0)))
    sold_cost_cny = Decimal(str(realized.get("sold_cost_cny", 0)))
    sold_cost_usd = Decimal(str(realized.get("sold_cost_usd", 0)))
    net_dividend_cny = Decimal(str(dividends.get("total_dividend_net_cny", 0)))
    net_dividend_usd = Decimal(str(dividends.get("total_dividend_net_usd", 0)))

    total_realized_return_cny = realized_trading_pnl_cny + net_dividend_cny
    total_realized_return_usd = realized_trading_pnl_usd + net_dividend_usd
    total_realized_return_rate = Decimal(0)
    if sold_cost_cny > 0:
        total_realized_return_rate = total_realized_return_cny / sold_cost_cny * Decimal(100)

    return {
        "realized_trading_pnl_cny": float(realized_trading_pnl_cny),
        "realized_trading_pnl_usd": float(realized_trading_pnl_usd),
        "net_dividend_income_cny": float(net_dividend_cny),
        "net_dividend_income_usd": float(net_dividend_usd),
        "total_realized_return_cny": float(total_realized_return_cny),
        "total_realized_return_usd": float(total_realized_return_usd),
        "total_realized_return": float(total_realized_return_cny),
        "sold_cost_cny": float(sold_cost_cny),
        "sold_cost_usd": float(sold_cost_usd),
        "total_realized_return_rate": float(total_realized_return_rate),
        "rate_denominator": "sold_cost_cny",
        "base_currency": "CNY",
    }


def _compose_account_total_return(
    db: Session,
    user_id: int,
    realized: Dict[str, Any],
    dividends: Dict[str, Any],
    current: Dict[str, Any],
    *,
    rate_lookup: ExchangeRateLookup,
    transactions: Optional[List[Transaction]] = None,
    dividend_actions: Optional[List[CorporateAction]] = None,
) -> Dict[str, Any]:
    realized_trading_pnl_cny = Decimal(str(realized.get("realized_pnl_cny", 0)))
    net_dividend_cny = Decimal(str(dividends.get("total_dividend_net_cny", 0)))
    unrealized_pnl_cny = Decimal(str(current.get("unrealized_pnl_cny", 0)))
    current_market_value_cny = Decimal(str(current.get("current_market_value_cny", 0)))

    total_return_cny = realized_trading_pnl_cny + unrealized_pnl_cny + net_dividend_cny
    net_invested_principal_cny = current_market_value_cny - total_return_cny

    # Convert each flow at the exchange rate on its own date, matching the TTWR
    # curve's FX basis, instead of translating every historical flow at today's
    # rate (issue #42).
    cash_flows: List[Tuple[date, Decimal]] = []
    if transactions is None:
        transactions = db.query(Transaction).filter(Transaction.user_id == user_id).all()
    for txn in transactions:
        amount = txn_signed_cash_flow(txn)
        if amount is None:
            continue
        cash_flows.append(
            (
                txn.transaction_date,
                to_cny_on_date(amount, txn.currency or "CNY", txn.transaction_date, rate_lookup),
            )
        )

    if dividend_actions is None:
        dividend_actions = (
            db.query(CorporateAction)
            .filter(
                CorporateAction.user_id == user_id,
                CorporateAction.action_type == "CASH_DIVIDEND",
            )
            .all()
        )
    for div in dividend_actions:
        currency = div.currency or "CNY"
        _, _, net = cash_dividend_amounts(div)
        flow_date = div.payment_date or div.ex_date
        cash_flows.append(
            (
                flow_date,
                to_cny_on_date(net, currency, flow_date, rate_lookup),
            )
        )

    # Return-rate denominator. net_invested_principal_cny (== cumulative cash-in
    # minus cash-out) drops to zero or negative once an account is fully or nearly
    # exited, which previously forced total_return_rate to a misleading 0%. Track
    # the peak deployed capital and fall back to it in that case so a fully-sold
    # profitable account still reports a real rate.
    peak_invested_principal_cny = Decimal("0")
    running_invested_cny = Decimal("0")
    for _, flow_amount in sorted(cash_flows, key=lambda cf: cf[0]):
        running_invested_cny -= flow_amount  # buys are negative flows -> capital deployed
        if running_invested_cny > peak_invested_principal_cny:
            peak_invested_principal_cny = running_invested_cny

    if net_invested_principal_cny > 0:
        rate_denominator_cny = net_invested_principal_cny
        rate_denominator_basis = "net_invested_principal_cny"
    else:
        rate_denominator_cny = peak_invested_principal_cny
        rate_denominator_basis = "peak_invested_principal_cny"

    total_return_rate = Decimal("0")
    if rate_denominator_cny > 0:
        total_return_rate = total_return_cny / rate_denominator_cny * Decimal("100")

    if current_market_value_cny > 0:
        cash_flows.append((date.today(), current_market_value_cny))

    xirr_rate = xirr(cash_flows)

    total_return_usd = to_usd_or_zero(db, total_return_cny)
    net_invested_principal_usd = to_usd_or_zero(db, net_invested_principal_cny)
    current_market_value_usd = to_usd_or_zero(db, current_market_value_cny)

    return {
        "total_return_cny": float(total_return_cny),
        "total_return_usd": float(total_return_usd),
        "total_return": float(total_return_cny),
        "total_return_rate": float(total_return_rate),
        "annualized_return_rate": (
            float(xirr_rate * Decimal("100")) if xirr_rate is not None else None
        ),
        "net_invested_principal_cny": float(net_invested_principal_cny),
        "net_invested_principal_usd": float(net_invested_principal_usd),
        "peak_invested_principal_cny": float(peak_invested_principal_cny),
        "current_market_value_cny": float(current_market_value_cny),
        "current_market_value_usd": float(current_market_value_usd),
        "realized_trading_pnl_cny": float(realized_trading_pnl_cny),
        "unrealized_pnl_cny": float(unrealized_pnl_cny),
        "net_dividend_income_cny": float(net_dividend_cny),
        "cash_flow_count": len(cash_flows),
        "rate_denominator": rate_denominator_basis,
        "annualized_method": "xirr",
        "fx_basis": "transaction_date",
        "base_currency": "CNY",
        "calculation_status": "exact",
        "calculation_scope": "invested_securities_only",
        "methodology_notes": [
            "权益仓口径：仅统计投入证券的资金，口径内精确；"
            "账户闲置现金与外部出入金按设计不计入、不稀释收益率。",
            "XIRR 现金流为证券买卖与股息（按各自日期汇率折算）。",
        ],
    }


def calculate_performance_summary(
    db: Session, user_id: int, current_prices: Dict[str, float]
) -> Dict[str, Any]:
    """Return the statistics tab's performance cards in one pass."""
    # Load the user's transactions and corporate actions once and share them
    # with every downstream computation (issue #49).
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
    dividend_actions = [
        action for action in corporate_actions if action.action_type == "CASH_DIVIDEND"
    ]

    fifo_results = fifo_results_for_user(
        db, user_id, transactions=transactions, corporate_actions=corporate_actions
    )
    realized = calculate_realized_pnl_fifo(db, user_id, fifo_results=fifo_results)
    dividends = get_dividend_summary(db, user_id, dividend_actions=dividend_actions)
    current = calculate_current_holdings_performance(
        db, user_id, current_prices, fifo_results=fifo_results
    )
    total_realized = _compose_total_realized_return(realized, dividends)
    account = _compose_account_total_return(
        db,
        user_id,
        realized,
        dividends,
        current,
        rate_lookup=DbExchangeRateLookup.from_db(db),
        transactions=transactions,
        dividend_actions=dividend_actions,
    )

    return {
        "current_performance": current,
        "realized_pnl": realized,
        "dividend_summary": dividends,
        "total_realized_return": total_realized,
        "account_return": account,
    }
