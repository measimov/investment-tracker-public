from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import event

from app.models.broker_fund_flow import BrokerFundFlow
from app.models.corporate_action import CorporateAction
from app.models.corporate_action_suggestion import CorporateActionSuggestion
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.services.dividend_forecast_service import forecast_details
from app.services.dividend_settlement_service import build_settlement_context
from tests.helpers import get_user, make_account
from tests.test_dividend_receipt_status import announcement
from tests.test_dividend_receipts import db as db


def receipt(db, row, amount="1000", *, paid=date(2026, 6, 1), linked=True, **overrides):
    values = dict(
        user_id=row.user_id,
        broker_account_id=row.broker_account_id,
        symbol=row.symbol,
        market=row.market,
        action_type="CASH_DIVIDEND",
        ex_date=row.ex_date,
        payment_date=paid,
        currency=row.currency,
        net_dividend=Decimal(amount),
        amount_basis="NET_ONLY",
        receipt_status="RECEIVED",
        dividend_suggestion_id=row.id if linked else None,
    )
    values.update(overrides)
    action = CorporateAction(**values)
    db.add(action)
    db.flush()
    if action.amount_basis == "NET_ONLY":
        # The model keeps old INSERT defaults; receipt entry explicitly clears
        # unknown gross/tax after construction, so the fixture must do so too.
        action.total_dividend = action.tax_withheld = None
        db.flush()
    return action


def broker_source(db, action, **overrides):
    values = dict(
        user_id=action.user_id,
        broker_account_id=action.broker_account_id,
        corporate_action_id=action.id,
        broker="招商证券",
        row_hash=f"settlement-{action.id}",
        security_code=action.symbol,
        currency=action.currency,
        trade_date=action.payment_date,
        amount=action.net_dividend,
        business_name="股息入账",
    )
    values.update(overrides)
    source = BrokerFundFlow(**values)
    db.add(source)
    db.flush()
    return source


def ibkr_source(db, action, *, gross=None, tax=Decimal(0), **overrides):
    gross = gross if gross is not None else action.net_dividend + tax
    rows = []
    for kind, amount in (("股息", gross), ("外国预扣税", -tax)):
        if not amount:
            continue
        values = dict(
            user_id=action.user_id,
            broker_account_id=action.broker_account_id,
            corporate_action_id=action.id,
            row_hash=f"settlement-{action.id}-{kind}",
            source_row_number=action.id,
            trade_date=action.payment_date,
            activity_type=kind,
            symbol=action.symbol,
            market=action.market,
            base_currency=action.currency,
            gross_amount=amount,
            net_amount=amount,
        )
        values.update(overrides)
        source = IbkrActivityFlow(**values)
        db.add(source)
        rows.append(source)
    db.flush()
    return rows


def details(db, row, **kwargs):
    return forecast_details(db, row.user_id, [row], today=date(2026, 8, 1), **kwargs)[row.id]


def test_net_only_broker_receipt_closes_announcement_without_changing_ledger(db):
    account = make_account(db)
    row = announcement(db, account)
    action = receipt(db, row, linked=False)
    source = broker_source(db, action)
    db.commit()
    before = (action.net_dividend, action.total_dividend, action.tax_withheld, source.row_hash)
    result = details(db, row)
    assert result["receipt_state"] == "RECEIVED"
    assert result["completion_source"] == "statement"
    assert result["completion_date"] == date(2026, 6, 1)
    assert result["remaining_estimated_gross"] == 0
    assert result["receipt_ids"] == [action.id]
    assert not result["overdue"]
    assert not row.receipt_complete  # derived; no GET-time write
    assert action.dividend_suggestion_id is None
    assert not db.dirty and not db.new
    assert before == (
        action.net_dividend,
        action.total_dividend,
        action.tax_withheld,
        source.row_hash,
    )
    assert details(db, row) == result


def test_suggestion_api_exposes_auto_completion_and_hides_it_from_pending(db, monkeypatch):
    from app.api.corporate_actions import list_suggestions

    monkeypatch.setattr(
        "app.services.dividend_forecast_service.local_today", lambda: date(2026, 8, 1)
    )
    row = announcement(db, make_account(db))
    action = receipt(db, row, linked=False)
    broker_source(db, action)
    db.commit()
    params = dict(
        status=None,
        symbol=None,
        market=None,
        skip=0,
        limit=50,
        current_user=get_user(db),
        db=db,
    )
    shown = list_suggestions(pending_only=False, **params)[0].model_dump(mode="json")
    assert shown["receipt_complete"]
    assert shown["completion_source"] == "statement"
    assert shown["completion_date"] == "2026-06-01"
    assert list_suggestions(pending_only=True, **params) == []
    assert not row.receipt_complete


