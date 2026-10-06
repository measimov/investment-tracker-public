from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.api.corporate_actions import (
    create_corporate_action,
    update_corporate_action,
    update_suggestion_receipts,
)
from app.models.corporate_action import CorporateAction
from app.models.corporate_action_suggestion import CorporateActionSuggestion
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.schemas.corporate_action import CorporateActionCreate, CorporateActionUpdate
from app.schemas.corporate_action_suggestion import SuggestionReceiptsUpdate
from app.services import ibkr_activity_importer as ibkr
from app.services import cmb_fund_flow_importer as cmb
from app.services import eastmoney_statement_importer as eastmoney
from app.services.dividend_forecast_service import forecast_details, forecast_summary
from app.services.dividend_receipt_service import DividendReceipt, prepare_dividend_receipts
from tests.helpers import get_user, make_account
from tests.test_cmb_fund_flow_importer import parsed_flow
from tests.test_eastmoney_statement_importer import flow_row, patch_statement, statement_context
from tests.test_dividend_receipts import db as db
from tests.test_dividend_receipt_status import announcement
from tests.test_ibkr_activity_importer import parsed_ibkr_flow, stub_parsed_rows


def confirmed_manual_receipt(db, *, account=None, currency="HKD"):
    account = account or make_account(db)
    suggestion = announcement(db, account)
    suggestion.currency = currency
    db.commit()
    user = get_user(db)
    action = create_corporate_action(
        CorporateActionCreate(
            symbol="00700",
            market="港股",
            action_type="CASH_DIVIDEND",
            broker_account_id=account.id,
            ex_date=date(2026, 5, 1),
            payment_date=date(2026, 6, 1),
            currency=currency,
            amount_basis="NET_ONLY",
            net_dividend=90,
            receipt_confirmed=True,
        ),
        user,
        db,
    )
    update_suggestion_receipts(
        suggestion.id,
        SuggestionReceiptsUpdate(
            receipt_ids=[action.id],
            receipt_complete=True,
            expected_updated_at=suggestion.updated_at,
        ),
        user,
        db,
    )
    return action, suggestion, user


def nearby_pending_announcement(db, account):
    # Create with a different identity from A before flushing the unique key.
    suggestion = CorporateActionSuggestion(
        user_id=1,
        broker_account_id=account.id,
        symbol="00700",
        market="港股",
        action_type="CASH_DIVIDEND",
        ex_date=date(2026, 5, 15),
        pay_date=date(2026, 6, 2),
        currency="HKD",
        record_date_quantity=1000,
        estimated_total_dividend=1000,
        status="NEW",
    )
    db.add(suggestion)
    db.commit()
    return suggestion


