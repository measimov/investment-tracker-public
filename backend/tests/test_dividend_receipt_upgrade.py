import copy
from datetime import date
from decimal import Decimal

import pytest

from app.models.broker_fund_flow import BrokerFundFlow
from app.models.cash_event import CashEvent
from app.models.corporate_action import CorporateAction
from app.models.dividend_tax_allocation import DividendTaxAllocation
from app.services.dividend_receipt_upgrade import (
    apply_dividend_receipt_upgrade_plan,
    build_dividend_receipt_upgrade_plan,
)
from app.services.reconciliation_service import derive_account_cash_asof
from tests.helpers import make_account
from tests.test_dividend_receipts import accepted, db as db


def cash_source(db, account, action, *, amount=Decimal(90)):
    source = BrokerFundFlow(
        user_id=1,
        broker_account_id=account.id,
        broker="招商证券",
        corporate_action_id=action.id,
        row_hash="receipt-upgrade-proof",
        source_filename="fixture.pdf",
        source_row_number=1,
        trade_date=action.payment_date,
        security_code=action.symbol,
        business_name="股息入账",
        amount=amount,
        currency=action.currency,
    )
    db.add(source)
    db.commit()
    return source


def test_dry_run_no_mutation_apply_preserves_sources_and_second_apply_noop(db):
    account = make_account(db)
    action, suggestion = accepted(db, account)
    before = derive_account_cash_asof(db, 1, account.id, date(2026, 10, 1))
    plan = build_dividend_receipt_upgrade_plan(db, 1, as_of=date(2026, 10, 1))
    assert not plan["blockers"]
    assert action.receipt_status == "RECEIVED"
    assert plan["changes"][0]["expected_cash_delta_as_of"] == "-90.00000000"
    assert apply_dividend_receipt_upgrade_plan(db, plan) == 1
    db.commit()
    db.expire_all()
    assert action.receipt_status == "UNVERIFIED"
    assert action.total_dividend == Decimal(100)
    assert action.net_dividend == Decimal(90)
    assert action.dividend_suggestion_id == suggestion.id
    assert derive_account_cash_asof(db, 1, account.id, date(2026, 10, 1)).get(
        "HKD", Decimal(0)
    ) == before["HKD"] - Decimal(90)
    assert apply_dividend_receipt_upgrade_plan(db, plan) == 0
    assert build_dividend_receipt_upgrade_plan(db, 1, as_of=date(2026, 10, 1))["changes"] == []


def test_net_source_keeps_cash_and_audits_unknown_old_tax(db):
    account = make_account(db)
    action, _ = accepted(db, account)
    source = cash_source(db, account, action)
    source_hash = source.row_hash
    plan = build_dividend_receipt_upgrade_plan(db, 1, as_of=date(2026, 10, 1))
    assert not plan["blockers"]
    assert plan["changes"][0]["after"]["amount_basis"] == "NET_ONLY"
    assert apply_dividend_receipt_upgrade_plan(db, plan) == 1
    db.commit()
    db.expire_all()
    assert action.receipt_status == "RECEIVED"
    assert action.total_dividend is action.tax_withheld is None
    assert action.net_dividend == Decimal(90)
    assert source.row_hash == source_hash and source.corporate_action_id == action.id
    assert "原税前=100" in action.notes and "税额=10" in action.notes
    assert apply_dividend_receipt_upgrade_plan(db, plan) == 0
    assert plan["coverage"][0]["continuous_coverage_verified"] is False


@pytest.mark.parametrize("change", ["source", "amount", "tamper"])
def test_stale_or_edited_plan_is_rejected_without_writes(db, change):
    account = make_account(db)
    action, _ = accepted(db, account)
    plan = build_dividend_receipt_upgrade_plan(db, 1)
    if change == "source":
        cash_source(db, account, action)
    elif change == "amount":
        action.net_dividend = Decimal(80)
        db.commit()
    else:
        plan = copy.deepcopy(plan)
        plan["changes"][0]["after"]["receipt_status"] = "RECEIVED"
    with pytest.raises(ValueError, match="重新预演"):
        apply_dividend_receipt_upgrade_plan(db, plan)
    assert action.receipt_status == "RECEIVED"
    assert "upgrade-v1" not in (action.notes or "")


