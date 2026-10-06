from datetime import date
from decimal import Decimal

import pytest

from app.models.corporate_action import CorporateAction
from app.services import exchange_rate_service
from app.services.statistics import calculate_performance_summary
from app.services.statistics.receivables import build_receivable_return
from tests.helpers import make_account
from tests.test_dividend_receipt_status import announcement
from tests.test_dividend_receipts import db as db

TODAY = date(2026, 6, 2)


@pytest.fixture(autouse=True)
def rates(monkeypatch):
    def convert(db, amount, currency):
        if currency not in {"CNY", "HKD", "USD"}:
            raise ValueError("缺汇率")
        return amount * {"CNY": Decimal(1), "HKD": Decimal("0.9"), "USD": Decimal(7)}[currency]

    monkeypatch.setattr(exchange_rate_service, "convert_to_cny", convert)


def receipt(db, row, **overrides):
    values = dict(
        user_id=row.user_id,
        broker_account_id=row.broker_account_id,
        symbol=row.symbol,
        market=row.market,
        action_type="CASH_DIVIDEND",
        ex_date=row.ex_date,
        payment_date=date(2026, 6, 1),
        currency=row.currency,
        total_dividend=Decimal(400),
        tax_withheld=Decimal(40),
        net_dividend=Decimal(360),
        amount_basis="GROSS_NET",
        receipt_status="RECEIVED",
        dividend_suggestion_id=row.id,
    )
    values.update(overrides)
    action = CorporateAction(**values)
    db.add(action)
    db.commit()
    return action


def preview(db, actions=(), actual=100):
    return build_receivable_return(db, 1, actual, actions, today=TODAY)


def test_ex_date_starts_estimate_and_receipt_replaces_it_without_double_counting(db):
    row = announcement(db, make_account(db))
    before = build_receivable_return(db, 1, 100, [], today=date(2026, 4, 30))
    assert before["known_pending_gross_cny"] == 0
    on_ex_date = build_receivable_return(db, 1, 100, [], today=row.ex_date)
    assert on_ex_date["estimated_return_cny"] == 1000  # 100 + 1000 HKD × 0.9
    action = receipt(db, row)
    partial = preview(db, [action], actual=424)  # 100 + 360 HKD actual net × 0.9
    assert partial["known_pending_gross_cny"] == 540  # remaining gross = 1000 − 400
    assert partial["estimated_return_cny"] == 964  # only the actual withholding reduces return
    second = receipt(db, row, total_dividend=600, tax_withheld=60, net_dividend=540)
    row.receipt_complete = True
    db.commit()
    complete = preview(db, [action, second], actual=910)
    assert complete["known_pending_gross_cny"] == 0
    assert complete["estimated_return_cny"] == complete["cash_basis_return_cny"] == 910
    assert complete["unresolved_count"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"amount_basis": "NET_ONLY", "total_dividend": None, "tax_withheld": None},
        {"currency": "USD"},
        {"amount_basis": "LEGACY"},
    ],
)
def test_unknown_remaining_amount_is_not_gross_minus_net(db, changes):
    row = announcement(db, make_account(db))
    action = receipt(db, row, **changes)
    result = preview(db, [action])
    assert result["known_pending_gross_cny"] == 0
    assert result["unresolved_count"] == 1
    assert result["is_partial"] is True


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "IGNORED"},
        {"match_detail": {"forecast_withdrawn": True}},
        {"announcement_detail": {"superseded_by_ex_date": "2026-05-02"}},
        {"ex_date": date(2026, 6, 3)},
        {"record_date_quantity": Decimal(0)},
    ],
)
def test_inactive_future_or_unentitled_announcements_are_not_income(db, changes):
    row = announcement(db, make_account(db))
    for key, value in changes.items():
        setattr(row, key, value)
    db.commit()
    result = preview(db)
    assert result["estimated_return_cny"] == 100
    assert result["included_count"] == result["unresolved_count"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"broker_account_id": None},
        {"record_date_quantity": None},
        {"estimated_total_dividend": None},
        {"market": "B股", "source": "tushare-dividend"},
    ],
)
def test_unknown_entitlement_or_b_share_payout_is_flagged_not_added(db, changes):
    row = announcement(db, make_account(db))
    for key, value in changes.items():
        setattr(row, key, value)
    db.commit()
    result = preview(db)
    assert result["estimated_return_cny"] == 100
    assert result["unresolved_count"] == 1


def test_missing_fx_preserves_original_currency_and_marks_partial(db):
    row = announcement(db, make_account(db))
    row.currency = "CHF"
    db.commit()
    result = preview(db)
    assert result["known_pending_gross_by_currency"] == {"CHF": 1000}
    assert result["known_pending_gross_cny"] == 0
    assert result["estimated_return_cny"] == 100
    assert result["missing_rate_currencies"] == ["CHF"]
    assert result["is_partial"] is True


