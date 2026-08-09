"""收益分析：区间钳制 → 行情装载/同步 → 曲线重放 → 风险与交易能力指标 → 响应组装。

原 statistics_service 的 380 行单函数按阶段拆成命名步骤（issue #136）；
每一步的数值口径与输出逐字段保持不变，重放/指标计算全部在 portfolio 内核。
"""

from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from ...models.corporate_action import CorporateAction
from ...models.holding import Holding
from ...models.security_price import SecurityPrice
from ...models.transaction import Transaction
from .. import benchmark_service
from ..market_data_service import (
    fetch_and_store_security_price_history_incremental,
    infer_price_currency,
)
from ..portfolio.benchmark import build_benchmark_series, calculate_benchmark_comparison
from ..portfolio.curve import (
    build_return_curve,
    corporate_action_curve_date,
    decimal_close,
)
from ..portfolio.fx import ExchangeRateLookup
from ..portfolio.metrics import (
    calculate_risk_metrics,
    calculate_trade_skill_metrics,
    xirr,
)
from ..portfolio.semantics import cash_dividend_amounts
from .aggregates import calculate_realized_pnl_fifo
from .fifo_results import fifo_results_for_user
from .fx import (
    DbExchangeRateLookup,
    missing_rate_warning,
    to_cny_on_date,
    to_cny_or_track_missing,
    txn_signed_cash_flow,
)


