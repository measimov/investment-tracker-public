from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.api.corporate_actions import delete_corporate_action
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.cash_event import CashEvent
from app.models.dividend_tax_allocation import DividendTaxAllocation
from app.models.corporate_action import CorporateAction
from app.models.corporate_action_suggestion import CorporateActionSuggestion
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.import_batch import ImportBatch
from app.models.reconciliation_snapshot import ReconciliationSnapshot
from app.models.transaction import Transaction
from app.services import cmb_fund_flow_importer as cmb
from app.services import eastmoney_statement_importer as eastmoney
from app.services.dividend_receipt_service import DividendReceipt, prepare_dividend_receipts
from app.services.dividend_sync_service import accept_suggestion
from tests.helpers import get_user, make_account, reset_tables
from tests.test_cmb_fund_flow_importer import parsed_flow
from tests.test_eastmoney_statement_importer import flow_row, patch_statement, statement_context

RESET_MODELS = (
    CorporateActionSuggestion,
    BrokerFundFlow,
    IbkrActivityFlow,
    Holding,
    CashEvent,
    CorporateAction,
    Transaction,
    ReconciliationSnapshot,
    ImportBatch,
    BrokerAccount,
)


@pytest.fixture
def db():
    session = SessionLocal()
    reset_tables(session, RESET_MODELS)
    try:
        yield session
    finally:
        session.rollback()
        reset_tables(session, RESET_MODELS)
        session.close()


def accepted(db, account, *, ex_date=date(2026, 5, 1), status="ACCEPTED"):
    action = CorporateAction(
        user_id=1,
        broker_account_id=account.id,
        symbol="00700",
        market="港股",
        action_type="CASH_DIVIDEND",
        ex_date=ex_date,
        payment_date=date(2026, 6, 10),
        total_dividend=Decimal("100"),
        tax_withheld=Decimal("10"),
        net_dividend=Decimal("90"),
        currency="HKD",
    )
    db.add(action)
    db.flush()
    suggestion = CorporateActionSuggestion(
        user_id=1,
        broker_account_id=account.id,
        symbol="00700",
        market="港股",
        action_type="CASH_DIVIDEND",
        ex_date=ex_date,
        pay_date=date(2026, 6, 10),
        currency="HKD",
        cash_div_pre_tax=Decimal("1"),
        record_date_quantity=Decimal("100"),
        status=status,
        created_corporate_action_id=action.id,
    )
    db.add(suggestion)
    db.commit()
    return action, suggestion


@pytest.mark.parametrize(
    "currency,payment_date", [("USD", date(2026, 6, 10)), ("HKD", date(2026, 6, 25))]
)
def test_receipt_cannot_invalidate_existing_dividend_tax_allocation(db, currency, payment_date):
    account = make_account(db)
    action, _ = accepted(db, account)
    event = CashEvent(
        user_id=1,
        broker_account_id=account.id,
        event_type="TAX",
        tax_kind="DIVIDEND",
        currency="HKD",
        amount=Decimal(5),
        event_date=date(2026, 6, 20),
    )
    db.add(event)
    db.flush()
    db.add(
        DividendTaxAllocation(
            user_id=1, cash_event_id=event.id, corporate_action_id=action.id, amount=Decimal(5)
        )
    )
    db.commit()
    with pytest.raises(ValueError, match="已分摊递延税"):
        prepare_dividend_receipts(
            db,
            1,
            account.id,
            [DividendReceipt("allocated", "00700", "港股", payment_date, currency, Decimal(90))],
            existing_hashes=set(),
        )
    assert action.currency == "HKD"
    assert action.payment_date == date(2026, 6, 10)


@pytest.mark.parametrize("broker", ["cmb", "eastmoney"])
def test_real_receipt_merges_accepted_estimate_and_reimport_is_idempotent(db, monkeypatch, broker):
    if broker == "cmb":
        account = make_account(db, broker=cmb.BROKER_NAME, account_number_masked="****A123")
        receipt = parsed_flow(
            row_number=2,
            row_hash="a" * 64,
            business_name="股息入账",
            trade_date=date(2026, 6, 11),
            quantity="100",
            price="0",
            amount="72",
        )
        receipt.security_code = "00700"
        monkeypatch.setattr(
            cmb, "parse_rows", lambda *args, **kwargs: ([receipt], {"股息入账": 1}, 1, [])
        )
        preview, commit = cmb.preview_cmb_fund_flow, cmb.import_cmb_fund_flow
    else:
        account = make_account(db, broker=eastmoney.BROKER_NAME)
        rows = [(1, flow_row("20260611", "红利入账", "00700", "测试股", "100", "0", "72"))]
        rows[0][1]["_statement_type"] = "hk_connect"
        rows[0][1].update(
            {
                key: "0"
                for key in ("结算汇率", "交易费", "交易征费", "交收费", "系统费用", "其他费用")
            }
        )
        patch_statement(monkeypatch, rows, statement_context(statement_type="hk_connect"))
        preview, commit = (
            eastmoney.preview_eastmoney_statement,
            eastmoney.import_eastmoney_statement,
        )
    action, suggestion = accepted(db, account)
    source = b"%PDF-dividend-receipt"
    result = preview(db, 1, source, "statement.pdf", broker_account_id=account.id)
    assert result["merged_dividend_rows"] == 1
    assert any("合并至已接受" in item for item in result["warnings"])
    assert action.total_dividend == Decimal("100")  # preview is read-only
    result = commit(db, 1, source, "statement.pdf", broker_account_id=account.id)
    assert result["merged_dividend_rows"] == 1
    db.refresh(action)
    db.refresh(suggestion)
    assert db.query(CorporateAction).count() == 1
    assert action.ex_date == date(2026, 5, 1)
    assert action.payment_date == date(2026, 6, 11)
    assert action.currency == "CNY"
    assert action.net_dividend == Decimal("72")
    assert action.import_batch_id == result["import_batch_id"]
    assert db.query(BrokerFundFlow).one().corporate_action_id == action.id
    assert suggestion.match_detail["broker_receipt"]["previous_gross"] == "100.00000000"
    assert suggestion.match_detail["broker_receipt"]["amount_basis"] == "net_receipt"
    repeat = commit(db, 1, source, "statement.pdf", broker_account_id=account.id)
    assert repeat["duplicate_rows"] == 1
    assert db.query(CorporateAction).count() == db.query(BrokerFundFlow).count() == 1


