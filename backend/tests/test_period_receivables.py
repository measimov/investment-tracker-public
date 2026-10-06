"""手算跨月/跨年股息应收，验证到账冲销和期初期末对称排除。"""

from datetime import date
from decimal import Decimal

import pytest

from app.models.corporate_action import CorporateAction
from app.models.exchange_rate import ExchangeRate
from app.models.security_price import SecurityPrice
from app.services.statistics import calculate_period_pnl
from app.services.statistics.period_receivables import add_period_receivables
from tests.helpers import add_transaction, make_account
from tests.test_dividend_receipt_status import announcement
from tests.test_dividend_receipts import db as db


@pytest.fixture(autouse=True)
def clean_prices(db):
    for model in (SecurityPrice, ExchangeRate):
        db.query(model).delete()
    db.commit()
    yield
    for model in (SecurityPrice, ExchangeRate):
        db.query(model).delete()
    db.commit()


def event(db, *, ex_date=date(2026, 6, 10), currency="CNY"):
    account = make_account(db)
    row = announcement(db, account)
    row.ex_date = ex_date
    row.pay_date = date(2026, 7, 10)
    row.currency = currency
    row.cash_div_pre_tax = Decimal(1)
    row.record_date_quantity = Decimal(100)
    row.estimated_total_dividend = Decimal(100)
    row.receipt_complete = True
    add_transaction(
        db,
        broker_account_id=account.id,
        symbol=row.symbol,
        market=row.market,
        currency="CNY",
        quantity=100,
        price=10,
        transaction_date=date(2025, 1, 1),
    )
    for day in (date(2025, 12, 31), date(2026, 5, 31), date(2026, 6, 30), date(2026, 7, 30)):
        db.add(
            SecurityPrice(
                symbol=row.symbol,
                market=row.market,
                ts_code="00700.HK",
                price_date=day,
                currency="CNY",
                close_price=10,
                source="test",
            )
        )
    db.commit()
    return row


def pay(db, row, day, gross=100, net=90, *, net_only=False):
    action = CorporateAction(
        user_id=1,
        broker_account_id=row.broker_account_id,
        symbol=row.symbol,
        market=row.market,
        action_type="CASH_DIVIDEND",
        ex_date=row.ex_date,
        payment_date=day,
        currency=row.currency,
        total_dividend=None if net_only else Decimal(gross),
        tax_withheld=None if net_only else Decimal(gross - net),
        net_dividend=Decimal(net),
        amount_basis="NET_ONLY" if net_only else "GROSS_NET",
        receipt_status="RECEIVED",
        dividend_suggestion_id=row.id,
    )
    db.add(action)
    db.commit()
    return action


def period(db, row, day, key="mtd"):
    return calculate_period_pnl(db, 1, {f"{row.symbol}:{row.market}": 10}, today=day)["periods"][
        key
    ]


@pytest.mark.parametrize("net_only", [False, True])
def test_june_accrual_july_receipt_and_tax_do_not_count_twice(db, net_only):
    row = event(db)
    pay(db, row, date(2026, 7, 10), net_only=net_only)
    june = period(db, row, date(2026, 6, 30))
    july = period(db, row, date(2026, 7, 31))
    annual = period(db, row, date(2026, 7, 31), "ytd")
    assert june["pnl_cny"] == 0
    assert june["receivable_pnl"]["estimated_pnl_cny"] == 100
    assert july["pnl_cny"] == 90
    assert july["receivable_pnl"]["opening_receivable_cny"] == 100
    assert july["receivable_pnl"]["closing_receivable_cny"] == 0
    assert july["receivable_pnl"]["estimated_pnl_cny"] == -10
    assert annual["receivable_pnl"]["estimated_pnl_cny"] == 90
    assert "receivable_pnl" not in period(db, row, date(2026, 7, 31), "daily")
    assert row.receipt_complete is True  # readonly computation preserves user confirmation


def test_last_year_ex_date_is_not_this_year_dividend_income_again(db):
    row = event(db, ex_date=date(2025, 12, 20))
    pay(db, row, date(2026, 7, 10))
    annual = period(db, row, date(2026, 7, 31), "ytd")
    assert annual["pnl_cny"] == 90
    assert annual["receivable_pnl"]["opening_receivable_cny"] == 100
    assert annual["receivable_pnl"]["estimated_pnl_cny"] == -10


def test_ex_date_on_month_start_enters_this_month_only(db):
    row = event(db, ex_date=date(2026, 7, 1))
    june = period(db, row, date(2026, 6, 30))["receivable_pnl"]
    july = period(db, row, date(2026, 7, 1))["receivable_pnl"]
    assert june["estimated_pnl_cny"] == 0
    assert july["opening_receivable_cny"] == 0
    assert july["closing_receivable_cny"] == 100
    assert july["estimated_pnl_cny"] == 100