@pytest.mark.parametrize("broker", ["cmb", "eastmoney", "ibkr"])
def test_broker_proof_preserves_reviewed_manual_receipt_link_and_reimports_once(
    db, monkeypatch, broker
):
    if broker == "cmb":
        account = make_account(db, broker=cmb.BROKER_NAME, account_number_masked="****A123")
        flow = parsed_flow(
            row_number=2,
            row_hash="a" * 64,
            business_name="股息入账",
            trade_date=date(2026, 6, 1),
            quantity="100",
            price="0",
            amount="90",
        )
        flow.security_code, flow.currency = "00700", "HKD"
        monkeypatch.setattr(cmb, "parse_rows", lambda *a, **kw: ([flow], {"股息入账": 1}, 1, []))
        preview, commit = cmb.preview_cmb_fund_flow, cmb.import_cmb_fund_flow
    elif broker == "eastmoney":
        account = make_account(db, broker=eastmoney.BROKER_NAME)
        row = flow_row("20260601", "红利入账", "00700", "测试股", "100", "0", "90")
        row["_statement_type"] = "hk_connect"
        row.update(
            {k: "0" for k in ("结算汇率", "交易费", "交易征费", "交收费", "系统费用", "其他费用")}
        )
        patch_statement(monkeypatch, [(1, row)], statement_context(statement_type="hk_connect"))
        preview, commit = (
            eastmoney.preview_eastmoney_statement,
            eastmoney.import_eastmoney_statement,
        )
    else:
        account = make_account(db, broker=ibkr.BROKER_NAME, account_number_masked="U***00001")
        flow = parsed_ibkr_flow("a" * 64, activity_type="股息", gross_amount="90")
        flow.symbol, flow.raw_symbol, flow.market = "00700", "700", "港股"
        flow.trade_date, flow.base_currency, flow.price_currency = date(2026, 6, 1), "HKD", "HKD"
        stub_parsed_rows(monkeypatch, flow)
        monkeypatch.setattr(ibkr, "enrich_security_names", lambda *args, **kwargs: None)
        preview, commit = ibkr.preview_ibkr_activity, ibkr.import_ibkr_activity
    currency = "CNY" if broker == "eastmoney" else "HKD"
    action, first, _ = confirmed_manual_receipt(db, account=account, currency=currency)
    other = nearby_pending_announcement(db, account)
    stamps = first.updated_at, other.updated_at
    before = forecast_details(db, 1, [first, other])
    assert before[first.id]["receipt_state"] == "RECEIVED"
    assert before[other.id]["receipt_state"] == "PENDING"
    source, filename = (
        b"%PDF-existing-manual-receipt",
        "statement.csv" if broker == "ibkr" else "statement.pdf",
    )
    options = {"broker_account_id": account.id}
    if broker == "ibkr":
        # Preserve IBKR's existing suspected-duplicate confirmation step.
        options["confirmed_row_hashes"] = frozenset({flow.row_hash})
    result = preview(db, 1, source, filename, **options)
    assert not result["errors"]
    assert any(f"保留公告 #{first.id}" in warning for warning in result["warnings"])
    assert (first.updated_at, other.updated_at) == stamps
    assert forecast_details(db, 1, [first, other]) == before
    assert db.query(CorporateAction).count() == 1
    result = commit(db, 1, source, filename, **options)
    assert not result["errors"]
    db.expire_all()
    assert action.dividend_suggestion_id == first.id
    assert forecast_details(db, 1, [first, other]) == before
    assert other.updated_at == stamps[1]
    assert action.net_dividend == Decimal(90)
    assert action.import_batch_id == result["import_batch_id"]
    source_model = IbkrActivityFlow if broker == "ibkr" else BrokerFundFlow
    assert db.query(source_model).one().corporate_action_id == action.id
    stamp = first.updated_at
    repeated = commit(db, 1, source, filename, **options)
    assert repeated["duplicate_rows"] == 1
    db.expire_all()
    assert db.query(CorporateAction).count() == db.query(source_model).count() == 1
    assert first.updated_at == stamp
    assert forecast_details(db, 1, [first, other]) == before


@pytest.mark.parametrize("state", ["partial", "outside_window", "ignored"])
def test_existing_receipt_link_has_priority_over_date_candidates(db, state):
    action, first, _ = confirmed_manual_receipt(db)
    account = db.get(BrokerAccount, action.broker_account_id)
    other = nearby_pending_announcement(db, account)
    if state == "partial":
        first.receipt_complete = False
    elif state == "outside_window":
        first.ex_date, first.pay_date = date(2025, 5, 1), date(2025, 6, 1)
    else:
        first.status = "IGNORED"
    db.commit()
    before = forecast_details(db, 1, [first, other])
    plan = prepare_dividend_receipts(
        db,
        1,
        account.id,
        [DividendReceipt("proof", "00700", "港股", action.payment_date, "HKD", Decimal(90))],
        existing_hashes=set(),
    )
    assert plan.matches["proof"] == (action, first)
    assert plan.apply("proof", batch_id=None, source_note="补导券商凭证").id == action.id
    db.commit()
    assert forecast_details(db, 1, [first, other]) == before


