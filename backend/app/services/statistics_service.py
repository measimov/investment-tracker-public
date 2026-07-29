"""统计编排层：查 DB → 喂 portfolio 纯内核 → 组响应。

所有重放/FIFO/曲线/指标计算都在 services/portfolio/ 内核中（无 DB 依赖）；
本模块负责数据装载（transactions/corporate actions/prices/rates）、汇率
换算到本位币和响应字段组装。私有别名（_xirr 等）保留原名以兼容既有测试。
"""

from sqlalchemy.orm import Session
from sqlalchemy import tuple_
from decimal import Decimal
from datetime import date, datetime, timezone
from typing import Dict, List, Any, Tuple, Optional
from collections import defaultdict
from ..models.transaction import Transaction
from ..models.holding import Holding
from ..models.corporate_action import CorporateAction
from ..models.exchange_rate import ExchangeRate
from ..models.security_price import SecurityPrice
from . import exchange_rate_service
from .market_data_service import (
    fetch_and_store_security_price_history,
    infer_price_currency,
)
from .portfolio.curve import (
    build_return_curve,
    corporate_action_curve_date,
    decimal_close,
    get_current_price,
)
from ..core.logging import get_app_logger
from .portfolio.fifo import (
    FIFO_ACTION_TYPES,
    AccountFifoFallback,
    calculate_fifo_pnl,
    empty_fifo_result,
    fifo_data_quality,
    merge_account_fifo_results,
    replay_fifo_multi_account,
)
from .portfolio.fx import ExchangeRateLookup, convert_on_date
from .portfolio.metrics import (
    calculate_risk_metrics,
    calculate_trade_skill_metrics,
    xirr,
)

logger = get_app_logger(__name__)

# 兼容别名：既有测试与旧调用方仍以私有名引用这些内核函数。
_xirr = xirr
_calculate_risk_metrics = calculate_risk_metrics
_calculate_trade_skill_metrics = calculate_trade_skill_metrics
_get_current_price = get_current_price
_empty_fifo_result = empty_fifo_result
_calculate_fifo_pnl_from_records = calculate_fifo_pnl
_fifo_data_quality = fifo_data_quality
_corporate_action_curve_date = corporate_action_curve_date
_decimal_close = decimal_close


class _ExchangeRateLookup(ExchangeRateLookup):
    """纯内核 ExchangeRateLookup + DB 装载入口。"""

    @classmethod
    def from_db(cls, db: Session) -> "_ExchangeRateLookup":
        records = db.query(ExchangeRate).filter(
            ExchangeRate.is_active.is_(True),
        ).order_by(
            ExchangeRate.from_currency,
            ExchangeRate.to_currency,
            ExchangeRate.effective_date,
        ).all()
        return cls(records)


def _to_cny_on_date(
    db: Session,
    amount: Decimal,
    currency: Optional[str],
    effective_date: date,
    rate_lookup: ExchangeRateLookup,
) -> Decimal:
    # db 参数保留以兼容旧签名；换算完全由 rate_lookup 完成。
    return convert_on_date(amount, currency, effective_date, rate_lookup)


def _build_return_curve(
    db: Session,
    transactions: List[Transaction],
    corporate_actions: List[CorporateAction],
    price_maps: Dict[Tuple[str, str], Dict[date, Decimal]],
    currency_by_key: Dict[Tuple[str, str], str],
    current_prices: Dict[str, float],
    start_date: date,
    end_date: date,
) -> Tuple[List[Dict[str, Any]], str, Dict[str, Any]]:
    """装载汇率并调用纯内核曲线重放（保持旧签名）。"""
    return build_return_curve(
        transactions,
        corporate_actions,
        price_maps,
        currency_by_key,
        current_prices,
        start_date,
        end_date,
        rate_lookup=_ExchangeRateLookup.from_db(db),
        fallback_currency=infer_price_currency,
        today=date.today(),
    )


def get_summary_statistics(db: Session, user_id: int) -> Dict[str, Any]:
    """Get overall summary statistics with multi-currency support."""

    # Total holdings value (total cost) - 需要按币种转换
    holdings = db.query(Holding).filter(Holding.user_id == user_id).all()

    total_invested_cny = Decimal("0")
    total_invested_by_currency = {}

    for h in holdings:
        currency = h.currency or "CNY"
        amount = Decimal(str(h.total_cost))

        # 记录各币种明细
        if currency not in total_invested_by_currency:
            total_invested_by_currency[currency] = Decimal("0")
        total_invested_by_currency[currency] += amount

        # 转换为CNY汇总
        try:
            amount_cny = exchange_rate_service.convert_to_cny(db, amount, currency)
            total_invested_cny += amount_cny
        except ValueError:
            # 如果找不到汇率，直接加（假设是CNY）
            total_invested_cny += amount

    # 账户级持仓下同一证券可能多行（每账户一行），用户可见计数按证券去重。
    total_holdings = len({(h.symbol, h.market) for h in holdings})

    # Total transactions（转仓对是内部迁移，不计入用户可见交易数）
    total_transactions = db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.transaction_type.in_(["BUY", "SELL"]),
    ).count()

    # 转换为USD
    try:
        total_invested_usd = exchange_rate_service.convert_to_usd(db, total_invested_cny)
    except ValueError:
        total_invested_usd = Decimal("0")

    # 获取使用的汇率
    exchange_rates_used = {}
    for currency in total_invested_by_currency.keys():
        if currency != "CNY":
            try:
                rate_info = exchange_rate_service.get_rate_info(db, currency, "CNY")
                if rate_info:
                    exchange_rates_used[currency] = float(rate_info['rate'])
            except ValueError:
                pass

    return {
        "total_invested_cny": round(float(total_invested_cny), 2),
        "total_invested_usd": round(float(total_invested_usd), 2),
        "total_invested": round(float(total_invested_cny), 2),  # 保持向后兼容
        "total_invested_by_currency": {k: round(float(v), 2) for k, v in total_invested_by_currency.items()},
        "total_holdings": total_holdings,
        "total_transactions": total_transactions,
        "markets_count": db.query(Transaction.market).filter(Transaction.user_id == user_id).distinct().count(),
        "base_currency": "CNY",
        "exchange_rates_used": exchange_rates_used
    }


