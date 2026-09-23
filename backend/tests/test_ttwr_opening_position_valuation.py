"""成本未知的期初建仓在 TTWR 曲线里的估值（#174 评审 P1）。

实物到达不是收益：流入必须用与当日市值**同一套**价格回退估值；连估值价都没有的
份额挂起，在首个能定价的日子按该价补记流入。三种形态各一例，直接调纯内核。
"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.portfolio.curve import build_return_curve
from app.services.portfolio.fx import ExchangeRateLookup

D1, D2, D3 = date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)
A, B = ("600000", "A股"), ("161226", "A股")
RATES = ExchangeRateLookup([])


def _buy(symbol, market, quantity, price, on, txn_id=1, txn_type="BUY"):
    return SimpleNamespace(
        id=txn_id, symbol=symbol, market=market, transaction_type=txn_type,
        quantity=Decimal(quantity), price=Decimal(price), fee=Decimal("0"),
        currency="CNY", transaction_date=on,
    )


def _opening(symbol, market, quantity, on, **cost):
    return SimpleNamespace(
        action_type="OPENING_POSITION", symbol=symbol, market=market, ex_date=on,
        payment_date=None, currency="CNY", adjusted_quantity=Decimal(quantity),
        adjusted_cost_per_share=cost.get("cost_per_share"),
        cost_basis_adjustment=cost.get("total_cost"),
        broker_account_id=1,
    )


def _curve(transactions, actions, price_maps, current_prices, *, start, end, today):
    return build_return_curve(
        transactions, actions, price_maps, {A: "CNY", B: "CNY"}, current_prices,
        start, end, rate_lookup=RATES, fallback_currency=lambda market: "CNY", today=today,
    )


def test_unknown_cost_inflow_uses_the_same_snapshot_price_as_market_value():
    """评审复现：无历史行情、但当日快照有价（末日=今天）。流入与市值同价，收益为 0。"""
    curve, _, quality = _curve(
        [_buy(*A, "100", "10", D1)],
        [_opening(*B, "100", D2)],
        {A: {D1: Decimal("10"), D2: Decimal("10")}},
        {"161226:A股": 10.0},
        start=D1, end=D2, today=D2,
    )
    last = curve[-1]
    assert last["cash_in_cny"] == pytest.approx(1000.0)
    assert last["market_value_cny"] == pytest.approx(2000.0)
    assert last["daily_return_rate"] == pytest.approx(0.0)
    assert last["cumulative_return_rate"] == pytest.approx(0.0)
    (event,) = quality["estimated_inflow_events"]
    assert event["valued"] is True and event["valuation_price"] == 10.0
    assert event["valued_on"] == D2.isoformat()


def test_unpriceable_inflow_is_settled_on_the_first_priced_day_not_as_return():
    """到达当天无任何价格：市值不含它、流入 0；次日历史行情出现 → 按该价补记流入。"""
    curve, _, quality = _curve(
        [_buy(*A, "100", "10", D1)],
        [_opening(*B, "100", D2)],
        {A: {D1: Decimal("10"), D2: Decimal("10"), D3: Decimal("10")}, B: {D3: Decimal("8")}},
        {},
        start=D1, end=D3, today=date(2026, 3, 10),
    )
    by_date = {point["date"]: point for point in curve}
    arrival, priced = by_date[D2.isoformat()], by_date[D3.isoformat()]
    assert arrival["cash_in_cny"] == 0.0 and arrival["market_value_cny"] == pytest.approx(1000.0)
    assert arrival["unpriced_positions"] == [{"symbol": "161226", "market": "A股"}]
    assert priced["cash_in_cny"] == pytest.approx(800.0)
    assert priced["market_value_cny"] == pytest.approx(1800.0)
    assert priced["daily_return_rate"] == pytest.approx(0.0)
    assert curve[-1]["cumulative_return_rate"] == pytest.approx(0.0)
    (event,) = quality["estimated_inflow_events"]
    assert event["valued"] is True and event["valuation_price"] == 8.0
    assert event["valued_on"] == D3.isoformat()


def test_never_priced_inflow_stays_out_of_both_inflow_and_market_value():
    curve, _, quality = _curve(
        [_buy(*A, "100", "10", D1)],
        [_opening(*B, "100", D2)],
        {A: {D1: Decimal("10"), D2: Decimal("10")}},
        {},
        start=D1, end=D2, today=date(2026, 3, 10),
    )
    assert curve[-1]["cash_in_cny"] == 0.0 and curve[-1]["market_value_cny"] == pytest.approx(1000.0)
    assert curve[-1]["cumulative_return_rate"] == pytest.approx(0.0)
    (event,) = quality["estimated_inflow_events"]
    assert event["valued"] is False and event["valued_on"] is None


def test_known_cost_inflow_is_booked_at_cost_regardless_of_price():
    curve, _, quality = _curve(
        [_buy(*A, "100", "10", D1)],
        [_opening(*B, "100", D2, cost_per_share=Decimal("6"))],
        {A: {D1: Decimal("10"), D2: Decimal("10")}},
        {"161226:A股": 10.0},
        start=D1, end=D2, today=D2,
    )
    last = curve[-1]
    assert last["cash_in_cny"] == pytest.approx(600.0)
    assert last["market_value_cny"] == pytest.approx(2000.0)
    # (2000 − 1600) / 1600 = 25%：真实的持有收益，不是到达本身
    assert last["daily_return_rate"] == pytest.approx(25.0)
    assert quality["estimated_inflow_events"] == []


D0, D4 = date(2026, 3, 1), date(2026, 3, 5)


def test_unknown_cost_lot_cleared_before_the_window_is_not_settled_as_inflow():
    """评审 P1 复现：3/2 转入 B（无价）、3/3 全部卖出、只看 3/4——期初期末都只有 A，收益 0%。"""
    curve, _, quality = _curve(
        [_buy(*A, "100", "10", D1), _buy(*B, "100", "10", D2, txn_id=2, txn_type="SELL")],
        [_opening(*B, "100", D1)],
        {A: {D1: Decimal("10"), D2: Decimal("10"), D3: Decimal("10")}},
        {},
        start=D3, end=D3, today=date(2026, 3, 10),
    )
    (point,) = curve
    assert point["cash_in_cny"] == 0.0 and point["market_value_cny"] == pytest.approx(1000.0)
    assert point["daily_return_rate"] == pytest.approx(0.0)
    assert point["cumulative_return_rate"] == pytest.approx(0.0)
    assert quality["estimated_inflow_events"][0]["valued"] is False


def test_partially_sold_unknown_cost_lot_is_in_opening_market_value_via_sale_price():
    """区间前卖出 40 股（成交价给了期初价）：剩余 60 股已在期初市值里，不再挂起，收益 0%。"""
    curve, _, quality = _curve(
        [_buy(*A, "100", "10", D1), _buy(*B, "40", "10", D2, txn_id=2, txn_type="SELL")],
        [_opening(*B, "100", D1)],
        {A: {D1: Decimal("10"), D2: Decimal("10"), D3: Decimal("10")}, B: {D3: Decimal("10")}},
        {},
        start=D3, end=D3, today=date(2026, 3, 10),
    )
    (point,) = curve
    assert quality["opening_market_value_cny"] == pytest.approx(1600.0)
    assert point["cash_in_cny"] == 0.0 and point["market_value_cny"] == pytest.approx(1600.0)
    assert point["daily_return_rate"] == pytest.approx(0.0)
    (event,) = quality["estimated_inflow_events"]
    assert event["valued"] is True and event["valuation_price"] == 10.0 and event["valued_on"] == D3.isoformat()


def test_unknown_cost_lot_still_held_and_unpriced_at_start_is_settled_when_priced():
    """区间前转入、期初无价、仍持有：不在期初市值里；区间内首次有价那天按该价补记流入。"""
    curve, _, quality = _curve(
        [_buy(*A, "100", "10", D1)],
        [_opening(*B, "100", D2)],
        {A: {D1: Decimal("10"), D3: Decimal("10"), D4: Decimal("10")}, B: {D4: Decimal("8")}},
        {},
        start=D3, end=D4, today=date(2026, 3, 10),
    )
    by_date = {point["date"]: point for point in curve}
    assert quality["opening_unpriced_positions"] == [{"symbol": "161226", "market": "A股"}]
    assert by_date[D3.isoformat()]["market_value_cny"] == pytest.approx(1000.0)
    priced = by_date[D4.isoformat()]
    assert priced["cash_in_cny"] == pytest.approx(800.0)
    assert priced["market_value_cny"] == pytest.approx(1800.0)
    assert priced["daily_return_rate"] == pytest.approx(0.0)
    assert curve[-1]["cumulative_return_rate"] == pytest.approx(0.0)


def test_unknown_cost_lot_priced_by_history_before_start_is_not_deferred():
    curve, _, quality = _curve(
        [_buy(*A, "100", "10", D1)],
        [_opening(*B, "100", D1)],
        {A: {D1: Decimal("10"), D3: Decimal("10")}, B: {D2: Decimal("8"), D3: Decimal("8")}},
        {},
        start=D3, end=D3, today=date(2026, 3, 10),
    )
    (point,) = curve
    assert quality["opening_market_value_cny"] == pytest.approx(1800.0)
    assert point["cash_in_cny"] == 0.0 and point["daily_return_rate"] == pytest.approx(0.0)
    assert quality["estimated_inflow_events"][0]["valued"] is True


def _split(symbol, market, ratio, on):
    return SimpleNamespace(
        action_type="STOCK_SPLIT", symbol=symbol, market=market, ex_date=on, payment_date=None,
        currency="CNY", split_ratio=ratio, new_shares=None,
    )


def _bonus(symbol, market, ratio, on):
    return SimpleNamespace(
        action_type="BONUS_ISSUE", symbol=symbol, market=market, ex_date=on, payment_date=None,
        currency="CNY", distribution_ratio=ratio, shares_received=None,
    )


def test_deferred_quantity_follows_a_split_inside_the_window():
    """评审 P2 复现：3/3 转入 B 100（无价），3/4 1:2 拆成 200 仍无价，3/5 首次有价 5——
    流入须按拆后 200 股计 1000，与市值同口径，收益 0%（不同步则 +33.3%）。"""
    curve, _, quality = _curve(
        [_buy(*A, "100", "10", D1)],
        [_opening(*B, "100", D2), _split(*B, "1:2", D3)],
        {A: {D1: Decimal("10"), D2: Decimal("10"), D3: Decimal("10"), D4: Decimal("10")}, B: {D4: Decimal("5")}},
        {},
        start=D1, end=D4, today=date(2026, 3, 10),
    )
    priced = {point["date"]: point for point in curve}[D4.isoformat()]
    assert priced["cash_in_cny"] == pytest.approx(1000.0)
    assert priced["market_value_cny"] == pytest.approx(2000.0)
    assert priced["daily_return_rate"] == pytest.approx(0.0)
    assert curve[-1]["cumulative_return_rate"] == pytest.approx(0.0)
    (event,) = quality["estimated_inflow_events"]
    assert event["valued"] is True and event["valuation_price"] == 5.0


def test_deferred_quantity_follows_split_and_bonus_before_the_window():
    """区间前：3/1 转入 100、3/2 1:2 拆股、3/3 10:1 送股 → 220 股仍无价；区间内首次有价 5
    → 流入 1100，收益 0%。"""
    curve, _, quality = _curve(
        [_buy(*A, "100", "10", D0)],
        [_opening(*B, "100", D0), _split(*B, "1:2", D1), _bonus(*B, "10:1", D2)],
        {A: {D0: Decimal("10"), D3: Decimal("10"), D4: Decimal("10")}, B: {D4: Decimal("5")}},
        {},
        start=D3, end=D4, today=date(2026, 3, 10),
    )
    assert quality["opening_unpriced_positions"] == [{"symbol": "161226", "market": "A股"}]
    priced = {point["date"]: point for point in curve}[D4.isoformat()]
    assert priced["cash_in_cny"] == pytest.approx(1100.0)
    assert priced["market_value_cny"] == pytest.approx(2100.0)
    assert priced["daily_return_rate"] == pytest.approx(0.0)
    assert curve[-1]["cumulative_return_rate"] == pytest.approx(0.0)


def test_deferred_quantity_after_split_is_still_capped_by_remaining_position():
    """区间前：转入 100、卖出 40、再 1:2 拆股（卖出后 B 已有成交价 → 已在期初市值，不挂起）；
    换成拆股前无成交价的形态：转入 100、1:2 拆股、卖出 40（成交价给了期初价）→ 160 股在期初市值。"""
    curve, _, quality = _curve(
        [_buy(*A, "100", "10", D0), _buy(*B, "40", "5", D2, txn_id=2, txn_type="SELL")],
        [_opening(*B, "100", D0), _split(*B, "1:2", D1)],
        {A: {D0: Decimal("10"), D3: Decimal("10")}, B: {D3: Decimal("5")}},
        {},
        start=D3, end=D3, today=date(2026, 3, 10),
    )
    (point,) = curve
    assert quality["opening_market_value_cny"] == pytest.approx(1800.0)  # 1000 + 160×5
    assert point["cash_in_cny"] == 0.0 and point["daily_return_rate"] == pytest.approx(0.0)