@pytest.mark.parametrize(
    "net,completed",
    [
        ("999.99288", True),
        ("1000.009", True),
        ("999.99", False),
        ("1000.01", False),
        ("990", False),
    ],
)
def test_completion_only_allows_sub_cent_rounding_not_relative_tolerance(db, net, completed):
    row = announcement(db, make_account(db))
    action = receipt(db, row, net)
    broker_source(db, action)
    result = details(db, row)
    assert result["receipt_complete"] is completed
    assert action.tax_withheld is None


@pytest.mark.parametrize(
    "source_change",
    [
        {"user_id": 2},
        {"security_code": "00999"},
        {"trade_date": date(2026, 6, 2)},
        {"currency": "CNY"},
        {"amount": Decimal(999)},
        {"skip_reason": "suspected_duplicate"},
        {"business_name": "未知业务"},
    ],
)
def test_source_must_prove_the_same_cash_fact(db, source_change):
    row = announcement(db, make_account(db))
    action = receipt(db, row)
    broker_source(db, action, **source_change)
    assert details(db, row)["completion_source"] is None


def test_matching_ledger_amount_without_statement_is_not_automatic_evidence(db):
    row = announcement(db, make_account(db))
    receipt(db, row)
    result = details(db, row)
    assert result["receipt_state"] == "PARTIAL"
    assert result["review_reason"] == "statement_evidence_missing"


def test_partial_net_does_not_invent_tax_or_remaining_gross(db):
    row = announcement(db, make_account(db))
    action = receipt(db, row, "800")
    broker_source(db, action)
    result = details(db, row)
    assert result["receipt_state"] == "PARTIAL"
    assert result["remaining_estimated_gross"] is None
    assert result["review_reason"] == "receipt_amount_unresolved"


def test_actual_ibkr_gross_and_tax_support_completion(db):
    row = announcement(db, make_account(db))
    action = receipt(db, row, "900")
    ibkr_source(db, action, gross=Decimal(1000), tax=Decimal(100))
    result = details(db, row)
    assert result["completion_source"] == "statement"
    assert result["received_by_currency"] == {"HKD": Decimal(900)}
    # Deriving completion does not mutate the amount basis or invent a ledger tax.
    assert action.amount_basis == "NET_ONLY" and action.tax_withheld is None


def test_confirmed_gross_is_not_replaced_by_net_when_comparing_announced_amount(db):
    row = announcement(db, make_account(db))
    row.estimated_total_dividend = Decimal(900)
    action = receipt(
        db,
        row,
        "900",
        amount_basis="GROSS_NET",
        total_dividend=Decimal(1000),
        tax_withheld=Decimal(100),
    )
    broker_source(db, action)
    assert not details(db, row)["receipt_complete"]
    row.estimated_total_dividend = Decimal(1000)
    assert details(db, row)["completion_source"] == "statement"


def test_split_payments_complete_at_the_last_receipt_and_replay_partial_month(db):
    row = announcement(db, make_account(db))
    row.pay_date = date(2026, 7, 15)
    first = receipt(db, row, "360", paid=date(2026, 6, 30), linked=False)
    second = receipt(db, row, "540", paid=date(2026, 7, 15), linked=False)
    ibkr_source(db, first, gross=Decimal(400), tax=Decimal(40))
    ibkr_source(db, second, gross=Decimal(600), tax=Decimal(60))
    context = build_settlement_context(db, 1, [row], [first, second])
    june = forecast_details(
        db, 1, [row], today=date(2026, 6, 30), actions=[first, second], settlement_context=context
    )[row.id]
    july = details(db, row, actions=[first, second], settlement_context=context)
    assert june["receipt_ids"] == [first.id]
    assert june["receipt_state"] == "PARTIAL"
    assert june["remaining_estimated_gross"] == 600
    assert june["received_by_currency"] == {"HKD": Decimal(360)}
    assert june["completion_date"] == date(2026, 7, 15)
    assert july["receipt_ids"] == [first.id, second.id]
    assert july["receipt_state"] == "RECEIVED"


def test_manual_completion_is_not_retroactive_to_first_split_receipt(db):
    row = announcement(db, make_account(db))
    first = receipt(
        db,
        row,
        "360",
        paid=date(2026, 6, 30),
        amount_basis="GROSS_NET",
        total_dividend=Decimal(400),
        tax_withheld=Decimal(40),
    )
    second = receipt(
        db,
        row,
        "540",
        paid=date(2026, 7, 15),
        amount_basis="GROSS_NET",
        total_dividend=Decimal(600),
        tax_withheld=Decimal(60),
    )
    row.receipt_complete = True
    row.match_detail = {"receipt_links_reviewed": True}
    june = forecast_details(db, 1, [row], today=date(2026, 6, 30))[row.id]
    july = details(db, row)
    assert june["receipt_ids"] == [first.id]
    assert june["receipt_state"] == "PARTIAL"
    assert june["remaining_estimated_gross"] == 600
    assert july["receipt_ids"] == [first.id, second.id]
    assert july["completion_source"] == "manual"