def test_ambiguous_receipt_is_blocked_instead_of_picking_nearest(db):
    account = make_account(db)
    accepted(db, account)
    accepted(db, account, ex_date=date(2026, 5, 15))
    receipt = DividendReceipt("a", "00700", "港股", date(2026, 6, 11), "CNY", Decimal("72"))
    with pytest.raises(ValueError, match="多个分红候选"):
        prepare_dividend_receipts(db, 1, account.id, [receipt], existing_hashes=set())
    assert db.query(CorporateAction).count() == 2


def test_same_currency_receipt_does_not_treat_announcement_tax_as_verified(db):
    account = make_account(db)
    action, _ = accepted(db, account)
    receipt = DividendReceipt("a", "00700", "港股", date(2026, 6, 11), "HKD", Decimal("90"))
    plan = prepare_dividend_receipts(db, 1, account.id, [receipt], existing_hashes=set())
    plan.apply("a", batch_id=None, source_note="测试到账")
    assert action.total_dividend is action.tax_withheld is None
    assert action.amount_basis == "NET_ONLY"
    assert action.net_dividend == Decimal("90")


def test_ambiguous_cmb_receipt_preview_and_commit_both_block_without_partial_booking(
    db, monkeypatch
):
    account = make_account(db, broker=cmb.BROKER_NAME, account_number_masked="****A123")
    accepted(db, account)
    accepted(db, account, ex_date=date(2026, 5, 15))
    receipt = parsed_flow(
        row_number=2,
        row_hash="a" * 64,
        business_name="股息入账",
        trade_date=date(2026, 6, 11),
        quantity="100",
        price="0",
        amount="72",
    )
    receipt.security_code = "00700"
    monkeypatch.setattr(
        cmb, "parse_rows", lambda *args, **kwargs: ([receipt], {"股息入账": 1}, 1, [])
    )
    source = b"%PDF-ambiguous-receipt"
    preview = cmb.preview_cmb_fund_flow(
        db, 1, source, "statement.pdf", broker_account_id=account.id
    )
    assert any("多个分红候选" in message for message in preview["errors"])
    with pytest.raises(ValueError, match="多个分红候选"):
        cmb.import_cmb_fund_flow(db, 1, source, "statement.pdf", broker_account_id=account.id)
    assert db.query(CorporateAction).count() == 2
    assert db.query(BrokerFundFlow).count() == 0
    assert db.query(ImportBatch).one().status == "FAILED"


def test_different_payment_dates_cannot_be_collapsed_to_one_cash_date(db):
    account = make_account(db)
    accepted(db, account)
    receipts = [
        DividendReceipt(
            str(i), "00700", "港股", date(2026, 6, 11) + timedelta(days=i), "CNY", Decimal("36")
        )
        for i in range(2)
    ]
    with pytest.raises(ValueError, match="分次到账"):
        prepare_dividend_receipts(db, 1, account.id, receipts, existing_hashes=set())


def test_other_account_receipt_does_not_replace_this_accounts_dividend(db):
    first, second = make_account(db), make_account(db, broker="IBKR")
    action, _ = accepted(db, first)
    receipt = DividendReceipt("a", "00700", "港股", date(2026, 6, 11), "CNY", Decimal("72"))
    assert not prepare_dividend_receipts(db, 1, second.id, [receipt], existing_hashes=set()).matches
    assert action.currency == "HKD"


@pytest.mark.parametrize("status", ["ACCEPTED", "MATCHED", "IGNORED"])
def test_deleting_suggestion_action_restores_available_status_but_preserves_ignore(db, status):
    account = make_account(db)
    action, suggestion = accepted(db, account, status=status)
    if status != "ACCEPTED":
        suggestion.created_corporate_action_id = None
        suggestion.matched_corporate_action_id = action.id
        db.commit()
    delete_corporate_action(action.id, current_user=get_user(db), db=db)
    db.refresh(suggestion)
    assert suggestion.status == ("IGNORED" if status == "IGNORED" else "NEW")
    assert suggestion.created_corporate_action_id is suggestion.matched_corporate_action_id is None
    if status == "ACCEPTED":
        from app.services.dividend_sync_service import SuggestionStateError

        with pytest.raises(SuggestionStateError, match="不能通过接受公告"):
            accept_suggestion(db, get_user(db), suggestion.id, {"tax_withheld": "0"})
        assert db.query(CorporateAction).count() == 0