def test_allocated_tax_blocks_excluding_announcement(db):
    account = make_account(db)
    action, _ = accepted(db, account)
    event = CashEvent(
        user_id=1,
        broker_account_id=account.id,
        event_type="TAX",
        tax_kind="DIVIDEND",
        event_date=date(2026, 6, 12),
        amount=Decimal(5),
        currency="HKD",
    )
    db.add(event)
    db.flush()
    db.add(
        DividendTaxAllocation(
            user_id=1, cash_event_id=event.id, corporate_action_id=action.id, amount=Decimal(5)
        )
    )
    db.commit()
    plan = build_dividend_receipt_upgrade_plan(db, 1)
    assert plan["blockers"]
    with pytest.raises(ValueError):
        apply_dividend_receipt_upgrade_plan(db, plan)
    assert action.receipt_status == "RECEIVED"


def test_unexplained_net_amount_blocks_upgrade(db):
    account = make_account(db)
    action, _ = accepted(db, account)
    cash_source(db, account, action, amount=Decimal(80))
    plan = build_dividend_receipt_upgrade_plan(db, 1)
    assert "净额与原始红利流水不符" in plan["blockers"][0]
    assert not plan["changes"]


def test_failed_postcheck_rolls_back_even_if_caller_commits(db, monkeypatch):
    from app.services import dividend_receipt_upgrade as upgrade

    account = make_account(db)
    action, _ = accepted(db, account)
    plan = build_dividend_receipt_upgrade_plan(db, 1)
    original = upgrade.derive_account_cash_asof

    def corrupted_after(session, user_id, account_id, cutoff):
        result = original(session, user_id, account_id, cutoff)
        if session.get(CorporateAction, action.id).receipt_status == "UNVERIFIED":
            result["HKD"] = Decimal(999)
        return result

    monkeypatch.setattr(upgrade, "derive_account_cash_asof", corrupted_after)
    with pytest.raises(ValueError, match="已回滚"):
        apply_dividend_receipt_upgrade_plan(db, plan)
    db.commit()
    db.expire_all()
    assert action.receipt_status == "RECEIVED"
    assert action.notes is None


def test_cash_management_income_is_not_reported_as_unlinked_dividend(db):
    account = make_account(db)
    event = CashEvent(
        user_id=1,
        broker_account_id=account.id,
        event_type="INTEREST",
        event_date=date(2026, 6, 1),
        amount=Decimal(5),
        currency="CNY",
    )
    db.add(event)
    db.flush()
    db.add(
        BrokerFundFlow(
            user_id=1,
            broker_account_id=account.id,
            broker="招商证券",
            row_hash="cash-management-income",
            source_filename="fixture.pdf",
            source_row_number=1,
            trade_date=event.event_date,
            business_name="产品红利发放",
            security_code="999990",
            amount=Decimal(5),
            currency="CNY",
            cash_event_id=event.id,
        )
    )
    db.commit()
    plan = build_dividend_receipt_upgrade_plan(db, 1)
    assert plan["coverage"][0]["unlinked_dividend_sources"] == []
    assert plan["coverage"][0]["cash_event_classified_income_count"] == 1


def test_legacy_date_match_does_not_claim_full_payment(db):
    from app.services.dividend_forecast_service import forecast_details

    account = make_account(db)
    action, suggestion = accepted(db, account, status="MATCHED")
    suggestion.created_corporate_action_id = None
    suggestion.matched_corporate_action_id = action.id
    db.commit()
    detail = forecast_details(db, 1, [suggestion])[suggestion.id]
    assert detail["receipt_state"] == "PARTIAL"
    assert detail["receipt_complete"] is False
