"""持仓重放与 TTWR 收益曲线（纯内核，无 DB 依赖）。

与 statistics_service 中原实现逐行为等价，仅把三个隐式依赖显式化：
- rate_lookup：日期感知汇率查找（原先内部 from_db 构造）
- fallback_currency(market)：币种兜底推断（原先直接调 market_data_service）
- today：``date.today()`` 由调用方传入，回测/反事实可自由设定"现在"
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .fx import ExchangeRateLookup, convert_on_date
from .semantics import (
    OPENING_POSITION,
    bonus_share_factor,
    cash_dividend_amounts,
    opening_position_lot,
    split_share_factor,
)


def get_current_price(current_prices: Dict[str, float], symbol: str, market: str) -> Optional[Decimal]:
    candidates = (
        f"{symbol}:{market}",
        f"{market}:{symbol}",
        symbol,
    )
    for key in candidates:
        value = current_prices.get(key)
        if value is not None and value > 0:
            return Decimal(str(value))
    return None


def corporate_action_curve_date(action) -> date:
    if action.action_type == "CASH_DIVIDEND":
        return action.payment_date or action.ex_date
    return action.ex_date


def apply_position_corporate_action(
    action,
    positions: Dict[Tuple[str, str], Decimal],
    effective_date: date,
    rate_lookup: ExchangeRateLookup,
    fallback_currency: Callable[[str], str],
    *,
    valuation_price: Optional[Decimal] = None,
    estimated_inflow_events: Optional[List[Dict[str, Any]]] = None,
    deferred_inflows: Optional[Dict[Tuple[str, str], Decimal]] = None,
    scaled_quantities: Sequence[Dict[Tuple[str, str], Decimal]] = (),
) -> Decimal:
    """返回本行动带来的外部现金流入（CNY）。

    `deferred_inflows` 与 `scaled_quantities` 里的份额是**与持仓同口径的股数**：拆股/合股/送股
    时必须随 positions 一起按同一因子变换，否则首次取得的是拆股后价格时，流入按拆前股数、
    市值按拆后股数，差额被算成收益（PR #208 评审 P2）。

    期初建仓（OPENING_POSITION）是实物到达：成本已知按成本计流入；成本未知按
    `valuation_price × 数量` 估值计入并记入 `estimated_inflow_events`——按 0 计会把
    这部分市值当成收益凭空做高 TTWR。**估值价必须与当日市值用同一套回退**（调用方
    负责：当日快照 / 历史 / 成交价），连估值价都没有时把份额记入 `deferred_inflows`，
    由调用方在首个能定价的日子按该价补记流入——否则后来取得的市值差会整段算成收益。
    """
    key = (action.symbol, action.market)
    cash_in_cny = Decimal("0")

    if action.action_type == OPENING_POSITION:
        lot = opening_position_lot(action)
        if lot is not None:
            quantity, total_cost = lot
            positions[key] += quantity
            currency = action.currency or fallback_currency(action.market)
            if total_cost is not None:
                cash_in_cny += convert_on_date(total_cost, currency, effective_date, rate_lookup)
            else:
                valued = valuation_price is not None and valuation_price > 0
                if valued:
                    cash_in_cny += convert_on_date(
                        quantity * valuation_price, currency, effective_date, rate_lookup
                    )
                elif deferred_inflows is not None:
                    deferred_inflows[key] = deferred_inflows.get(key, Decimal("0")) + quantity
                if estimated_inflow_events is not None:
                    estimated_inflow_events.append({
                        "symbol": action.symbol,
                        "market": action.market,
                        "date": effective_date.isoformat(),
                        "quantity": float(quantity),
                        "valued": bool(valued),
                        "valuation_price": float(valuation_price) if valued else None,
                        "valued_on": effective_date.isoformat() if valued else None,
                    })
        return cash_in_cny


    trackers = [tracker for tracker in (deferred_inflows, *scaled_quantities) if tracker is not None]

    def scale_tracked(factor: Decimal) -> None:
        for tracker in trackers:
            if key in tracker:
                tracker[key] *= factor

    if action.action_type in {"STOCK_DIVIDEND", "BONUS_ISSUE"}:
        factor = bonus_share_factor(action, positions[key])
        if factor is not None:
            positions[key] *= factor
            scale_tracked(factor)

    elif action.action_type == "RIGHTS_ISSUE":
        if action.subscription_quantity and action.subscription_price:
            quantity = Decimal(str(action.subscription_quantity))
            price = Decimal(str(action.subscription_price))
            total_cost = Decimal(str(action.subscription_amount)) if action.subscription_amount else quantity * price
            positions[key] += quantity
            cash_in_cny += convert_on_date(
                total_cost,
                action.currency or fallback_currency(action.market),
                effective_date,
                rate_lookup,
            )

    elif action.action_type in {"STOCK_SPLIT", "REVERSE_SPLIT"}:
        factor = split_share_factor(action, positions[key])
        if factor is not None:
            positions[key] *= factor
            scale_tracked(factor)

    return cash_in_cny


def settle_deferred_inflows(
    deferred_inflows: Dict[Tuple[str, str], Decimal],
    resolve_price: Callable[[Tuple[str, str]], Optional[Decimal]],
    current_date: date,
    rate_lookup: ExchangeRateLookup,
    currency_of: Callable[[Tuple[str, str]], str],
    estimated_inflow_events: List[Dict[str, Any]],
) -> Decimal:
    """把此前无法估价的期初建仓份额在首个能定价的日子按该价补记为外部流入。

    这一天的市值循环用的正是同一个价格，所以流入与市值同额进入分母与分子，
    当日收益为 0——实物到达本身不是收益。返回补记的 CNY 流入并回写事件的估值信息。
    """
    cash_in_cny = Decimal("0")
    for key in list(deferred_inflows):
        price = resolve_price(key)
        if price is None or price <= 0:
            continue
        quantity = deferred_inflows.pop(key)
        cash_in_cny += convert_on_date(quantity * price, currency_of(key), current_date, rate_lookup)
        for event in estimated_inflow_events:
            if (
                not event.get("valued")
                and (event.get("symbol"), event.get("market")) == key
            ):
                event["valued"] = True
                event["valuation_price"] = float(price)
                event["valued_on"] = current_date.isoformat()
    return cash_in_cny


def daily_curve_transaction_sort_key(txn) -> Tuple[str, str, int, int]:
    priority = 0 if txn.transaction_type == "BUY" else 1
    return (txn.symbol, txn.market, priority, txn.id if txn.id is not None else 10**18)


def decimal_close(left: Decimal, right: Decimal, tolerance: Decimal = Decimal("0.000001")) -> bool:
    return abs(left - right) <= tolerance


def select_curve_dates(
    price_maps: Dict[Tuple[str, str], Dict[date, Decimal]],
    transactions: Sequence[Any],
    corporate_actions: Sequence[Any],
    start_date: date,
    end_date: date,
) -> Tuple[List[date], str]:
    price_dates = {
        price_date
        for price_map in price_maps.values()
        for price_date in price_map.keys()
        if start_date <= price_date <= end_date
    }
    event_dates = {
        txn.transaction_date
        for txn in transactions
        if start_date <= txn.transaction_date <= end_date
    }
    event_dates |= {
        action_date
        for action in corporate_actions
        for action_date in [corporate_action_curve_date(action)]
        if start_date <= action_date <= end_date
    }

    boundary_dates = {start_date, end_date}
    if price_dates:
        return sorted(price_dates | event_dates | boundary_dates), "daily_price_history"
    return sorted(event_dates | boundary_dates), "event_level"


def invalid_position_event(
    event_date: date,
    key: Tuple[str, str],
    transaction_type: str,
    quantity: Decimal,
    available_quantity: Decimal,
) -> Dict[str, Any]:
    return {
        "date": event_date.isoformat(),
        "symbol": key[0],
        "market": key[1],
        "transaction_type": transaction_type,
        "quantity": float(quantity),
        "available_quantity": float(available_quantity),
    }


def replay_opening_positions(
    transactions_by_date: Dict[date, List[Any]],
    corporate_actions_by_date: Dict[date, List[Any]],
    price_maps: Dict[Tuple[str, str], Dict[date, Decimal]],
    start_date: date,
    rate_lookup: ExchangeRateLookup,
    fallback_currency: Callable[[str], str],
) -> Tuple[
    Dict[Tuple[str, str], Decimal],
    Dict[Tuple[str, str], Decimal],
    List[Dict[str, Any]],
    List[Dict[str, str]],
    List[Dict[str, Any]],
    Dict[Tuple[str, str], Decimal],
]:
    """区间开始前的重放 → (positions, last_prices, invalid_events, opening_estimated_positions,
    estimated_inflow_events, deferred_inflows)。

    deferred_inflows = 成本未知的期初建仓里**仍持有且期初无价**的份额：它们不在期初市值里，
    区间内首个能定价的日子按该价补记流入。区间前已卖出的份额不挂起（买卖都在区间前，与曲线
    无关——评审 P1：把事件原始数量全部恢复会让区间前已清仓的转入在区间内被"结算"成流入）；
    期初已有价（历史/成交价）的份额已随期初市值计入，事件按期初价标记已估值。"""
    positions: Dict[Tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    last_prices: Dict[Tuple[str, str], Decimal] = {}
    last_price_dates: Dict[Tuple[str, str], date] = {}
    last_price_sources: Dict[Tuple[str, str], str] = {}
    invalid_position_events = []
    estimated_inflow_events: List[Dict[str, Any]] = []
    # 成本未知建仓的份额（无论建仓当刻有无报价），随后续拆股/送股同步变换
    unknown_by_key: Dict[Tuple[str, str], Decimal] = {}

    for key, price_map in price_maps.items():
        prior_dates = [price_date for price_date in price_map if price_date < start_date]
        if prior_dates:
            latest_date = max(prior_dates)
            last_prices[key] = price_map[latest_date]
            last_price_dates[key] = latest_date
            last_price_sources[key] = "history"

    opening_event_dates = sorted(
        {
            event_date
            for event_date in set(transactions_by_date) | set(corporate_actions_by_date)
            if event_date < start_date
        }
    )
    for event_date in opening_event_dates:
        for action in corporate_actions_by_date.get(event_date, []):
            if action.action_type != "CASH_DIVIDEND":
                seen_events = len(estimated_inflow_events)
                apply_position_corporate_action(
                    action,
                    positions,
                    event_date,
                    rate_lookup,
                    fallback_currency,
                    valuation_price=last_prices.get((action.symbol, action.market)),
                    estimated_inflow_events=estimated_inflow_events,
                    scaled_quantities=(unknown_by_key,),
                )
                key = (action.symbol, action.market)
                for event in estimated_inflow_events[seen_events:]:
                    unknown_by_key[key] = unknown_by_key.get(key, Decimal("0")) + Decimal(
                        str(event["quantity"])
                    )
                if last_price_dates.get(key, date.min) < event_date:
                    last_prices.pop(key, None)
                    last_price_dates.pop(key, None)
                    last_price_sources.pop(key, None)

        daily_transactions = sorted(
            transactions_by_date.get(event_date, []),
            key=daily_curve_transaction_sort_key,
        )
        for txn in daily_transactions:
            key = (txn.symbol, txn.market)
            quantity = Decimal(str(txn.quantity))
            transaction_price = Decimal(str(txn.price))
            if last_price_dates.get(key, date.min) < event_date:
                last_prices[key] = transaction_price
                last_price_dates[key] = event_date
                last_price_sources[key] = "transaction"

            if txn.transaction_type == "BUY":
                positions[key] += quantity
            elif txn.transaction_type == "SELL":
                available_quantity = positions[key]
                matched_quantity = min(quantity, available_quantity)
                if matched_quantity < quantity:
                    invalid_position_events.append(
                        invalid_position_event(
                            event_date,
                            key,
                            txn.transaction_type,
                            quantity,
                            available_quantity,
                        )
                    )
                if matched_quantity > 0:
                    positions[key] -= matched_quantity

    opening_estimated_positions = [
        {"symbol": symbol, "market": market}
        for (symbol, market), quantity in sorted(positions.items())
        if quantity > 0 and last_price_sources.get((symbol, market)) == "transaction"
    ]

    # 按期初状态而不是事件原始数量决定去向（unknown_by_key 已随拆股/送股变换）
    deferred_inflows: Dict[Tuple[str, str], Decimal] = {}
    for key, quantity in unknown_by_key.items():
        held = positions.get(key, Decimal("0"))
        if held <= 0:
            continue  # 区间前已清仓：买卖都在区间前，不挂起
        opening_price = last_prices.get(key)
        if opening_price is not None and opening_price > 0:
            # 期初市值已按该价计入这些份额：事件按期初价标记，不再挂起
            for event in estimated_inflow_events:
                if not event.get("valued") and (event["symbol"], event["market"]) == key:
                    event["valued"] = True
                    event["valuation_price"] = float(opening_price)
                    event["valued_on"] = start_date.isoformat()
            continue
        deferred_inflows[key] = min(quantity, held)
    return (
        positions, last_prices, invalid_position_events, opening_estimated_positions,
        estimated_inflow_events, deferred_inflows,
    )


def build_return_curve(
    transactions: Sequence[Any],
    corporate_actions: Sequence[Any],
    price_maps: Dict[Tuple[str, str], Dict[date, Decimal]],
    currency_by_key: Dict[Tuple[str, str], str],
    current_prices: Dict[str, float],
    start_date: date,
    end_date: date,
    *,
    rate_lookup: ExchangeRateLookup,
    fallback_currency: Callable[[str], str],
    today: date,
) -> Tuple[List[Dict[str, Any]], str, Dict[str, Any]]:
    curve_dates, calculation_level = select_curve_dates(
        price_maps,
        transactions,
        corporate_actions,
        start_date,
        end_date,
    )
    if not curve_dates:
        return [], calculation_level, {"invalid_position_events": [], "terminal_positions": []}

    transactions_by_date = defaultdict(list)
    for txn in transactions:
        transactions_by_date[txn.transaction_date].append(txn)

    corporate_actions_by_date = defaultdict(list)
    for action in corporate_actions:
        corporate_actions_by_date[corporate_action_curve_date(action)].append(action)

    (
        positions,
        last_prices,
        invalid_position_events,
        opening_estimated_positions,
        estimated_inflow_events,
        deferred_inflows,
    ) = replay_opening_positions(
        transactions_by_date,
        corporate_actions_by_date,
        price_maps,
        start_date,
        rate_lookup,
        fallback_currency,
    )
    opening_positions = [
        {"symbol": symbol, "market": market, "quantity": float(quantity)}
        for (symbol, market), quantity in sorted(positions.items())
        if quantity > 0
    ]
    opening_market_value_cny = Decimal("0")
    opening_unpriced_positions = []
    for key, quantity in positions.items():
        if quantity <= 0:
            continue
        price = last_prices.get(key)
        if price is None:
            opening_unpriced_positions.append({"symbol": key[0], "market": key[1]})
            continue
        currency = currency_by_key.get(key) or fallback_currency(key[1])
        opening_market_value_cny += convert_on_date(
            quantity * price,
            currency,
            start_date,
            rate_lookup,
        )

    # deferred_inflows（来自区间前重放）：成本未知、仍持有且期初无价的份额——不在期初市值里，
    # 首个能定价的日子按该价补记流入（与区间内的处理一致），不让它变成那天的"收益"

    cumulative_cash_in_cny = opening_market_value_cny
    cumulative_cash_out_cny = Decimal("0")
    cumulative_sell_proceeds_cny = Decimal("0")
    cumulative_dividend_income_cny = Decimal("0")
    previous_market_value_cny = opening_market_value_cny
    cumulative_factor = Decimal("1")
    peak_factor = Decimal("1")
    curve = []

    for current_date in curve_dates:
        use_current_price_snapshot = current_date == end_date and end_date >= today

        def resolve_price(key: Tuple[str, str]) -> Optional[Decimal]:
            # 与下面市值循环逐字相同的回退：当日快照（仅末日=今天）> 最近历史/成交价 > 快照
            current_price = get_current_price(current_prices, key[0], key[1])
            if use_current_price_snapshot and current_price is not None:
                return current_price
            return last_prices.get(key) or current_price

        today_prices = {}
        for key, price_map in price_maps.items():
            price = price_map.get(current_date)
            if price is not None:
                last_prices[key] = price
                today_prices[key] = price

        cash_in_cny = Decimal("0")
        cash_out_cny = Decimal("0")
        sell_proceeds_today_cny = Decimal("0")
        dividend_income_today_cny = Decimal("0")

        for action in corporate_actions_by_date.get(current_date, []):
            if action.action_type == "CASH_DIVIDEND":
                _, _, net = cash_dividend_amounts(action)
                dividend_cny = convert_on_date(
                    net,
                    action.currency or "CNY",
                    current_date,
                    rate_lookup,
                )
                cash_out_cny += dividend_cny
                dividend_income_today_cny += dividend_cny
            else:
                cash_in_cny += apply_position_corporate_action(
                    action,
                    positions,
                    current_date,
                    rate_lookup,
                    fallback_currency,
                    valuation_price=resolve_price((action.symbol, action.market)),
                    estimated_inflow_events=estimated_inflow_events,
                    deferred_inflows=deferred_inflows,
                )

        daily_transactions = sorted(
            transactions_by_date.get(current_date, []),
            key=daily_curve_transaction_sort_key,
        )
        for txn in daily_transactions:
            key = (txn.symbol, txn.market)
            quantity = Decimal(str(txn.quantity))
            price = Decimal(str(txn.price))
            fee = Decimal(str(txn.fee or 0))
            currency = txn.currency or fallback_currency(txn.market)
            gross = quantity * price
            if key not in today_prices:
                last_prices[key] = price

            if txn.transaction_type == "BUY":
                positions[key] += quantity
                cash_in_cny += convert_on_date(
                    gross + fee,
                    currency,
                    current_date,
                    rate_lookup,
                )
            elif txn.transaction_type == "SELL":
                available_quantity = positions[key]
                matched_quantity = min(quantity, available_quantity)
                if matched_quantity <= 0:
                    invalid_position_events.append(
                        invalid_position_event(
                            current_date,
                            key,
                            txn.transaction_type,
                            quantity,
                            available_quantity,
                        )
                    )
                    continue
                if matched_quantity < quantity:
                    invalid_position_events.append(
                        invalid_position_event(
                            current_date,
                            key,
                            txn.transaction_type,
                            quantity,
                            available_quantity,
                        )
                    )
                positions[key] -= matched_quantity
                proceeds = gross - fee
                if quantity > 0 and matched_quantity < quantity:
                    proceeds *= matched_quantity / quantity
                proceeds_cny = convert_on_date(
                    proceeds,
                    currency,
                    current_date,
                    rate_lookup,
                )
                cash_out_cny += proceeds_cny
                sell_proceeds_today_cny += proceeds_cny

        if use_current_price_snapshot:
            for key, quantity in positions.items():
                if quantity > 0:
                    current_price = get_current_price(current_prices, key[0], key[1])
                    if current_price is not None:
                        last_prices[key] = current_price

        if deferred_inflows:
            # 当日成交价 / 快照价此时都已进 last_prices，与市值循环取价一致
            cash_in_cny += settle_deferred_inflows(
                deferred_inflows,
                resolve_price,
                current_date,
                rate_lookup,
                lambda key: currency_by_key.get(key) or fallback_currency(key[1]),
                estimated_inflow_events,
            )

        market_value_cny = Decimal("0")
        priced_positions = 0
        unpriced_positions = []
        stale_price_positions = []
        for key, quantity in positions.items():
            if quantity <= 0:
                continue
            current_price = get_current_price(current_prices, key[0], key[1])
            price = current_price if use_current_price_snapshot and current_price is not None else last_prices.get(key) or current_price
            if price is None:
                unpriced_positions.append({"symbol": key[0], "market": key[1]})
                continue
            if use_current_price_snapshot and current_price is None:
                stale_price_positions.append({"symbol": key[0], "market": key[1]})
            priced_positions += 1
            currency = currency_by_key.get(key) or fallback_currency(key[1])
            market_value_cny += convert_on_date(
                quantity * price,
                currency,
                current_date,
                rate_lookup,
            )

        denominator = previous_market_value_cny + cash_in_cny
        numerator = market_value_cny + cash_out_cny
        period_return_cny = numerator - denominator
        daily_return_rate = None
        if denominator > 0:
            daily_return = numerator / denominator - Decimal("1")
            cumulative_factor *= Decimal("1") + daily_return
            daily_return_rate = float(daily_return * Decimal("100"))

        if cumulative_factor > peak_factor:
            peak_factor = cumulative_factor
        drawdown_rate = (
            ((cumulative_factor - peak_factor) / peak_factor) * Decimal("100")
            if peak_factor > 0
            else Decimal("0")
        )
        cumulative_return_rate = (cumulative_factor - Decimal("1")) * Decimal("100")
        cumulative_cash_in_cny += cash_in_cny
        cumulative_cash_out_cny += cash_out_cny
        cumulative_sell_proceeds_cny += sell_proceeds_today_cny
        cumulative_dividend_income_cny += dividend_income_today_cny

        curve.append({
            "date": current_date.isoformat(),
            "equity_cny": float(market_value_cny),
            "market_value_cny": float(market_value_cny),
            "begin_market_value_cny": float(previous_market_value_cny),
            "cash_in_cny": float(cash_in_cny),
            "cash_out_cny": float(cash_out_cny),
            "cumulative_cash_in_cny": float(cumulative_cash_in_cny),
            "cumulative_cash_out_cny": float(cumulative_cash_out_cny),
            "capital_base_cny": float(cumulative_cash_in_cny),
            "net_invested_principal_cny": float(cumulative_cash_in_cny - cumulative_cash_out_cny),
            "sell_proceeds_cny": float(cumulative_sell_proceeds_cny),
            "dividend_income_cny": float(cumulative_dividend_income_cny),
            "total_return_cny": float(period_return_cny),
            "cumulative_return_rate": float(cumulative_return_rate),
            "drawdown_rate": float(drawdown_rate),
            "daily_return_rate": daily_return_rate,
            "return_method": "ttwr",
            "priced_positions": priced_positions,
            "unpriced_positions": unpriced_positions,
            "stale_price_positions": stale_price_positions,
        })
        previous_market_value_cny = market_value_cny

    terminal_positions = [
        {"symbol": symbol, "market": market, "quantity": float(quantity)}
        for (symbol, market), quantity in sorted(positions.items())
        if quantity > 0
    ]

    return curve, calculation_level, {
        "invalid_position_events": invalid_position_events,
        "opening_market_value_cny": float(opening_market_value_cny),
        "opening_positions": opening_positions,
        "opening_estimated_positions": opening_estimated_positions,
        "opening_unpriced_positions": opening_unpriced_positions,
        "terminal_positions": terminal_positions,
        # 成本未知的期初建仓：流入按与当日市值同一回退的价格估算并记 valuation_price；
        # 到达当天无价可估的份额挂起，在首个能定价的日子（valued_on）按该价补记流入，
        # valued=False = 直到区间末仍无任何可用价格（此时它也不在市值里）
        "estimated_inflow_events": estimated_inflow_events,
    }