def get_statistics_by_market(db: Session, user_id: int) -> List[Dict[str, Any]]:
    """Get statistics grouped by market with multi-currency support."""

    holdings = db.query(Holding).filter(Holding.user_id == user_id).all()

    market_stats = {}
    for holding in holdings:
        market = holding.market
        currency = holding.currency or "CNY"
        amount = Decimal(str(holding.total_cost))

        if market not in market_stats:
            market_stats[market] = {
                "market": market,
                "total_cost_cny": Decimal("0"),
                "total_cost_usd": Decimal("0"),
                "total_cost_by_currency": {},
                # 按证券去重计数：账户级持仓下同一证券可能有多行
                "security_keys": set(),
            }

        # 记录各币种明细
        if currency not in market_stats[market]["total_cost_by_currency"]:
            market_stats[market]["total_cost_by_currency"][currency] = Decimal("0")
        market_stats[market]["total_cost_by_currency"][currency] += amount

        # 转换为CNY
        try:
            amount_cny = exchange_rate_service.convert_to_cny(db, amount, currency)
            market_stats[market]["total_cost_cny"] += amount_cny
        except ValueError:
            market_stats[market]["total_cost_cny"] += amount

        market_stats[market]["security_keys"].add((holding.symbol, holding.market))

    # 转换为USD
    result = []
    for item in market_stats.values():
        try:
            item["total_cost_usd"] = exchange_rate_service.convert_to_usd(db, item["total_cost_cny"])
        except ValueError:
            item["total_cost_usd"] = Decimal("0")

        result.append({
            "market": item["market"],
            "total_cost_cny": round(float(item["total_cost_cny"]), 2),
            "total_cost_usd": round(float(item["total_cost_usd"]), 2),
            "total_cost": round(float(item["total_cost_cny"]), 2),  # 向后兼容
            "total_cost_by_currency": {k: round(float(v), 2) for k, v in item["total_cost_by_currency"].items()},
            "holdings_count": len(item["security_keys"])
        })

    return result


def get_statistics_by_time(db: Session, user_id: int, group_by: str = "month") -> List[Dict[str, Any]]:
    """Transaction statistics grouped by time period (month or year).

    Amounts are converted to CNY at the transaction-date exchange rate so
    multi-currency accounts are not summed across raw currencies (issue #44).
    """
    if group_by not in ("month", "year"):
        raise ValueError(f"Unsupported group_by '{group_by}'. Use 'month' or 'year'.")

    transactions = (
        db.query(Transaction).filter(Transaction.user_id == user_id).all()
    )
    rate_lookup = _ExchangeRateLookup.from_db(db)

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
        amount_cny = float(_to_cny_on_date(db, gross, txn.currency, txn_date, rate_lookup))

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
        analysis.append({
            "symbol": holding.symbol,
            "name": holding.name,
            "market": holding.market,
            "quantity": float(holding.quantity),
            "avg_cost": float(holding.avg_cost),
            "total_cost": float(holding.total_cost),
            "currency": holding.currency
        })

    # Sort by total cost descending
    analysis.sort(key=lambda x: x["total_cost"], reverse=True)

    return analysis