@pytest.mark.parametrize("account", ["same", "unassigned", "other"])
def test_unlinked_cash_blocks_only_possible_same_account_estimates(db, account):
    row = announcement(db, make_account(db))
    account_id = row.broker_account_id
    if account == "unassigned":
        account_id = None
    elif account == "other":
        account_id = make_account(db, broker="other").id
    action = receipt(db, row, dividend_suggestion_id=None, broker_account_id=account_id)
    result = preview(db, [action])
    assert result["unresolved_count"] == (0 if account == "other" else 1)
    assert result["known_pending_gross_cny"] == (900 if account == "other" else 0)


def test_unverified_record_and_future_cash_do_not_suppress_real_pending_estimate(db):
    row = announcement(db, make_account(db))
    old = receipt(db, row, receipt_status="UNVERIFIED")
    future = receipt(db, row, payment_date=date(2026, 7, 1))
    result = preview(db, [old, future])
    assert result["known_pending_gross_cny"] == 900


def test_other_user_is_excluded_and_missing_actual_is_not_zero(db):
    row = announcement(db, make_account(db))
    row.user_id = 2
    db.commit()
    result = preview(db, actual=None)
    assert result["known_pending_gross_cny"] == 0
    assert result["estimated_return_cny"] is result["cash_basis_return_cny"] is None


def test_forecast_does_not_change_any_existing_performance_metric_or_write_cash(db):
    before = calculate_performance_summary(db, 1, {}, today=TODAY)
    announcement(db, make_account(db))
    after = calculate_performance_summary(db, 1, {}, today=TODAY)
    for key in ("current_performance", "realized_pnl", "total_realized_return", "account_return"):
        assert before[key] == after[key]
    assert after["dividend_summary"]["total_dividend_net"] == 0
    assert after["receivable_return"]["estimated_return_cny"] == 900
    assert db.query(CorporateAction).count() == 0


def test_linked_legacy_receipt_is_not_counted_again_as_pending(db):
    row = announcement(db, make_account(db))
    action = receipt(db, row, dividend_suggestion_id=None, amount_basis="LEGACY")
    row.matched_corporate_action_id = action.id
    db.commit()
    result = preview(db, [action])
    assert result["known_pending_gross_cny"] == 0
    assert result["unresolved_count"] == 1


def test_two_accounts_keep_separate_entitlements_and_api_views_agree(db, monkeypatch):
    from app.api.statistics import get_performance_summary_server_priced, get_portfolio_snapshot
    from app.services.statistics import aggregates, receivables
    from tests.helpers import get_user

    monkeypatch.setattr(aggregates, "local_today", lambda: TODAY)
    monkeypatch.setattr(receivables, "local_today", lambda: TODAY)
    first = announcement(db, make_account(db))
    announcement(db, make_account(db, broker="other"))
    receipt(db, first, total_dividend=1000, tax_withheld=100, net_dividend=900)
    first.receipt_complete = True
    db.commit()
    user = get_user(db)
    stats = get_performance_summary_server_priced(user, db)
    dashboard = get_portfolio_snapshot(user, db)
    assert dashboard["performance"]["receivable_return"] == stats["receivable_return"]
    assert stats["receivable_return"]["known_pending_gross_cny"] == 900
    assert stats["receivable_return"]["estimated_return_cny"] == 1710
    assert stats["receivable_return"]["included_count"] == 1


@pytest.mark.parametrize(
    "overrides,reason",
    [
        (
            {"amount_basis": "NET_ONLY", "total_dividend": None, "tax_withheld": None},
            "net_amount_only",
        ),
        ({"currency": "USD"}, "currency_mismatch"),
    ],
)
def test_recorded_receipts_are_explained_separately_from_missing_entitlement(db, overrides, reason):
    row = announcement(db, make_account(db))
    action = receipt(db, row, **overrides)
    result = preview(db, [action])
    assert result["review_counts"]["received"] == 1
    assert result["review_counts"]["entitlement"] == 0
    assert result["received_review_reasons"][reason] == 1
    assert result["pending_overdue_count"] == 0
    assert result["overdue_count"] == 1  # compatibility field is not shown as unpaid


def test_zero_remaining_receipt_is_not_called_overdue_pending(db):
    row = announcement(db, make_account(db))
    action = receipt(db, row, total_dividend=1000, tax_withheld=100, net_dividend=900)
    result = preview(db, [action])  # no statement evidence or explicit manual completion
    assert result["review_counts"]["zero_remaining"] == 1
    assert result["unresolved_count"] == result["pending_overdue_count"] == 0
    assert result["known_pending_gross_cny"] == 0


def test_real_pending_and_possible_unlinked_receipt_are_separate_categories(db):
    account = make_account(db)
    row = announcement(db, account)
    pending = preview(db)
    assert pending["included_count"] == pending["pending_overdue_count"] == 1
    action = receipt(db, row, dividend_suggestion_id=None)
    review = preview(db, [action])
    assert review["review_counts"]["possible_receipt"] == 1
    assert review["review_counts"]["received"] == 0
    assert review["pending_overdue_count"] == review["included_count"] == 0