def test_receipt_on_month_start_reverses_previous_month_receivable(db):
    row = event(db)
    pay(db, row, date(2026, 7, 1))
    june = period(db, row, date(2026, 6, 30))["receivable_pnl"]
    july = period(db, row, date(2026, 7, 1))["receivable_pnl"]
    assert june["closing_receivable_cny"] == 100
    assert july["opening_receivable_cny"] == 100
    assert july["closing_receivable_cny"] == 0
    assert july["cash_basis_pnl_cny"] == 90
    assert july["estimated_pnl_cny"] == -10


def test_first_installment_does_not_backdate_full_completion(db):
    row = event(db)
    pay(db, row, date(2026, 6, 20), gross=40, net=36)
    pay(db, row, date(2026, 7, 10), gross=60, net=54)
    june = period(db, row, date(2026, 6, 30))
    july = period(db, row, date(2026, 7, 31))
    assert june["pnl_cny"] == 36
    assert june["receivable_pnl"]["closing_receivable_cny"] == 60
    assert june["receivable_pnl"]["estimated_pnl_cny"] == 96
    assert july["receivable_pnl"]["opening_receivable_cny"] == 60
    assert july["receivable_pnl"]["estimated_pnl_cny"] == -6


def test_unknown_opening_remaining_does_not_create_one_sided_adjustment(db):
    row = event(db)
    pay(db, row, date(2026, 6, 20), gross=40, net=36, net_only=True)
    pay(db, row, date(2026, 7, 10), gross=60, net=54, net_only=True)
    result = period(db, row, date(2026, 7, 31))["receivable_pnl"]
    assert result["is_partial"] is True
    assert result["unresolved_count"] == 1
    assert result["receivable_change_cny"] == 0
    assert result["estimated_pnl_cny"] == result["cash_basis_pnl_cny"] == 54


def fx(db, day, rate):
    db.add(
        ExchangeRate(
            from_currency="HKD",
            to_currency="CNY",
            effective_date=day,
            rate=Decimal(rate),
            is_active=True,
            source="test",
        )
    )
    db.commit()


def balances(db, row, actions=(), *, cash=0):
    periods = {
        key: {"start_date": start, "pnl_cny": cash}
        for key, start in (("mtd", "2026-07-01"), ("ytd", "2026-01-01"))
    }
    add_period_receivables(db, 1, periods, list(actions), date(2026, 7, 31))
    return periods["mtd"]["receivable_pnl"]


def test_receivable_fx_movement_uses_two_boundary_dates(db):
    row = event(db, currency="HKD")
    fx(db, date(2026, 6, 30), "0.90")
    fx(db, date(2026, 7, 31), "0.95")
    result = balances(db, row)
    assert result["opening_receivable_cny"] == 90
    assert result["closing_receivable_cny"] == 95
    assert result["estimated_pnl_cny"] == 5
    assert result["is_partial"] is False


def test_foreign_receipt_keeps_only_tax_and_fx_difference(db):
    row = event(db, currency="HKD")
    action = pay(db, row, date(2026, 7, 10))
    fx(db, date(2026, 6, 30), "0.90")
    fx(db, date(2026, 7, 10), "0.94")
    result = balances(db, row, [action], cash=84.6)
    assert result["opening_receivable_cny"] == 90
    assert result["closing_receivable_cny"] == 0
    assert result["estimated_pnl_cny"] == -5.4


def test_missing_opening_fx_never_uses_future_rate_or_single_endpoint(db):
    row = event(db, currency="HKD")
    fx(db, date(2026, 7, 1), "0.95")
    result = balances(db, row)
    assert result["is_partial"] is True
    assert result["missing_rate_currencies"] == ["HKD"]
    assert result["opening_receivable_cny"] == result["closing_receivable_cny"] == 0
    assert result["receivable_change_cny"] == 0


def test_unavailable_cash_pnl_does_not_turn_into_available_accrual_number(db):
    row = event(db)
    result = balances(db, row, cash=None)
    assert result["cash_basis_pnl_cny"] is result["estimated_pnl_cny"] is None


def test_completed_before_period_creates_no_receivable_change(db):
    row = event(db)
    action = pay(db, row, date(2026, 6, 20))
    result = balances(db, row, [action])
    assert result["opening_receivable_cny"] == result["closing_receivable_cny"] == 0
    assert result["is_partial"] is False


def test_manual_price_endpoint_passes_the_same_price_map_as_summary(db, monkeypatch):
    from app.api import statistics
    from tests.helpers import get_user

    prices = {"00700:港股": 432.1}
    calls = []

    def calculate(session, user_id, current_prices):
        calls.append((session, user_id, current_prices))
        return {"periods": {}}

    monkeypatch.setattr(statistics, "calculate_period_pnl", calculate)
    statistics.get_period_pnl_with_prices(prices, get_user(db), db)
    assert calls == [(db, 1, prices)]