def _get_fifo_results_for_user(
    db: Session,
    user_id: int,
    symbols_markets: Optional[set[Tuple[str, str]]] = None,
    *,
    transactions: Optional[List[Transaction]] = None,
    corporate_actions: Optional[List[CorporateAction]] = None,
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Replay FIFO per (symbol, market).

    Callers that already hold the user's transactions/corporate actions can pass
    them in to avoid re-querying (issue #49); per-key event ordering is
    re-established inside the FIFO kernel, so input order does not matter.
    """
    if transactions is None:
        transactions = db.query(Transaction).filter(
            Transaction.user_id == user_id
        ).order_by(
            Transaction.symbol,
            Transaction.market,
            Transaction.transaction_date,
            Transaction.id
        ).all()

    if corporate_actions is None:
        corporate_actions = db.query(CorporateAction).filter(
            CorporateAction.user_id == user_id,
            CorporateAction.action_type.in_(FIFO_ACTION_TYPES)
        ).order_by(
            CorporateAction.symbol,
            CorporateAction.market,
            CorporateAction.ex_date,
            CorporateAction.id
        ).all()
    else:
        corporate_actions = [
            action for action in corporate_actions
            if action.action_type in FIFO_ACTION_TYPES
        ]

    transactions_by_key = defaultdict(list)
    actions_by_key = defaultdict(list)

    for txn in transactions:
        key = (txn.symbol, txn.market)
        if symbols_markets is None or key in symbols_markets:
            transactions_by_key[key].append(txn)

    for action in corporate_actions:
        key = (action.symbol, action.market)
        if symbols_markets is None or key in symbols_markets:
            actions_by_key[key].append(action)

    keys = set(transactions_by_key.keys()) | set(actions_by_key.keys())
    if symbols_markets is not None:
        keys |= symbols_markets

    return {
        key: _security_fifo(
            key[0],
            key[1],
            transactions_by_key.get(key, []),
            actions_by_key.get(key, [])
        )
        for key in keys
    }


def _security_fifo(symbol, market, transactions, corporate_actions):
    """单证券 FIFO：多账户/含转仓时按账户重放后聚合，矛盾时降级合并重放。

    降级条件与 holding_service 的合并桶降级一致；合并重放中转仓是恒等操作，
    数字与账户化之前完全相同。
    """
    accounts = {txn.broker_account_id for txn in transactions}
    has_transfer = any(
        txn.transaction_type in ("TRANSFER_OUT", "TRANSFER_IN") for txn in transactions
    )
    if len(accounts) <= 1 and not has_transfer:
        return calculate_fifo_pnl(symbol, market, transactions, corporate_actions)
    try:
        account_results = replay_fifo_multi_account(
            symbol, market, transactions, corporate_actions
        )
        return merge_account_fifo_results(symbol, market, account_results)
    except AccountFifoFallback as exc:
        logger.warning(
            "Account-scoped FIFO fell back to merged replay for %s(%s): %s",
            symbol, market, exc,
        )
        return calculate_fifo_pnl(symbol, market, transactions, corporate_actions)


def calculate_current_holdings_performance(
    db: Session,
    user_id: int,
    current_prices: Dict[str, float],
    fifo_results: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    计算当前持仓表现（基于FIFO剩余批次，优化1）- 支持多币种

    Args:
        user_id: User ID
        current_prices: {symbol: current_price}

    Returns:
        {
            'unrealized_pnl_cny': float,           # 未实现盈亏（CNY）
            'unrealized_pnl_usd': float,           # 未实现盈亏（USD）
            'current_holdings_cost_cny': float,    # 当前持仓成本（CNY）
            'current_holdings_cost_usd': float,    # 当前持仓成本（USD）
            'unrealized_pnl_rate': float,          # 浮盈率
            'current_market_value_cny': float,     # 当前市值（CNY）
            'current_market_value_usd': float,     # 当前市值（USD）
            'holdings_detail': list                # 各股票明细
        }
    """
    # 获取所有持仓的symbol列表（包含currency信息）。
    # 账户级持仓下同一 (symbol, market) 可能有多行（每账户一行）；FIFO 结果
    # 目前是用户级的，这里按证券去重，避免逐账户行重复累加同一份 FIFO 队列。
    holding_rows = db.query(Holding.symbol, Holding.market, Holding.name, Holding.currency).filter(
        Holding.user_id == user_id
    ).all()
    seen_keys = set()
    holdings = []
    for row in holding_rows:
        key = (row[0], row[1])
        if key not in seen_keys:
            seen_keys.add(key)
            holdings.append(row)
    if fifo_results is None:
        fifo_results = _get_fifo_results_for_user(
            db,
            user_id,
            {(symbol, market) for symbol, market, _, _ in holdings}
        )

    total_unrealized_pnl_cny = Decimal(0)
    total_holdings_cost_cny = Decimal(0)
    total_market_value_cny = Decimal(0)
    holdings_detail = []
    unpriced_positions = []

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
            if Decimal(str(fifo_result.get('current_holdings_cost', 0))) > 0:
                unpriced_positions.append({
                    "symbol": symbol,
                    "market": market,
                    "name": name,
                })
            continue

        buy_queue = fifo_result['buy_queue']
        holdings_cost = Decimal(str(fifo_result['current_holdings_cost']))

        # 基于FIFO剩余批次计算未实现盈亏（优化1）
        unrealized_pnl = Decimal(0)
        total_qty = Decimal(0)

        for batch in buy_queue:
            batch_pnl = (Decimal(str(current_price)) - Decimal(str(batch['price']))) * Decimal(str(batch['quantity']))
            unrealized_pnl += batch_pnl
            total_qty += Decimal(str(batch['quantity']))

        market_value = Decimal(str(current_price)) * total_qty

        # 转换为CNY
        try:
            holdings_cost_cny = exchange_rate_service.convert_to_cny(db, holdings_cost, currency)
            unrealized_pnl_cny = exchange_rate_service.convert_to_cny(db, unrealized_pnl, currency)
            market_value_cny = exchange_rate_service.convert_to_cny(db, market_value, currency)
        except ValueError:
            # 如果找不到汇率，假设是CNY
            holdings_cost_cny = holdings_cost
            unrealized_pnl_cny = unrealized_pnl
            market_value_cny = market_value

        total_unrealized_pnl_cny += unrealized_pnl_cny
        total_holdings_cost_cny += holdings_cost_cny
        total_market_value_cny += market_value_cny

        holdings_detail.append({
            'symbol': symbol,
            'name': name,
            'market': market,
            'currency': currency,
            'quantity': float(total_qty),
            'current_price': current_price,
            'holdings_cost': float(holdings_cost),
            'holdings_cost_cny': float(holdings_cost_cny),
            'market_value': float(market_value),
            'market_value_cny': float(market_value_cny),
            'unrealized_pnl': float(unrealized_pnl),
            'unrealized_pnl_cny': float(unrealized_pnl_cny),
            'unrealized_pnl_rate': float(unrealized_pnl / holdings_cost * 100) if holdings_cost > 0 else 0
        })

    # 计算总收益率
    unrealized_pnl_rate = Decimal(0)
    if total_holdings_cost_cny > 0:
        unrealized_pnl_rate = total_unrealized_pnl_cny / total_holdings_cost_cny * Decimal(100)

    # 转换为USD
    try:
        total_unrealized_pnl_usd = exchange_rate_service.convert_to_usd(db, total_unrealized_pnl_cny)
        total_holdings_cost_usd = exchange_rate_service.convert_to_usd(db, total_holdings_cost_cny)
        total_market_value_usd = exchange_rate_service.convert_to_usd(db, total_market_value_cny)
    except ValueError:
        total_unrealized_pnl_usd = Decimal(0)
        total_holdings_cost_usd = Decimal(0)
        total_market_value_usd = Decimal(0)

    return {
        'unrealized_pnl_cny': float(total_unrealized_pnl_cny),
        'unrealized_pnl_usd': float(total_unrealized_pnl_usd),
        'unrealized_pnl': float(total_unrealized_pnl_cny),  # 向后兼容
        'current_holdings_cost_cny': float(total_holdings_cost_cny),
        'current_holdings_cost_usd': float(total_holdings_cost_usd),
        'current_holdings_cost': float(total_holdings_cost_cny),  # 向后兼容
        'unrealized_pnl_rate': float(unrealized_pnl_rate),
        'current_market_value_cny': float(total_market_value_cny),
        'current_market_value_usd': float(total_market_value_usd),
        'current_market_value': float(total_market_value_cny),  # 向后兼容
        'holdings_detail': holdings_detail,
        'unpriced_positions': unpriced_positions,
        'base_currency': 'CNY',
        'data_quality': _current_holdings_data_quality(fifo_results, unpriced_positions),
    }


def _current_holdings_data_quality(
    fifo_results: Dict[Tuple[str, str], Dict[str, Any]],
    unpriced_positions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """FIFO data quality plus the list of holdings excluded for lack of a price."""
    data_quality = fifo_data_quality(fifo_results)
    data_quality["unpriced_positions"] = unpriced_positions
    data_quality["unpriced_position_count"] = len(unpriced_positions)
    if unpriced_positions:
        data_quality.setdefault("warnings", []).append(
            "部分当前持仓缺少可用估值价格，其成本与市值未计入汇总。"
        )
    return data_quality


def calculate_realized_pnl_fifo(
    db: Session,
    user_id: int,
    fifo_results: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    计算已实现盈亏（FIFO方法）- 支持多币种

    Returns:
        {
            'realized_pnl_cny': float,         # 已实现盈亏总额（CNY）
            'realized_pnl_usd': float,         # 已实现盈亏总额（USD）
            'sold_cost_cny': float,            # 已卖出部分成本（CNY）
            'sold_cost_usd': float,            # 已卖出部分成本（USD）
            'realized_pnl_rate': float,        # 已实现收益率
            'trades_detail': list              # 各股票明细
        }
    """
    # 获取所有有交易的symbol（包含currency）
    symbols_query = db.query(
        Transaction.symbol,
        Transaction.market,
        Transaction.currency
    ).filter(
        Transaction.user_id == user_id
    ).distinct().all()
    if fifo_results is None:
        fifo_results = _get_fifo_results_for_user(
            db,
            user_id,
            {(symbol, market) for symbol, market, _ in symbols_query}
        )

    total_realized_pnl_cny = Decimal(0)
    total_sold_cost_cny = Decimal(0)
    trades_detail = []
    closed_trades = []

    for symbol, market, currency in symbols_query:
        currency = currency or "CNY"  # 默认CNY

        result = fifo_results.get((symbol, market), empty_fifo_result(symbol, market))

        realized_pnl = Decimal(str(result['realized_pnl']))
        sold_cost = Decimal(str(result['sold_cost']))

        if realized_pnl != 0 or sold_cost != 0:
            # 转换为CNY
            try:
                realized_pnl_cny = exchange_rate_service.convert_to_cny(db, realized_pnl, currency)
                sold_cost_cny = exchange_rate_service.convert_to_cny(db, sold_cost, currency)
            except ValueError:
                realized_pnl_cny = realized_pnl
                sold_cost_cny = sold_cost

            total_realized_pnl_cny += realized_pnl_cny
            total_sold_cost_cny += sold_cost_cny

            # Per-closing-lot detail, with pnl/cost in CNY, for per-trade metrics.
            for lot in result.get('closed_trades', []):
                lot_pnl = Decimal(str(lot['realized_pnl']))
                lot_cost = Decimal(str(lot['matched_cost']))
                try:
                    lot_pnl_cny = exchange_rate_service.convert_to_cny(db, lot_pnl, currency)
                    lot_cost_cny = exchange_rate_service.convert_to_cny(db, lot_cost, currency)
                except ValueError:
                    lot_pnl_cny = lot_pnl
                    lot_cost_cny = lot_cost
                closed_trades.append({
                    **lot,
                    'currency': currency,
                    'realized_pnl_cny': float(lot_pnl_cny),
                    'matched_cost_cny': float(lot_cost_cny),
                })

            trades_detail.append({
                'symbol': symbol,
                'market': market,
                'currency': currency,
                'realized_pnl': float(realized_pnl),
                'realized_pnl_cny': float(realized_pnl_cny),
                'sold_cost': float(sold_cost),
                'sold_cost_cny': float(sold_cost_cny),
                'realized_pnl_rate': float(realized_pnl / sold_cost * 100) if sold_cost > 0 else 0
            })

    # 计算总收益率
    realized_pnl_rate = Decimal(0)
    if total_sold_cost_cny > 0:
        realized_pnl_rate = total_realized_pnl_cny / total_sold_cost_cny * Decimal(100)

    # 转换为USD
    try:
        total_realized_pnl_usd = exchange_rate_service.convert_to_usd(db, total_realized_pnl_cny)
        total_sold_cost_usd = exchange_rate_service.convert_to_usd(db, total_sold_cost_cny)
    except ValueError:
        total_realized_pnl_usd = Decimal(0)
        total_sold_cost_usd = Decimal(0)

    return {
        'realized_pnl_cny': float(total_realized_pnl_cny),
        'realized_pnl_usd': float(total_realized_pnl_usd),
        'realized_pnl': float(total_realized_pnl_cny),  # 向后兼容
        'sold_cost_cny': float(total_sold_cost_cny),
        'sold_cost_usd': float(total_sold_cost_usd),
        'sold_cost': float(total_sold_cost_cny),  # 向后兼容
        'realized_pnl_rate': float(realized_pnl_rate),
        'trades_detail': trades_detail,
        'closed_trades': closed_trades,
        'base_currency': 'CNY',
        'data_quality': fifo_data_quality(fifo_results),
    }


def cash_dividend_amounts(action) -> Tuple[Decimal, Decimal, Decimal]:
    """现金股息金额归一：返回 (gross, tax, net)。

    显式 net_dividend=0 是有效值（如全额预扣），必须以 is not None 区分
    0 与 NULL——公司行动页汇总与本模块共用此 helper，防止两份复制逻辑漂移。
    """
    gross = Decimal(str(action.total_dividend or 0))
    tax = Decimal(str(action.tax_withheld or 0))
    net = Decimal(str(action.net_dividend if action.net_dividend is not None else gross - tax))
    return gross, tax, net


def get_dividend_summary(
    db: Session,
    user_id: int,
    *,
    dividend_actions: Optional[List[CorporateAction]] = None,
) -> Dict[str, Any]:
    """
    股息统计摘要（独立模块，不混入盈亏）- 支持多币种

    Returns:
        {
            'total_dividend_gross_cny': float,  # 税前总额（CNY）
            'total_dividend_gross_usd': float,  # 税前总额（USD）
            'total_tax_cny': float,             # 总税费（CNY）
            'total_tax_usd': float,             # 总税费（USD）
            'total_dividend_net_cny': float,    # 税后总额（CNY）
            'total_dividend_net_usd': float,    # 税后总额（USD）
            'by_symbol': list                   # 按股票分组
        }
    """
    if dividend_actions is None:
        dividend_actions = db.query(CorporateAction).filter(
            CorporateAction.user_id == user_id,
            CorporateAction.action_type == 'CASH_DIVIDEND'
        ).all()
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

        if symbol not in by_symbol:
            by_symbol[symbol] = {
                'symbol': symbol,
                'name': div.name,
                'market': div.market,
                'total_gross': Decimal(0),
                'total_gross_cny': Decimal(0),
                'total_tax': Decimal(0),
                'total_tax_cny': Decimal(0),
                'total_net': Decimal(0),
                'total_net_cny': Decimal(0),
                'count': 0,
                'currency': currency
            }

        by_symbol[symbol]['total_gross'] += gross
        by_symbol[symbol]['total_gross_cny'] += gross_cny
        by_symbol[symbol]['total_tax'] += tax
        by_symbol[symbol]['total_tax_cny'] += tax_cny
        by_symbol[symbol]['total_net'] += net
        by_symbol[symbol]['total_net_cny'] += net_cny
        by_symbol[symbol]['count'] += 1

    # 转换为USD
    try:
        total_gross_usd = exchange_rate_service.convert_to_usd(db, total_gross_cny)
        total_tax_usd = exchange_rate_service.convert_to_usd(db, total_tax_cny)
        total_net_usd = exchange_rate_service.convert_to_usd(db, total_net_cny)
    except ValueError:
        total_gross_usd = Decimal(0)
        total_tax_usd = Decimal(0)
        total_net_usd = Decimal(0)

    # 转换为列表并格式化
    by_symbol_list = [
        {
            'symbol': v['symbol'],
            'name': v['name'],
            'market': v['market'],
            'currency': v['currency'],
            'total_gross': float(v['total_gross']),
            'total_gross_cny': float(v['total_gross_cny']),
            'total_tax': float(v['total_tax']),
            'total_tax_cny': float(v['total_tax_cny']),
            'total_net': float(v['total_net']),
            'total_net_cny': float(v['total_net_cny']),
            'count': v['count']
        }
        for v in by_symbol.values()
    ]

    return {
        'total_dividend_gross_cny': float(total_gross_cny),
        'total_dividend_gross_usd': float(total_gross_usd),
        'total_dividend_gross': float(total_gross_cny),  # 向后兼容
        'total_tax_cny': float(total_tax_cny),
        'total_tax_usd': float(total_tax_usd),
        'total_tax': float(total_tax_cny),  # 向后兼容
        'total_dividend_net_cny': float(total_net_cny),
        'total_dividend_net_usd': float(total_net_usd),
        'total_dividend_net': float(total_net_cny),  # 向后兼容
        'by_symbol': by_symbol_list,
        'missing_rate_currencies': sorted(missing_rate_currencies),
        'base_currency': 'CNY'
    }


def _compose_total_realized_return(
    realized: Dict[str, Any],
    dividends: Dict[str, Any]
) -> Dict[str, Any]:
    realized_trading_pnl_cny = Decimal(str(realized.get('realized_pnl_cny', 0)))
    realized_trading_pnl_usd = Decimal(str(realized.get('realized_pnl_usd', 0)))
    sold_cost_cny = Decimal(str(realized.get('sold_cost_cny', 0)))
    sold_cost_usd = Decimal(str(realized.get('sold_cost_usd', 0)))
    net_dividend_cny = Decimal(str(dividends.get('total_dividend_net_cny', 0)))
    net_dividend_usd = Decimal(str(dividends.get('total_dividend_net_usd', 0)))

    total_realized_return_cny = realized_trading_pnl_cny + net_dividend_cny
    total_realized_return_usd = realized_trading_pnl_usd + net_dividend_usd
    total_realized_return_rate = Decimal(0)
    if sold_cost_cny > 0:
        total_realized_return_rate = total_realized_return_cny / sold_cost_cny * Decimal(100)

    return {
        'realized_trading_pnl_cny': float(realized_trading_pnl_cny),
        'realized_trading_pnl_usd': float(realized_trading_pnl_usd),
        'net_dividend_income_cny': float(net_dividend_cny),
        'net_dividend_income_usd': float(net_dividend_usd),
        'total_realized_return_cny': float(total_realized_return_cny),
        'total_realized_return_usd': float(total_realized_return_usd),
        'total_realized_return': float(total_realized_return_cny),
        'sold_cost_cny': float(sold_cost_cny),
        'sold_cost_usd': float(sold_cost_usd),
        'total_realized_return_rate': float(total_realized_return_rate),
        'rate_denominator': 'sold_cost_cny',
        'base_currency': 'CNY',
    }


def calculate_total_realized_return(db: Session, user_id: int) -> Dict[str, Any]:
    """Combine realized trading PnL with net dividend income."""
    realized = calculate_realized_pnl_fifo(db, user_id)
    dividends = get_dividend_summary(db, user_id)
    return _compose_total_realized_return(realized, dividends)


def _compose_account_total_return(
    db: Session,
    user_id: int,
    realized: Dict[str, Any],
    dividends: Dict[str, Any],
    current: Dict[str, Any],
    *,
    transactions: Optional[List[Transaction]] = None,
    dividend_actions: Optional[List[CorporateAction]] = None,
) -> Dict[str, Any]:
    realized_trading_pnl_cny = Decimal(str(realized.get('realized_pnl_cny', 0)))
    net_dividend_cny = Decimal(str(dividends.get('total_dividend_net_cny', 0)))
    unrealized_pnl_cny = Decimal(str(current.get('unrealized_pnl_cny', 0)))
    current_market_value_cny = Decimal(str(current.get('current_market_value_cny', 0)))

    total_return_cny = realized_trading_pnl_cny + unrealized_pnl_cny + net_dividend_cny
    net_invested_principal_cny = current_market_value_cny - total_return_cny

    # Convert each flow at the exchange rate on its own date, matching the TTWR
    # curve's FX basis, instead of translating every historical flow at today's
    # rate (issue #42).
    rate_lookup = _ExchangeRateLookup.from_db(db)

    cash_flows: List[Tuple[date, Decimal]] = []
    if transactions is None:
        transactions = db.query(Transaction).filter(Transaction.user_id == user_id).all()
    for txn in transactions:
        currency = txn.currency or "CNY"
        quantity = Decimal(str(txn.quantity))
        gross = quantity * Decimal(str(txn.price))
        fee = Decimal(str(txn.fee or 0))
        if txn.transaction_type == "BUY":
            amount = -(gross + fee)
        elif txn.transaction_type == "SELL":
            amount = gross - fee
        else:
            continue
        cash_flows.append((
            txn.transaction_date,
            _to_cny_on_date(db, amount, currency, txn.transaction_date, rate_lookup),
        ))

    if dividend_actions is None:
        dividend_actions = db.query(CorporateAction).filter(
            CorporateAction.user_id == user_id,
            CorporateAction.action_type == 'CASH_DIVIDEND'
        ).all()
    for div in dividend_actions:
        currency = div.currency or "CNY"
        _, _, net = cash_dividend_amounts(div)
        flow_date = div.payment_date or div.ex_date
        cash_flows.append((
            flow_date,
            _to_cny_on_date(db, net, currency, flow_date, rate_lookup),
        ))

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
        rate_denominator_basis = 'net_invested_principal_cny'
    else:
        rate_denominator_cny = peak_invested_principal_cny
        rate_denominator_basis = 'peak_invested_principal_cny'

    total_return_rate = Decimal("0")
    if rate_denominator_cny > 0:
        total_return_rate = total_return_cny / rate_denominator_cny * Decimal("100")

    if current_market_value_cny > 0:
        cash_flows.append((date.today(), current_market_value_cny))

    xirr_rate = xirr(cash_flows)

    try:
        total_return_usd = exchange_rate_service.convert_to_usd(db, total_return_cny)
        net_invested_principal_usd = exchange_rate_service.convert_to_usd(db, net_invested_principal_cny)
        current_market_value_usd = exchange_rate_service.convert_to_usd(db, current_market_value_cny)
    except ValueError:
        total_return_usd = Decimal("0")
        net_invested_principal_usd = Decimal("0")
        current_market_value_usd = Decimal("0")

    return {
        'total_return_cny': float(total_return_cny),
        'total_return_usd': float(total_return_usd),
        'total_return': float(total_return_cny),
        'total_return_rate': float(total_return_rate),
        'annualized_return_rate': float(xirr_rate * Decimal("100")) if xirr_rate is not None else None,
        'net_invested_principal_cny': float(net_invested_principal_cny),
        'net_invested_principal_usd': float(net_invested_principal_usd),
        'peak_invested_principal_cny': float(peak_invested_principal_cny),
        'current_market_value_cny': float(current_market_value_cny),
        'current_market_value_usd': float(current_market_value_usd),
        'realized_trading_pnl_cny': float(realized_trading_pnl_cny),
        'unrealized_pnl_cny': float(unrealized_pnl_cny),
        'net_dividend_income_cny': float(net_dividend_cny),
        'cash_flow_count': len(cash_flows),
        'rate_denominator': rate_denominator_basis,
        'annualized_method': 'xirr',
        'fx_basis': 'transaction_date',
        'base_currency': 'CNY',
        'calculation_status': 'estimated',
        'calculation_scope': 'invested_securities_only',
        'methodology_notes': [
            'Account cash and external deposits or withdrawals are not included.',
            'Security buys, sells and dividends are used as XIRR cash-flow proxies.',
        ],
    }


def _build_price_maps(
    db: Session,
    symbols: List[Tuple[str, str, str]],
    start_date: date,
    end_date: date,
) -> Tuple[Dict[Tuple[str, str], Dict[date, Decimal]], Dict[Tuple[str, str], int]]:
    # Two batched queries for all symbols instead of two round-trips per symbol
    # (issue #49): one IN query for in-range rows, one DISTINCT ON query for the
    # latest pre-range close per (symbol, market).
    pairs = [(symbol, market) for symbol, market, _ in symbols]
    price_maps: Dict[Tuple[str, str], Dict[date, Decimal]] = {pair: {} for pair in pairs}
    counts: Dict[Tuple[str, str], int] = {}
    if not pairs:
        return price_maps, counts

    rows = db.query(
        SecurityPrice.symbol,
        SecurityPrice.market,
        SecurityPrice.price_date,
        SecurityPrice.close_price,
    ).filter(
        tuple_(SecurityPrice.symbol, SecurityPrice.market).in_(pairs),
        SecurityPrice.price_date >= start_date,
        SecurityPrice.price_date <= end_date,
    ).all()
    for symbol, market, price_date, close_price in rows:
        if close_price is not None:
            price_maps[(symbol, market)][price_date] = Decimal(str(close_price))

    for pair in pairs:
        counts[pair] = len(price_maps[pair])

    opening_rows = db.query(
        SecurityPrice.symbol,
        SecurityPrice.market,
        SecurityPrice.price_date,
        SecurityPrice.close_price,
    ).filter(
        tuple_(SecurityPrice.symbol, SecurityPrice.market).in_(pairs),
        SecurityPrice.price_date < start_date,
    ).order_by(
        SecurityPrice.symbol,
        SecurityPrice.market,
        SecurityPrice.price_date.desc(),
    ).distinct(SecurityPrice.symbol, SecurityPrice.market).all()
    for symbol, market, price_date, close_price in opening_rows:
        if close_price is not None:
            price_maps[(symbol, market)][price_date] = Decimal(str(close_price))

    return price_maps, counts


def calculate_performance_analytics(
    db: Session,
    user_id: int,
    current_prices: Dict[str, float],
    *,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    risk_free_rate: Decimal = Decimal("0"),
    refresh_history: bool = False,
) -> Dict[str, Any]:
    if start_date and end_date and end_date < start_date:
        raise ValueError("end_date must be on or after start_date")

    transactions = db.query(Transaction).filter(
        Transaction.user_id == user_id
    ).order_by(Transaction.transaction_date, Transaction.id).all()
    corporate_actions = db.query(CorporateAction).filter(
        CorporateAction.user_id == user_id,
    ).order_by(CorporateAction.ex_date, CorporateAction.id).all()

    if not transactions:
        return {
            "base_currency": "CNY",
            "calculation_level": "empty",
            "curve": [],
            "metrics": {},
            "trade_skill": calculate_trade_skill_metrics({"trades_detail": [], "closed_trades": []}),
            "range_summary": {
                "status": "experimental",
                "realized_pnl_cny": 0.0,
                "closed_trade_count": 0,
                "dividend_net_cny": 0.0,
                "dividend_count": 0,
                "opening_market_value_cny": 0.0,
                "closing_market_value_cny": 0.0,
                "xirr_annualized_rate": None,
                "fx_basis": {"display": "latest_rate", "xirr_flows": "transaction_date"},
            },
            "methodology": {
                "status": "experimental",
                "scope": "invested_securities_only",
                "return_method": "ttwr_proxy",
                "cash_flow_assumption": "Security buys and sells are treated as external flows.",
            },
            "data_quality": {
                "warnings": ["暂无交易记录，无法生成收益率曲线。"],
                "price_history_symbols": 0,
                "total_symbols": 0,
                "missing_price_history": [],
                "sync_results": [],
            },
        }

    first_date = min(txn.transaction_date for txn in transactions)
    last_event_date = max(
        [txn.transaction_date for txn in transactions]
        + [corporate_action_curve_date(action) for action in corporate_actions]
        + [date.today()]
    )
    # 请求区间钳制到 [首笔交易日, 最后事件日]：越界只会得到平直的边界填充点，
    # 钳制并回显 requested vs effective，前端可提示"已按有效区间计算"。
    requested_start = start_date
    requested_end = end_date
    start_date = max(start_date or first_date, first_date)
    end_date = min(end_date or last_event_date, last_event_date)
    if end_date < start_date:
        # 请求区间与历史完全无交集（原始顺序合法，钳制后才反转）：
        # 钳制到最近的边界单日并置 clamped，前端据此提示"已按有效数据区间调整"，
        # 而不是在 API 预校验之后抛错变成 500。
        if requested_end is not None and requested_end < first_date:
            start_date = end_date = first_date
        else:
            start_date = end_date = last_event_date
    range_clamped = bool(
        (requested_start and requested_start != start_date)
        or (requested_end and requested_end != end_date)
    )

    symbols_by_key = {}
    for txn in transactions:
        key = (txn.symbol, txn.market)
        symbols_by_key[key] = txn.currency or infer_price_currency(txn.market)
    holdings = db.query(Holding).filter(Holding.user_id == user_id).all()
    for holding in holdings:
        key = (holding.symbol, holding.market)
        symbols_by_key.setdefault(key, holding.currency or infer_price_currency(holding.market))

    symbols = [(symbol, market, currency) for (symbol, market), currency in symbols_by_key.items()]
    sync_results = []
    if refresh_history:
        for symbol, market, currency in symbols:
            sync_results.append(
                fetch_and_store_security_price_history(
                    db,
                    symbol=symbol,
                    market=market,
                    start_date=start_date,
                    end_date=end_date,
                    currency=currency,
                )
            )

    price_maps, price_counts = _build_price_maps(db, symbols, start_date, end_date)
    curve, calculation_level, curve_quality = _build_return_curve(
        db,
        transactions,
        corporate_actions,
        price_maps,
        symbols_by_key,
        current_prices,
        start_date,
        end_date,
    )

    # Reuse the transactions/corporate actions already loaded above instead of
    # replaying the full FIFO from fresh queries (issue #49).
    fifo_results = _get_fifo_results_for_user(
        db,
        user_id,
        transactions=transactions,
        corporate_actions=corporate_actions,
    )
    realized = calculate_realized_pnl_fifo(db, user_id, fifo_results=fifo_results)
    risk_metrics = calculate_risk_metrics(curve, risk_free_rate, calculation_level)
    # 交易能力指标遵守所选区间：只统计平仓日落在区间内的已平仓交易，
    # 与同一响应里区间化的曲线/风险指标口径一致（此前为全历史，具有误导性）。
    start_iso, end_iso = start_date.isoformat(), end_date.isoformat()
    range_closed_trades = [
        trade for trade in realized["closed_trades"] if start_iso <= trade["date"] <= end_iso
    ]
    trade_skill = calculate_trade_skill_metrics({"closed_trades": range_closed_trades})
    range_realized_pnl_cny = sum(
        (Decimal(str(trade["realized_pnl_cny"])) for trade in range_closed_trades),
        Decimal("0"),
    )

    # 区间内税后股息（展示口径与股息摘要一致：最新汇率折算）
    range_dividends = []
    for action in corporate_actions:
        if action.action_type != "CASH_DIVIDEND":
            continue
        flow_date = action.payment_date or action.ex_date
        if flow_date is None or not (start_date <= flow_date <= end_date):
            continue
        _, _, net = cash_dividend_amounts(action)
        range_dividends.append((flow_date, net, action.currency or "CNY"))
    range_dividend_net_cny = Decimal("0")
    for _, net, currency in range_dividends:
        try:
            range_dividend_net_cny += exchange_rate_service.convert_to_cny(db, net, currency)
        except ValueError:
            range_dividend_net_cny += net

    # 区间资金加权收益（XIRR）：期初市值作为区间起点的合成投入流，期末市值
    # 作为终点回收流，区间内买卖/股息按各自日期汇率折算（与 TTWR 同基准，#42）。
    rate_lookup = _ExchangeRateLookup.from_db(db)
    range_cash_flows: List[Tuple[date, Decimal]] = []
    opening_market_value_cny = Decimal(str(curve_quality.get("opening_market_value_cny", 0)))
    if opening_market_value_cny > 0:
        range_cash_flows.append((start_date, -opening_market_value_cny))
    for txn in transactions:
        if not (start_date <= txn.transaction_date <= end_date):
            continue
        currency = txn.currency or "CNY"
        gross = Decimal(str(txn.quantity)) * Decimal(str(txn.price))
        fee = Decimal(str(txn.fee or 0))
        if txn.transaction_type == "BUY":
            amount = -(gross + fee)
        elif txn.transaction_type == "SELL":
            amount = gross - fee
        else:
            continue
        range_cash_flows.append((
            txn.transaction_date,
            _to_cny_on_date(db, amount, currency, txn.transaction_date, rate_lookup),
        ))
    for flow_date, net, currency in range_dividends:
        range_cash_flows.append((
            flow_date,
            _to_cny_on_date(db, net, currency, flow_date, rate_lookup),
        ))
    closing_market_value_cny = Decimal(str(curve[-1]["equity_cny"])) if curve else Decimal("0")
    if closing_market_value_cny > 0:
        range_cash_flows.append((end_date, closing_market_value_cny))
    range_xirr = xirr(range_cash_flows)
    missing_price_history = [
        {"symbol": symbol, "market": market}
        for symbol, market, _ in symbols
        if price_counts.get((symbol, market), 0) == 0
    ]
    warnings = []
    if calculation_level == "event_level":
        warnings.append("未找到足够历史行情，当前曲线使用交易事件和最近价格估算。")
    if missing_price_history:
        warnings.append("部分标的缺少历史行情，夏普率等风险指标可能不完整；可点击同步历史行情补齐。")

    should_reconcile_terminal_positions = end_date >= date.today()
    actual_holding_quantities: Dict[Tuple[str, str], Decimal] = {}
    if should_reconcile_terminal_positions:
        # 账户级持仓下同一证券可能多行，对齐曲线终点时按证券聚合数量。
        for holding in holdings:
            quantity = Decimal(str(holding.quantity))
            if quantity > 0:
                key = (holding.symbol, holding.market)
                actual_holding_quantities[key] = (
                    actual_holding_quantities.get(key, Decimal("0")) + quantity
                )

    curve_terminal_quantities = {
        (position["symbol"], position["market"]): Decimal(str(position["quantity"]))
        for position in curve_quality.get("terminal_positions", [])
    }
    terminal_position_mismatches = []
    if should_reconcile_terminal_positions:
        for key in sorted(set(actual_holding_quantities) | set(curve_terminal_quantities)):
            curve_quantity = curve_terminal_quantities.get(key, Decimal("0"))
            holding_quantity = actual_holding_quantities.get(key, Decimal("0"))
            if not decimal_close(curve_quantity, holding_quantity):
                terminal_position_mismatches.append({
                    "symbol": key[0],
                    "market": key[1],
                    "curve_quantity": float(curve_quantity),
                    "holding_quantity": float(holding_quantity),
                })

    terminal_curve_unpriced_positions = curve[-1].get("unpriced_positions", []) if curve else []
    terminal_unpriced_positions = [
        position for position in terminal_curve_unpriced_positions
        if (position["symbol"], position["market"]) in actual_holding_quantities
    ]
    curve_only_terminal_unpriced_positions = [
        position for position in terminal_curve_unpriced_positions
        if (position["symbol"], position["market"]) not in actual_holding_quantities
    ]
    if terminal_unpriced_positions:
        warnings.append("部分当前持仓缺少可用估值价格，TTWR 曲线和风险指标可能不完整。")

    terminal_curve_stale_price_positions = curve[-1].get("stale_price_positions", []) if curve else []
    terminal_stale_price_positions = [
        position for position in terminal_curve_stale_price_positions
        if (position["symbol"], position["market"]) in actual_holding_quantities
    ]
    curve_only_terminal_stale_price_positions = [
        position for position in terminal_curve_stale_price_positions
        if (position["symbol"], position["market"]) not in actual_holding_quantities
    ]
    if terminal_stale_price_positions:
        warnings.append("部分当前持仓缺少当前价格，TTWR 曲线末端使用最近历史行情估值。")

    if terminal_position_mismatches:
        warnings.append("TTWR 曲线回放持仓与当前持仓表不一致，请检查同日交易顺序、重复导入或缺失期初持仓。")
    opening_unpriced_positions = curve_quality.get("opening_unpriced_positions", [])
    if opening_unpriced_positions:
        warnings.append("部分期初持仓缺少起始日前估值价格，自定义区间 TTWR 可能不完整。")
    opening_estimated_positions = curve_quality.get("opening_estimated_positions", [])
    if opening_estimated_positions:
        warnings.append("部分期初持仓缺少历史收盘价，已使用最近交易价估算期初市值。")
    invalid_position_events = curve_quality.get("invalid_position_events", [])
    if invalid_position_events:
        warnings.append("部分卖出记录超过曲线已知持仓，TTWR 已忽略超出部分现金流；请检查交易历史是否完整。")
    if risk_metrics.get("risk_sample_count", 0) < 2:
        warnings.append("收益序列样本不足，夏普率、索提诺率和卡玛率暂不展示。")

    return {
        "base_currency": "CNY",
        "calculation_level": calculation_level,
        "methodology": {
            "status": "experimental",
            "scope": "invested_securities_only",
            "return_method": "ttwr_proxy",
            "cash_flow_assumption": "Security buys and sells are treated as external flows.",
        },
        "date_range": {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "requested_start_date": requested_start.isoformat() if requested_start else None,
            "requested_end_date": requested_end.isoformat() if requested_end else None,
            "clamped": range_clamped,
        },
        "curve": curve,
        "metrics": risk_metrics,
        "trade_skill": trade_skill,
        # 区间汇总：与 curve/metrics/trade_skill 同一窗口的资金侧数字。
        # 展示金额按最新汇率折算（与既有摘要一致）；XIRR 现金流按流水日汇率。
        "range_summary": {
            "status": "experimental",
            "realized_pnl_cny": float(range_realized_pnl_cny),
            "closed_trade_count": len(range_closed_trades),
            "dividend_net_cny": float(range_dividend_net_cny),
            "dividend_count": len(range_dividends),
            "opening_market_value_cny": float(opening_market_value_cny),
            "closing_market_value_cny": float(closing_market_value_cny),
            "xirr_annualized_rate": (
                float(range_xirr * Decimal("100")) if range_xirr is not None else None
            ),
            "fx_basis": {"display": "latest_rate", "xirr_flows": "transaction_date"},
        },
        "data_quality": {
            "warnings": warnings,
            "price_history_symbols": len(symbols) - len(missing_price_history),
            "total_symbols": len(symbols),
            "missing_price_history": missing_price_history,
            "terminal_unpriced_positions": terminal_unpriced_positions,
            "terminal_stale_price_positions": terminal_stale_price_positions,
            "opening_market_value_cny": curve_quality.get("opening_market_value_cny", 0),
            "opening_positions": curve_quality.get("opening_positions", []),
            "opening_estimated_positions": opening_estimated_positions,
            "opening_unpriced_positions": opening_unpriced_positions,
            "curve_terminal_positions": curve_quality.get("terminal_positions", []),
            "terminal_position_mismatches": terminal_position_mismatches,
            "curve_only_terminal_unpriced_positions": curve_only_terminal_unpriced_positions,
            "curve_only_terminal_stale_price_positions": curve_only_terminal_stale_price_positions,
            "invalid_position_events": invalid_position_events[:50],
            "return_method": "ttwr",
            "sync_results": sync_results,
        },
    }


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
    holdings = db.query(
        Holding.symbol, Holding.market, Holding.current_price, Holding.price_updated_at
    ).filter(Holding.user_id == user_id).all()

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
            db.query(SecurityPrice.symbol, SecurityPrice.market,
                     SecurityPrice.close_price, SecurityPrice.price_date)
            .filter(
                tuple_(SecurityPrice.symbol, SecurityPrice.market).in_(missing)
            )
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
    today = date.today()
    for key, source in sources.items():
        as_of = price_as_of.get(key)
        as_of_date = as_of.date() if isinstance(as_of, datetime) else as_of
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


def calculate_performance_summary(
    db: Session,
    user_id: int,
    current_prices: Dict[str, float]
) -> Dict[str, Any]:
    """Return the statistics tab's performance cards in one pass."""
    # Load the user's transactions and corporate actions once and share them
    # with every downstream computation (issue #49).
    transactions = db.query(Transaction).filter(
        Transaction.user_id == user_id
    ).order_by(Transaction.transaction_date, Transaction.id).all()
    corporate_actions = db.query(CorporateAction).filter(
        CorporateAction.user_id == user_id
    ).order_by(CorporateAction.ex_date, CorporateAction.id).all()
    dividend_actions = [
        action for action in corporate_actions if action.action_type == 'CASH_DIVIDEND'
    ]

    fifo_results = _get_fifo_results_for_user(
        db, user_id, transactions=transactions, corporate_actions=corporate_actions
    )
    realized = calculate_realized_pnl_fifo(db, user_id, fifo_results=fifo_results)
    dividends = get_dividend_summary(db, user_id, dividend_actions=dividend_actions)
    current = calculate_current_holdings_performance(
        db,
        user_id,
        current_prices,
        fifo_results=fifo_results
    )
    total_realized = _compose_total_realized_return(realized, dividends)
    account = _compose_account_total_return(
        db, user_id, realized, dividends, current,
        transactions=transactions, dividend_actions=dividend_actions,
    )

    return {
        'current_performance': current,
        'realized_pnl': realized,
        'dividend_summary': dividends,
        'total_realized_return': total_realized,
        'account_return': account,
    }


def build_portfolio_snapshot(db: Session, user_id: int) -> Dict[str, Any]:
    """组合快照：持仓表现 + 价格新鲜度 + 市场分布 + 近期交易 + 对账状态（路线图序 5）。

    一次调用返回看板所需的全部数据，同时是 LLM 报告（目的③）的结构化输入底座：
    所有估算口径标记（estimated/experimental）与数据质量信号原样携带。
    """
    from ..models.broker_account import BrokerAccount
    from ..models.reconciliation_snapshot import ReconciliationSnapshot

    prices, sources, freshness = resolve_server_prices(db, user_id)
    performance = calculate_performance_summary(db, user_id, prices)
    markets = get_statistics_by_market(db, user_id)

    recent_transactions = [
        {
            "id": txn.id,
            "symbol": txn.symbol,
            "name": txn.name,
            "market": txn.market,
            "transaction_type": txn.transaction_type,
            "quantity": float(txn.quantity),
            "price": float(txn.price),
            "transaction_date": txn.transaction_date.isoformat(),
            "currency": txn.currency,
            "broker_account_id": txn.broker_account_id,
        }
        for txn in db.query(Transaction)
        .filter(Transaction.user_id == user_id)
        .order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
        .limit(10)
        .all()
    ]

    # 每个账户附带最近一次对账的红绿状态（对账闭环的看板出口）。
    # 分范围对账（东财 stock/hk_connect）同一快照日会有多条：聚合最新快照日
    # 的全部 scope，整体状态取最差（任一 MISMATCHED 即红，任一 PENDING 则非绿），
    # 避免后创建的绿色 scope 把另一范围的红灯挤出首页。
    _STATUS_SEVERITY = {"MISMATCHED": 2, "PENDING": 1, "MATCHED": 0}
    accounts = []
    for account in (
        db.query(BrokerAccount)
        .filter(BrokerAccount.user_id == user_id, BrokerAccount.is_active.is_(True))
        .order_by(BrokerAccount.id)
        .all()
    ):
        latest_date = (
            db.query(ReconciliationSnapshot.snapshot_date)
            .filter(
                ReconciliationSnapshot.user_id == user_id,
                ReconciliationSnapshot.broker_account_id == account.id,
            )
            .order_by(ReconciliationSnapshot.snapshot_date.desc())
            .limit(1)
            .scalar()
        )
        latest_reconciliation = None
        if latest_date is not None:
            rows = (
                db.query(ReconciliationSnapshot)
                .filter(
                    ReconciliationSnapshot.user_id == user_id,
                    ReconciliationSnapshot.broker_account_id == account.id,
                    ReconciliationSnapshot.snapshot_date == latest_date,
                )
                .order_by(ReconciliationSnapshot.id)
                .all()
            )
            overall = max(
                (row.status for row in rows),
                key=lambda status: _STATUS_SEVERITY.get(status, 2),
            )
            latest_reconciliation = {
                "snapshot_date": latest_date.isoformat(),
                "status": overall,
                "all_scoped": all(row.statement_scope for row in rows),
                "scopes": [
                    {
                        "statement_scope": row.statement_scope,
                        "status": row.status,
                        "compared_at": (
                            row.compared_at.isoformat() if row.compared_at else None
                        ),
                    }
                    for row in rows
                ],
            }
        accounts.append({
            "id": account.id,
            "account_name": account.account_name,
            "broker": account.broker,
            "base_currency": account.base_currency,
            "latest_reconciliation": latest_reconciliation,
        })

    stale_prices = sorted(
        key for key, info in freshness.items() if info["stale"] and info["source"] != "missing"
    )
    missing_prices = sorted(
        key for key, info in freshness.items() if info["source"] == "missing"
    )
    warnings = list(
        performance["current_performance"].get("data_quality", {}).get("warnings", [])
    )
    if stale_prices:
        warnings.append(
            f"以下标的估值价格超过 {PRICE_STALE_DAYS} 天未更新：{'、'.join(stale_prices)}"
        )
    if missing_prices:
        warnings.append(f"以下标的缺少可用估值价格：{'、'.join(missing_prices)}")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_currency": "CNY",
        "prices": {
            "map": prices,
            "sources": sources,
            "freshness": freshness,
            "stale_keys": stale_prices,
            "missing_keys": missing_prices,
        },
        "performance": performance,
        "markets": markets,
        "recent_transactions": recent_transactions,
        "accounts": accounts,
        "data_quality": {
            "warnings": warnings,
            "stale_price_count": len(stale_prices),
            "missing_price_count": len(missing_prices),
        },
    }