def test_reviewed_partial_state_is_not_overridden_by_auto_completion(db):
    row = announcement(db, make_account(db))
    action = receipt(db, row)
    broker_source(db, action)
    row.match_detail = {"receipt_links_reviewed": True}
    result = details(db, row)
    assert not result["receipt_complete"]
    assert result["review_reason"] == "manual_review_pending"


def test_ambiguity_checks_other_announcements_outside_requested_page(db):
    account = make_account(db)
    row = announcement(db, account)
    other = CorporateActionSuggestion(
        user_id=1,
        broker_account_id=account.id,
        symbol=row.symbol,
        market=row.market,
        action_type="CASH_DIVIDEND",
        ex_date=date(2026, 5, 15),
        pay_date=row.pay_date,
        currency=row.currency,
        estimated_total_dividend=row.estimated_total_dividend,
        record_date_quantity=row.record_date_quantity,
    )
    db.add(other)
    action = receipt(db, row, linked=False)
    broker_source(db, action)
    row.matched_corporate_action_id = action.id
    db.commit()
    result = details(db, row)
    assert result["review_reason"] == "ambiguous_receipt"
    assert not result["receipt_complete"]


def test_other_account_receipt_cannot_complete_or_attach(db):
    row = announcement(db, make_account(db))
    other_account = make_account(db, account_name="其他账户")
    action = receipt(db, row, broker_account_id=other_account.id)
    broker_source(db, action)
    row.matched_corporate_action_id = action.id
    result = details(db, row)
    assert not result["receipt_complete"]
    assert result["receipt_ids"] == []


def test_stale_legacy_match_does_not_settle_a_different_payment_period(db):
    row = announcement(db, make_account(db))
    action = receipt(db, row, linked=False, paid=date(2025, 6, 1))
    broker_source(db, action)
    row.matched_corporate_action_id = action.id
    result = details(db, row)
    assert result["receipt_ids"] == [action.id]
    assert not result["receipt_complete"]
    assert result["review_reason"] == "receipt_period_unverified"


@pytest.mark.parametrize("rate,completed", [(None, False), (Decimal("0.9"), True)])
def test_cross_currency_requires_source_settlement_rate(db, rate, completed):
    row = announcement(db, make_account(db))
    action = receipt(db, row, "900", currency="CNY")
    broker_source(db, action, settlement_rate=rate, statement_type="hk_connect")
    result = details(db, row)
    assert result["receipt_complete"] is completed
    assert result["received_by_currency"] == {"CNY": Decimal(900)}


def test_cross_currency_conversion_preserves_verified_gross_not_net(db):
    row = announcement(db, make_account(db))
    action = receipt(
        db,
        row,
        "900",
        currency="CNY",
        amount_basis="GROSS_NET",
        total_dividend=Decimal(1000),
        tax_withheld=Decimal(100),
    )
    broker_source(db, action, settlement_rate=Decimal("0.9"), statement_type="hk_connect")
    assert not details(db, row)["receipt_complete"]


def test_ibkr_description_per_share_does_not_prove_full_original_currency_payment(db):
    row = announcement(db, make_account(db))
    first = receipt(db, row, "25", linked=False, currency="USD")
    second = receipt(db, row, "100", linked=False, currency="USD")
    ibkr_source(db, first, description="700(KYG) 现金红利 HKD 0.2 每股 (常规股息)")
    ibkr_source(db, second, description="700(KYG) 现金红利 HKD 0.8 每股 (特别股息)")
    row.matched_corporate_action_id = first.id
    result = details(db, row)
    assert result["receipt_ids"] == [first.id, second.id]
    assert result["review_reason"] == "receipt_currency_mismatch"
    assert result["remaining_estimated_gross"] is None


def test_unverified_bshare_currency_never_auto_completes_from_matching_number(db):
    row = announcement(db, make_account(db))
    row.market = "B股"
    action = receipt(db, row)
    broker_source(db, action)
    result = details(db, row)
    assert result["review_reason"] == "currency_unverified"
    assert not result["receipt_complete"]


def test_reused_context_has_no_per_boundary_queries(db):
    row = announcement(db, make_account(db))
    action = receipt(db, row)
    broker_source(db, action)
    context = build_settlement_context(db, 1, [row], [action])
    queries = []

    def capture(*args):
        queries.append(args)

    connection = db.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        before = forecast_details(
            db, 1, [row], today=date(2026, 5, 31), actions=[action], settlement_context=context
        )[row.id]
        after = details(db, row, actions=[action], settlement_context=context)
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert before["remaining_estimated_gross"] == 1000
    assert before["receipt_ids"] == []
    assert after["receipt_complete"]
    assert queries == []