def test_broker_proof_rejects_incompatible_existing_receipt_link(db):
    action, first, _ = confirmed_manual_receipt(db)
    first.broker_account_id = make_account(db, broker="其他", commit=True).id
    db.commit()
    with pytest.raises(ValueError, match="原公告关联"):
        prepare_dividend_receipts(
            db,
            1,
            action.broker_account_id,
            [DividendReceipt("proof", "00700", "港股", action.payment_date, "HKD", Decimal(90))],
            existing_hashes=set(),
        )
    assert action.net_dividend == Decimal(90)
    assert action.import_batch_id is None


def test_receipt_apply_rejects_reparenting_before_any_cash_write(db):
    action, first, _ = confirmed_manual_receipt(db)
    other = nearby_pending_announcement(db, db.get(BrokerAccount, action.broker_account_id))
    plan = prepare_dividend_receipts(
        db,
        1,
        action.broker_account_id,
        [DividendReceipt("proof", "00700", "港股", action.payment_date, "HKD", Decimal(90))],
        existing_hashes=set(),
    )
    plan.matches["proof"] = (action, other)
    notes, stamp = action.notes, other.updated_at
    with pytest.raises(ValueError, match="不能自动改挂"):
        plan.apply("proof", batch_id=None, source_note="不应写入")
    db.commit()
    db.expire_all()
    assert action.dividend_suggestion_id == first.id
    assert action.notes == notes
    assert other.updated_at == stamp


@pytest.mark.parametrize(
    "change", ["account", "symbol", "market", "action_type", "legacy", "legacy_match"]
)
def test_edit_cannot_move_a_completed_receipt_to_another_account_or_security(db, change):
    action, suggestion, user = confirmed_manual_receipt(db)
    other = make_account(db, broker="其他", commit=True)
    update = {"receipt_confirmed": True}
    if change in {"account", "legacy", "legacy_match"}:
        update["broker_account_id"] = other.id
    elif change == "symbol":
        update["symbol"] = "00941"
    elif change == "market":
        update["market"] = "B股"
    else:
        update.update(action_type="STOCK_DIVIDEND", shares_received=10, receipt_confirmed=False)
    if change in {"legacy", "legacy_match"}:
        action.amount_basis = "LEGACY"
        action.total_dividend, action.tax_withheld = Decimal(100), Decimal(10)
        update["receipt_confirmed"] = False
    if change == "legacy_match":
        action.dividend_suggestion_id = None
        suggestion.matched_corporate_action_id = action.id
        suggestion.match_detail = {}
    db.commit()
    account_id, stamp = action.broker_account_id, suggestion.updated_at
    with pytest.raises(HTTPException) as exc:
        update_corporate_action(action.id, CorporateActionUpdate(**update), user, db)
    assert exc.value.status_code == 409
    assert "解除关联" in exc.value.detail
    db.expire_all()
    assert (action.broker_account_id, action.symbol, action.market, action.action_type) == (
        account_id,
        "00700",
        "港股",
        "CASH_DIVIDEND",
    )
    assert action.net_dividend == Decimal(90)
    assert suggestion.updated_at == stamp
    detail = forecast_details(db, 1, [suggestion])[suggestion.id]
    assert detail["receipt_state"] == "RECEIVED"
    assert detail["receipt_complete"] is True
    assert detail["received_by_currency"] == {"HKD": Decimal(90)}


def test_receipt_notes_remain_editable_and_unlink_allows_account_correction(db):
    action, suggestion, user = confirmed_manual_receipt(db)
    other = make_account(db, broker="其他", commit=True)
    # Old match references must not re-link a receipt after explicit unlinking.
    suggestion.matched_corporate_action_id = action.id
    db.commit()
    update_corporate_action(action.id, CorporateActionUpdate(notes="补充凭证说明"), user, db)
    assert forecast_details(db, 1, [suggestion])[suggestion.id]["receipt_state"] == "RECEIVED"
    update_suggestion_receipts(
        suggestion.id,
        SuggestionReceiptsUpdate(receipt_ids=[], expected_updated_at=suggestion.updated_at),
        user,
        db,
    )
    updated = update_corporate_action(
        action.id,
        CorporateActionUpdate(broker_account_id=other.id, receipt_confirmed=True),
        user,
        db,
    )
    assert updated.broker_account_id == other.id
    assert updated.dividend_suggestion_id is None
    assert updated.net_dividend == Decimal(90)
    assert db.query(CorporateAction).count() == 1
    detail = forecast_details(db, 1, [suggestion])[suggestion.id]
    assert detail["receipt_state"] == "PENDING"
    assert detail["receipt_complete"] is False
    assert detail["received_by_currency"] == {}


