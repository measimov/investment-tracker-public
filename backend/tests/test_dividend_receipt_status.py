from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.corporate_action import CorporateAction
from app.models.corporate_action_suggestion import CorporateActionSuggestion
from app.services.dividend_forecast_service import forecast_details, forecast_summary
from app.services.dividend_receipt_rules import validate_receipt_input
from app.services.dividend_sync_service import accept_suggestion, SuggestionStateError
from app.services.portfolio.semantics import is_received_dividend
from app.services.statistics.aggregates import get_dividend_summary
from tests.helpers import get_user, make_account
from tests.test_dividend_receipts import db as db


def announcement(db, account):
    row = CorporateActionSuggestion(
        user_id=1,
        broker_account_id=account.id,
        symbol="00700",
        market="港股",
        action_type="CASH_DIVIDEND",
        ex_date=date(2026, 5, 1),
        pay_date=date(2026, 6, 1),
        currency="HKD",
        cash_div_pre_tax=Decimal(1),
        record_date_quantity=Decimal(1000),
        estimated_total_dividend=Decimal(1000),
        status="NEW",
    )
    db.add(row)
    db.commit()
    return row


def test_announcement_cannot_be_accepted_as_cash(db):
    account = make_account(db)
    row = announcement(db, account)
    with pytest.raises(SuggestionStateError, match="不能通过接受公告入账"):
        accept_suggestion(db, get_user(db), row.id, {})
    assert db.query(CorporateAction).count() == 0
    assert row.status == "NEW"
    summary = forecast_summary(db, 1, today=date(2026, 6, 2))
    assert summary["pending_gross_by_currency"] == {"HKD": Decimal(1000)}
    assert summary["overdue_count"] == 1
    assert get_dividend_summary(db, 1)["total_dividend_net"] == 0


def test_partial_net_receipt_does_not_subtract_net_from_estimated_gross(db):
    account = make_account(db)
    row = announcement(db, account)
    action = CorporateAction(
        user_id=1,
        broker_account_id=account.id,
        symbol="00700",
        market="港股",
        action_type="CASH_DIVIDEND",
        ex_date=row.ex_date,
        payment_date=date(2026, 6, 1),
        currency="HKD",
        net_dividend=Decimal(400),
        amount_basis="NET_ONLY",
        receipt_status="RECEIVED",
        dividend_suggestion_id=row.id,
    )
    db.add(action)
    db.commit()
    detail = forecast_details(db, 1, [row], today=date(2026, 6, 2))[row.id]
    assert detail["receipt_state"] == "PARTIAL"
    assert detail["remaining_estimated_gross"] is None
    assert detail["received_by_currency"] == {"HKD": Decimal(400)}
    row.receipt_complete = True
    db.commit()
    assert forecast_details(db, 1, [row])[row.id]["receipt_state"] == "RECEIVED"


def test_unverified_and_future_receipts_are_excluded(db):
    for status, paid, amount in [
        ("RECEIVED", date(2026, 6, 1), 90),
        ("UNVERIFIED", date(2026, 6, 1), 1000),
        ("RECEIVED", date(2027, 6, 1), 2000),
    ]:
        db.add(
            CorporateAction(
                user_id=1,
                symbol="600000",
                market="A股",
                action_type="CASH_DIVIDEND",
                ex_date=paid,
                payment_date=paid,
                total_dividend=amount,
                net_dividend=amount,
                currency="CNY",
                receipt_status=status,
            )
        )
    db.commit()
    summary = get_dividend_summary(db, 1, today=date(2026, 10, 1))
    assert summary["total_dividend_net"] == 90


def test_net_only_is_not_presented_as_zero_tax(db):
    data = {
        "action_type": "CASH_DIVIDEND",
        "payment_date": date(2026, 6, 1),
        "broker_account_id": 1,
        "amount_basis": "NET_ONLY",
        "net_dividend": Decimal(90),
    }
    validate_receipt_input(data, confirmed=True, today=date(2026, 6, 2))
    assert data["tax_withheld"] is data["total_dividend"] is None
    assert data["receipt_status"] == "RECEIVED"


@pytest.mark.parametrize(
    "confirmed,paid", [(False, date(2026, 6, 1)), (True, None), (True, date(2027, 1, 1))]
)
def test_new_receipt_requires_confirmation_and_actual_date(confirmed, paid):
    with pytest.raises(ValueError):
        validate_receipt_input(
            {"action_type": "CASH_DIVIDEND", "payment_date": paid},
            confirmed=confirmed,
            today=date(2026, 10, 1),
        )


def test_receipt_cutoff_is_explicit_and_has_no_clock():
    row = SimpleNamespace(
        action_type="CASH_DIVIDEND",
        receipt_status="RECEIVED",
        payment_date=date(2026, 6, 1),
        ex_date=date(2026, 5, 1),
    )
    assert not is_received_dividend(row, date(2026, 5, 31))
    assert is_received_dividend(row, date(2026, 6, 1))


def test_b_share_announcement_with_unverified_payout_currency_is_not_totalled(db):
    account = make_account(db)
    row = announcement(db, account)
    row.market = "B股"
    row.source = "tushare-dividend"
    db.commit()
    summary = forecast_summary(db, 1, today=date(2026, 6, 2))
    assert summary["pending_gross_by_currency"] == {}
    assert summary["unknown_amount_count"] == 1


def test_net_only_manual_receipt_keeps_unknown_tax_and_requires_reconfirmation_on_edit(db):
    from fastapi import HTTPException
    from app.api.corporate_actions import create_corporate_action, update_corporate_action
    from app.schemas.corporate_action import CorporateActionCreate, CorporateActionUpdate

    account = make_account(db, commit=True)
    user = get_user(db)
    created = create_corporate_action(
        CorporateActionCreate(
            symbol="00700",
            market="港股",
            action_type="CASH_DIVIDEND",
            ex_date=date(2026, 5, 1),
            payment_date=date(2026, 6, 1),
            broker_account_id=account.id,
            currency="HKD",
            amount_basis="NET_ONLY",
            net_dividend=Decimal(90),
            receipt_confirmed=True,
        ),
        user,
        db,
    )
    assert created.tax_withheld is created.total_dividend is None
    updated = update_corporate_action(
        created.id, CorporateActionUpdate(notes="凭证仅含净额"), user, db
    )
    assert updated.net_dividend == Decimal(90)
    with pytest.raises(HTTPException) as exc:
        update_corporate_action(
            created.id, CorporateActionUpdate(net_dividend=Decimal(100)), user, db
        )
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        update_corporate_action(
            created.id,
            CorporateActionUpdate(payment_date=date(2099, 1, 1), receipt_confirmed=True),
            user,
            db,
        )
    assert exc.value.status_code == 422
