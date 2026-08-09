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
from .semantics import bonus_share_factor, cash_dividend_amounts, split_share_factor


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
) -> Decimal:
    key = (action.symbol, action.market)
    cash_in_cny = Decimal("0")

    if action.action_type in {"STOCK_DIVIDEND", "BONUS_ISSUE"}:
        factor = bonus_share_factor(action, positions[key])
        if factor is not None:
            positions[key] *= factor

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
]:
    positions: Dict[Tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    last_prices: Dict[Tuple[str, str], Decimal] = {}
    last_price_dates: Dict[Tuple[str, str], date] = {}
    last_price_sources: Dict[Tuple[str, str], str] = {}
    invalid_position_events = []

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
                apply_position_corporate_action(
                    action,
                    positions,
                    event_date,
                    rate_lookup,
                    fallback_currency,
                )
                key = (action.symbol, action.market)
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
    return positions, last_prices, invalid_position_events, opening_estimated_positions


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
    }