def test_multiple_dates_link_one_forecast_without_guessing_completion(db):
    account = make_account(db)
    suggestion = announcement(db, account)
    receipts = [
        DividendReceipt(str(i), "00700", "港股", date(2026, 6, i + 1), "HKD", Decimal(45))
        for i in range(2)
    ]
    plan = prepare_dividend_receipts(db, 1, account.id, receipts, existing_hashes=set())
    assert db.query(CorporateAction).count() == 0
    first = plan.apply("0", batch_id=None, source_note="第一期实际到账")
    second = plan.apply("1", batch_id=None, source_note="第二期实际到账")
    assert first.id != second.id
    assert first.payment_date != second.payment_date
    assert first.total_dividend is first.tax_withheld is None
    db.commit()
    detail = forecast_details(db, 1, [suggestion])[suggestion.id]
    assert detail["received_by_currency"] == {"HKD": Decimal(90)}
    assert detail["remaining_estimated_gross"] is None
    assert not detail["receipt_complete"]
    stamp = suggestion.updated_at
    response = update_suggestion_receipts(
        suggestion.id,
        SuggestionReceiptsUpdate(
            receipt_ids=[first.id, second.id], receipt_complete=True, expected_updated_at=stamp
        ),
        get_user(db),
        db,
    )
    assert response.receipt_state == "RECEIVED"
    assert forecast_summary(db, 1)["pending_count"] == 0
    assert sum(a.net_dividend for a in db.query(CorporateAction)) == Decimal(90)
    with pytest.raises(HTTPException) as exc:
        update_suggestion_receipts(
            suggestion.id,
            SuggestionReceiptsUpdate(receipt_ids=[], expected_updated_at=stamp),
            get_user(db),
            db,
        )
    assert exc.value.status_code == 409


def test_cross_account_link_is_rejected_without_changing_cash(db):
    account, other = make_account(db), make_account(db, broker="其他")
    suggestion = announcement(db, account)
    action = CorporateAction(
        user_id=1,
        broker_account_id=other.id,
        symbol="00700",
        market="港股",
        action_type="CASH_DIVIDEND",
        ex_date=date(2026, 5, 1),
        payment_date=date(2026, 6, 1),
        currency="HKD",
        net_dividend=Decimal(90),
        amount_basis="NET_ONLY",
    )
    db.add(action)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        update_suggestion_receipts(
            suggestion.id,
            SuggestionReceiptsUpdate(
                receipt_ids=[action.id],
                receipt_complete=True,
                expected_updated_at=suggestion.updated_at,
            ),
            get_user(db),
            db,
        )
    assert exc.value.status_code == 409
    assert action.dividend_suggestion_id is None
    assert action.net_dividend == Decimal(90)


def test_confirmed_manual_receipt_links_forecast_and_rejects_duplicate(db):
    account = make_account(db)
    suggestion = announcement(db, account)
    payload = CorporateActionCreate(
        symbol="00700",
        market="港股",
        action_type="CASH_DIVIDEND",
        broker_account_id=account.id,
        ex_date=date(2026, 5, 1),
        payment_date=date(2026, 6, 1),
        currency="HKD",
        amount_basis="NET_ONLY",
        net_dividend=90,
        receipt_confirmed=True,
    )
    user = get_user(db)
    first = create_corporate_action(payload, user, db)
    assert first.dividend_suggestion_id == suggestion.id
    with pytest.raises(HTTPException) as exc:
        create_corporate_action(payload, user, db)
    assert exc.value.status_code == 409
    assert db.query(CorporateAction).count() == 1
    # Later broker proof reuses this canonical cash receipt.
    plan = prepare_dividend_receipts(
        db,
        1,
        account.id,
        [DividendReceipt("proof", "00700", "港股", first.payment_date, "HKD", Decimal(90))],
        existing_hashes=set(),
    )
    assert plan.apply("proof", batch_id=None, source_note="券商凭证").id == first.id
    assert db.query(CorporateAction).count() == 1