def build_price_maps(
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

    rows = (
        db.query(
            SecurityPrice.symbol,
            SecurityPrice.market,
            SecurityPrice.price_date,
            SecurityPrice.close_price,
        )
        .filter(
            tuple_(SecurityPrice.symbol, SecurityPrice.market).in_(pairs),
            SecurityPrice.price_date >= start_date,
            SecurityPrice.price_date <= end_date,
        )
        .all()
    )
    for symbol, market, price_date, close_price in rows:
        if close_price is not None:
            price_maps[(symbol, market)][price_date] = Decimal(str(close_price))

    for pair in pairs:
        counts[pair] = len(price_maps[pair])

    opening_rows = (
        db.query(
            SecurityPrice.symbol,
            SecurityPrice.market,
            SecurityPrice.price_date,
            SecurityPrice.close_price,
        )
        .filter(
            tuple_(SecurityPrice.symbol, SecurityPrice.market).in_(pairs),
            SecurityPrice.price_date < start_date,
        )
        .order_by(
            SecurityPrice.symbol,
            SecurityPrice.market,
            SecurityPrice.price_date.desc(),
        )
        .distinct(SecurityPrice.symbol, SecurityPrice.market)
        .all()
    )
    for symbol, market, price_date, close_price in opening_rows:
        if close_price is not None:
            price_maps[(symbol, market)][price_date] = Decimal(str(close_price))

    return price_maps, counts


def _empty_analytics_response() -> Dict[str, Any]:
    """无交易记录时的完整空响应（字段与正常响应同构）。"""
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


def _clamp_range(
    transactions: List[Transaction],
    corporate_actions: List[CorporateAction],
    start_date: Optional[date],
    end_date: Optional[date],
) -> Tuple[date, date, Optional[date], Optional[date], bool]:
    """请求区间钳制到 [首笔交易日, 最后事件日]，回显 requested vs effective。"""
    first_date = min(txn.transaction_date for txn in transactions)
    last_event_date = max(
        [txn.transaction_date for txn in transactions]
        + [corporate_action_curve_date(action) for action in corporate_actions]
        + [date.today()]
    )
    # 越界只会得到平直的边界填充点，钳制并回显，前端可提示"已按有效区间计算"。
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
    return start_date, end_date, requested_start, requested_end, range_clamped


def _sync_price_history(
    db: Session,
    symbols: List[Tuple[str, str, str]],
    holdings: List[Holding],
    transactions: List[Transaction],
    start_date: date,
    end_date: date,
    benchmarks: Optional[List[str]],
) -> List[Dict[str, Any]]:
    """refresh_history=True 时的内联增量行情同步（含请求的基准指数）。"""
    # 已清仓标的的同步终点钳到最后一笔交易日：回测只需要持有区间内的
    # 行情，退市/摘牌标的清仓日后的尾部缺口永远补不上也不需要补。
    sync_results: List[Dict[str, Any]] = []
    held_keys = {
        (holding.symbol, holding.market)
        for holding in holdings
        if holding.quantity and holding.quantity > 0
    }
    last_txn_by_key: Dict[Tuple[str, str], date] = {}
    first_txn_by_key: Dict[Tuple[str, str], date] = {}
    for txn in transactions:
        key = (txn.symbol, txn.market)
        if key not in last_txn_by_key or txn.transaction_date > last_txn_by_key[key]:
            last_txn_by_key[key] = txn.transaction_date
        if key not in first_txn_by_key or txn.transaction_date < first_txn_by_key[key]:
            first_txn_by_key[key] = txn.transaction_date
    for symbol, market, currency in symbols:
        key = (symbol, market)
        symbol_start = max(start_date, first_txn_by_key.get(key, start_date))
        symbol_end = (
            end_date if key in held_keys else min(end_date, last_txn_by_key.get(key, end_date))
        )
        if symbol_end < symbol_start:
            symbol_end = symbol_start
        # 增量：已缓存区间只补边缘缺口，全覆盖时零外呼——此前每次全量
        # 重拉全部标的全区间，是 Tushare 配额的最大消耗点。
        sync_results.append(
            fetch_and_store_security_price_history_incremental(
                db,
                symbol=symbol,
                market=market,
                start_date=symbol_start,
                end_date=symbol_end,
                currency=currency,
            )
        )
    # 请求的基准指数同样内联增量同步（GET 热路径 refresh_history=false
    # 绝不外呼的约定不变）
    for code in benchmarks or []:
        sync_results.append(
            benchmark_service.sync_benchmark_history(db, code, start_date, end_date)
        )
    return sync_results


def _build_range_summary(
    db: Session,
    realized: Dict[str, Any],
    transactions: List[Transaction],
    corporate_actions: List[CorporateAction],
    curve: List[Dict[str, Any]],
    curve_quality: Dict[str, Any],
    start_date: date,
    end_date: date,
    rate_lookup: ExchangeRateLookup,
) -> Tuple[Dict[str, Any], Dict[str, Any], Set[str]]:
    """区间汇总 + 区间化交易能力指标。

    展示金额按最新汇率折算（与既有摘要一致）；XIRR 现金流按流水日汇率
    （与 TTWR 同基准，#42）。返回 (range_summary, trade_skill, 缺汇率币种)。
    """
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
    range_missing_rates: Set[str] = set()
    for _, net, currency in range_dividends:
        range_dividend_net_cny += to_cny_or_track_missing(db, net, currency, range_missing_rates)

    # 区间资金加权收益（XIRR）：期初市值作为区间起点的合成投入流，期末市值
    # 作为终点回收流，区间内买卖/股息按各自日期汇率折算。
    range_cash_flows: List[Tuple[date, Decimal]] = []
    opening_market_value_cny = Decimal(str(curve_quality.get("opening_market_value_cny", 0)))
    if opening_market_value_cny > 0:
        range_cash_flows.append((start_date, -opening_market_value_cny))
    for txn in transactions:
        if not (start_date <= txn.transaction_date <= end_date):
            continue
        amount = txn_signed_cash_flow(txn)
        if amount is None:
            continue
        range_cash_flows.append(
            (
                txn.transaction_date,
                to_cny_on_date(amount, txn.currency or "CNY", txn.transaction_date, rate_lookup),
            )
        )
    for flow_date, net, currency in range_dividends:
        range_cash_flows.append(
            (
                flow_date,
                to_cny_on_date(net, currency, flow_date, rate_lookup),
            )
        )
    closing_market_value_cny = Decimal(str(curve[-1]["equity_cny"])) if curve else Decimal("0")
    if closing_market_value_cny > 0:
        range_cash_flows.append((end_date, closing_market_value_cny))
    range_xirr = xirr(range_cash_flows)

    range_summary = {
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
    }
    return range_summary, trade_skill, range_missing_rates


def _reconcile_terminal_positions(
    holdings: List[Holding],
    curve: List[Dict[str, Any]],
    curve_quality: Dict[str, Any],
    end_date: date,
) -> Dict[str, Any]:
    """曲线终点持仓 vs 当前持仓表的对账（仅当区间终点覆盖今天时）。"""
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
                terminal_position_mismatches.append(
                    {
                        "symbol": key[0],
                        "market": key[1],
                        "curve_quantity": float(curve_quantity),
                        "holding_quantity": float(holding_quantity),
                    }
                )

    terminal_curve_unpriced_positions = curve[-1].get("unpriced_positions", []) if curve else []
    terminal_unpriced_positions = [
        position
        for position in terminal_curve_unpriced_positions
        if (position["symbol"], position["market"]) in actual_holding_quantities
    ]
    curve_only_terminal_unpriced_positions = [
        position
        for position in terminal_curve_unpriced_positions
        if (position["symbol"], position["market"]) not in actual_holding_quantities
    ]

    terminal_curve_stale_price_positions = (
        curve[-1].get("stale_price_positions", []) if curve else []
    )
    terminal_stale_price_positions = [
        position
        for position in terminal_curve_stale_price_positions
        if (position["symbol"], position["market"]) in actual_holding_quantities
    ]
    curve_only_terminal_stale_price_positions = [
        position
        for position in terminal_curve_stale_price_positions
        if (position["symbol"], position["market"]) not in actual_holding_quantities
    ]

    return {
        "terminal_position_mismatches": terminal_position_mismatches,
        "terminal_unpriced_positions": terminal_unpriced_positions,
        "curve_only_terminal_unpriced_positions": curve_only_terminal_unpriced_positions,
        "terminal_stale_price_positions": terminal_stale_price_positions,
        "curve_only_terminal_stale_price_positions": curve_only_terminal_stale_price_positions,
    }


def _build_benchmarks(
    db: Session,
    benchmarks: List[str],
    curve: List[Dict[str, Any]],
    risk_metrics: Dict[str, Any],
    start_date: date,
    end_date: date,
    warnings: List[str],
) -> List[Dict[str, Any]]:
    """基准对比：每个请求的基准 code 一个块；对比缺口的提示追加进 warnings。

    指数收盘价按曲线区间加载（含起点前最近一行做基点），喂纯内核对齐组合
    曲线栅格。
    """
    benchmarks_payload: List[Dict[str, Any]] = []
    curve_dates = [date.fromisoformat(point["date"]) for point in curve]
    portfolio_total_return = risk_metrics.get("total_return_rate")
    for code in benchmarks:
        meta = benchmark_service.BENCHMARKS[code]
        closes = benchmark_service.load_benchmark_closes(db, code, start_date, end_date)
        series = build_benchmark_series(closes, curve_dates)
        block: Dict[str, Any] = {
            "code": code,
            "name": meta["name"],
            "currency": meta["currency"],
            **series,
        }
        if series.get("status") == "ok":
            comparison = calculate_benchmark_comparison(portfolio_total_return, series)
            block["comparison"] = comparison
            if comparison is None and series.get("alignment") == "first_available":
                # 基准数据晚于区间起点：计量区间不一致，不产出超额收益
                warnings.append(
                    f"基准指数 {meta['name']} 数据晚于区间起点"
                    f"（缺 {series.get('alignment_gap_days', 0)} 天），"
                    "计量区间不一致，未计算超额收益；可同步更早历史行情补齐。"
                )
        else:
            warnings.append(
                f"基准指数 {meta['name']} 暂无历史行情，无法对比；"
                "可点击同步历史行情或检查 Tushare 配置。"
            )
        benchmarks_payload.append(block)
    return benchmarks_payload


def calculate_performance_analytics(
    db: Session,
    user_id: int,
    current_prices: Dict[str, float],
    *,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    risk_free_rate: Decimal = Decimal("0"),
    refresh_history: bool = False,
    benchmarks: Optional[List[str]] = None,
) -> Dict[str, Any]:
    if start_date and end_date and end_date < start_date:
        raise ValueError("end_date must be on or after start_date")

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

    if not transactions:
        return _empty_analytics_response()

    start_date, end_date, requested_start, requested_end, range_clamped = _clamp_range(
        transactions, corporate_actions, start_date, end_date
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
    sync_results = (
        _sync_price_history(db, symbols, holdings, transactions, start_date, end_date, benchmarks)
        if refresh_history
        else []
    )

    # 汇率查表一请求只构造一次（issue #136：此前曲线与区间 XIRR 各自全量加载）。
    rate_lookup = DbExchangeRateLookup.from_db(db)

    price_maps, price_counts = build_price_maps(db, symbols, start_date, end_date)
    curve, calculation_level, curve_quality = build_return_curve(
        transactions,
        corporate_actions,
        price_maps,
        symbols_by_key,
        current_prices,
        start_date,
        end_date,
        rate_lookup=rate_lookup,
        fallback_currency=infer_price_currency,
        today=date.today(),
    )

    # Reuse the transactions/corporate actions already loaded above instead of
    # replaying the full FIFO from fresh queries (issue #49).
    fifo_results = fifo_results_for_user(
        db, user_id, transactions=transactions, corporate_actions=corporate_actions
    )
    realized = calculate_realized_pnl_fifo(db, user_id, fifo_results=fifo_results)
    risk_metrics = calculate_risk_metrics(curve, risk_free_rate, calculation_level)
    range_summary, trade_skill, range_missing_rates = _build_range_summary(
        db,
        realized,
        transactions,
        corporate_actions,
        curve,
        curve_quality,
        start_date,
        end_date,
        rate_lookup,
    )

    missing_price_history = [
        {"symbol": symbol, "market": market}
        for symbol, market, _ in symbols
        if price_counts.get((symbol, market), 0) == 0
    ]
    warnings = []
    if calculation_level == "event_level":
        warnings.append("未找到足够历史行情，当前曲线使用交易事件和最近价格估算。")
    if missing_price_history:
        warnings.append(
            "部分标的缺少历史行情，夏普率等风险指标可能不完整；可点击同步历史行情补齐。"
        )

    terminal = _reconcile_terminal_positions(holdings, curve, curve_quality, end_date)
    if terminal["terminal_unpriced_positions"]:
        warnings.append("部分当前持仓缺少可用估值价格，TTWR 曲线和风险指标可能不完整。")
    if terminal["terminal_stale_price_positions"]:
        warnings.append("部分当前持仓缺少当前价格，TTWR 曲线末端使用最近历史行情估值。")
    if terminal["terminal_position_mismatches"]:
        warnings.append(
            "TTWR 曲线回放持仓与当前持仓表不一致，请检查同日交易顺序、重复导入或缺失期初持仓。"
        )
    opening_unpriced_positions = curve_quality.get("opening_unpriced_positions", [])
    if opening_unpriced_positions:
        warnings.append("部分期初持仓缺少起始日前估值价格，自定义区间 TTWR 可能不完整。")
    opening_estimated_positions = curve_quality.get("opening_estimated_positions", [])
    if opening_estimated_positions:
        warnings.append("部分期初持仓缺少历史收盘价，已使用最近交易价估算期初市值。")
    invalid_position_events = curve_quality.get("invalid_position_events", [])
    if invalid_position_events:
        warnings.append(
            "部分卖出记录超过曲线已知持仓，TTWR 已忽略超出部分现金流；请检查交易历史是否完整。"
        )
    if risk_metrics.get("risk_sample_count", 0) < 2:
        warnings.append("收益序列样本不足，夏普率、索提诺率和卡玛率暂不展示。")
    # 缺汇率：区间汇总（已实现盈亏等）会剔除这些币种，但 TTWR 曲线为保持时间序
    # 连续性仍按原值参与（剔除会让持仓在曲线上凭空消失、制造假暴跌）。两种口径
    # 都必须让用户看见，否则曲线与汇总的差异无从解释。
    analytics_missing_rates = (
        set(realized.get("missing_rate_currencies") or []) | range_missing_rates
    )
    missing_rate_note = missing_rate_warning(analytics_missing_rates)
    if missing_rate_note:
        warnings.append(
            f"{missing_rate_note}TTWR 曲线为保持连续性仍按原币数值参与，"
            "故曲线与汇总口径在这些币种上不可直接比较。"
        )

    benchmarks_payload = (
        _build_benchmarks(db, benchmarks, curve, risk_metrics, start_date, end_date, warnings)
        if benchmarks
        else []
    )

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
        **({"benchmarks": benchmarks_payload} if benchmarks else {}),
        "metrics": risk_metrics,
        "trade_skill": trade_skill,
        # 区间汇总：与 curve/metrics/trade_skill 同一窗口的资金侧数字。
        "range_summary": range_summary,
        "data_quality": {
            "warnings": warnings,
            "price_history_symbols": len(symbols) - len(missing_price_history),
            "total_symbols": len(symbols),
            "missing_price_history": missing_price_history,
            "terminal_unpriced_positions": terminal["terminal_unpriced_positions"],
            "terminal_stale_price_positions": terminal["terminal_stale_price_positions"],
            "opening_market_value_cny": curve_quality.get("opening_market_value_cny", 0),
            "opening_positions": curve_quality.get("opening_positions", []),
            "opening_estimated_positions": opening_estimated_positions,
            "opening_unpriced_positions": opening_unpriced_positions,
            "curve_terminal_positions": curve_quality.get("terminal_positions", []),
            "terminal_position_mismatches": terminal["terminal_position_mismatches"],
            "curve_only_terminal_unpriced_positions": terminal[
                "curve_only_terminal_unpriced_positions"
            ],
            "curve_only_terminal_stale_price_positions": terminal[
                "curve_only_terminal_stale_price_positions"
            ],
            "invalid_position_events": invalid_position_events[:50],
            "return_method": "ttwr",
            "sync_results": sync_results,
        },
    }