def test_ibkr_dividend_and_withholding_link_forecast_and_reimport_once(db, monkeypatch):
    account = make_account(db, broker=ibkr.BROKER_NAME, account_number_masked="U***00001")
    suggestion = announcement(db, account)
    suggestion.symbol, suggestion.market, suggestion.currency = "AAPL", "美股", "USD"
    suggestion.ex_date = date(2026, 1, 1)
    suggestion.pay_date = date(2026, 1, 2)
    db.commit()
    dividend = parsed_ibkr_flow("receipt-dividend", activity_type="股息", gross_amount="100")
    tax = parsed_ibkr_flow(
        "receipt-tax", activity_type=ibkr.WITHHOLDING_TAX_TYPE, gross_amount="-10"
    )
    stub_parsed_rows(monkeypatch, dividend, tax)
    monkeypatch.setattr(ibkr, "enrich_security_names", lambda *args, **kwargs: None)
    preview = ibkr.preview_ibkr_activity(
        db, 1, b"fixture", "dividend.csv", broker_account_id=account.id
    )
    assert not preview["errors"]
    assert any("关联预计股息" in warning for warning in preview["warnings"])
    assert db.query(CorporateAction).count() == 0
    result = ibkr.import_ibkr_activity(
        db, 1, b"fixture", "dividend.csv", broker_account_id=account.id
    )
    assert not result["errors"]
    action = db.query(CorporateAction).one()
    assert action.dividend_suggestion_id == suggestion.id
    assert (action.total_dividend, action.tax_withheld, action.net_dividend) == (
        Decimal(100),
        Decimal(10),
        Decimal(90),
    )
    repeated = ibkr.import_ibkr_activity(
        db, 1, b"fixture", "dividend.csv", broker_account_id=account.id
    )
    assert repeated["duplicate_rows"] == 2
    assert db.query(CorporateAction).count() == 1
    assert db.query(IbkrActivityFlow).count() == 2


def test_ambiguous_forecasts_leave_cash_unlinked_instead_of_selecting_nearest(db):
    account = make_account(db)
    first = announcement(db, account)
    first.ex_date = date(2026, 5, 5)
    db.commit()
    announcement(db, account)
    plan = prepare_dividend_receipts(
        db,
        1,
        account.id,
        [DividendReceipt("proof", "00700", "港股", date(2026, 6, 1), "HKD", Decimal(90))],
        existing_hashes=set(),
    )
    assert not plan.matches
    assert "暂不关联" in plan.warnings()[0]
    assert db.query(CorporateAction).count() == 0


def test_resync_withdrawal_keeps_receipt_relationship_and_cash_fact(db):
    from app.services import dividend_sync_service as sync

    account = make_account(db)
    suggestion = announcement(db, account)
    plan = prepare_dividend_receipts(
        db,
        1,
        account.id,
        [DividendReceipt("r", "00700", "港股", date(2026, 6, 1), "HKD", Decimal(90))],
        existing_hashes=set(),
    )
    receipt = plan.apply("r", batch_id=None, source_note="券商到账")
    db.commit()
    removed = sync._remove_stale_suggestions(db, 1, "00700", "港股", suggestion.ex_date, set())
    db.commit()
    assert removed == 0
    assert receipt.dividend_suggestion_id == suggestion.id
    assert forecast_details(db, 1, [suggestion])[suggestion.id]["receipt_state"] == "INACTIVE"
    assert receipt.net_dividend == Decimal(90)
