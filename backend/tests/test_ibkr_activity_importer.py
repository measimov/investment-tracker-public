import asyncio
import hashlib
import io
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import UploadFile

from app.api import import_export as import_export_api
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.import_batch import ImportBatch
from app.models.transaction import Transaction
from app.services import ibkr_activity_importer as importer
from app.services.ibkr_activity_importer import (
    calculate_position_before,
    import_ibkr_activity,
    is_option_symbol,
    parse_rows,
)
from tests.helpers import ibkr_csv, reset_tables, seed_security_rule, PCT_RELISTING_PAYLOAD


RESET_MODELS = (
    BrokerFundFlow,
    IbkrActivityFlow,
    Holding,
    CorporateAction,
    Transaction,
    ImportBatch,
    BrokerAccount,
)


def parsed_ibkr_flow(
    row_hash: str,
    *,
    activity_type: str = "买",
    gross_amount: str = "-1001",
) -> importer.ParsedIbkrFlow:
    is_trade = activity_type in {"买", "卖"}
    return importer.ParsedIbkrFlow(
        source_row_number=5,
        row_hash=row_hash,
        account="U***00001",
        trade_date=date(2026, 1, 2),
        description="APPLE INC",
        activity_type=activity_type,
        raw_symbol="AAPL",
        symbol="AAPL",
        name="Apple",
        market="美股",
        quantity=Decimal("100") if is_trade else None,
        price=Decimal("10") if is_trade else None,
        price_currency="USD",
        base_currency="USD",
        gross_amount=Decimal(gross_amount),
        commission=Decimal("-1") if is_trade else None,
        net_amount=Decimal("-1002") if is_trade else Decimal(gross_amount),
        fee_in_price_currency=Decimal("1") if is_trade else None,
    )


def stub_parsed_rows(monkeypatch, *flows: importer.ParsedIbkrFlow) -> None:
    counts: dict[str, int] = {}
    for flow in flows:
        counts[flow.activity_type] = counts.get(flow.activity_type, 0) + 1
    monkeypatch.setattr(
        importer,
        "parse_rows",
        lambda contents, filename, **kwargs: (list(flows), counts, len(flows), []),
    )


def test_ibkr_import_resolves_name_from_symbol_lookup(monkeypatch):
    monkeypatch.setattr(
        importer,
        "lookup_tushare_security_name",
        lambda symbol, market: {
            ("00883", "港股"): "中国海洋石油",
        }.get((symbol, market)),
    )

    contents = ibkr_csv(
        "Transaction History,Data,2026-05-07,U***00001,CNOOC LTD-H,买,883,"
        "1000.0,27.36,HKD,-3493.05,-2.79,-3499.52"
    )

    rows, _, _, errors = parse_rows(contents, "ibkr.csv")

    assert errors == []
    assert rows[0].symbol == "00883"
    assert rows[0].name == "中国海洋石油"
    assert rows[0].description == "CNOOC LTD-H"


def test_ibkr_option_detection_covers_occ_and_hk_alias_formats():
    assert is_option_symbol("PYPL  260417P00040000", "PYPL 17APR26 40 P")
    assert is_option_symbol("POP APR26 155 P", "9992 29APR26 155 P")
    assert is_option_symbol("CNC JAN26 20 P", "883 29JAN26 20 P")
    assert is_option_symbol("MIU JAN26 52.5 C", "1810 29JAN26 52.5 C")
    assert not is_option_symbol("883", "CNOOC LTD-H")
    assert not is_option_symbol("PCT", "PC PARTNER GROUP LTD")


def test_ibkr_exercise_rows_are_imported_only_when_long_only_safe(monkeypatch):
    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda symbol, market: None)

    contents = ibkr_csv(
        "Transaction History,Data,2026-03-20,U***00001,"
        "卖 -100 INVESCO CURRENCYSHARES EURO (行使),行权,FXE,"
        "-100.0,107.0,USD,10700.0,-0.0195,10699.9805",
        "Transaction History,Data,2025-08-28,U***00001,"
        "买 500 MEITUAN-CLASS B (转让),被行权,3690,"
        "500.0,125.0,HKD,-8125.0,-2.0,-8127.0",
    )

    rows, _, _, errors = parse_rows(contents, "ibkr-exercise.csv")

    assert errors == []
    assert len(rows) == 2
    assert rows[0].skip_reason == "option"
    assert not rows[0].is_trade
    assert rows[1].skip_reason is None
    assert rows[1].is_trade


def test_ibkr_identical_stock_fills_get_distinct_hashes(monkeypatch):
    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda symbol, market: None)

    contents = ibkr_csv(
        "Transaction History,Data,2025-04-07,U***00001,WEIBO CORP-CLASS A,"
        "买,9898,40.0,67.45,HKD,-347.34051999999997,-,-347.73663920482",
        "Transaction History,Data,2025-04-07,U***00001,WEIBO CORP-CLASS A,"
        "买,9898,40.0,67.45,HKD,-347.34051999999997,-,-347.73663920482",
    )

    rows, _, _, errors = parse_rows(contents, "ibkr-identical-fills.csv")

    assert errors == []
    assert len(rows) == 2
    assert rows[0].is_trade
    assert rows[1].is_trade
    assert rows[0].row_hash != rows[1].row_hash


def test_ibkr_statement_skips_representative_option_rows(monkeypatch):
    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda symbol, market: None)

    contents = ibkr_csv(
        "Transaction History,Data,2025-01-02,U***00001,CNOOC LTD-H,买,883,"
        "1000.0,27.36,HKD,-3493.05,-2.79,-3499.52",
        "Transaction History,Data,2025-01-03,U***00001,883 29JAN26 20 P,卖,CNC JAN26 20 P,"
        "-1.0,0.55,HKD,55.0,-1.0,54.0",
        "Transaction History,Data,2025-01-04,U***00001,PYPL 17APR26 40 P,买,"
        "PYPL  260417P00040000,1.0,1.25,USD,-125.0,-1.0,-126.0",
        "Transaction History,Data,2025-01-05,U***00001,"
        "卖 -100 INVESCO CURRENCYSHARES EURO (行使),行权,FXE,"
        "-100.0,107.0,USD,10700.0,-0.0195,10699.9805",
        "Transaction History,Data,2025-01-06,U***00001,PC PARTNER GROUP LTD,买,PCT,"
        "1000.0,1.91,SGD,-1910.0,-2.0,-1912.0",
    )
    rows, _, total_rows, errors = parse_rows(contents, "ibkr-representative-options.csv")

    assert errors == []
    assert total_rows == 5
    assert len([row for row in rows if row.skip_reason == "option"]) == 3

    skipped_symbols = {row.raw_symbol for row in rows if row.skip_reason == "option"}
    imported_symbols = {row.raw_symbol for row in rows if row.skip_reason is None}
    assert skipped_symbols == {"CNC JAN26 20 P", "PYPL  260417P00040000", "FXE"}
    assert imported_symbols == {"883", "PCT"}


def test_ibkr_import_handles_pc_partner_hk_to_sg_relisting(monkeypatch):
    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda symbol, market: None)

    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        seed_security_rule(
            db, 1, "RELISTING", "01263", "港股", payload=PCT_RELISTING_PAYLOAD
        )
        seed_security_rule(
            db, 1, "NAME_OVERRIDE", "01263", "港股", payload={"name": "柏能集团"}
        )
        seed_security_rule(
            db, 1, "NAME_OVERRIDE", "PCT", "新加坡股", payload={"name": "柏能集团"}
        )
        account = BrokerAccount(
            user_id=1,
            broker="IBKR",
            account_name="IBKR 测试账户",
            account_number_masked="****0001",
            base_currency="USD",
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        contents = ibkr_csv(
            "Transaction History,Data,2026-05-04,U***00001,PC PARTNER GROUP LTD,"
            "卖,PCT,-1000.0,1.91,SGD,1495.3963,-1.957325,1493.438975",
            "Transaction History,Data,2025-12-09,U***00001,PC PARTNER GROUP LTD,"
            "买,1263,2000.0,5.41,HKD,-1390.3700000000001,-2.313,-1394.1361255450001",
            "Transaction History,Data,2025-10-30,U***00001,PC PARTNER GROUP LTD,"
            "买,1263,2000.0,6.31,HKD,-1624.1940000000002,-2.3166,-1628.229989529",
            "Transaction History,Data,2025-10-09,U***00001,PC PARTNER GROUP LTD,"
            "买,1263,2000.0,7.09,HKD,-1822.2718000000002,-2.31318,-1826.5645647463002",
        )

        result = import_ibkr_activity(
            db,
            1,
            contents,
            "ibkr-pct.csv",
            broker_account_id=account.id,
        )

        old_holding = (
            db.query(Holding)
            .filter(Holding.user_id == 1, Holding.symbol == "01263", Holding.market == "港股")
            .first()
        )
        new_holding = (
            db.query(Holding)
            .filter(Holding.user_id == 1, Holding.symbol == "PCT", Holding.market == "新加坡股")
            .first()
        )
        synthetic_count = (
            db.query(Transaction)
            .filter(Transaction.notes.like("%synthetic_relisting_transfer%"))
            .count()
        )

        assert result["errors"] == []
        assert result["eligible_trade_rows"] == 4
        assert result["imported_transactions"] == 6
        assert result["archived_source_rows"] == 4
        assert result["batch_status"] == "COMPLETED"
        assert result["broker_account_id"] == account.id
        assert synthetic_count == 2
        assert old_holding is None
        assert new_holding is not None
        assert new_holding.name == "柏能集团"
        assert new_holding.quantity == 5000
        assert new_holding.currency == "SGD"
        assert 1 < float(new_holding.avg_cost) < 1.1

        transactions = db.query(Transaction).all()
        assert {txn.broker_account_id for txn in transactions} == {account.id}
        assert {txn.import_batch_id for txn in transactions} == {
            result["import_batch_id"]
        }
        flows = db.query(IbkrActivityFlow).all()
        assert len(flows) == 4
        assert {flow.import_batch_id for flow in flows} == {
            result["import_batch_id"]
        }
        assert {flow.broker_account_id for flow in flows} == {account.id}
        batch = db.get(ImportBatch, result["import_batch_id"])
        assert batch.source_sha256 == hashlib.sha256(contents).hexdigest()
        assert batch.row_count == 4
        assert batch.archived_count == 4
        assert batch.imported_count == 4
        assert batch.duplicate_count == 0
        assert result["canonical_objects_changed"] == 6

        preview = importer.preview_ibkr_activity(
            db,
            1,
            contents,
            "ibkr-pct.csv",
            broker_account_id=account.id,
        )
        assert preview["duplicate_rows"] == 4

        duplicate = import_ibkr_activity(
            db,
            1,
            contents,
            "ibkr-pct.csv",
            broker_account_id=account.id,
        )
        assert duplicate["batch_status"] == "COMPLETED"
        assert duplicate["imported_transactions"] == 0
        assert duplicate["duplicate_rows"] == 4
        assert duplicate["import_batch_id"] != result["import_batch_id"]
        duplicate_batch = db.get(ImportBatch, duplicate["import_batch_id"])
        assert duplicate_batch.imported_count == 0
        assert duplicate_batch.duplicate_count == 4
        assert duplicate_batch.skipped_count == 0
        assert db.query(Transaction).count() == 6
    finally:
        db.close()


def test_ibkr_preview_requires_owned_matching_broker_account():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        wrong_broker = BrokerAccount(
            user_id=1,
            broker="招商证券",
            account_name="错误券商",
            base_currency="CNY",
        )
        other_user = BrokerAccount(
            user_id=2,
            broker="IBKR",
            account_name="其他用户 IBKR",
            account_number_masked="****0001",
            base_currency="USD",
        )
        db.add_all([wrong_broker, other_user])
        db.commit()

        with pytest.raises(ValueError, match="请选择 IBKR 券商账户"):
            importer.preview_ibkr_activity(db, 1, b"unused", "ibkr.csv")
        with pytest.raises(ValueError, match="belongs to"):
            importer.preview_ibkr_activity(
                db,
                1,
                b"unused",
                "ibkr.csv",
                broker_account_id=wrong_broker.id,
            )
        with pytest.raises(ValueError, match="not found"):
            importer.preview_ibkr_activity(
                db,
                1,
                b"unused",
                "ibkr.csv",
                broker_account_id=other_user.id,
            )
    finally:
        db.close()


def test_ibkr_preview_api_forwards_broker_account_id(monkeypatch):
    captured = {}

    def fake_preview(db, user_id, contents, filename, broker_account_id=None):
        captured.update(
            {
                "db": db,
                "user_id": user_id,
                "contents": contents,
                "filename": filename,
                "broker_account_id": broker_account_id,
            }
        )
        return {"ok": True}

    monkeypatch.setattr(import_export_api, "preview_ibkr_activity", fake_preview)
    db_marker = object()
    result = asyncio.run(
        import_export_api.preview_ibkr_activity_statement(
            file=UploadFile(filename="ibkr.csv", file=io.BytesIO(b"statement")),
            broker_account_id=37,
            current_user=SimpleNamespace(id=1),
            db=db_marker,
        )
    )

    assert result == {"ok": True}
    assert captured == {
        "db": db_marker,
        "user_id": 1,
        "contents": b"statement",
        "filename": "ibkr.csv",
        "broker_account_id": 37,
    }


def test_ibkr_duplicate_hash_rejects_different_account_in_preview_and_import(
    monkeypatch,
):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        first_account = BrokerAccount(
            user_id=1,
            broker="IBKR",
            account_name="IBKR 账户一",
            account_number_masked="****0001",
            base_currency="USD",
        )
        second_account = BrokerAccount(
            user_id=1,
            broker="IBKR",
            account_name="IBKR 账户二",
            account_number_masked="****0001",
            base_currency="USD",
        )
        db.add_all([first_account, second_account])
        db.commit()

        flow = parsed_ibkr_flow("a" * 64)
        stub_parsed_rows(monkeypatch, flow)
        first_result = import_ibkr_activity(
            db,
            1,
            b"first-file",
            "first.csv",
            broker_account_id=first_account.id,
        )
        assert first_result["batch_status"] == "COMPLETED"

        with pytest.raises(ValueError, match="属于其他券商账户"):
            importer.preview_ibkr_activity(
                db,
                1,
                b"second-file-preview",
                "second.csv",
                broker_account_id=second_account.id,
            )
        with pytest.raises(ValueError, match="属于其他券商账户"):
            import_ibkr_activity(
                db,
                1,
                b"second-file-import",
                "second.csv",
                broker_account_id=second_account.id,
            )

        assert db.query(Transaction).count() == 1
        batches = db.query(ImportBatch).order_by(ImportBatch.id).all()
        assert [batch.status for batch in batches] == ["COMPLETED", "FAILED"]
        assert batches[1].imported_count == 0
        assert batches[1].archived_count == 0
    finally:
        db.close()


@pytest.mark.parametrize(
    ("source_state", "message"),
    [
        ("orphan", "孤儿记录"),
        ("conflicting_links", "同时链接交易和公司行动"),
    ],
)
def test_ibkr_duplicate_hash_rejects_unsafe_historical_source(
    monkeypatch,
    source_state,
    message,
):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = BrokerAccount(
            user_id=1,
            broker="IBKR",
            account_name="IBKR 目标账户",
            account_number_masked="****0001",
            base_currency="USD",
        )
        db.add(account)
        db.flush()
        transaction = Transaction(
            user_id=1,
            broker_account_id=account.id,
            symbol="AAPL",
            name="Apple",
            market="美股",
            transaction_type="BUY",
            quantity=Decimal("100"),
            price=Decimal("10"),
            fee=Decimal("1"),
            transaction_date=date(2026, 1, 2),
            currency="USD",
        )
        action = CorporateAction(
            user_id=1,
            broker_account_id=account.id,
            symbol="AAPL",
            name="Apple",
            market="美股",
            action_type="CASH_DIVIDEND",
            ex_date=date(2026, 1, 2),
            total_dividend=Decimal("10"),
            tax_withheld=Decimal("0"),
            net_dividend=Decimal("10"),
            currency="USD",
        )
        db.add_all([transaction, action])
        db.flush()

        flow = parsed_ibkr_flow("b" * 64)
        source = importer.create_ibkr_activity_flow(
            user_id=1,
            filename="legacy.csv",
            flow=flow,
            broker_account_id=account.id,
            transaction_id=(
                transaction.id if source_state == "conflicting_links" else None
            ),
            corporate_action_id=action.id if source_state == "conflicting_links" else None,
        )
        db.add(source)
        db.commit()
        stub_parsed_rows(monkeypatch, flow)

        with pytest.raises(ValueError, match=message):
            importer.preview_ibkr_activity(
                db,
                1,
                b"new-file",
                "ibkr.csv",
                broker_account_id=account.id,
            )
        with pytest.raises(ValueError, match=message):
            import_ibkr_activity(
                db,
                1,
                f"formal-{source_state}".encode(),
                "ibkr.csv",
                broker_account_id=account.id,
            )

        batch = db.query(ImportBatch).one()
        assert batch.status == "FAILED"
        assert batch.imported_count == 0
        assert batch.archived_count == 0
    finally:
        db.close()


def test_ibkr_account_mask_mismatch_fails_without_records(monkeypatch):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = BrokerAccount(
            user_id=1,
            broker="IBKR",
            account_name="IBKR 另一账户",
            account_number_masked="****0002",
            base_currency="USD",
        )
        db.add(account)
        db.commit()
        stub_parsed_rows(monkeypatch, parsed_ibkr_flow("e" * 64))

        with pytest.raises(ValueError, match="账户与所选券商账户不匹配"):
            import_ibkr_activity(
                db,
                1,
                b"wrong-account",
                "ibkr.csv",
                broker_account_id=account.id,
            )

        batch = db.query(ImportBatch).one()
        assert batch.status == "FAILED"
        assert batch.imported_count == 0
        assert batch.duplicate_count == 0
        assert batch.archived_count == 0
        assert db.query(Transaction).count() == 0
        assert db.query(IbkrActivityFlow).count() == 0
    finally:
        db.close()


def test_ibkr_tax_is_preserved_unbooked_when_same_account_candidate_is_ambiguous(
    monkeypatch,
):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = BrokerAccount(
            user_id=1,
            broker="IBKR",
            account_name="IBKR 税款账户",
            account_number_masked="****0001",
            base_currency="USD",
        )
        db.add(account)
        db.flush()
        actions = [
            CorporateAction(
                user_id=1,
                broker_account_id=account.id,
                symbol="AAPL",
                name="Apple",
                market="美股",
                action_type="CASH_DIVIDEND",
                ex_date=date(2026, 1, 2),
                payment_date=date(2026, 1, 2),
                total_dividend=Decimal("10"),
                tax_withheld=Decimal("0"),
                net_dividend=Decimal("10"),
                currency="USD",
            )
            for _ in range(2)
        ]
        db.add_all(actions)
        db.commit()
        tax_flow = parsed_ibkr_flow(
            "f" * 64,
            activity_type="外国预扣税",
            gross_amount="-1",
        )
        stub_parsed_rows(monkeypatch, tax_flow)

        result = import_ibkr_activity(
            db,
            1,
            b"ambiguous-tax",
            "ibkr.csv",
            broker_account_id=account.id,
        )
        batch = db.get(ImportBatch, result["import_batch_id"])
        source = db.query(IbkrActivityFlow).one()
        assert result["imported_tax_adjustments"] == 0
        assert result["eligible_unbooked_source_rows"] == 1
        assert any("found 2" in error for error in result["errors"])
        assert source.broker_account_id == account.id
        assert source.corporate_action_id is None
        assert source.skip_reason == "unattributed_tax"
        assert batch.status == "PARTIAL"
        assert batch.imported_count == 0
        assert batch.duplicate_count == 0
        assert batch.archived_count == 1
        assert batch.skipped_count == 1
        assert [action.tax_withheld for action in actions] == [
            Decimal("0"),
            Decimal("0"),
        ]
    finally:
        db.close()


def test_ibkr_same_account_corporate_action_hash_is_a_safe_duplicate(monkeypatch):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = BrokerAccount(
            user_id=1,
            broker="IBKR",
            account_name="IBKR 股息账户",
            account_number_masked="****0001",
            base_currency="USD",
        )
        db.add(account)
        db.flush()
        action = CorporateAction(
            user_id=1,
            broker_account_id=account.id,
            symbol="AAPL",
            name="Apple",
            market="美股",
            action_type="CASH_DIVIDEND",
            ex_date=date(2026, 1, 2),
            total_dividend=Decimal("10"),
            tax_withheld=Decimal("0"),
            net_dividend=Decimal("10"),
            currency="USD",
        )
        db.add(action)
        db.flush()
        flow = parsed_ibkr_flow(
            "c" * 64,
            activity_type="股息",
            gross_amount="10",
        )
        db.add(
            importer.create_ibkr_activity_flow(
                user_id=1,
                filename="existing.csv",
                flow=flow,
                broker_account_id=account.id,
                corporate_action_id=action.id,
            )
        )
        db.commit()
        stub_parsed_rows(monkeypatch, flow)

        result = importer.preview_ibkr_activity(
            db,
            1,
            b"same-account-file",
            "ibkr.csv",
            broker_account_id=account.id,
        )

        assert result["duplicate_rows"] == 1
        assert result["eligible_dividend_rows"] == 1
    finally:
        db.close()


def test_ibkr_position_lookup_keeps_unassigned_and_assigned_accounts_separate():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = BrokerAccount(
            user_id=1,
            broker="IBKR",
            account_name="IBKR 测试账户",
            account_number_masked="****0001",
            base_currency="USD",
        )
        db.add(account)
        db.flush()
        db.add_all(
            [
                Transaction(
                    user_id=1,
                    broker_account_id=None,
                    symbol="PCT",
                    market="新加坡股",
                    transaction_type="BUY",
                    quantity=Decimal("3"),
                    price=Decimal("10"),
                    fee=Decimal("0"),
                    transaction_date=date(2026, 1, 1),
                    currency="SGD",
                ),
                Transaction(
                    user_id=1,
                    broker_account_id=account.id,
                    symbol="PCT",
                    market="新加坡股",
                    transaction_type="BUY",
                    quantity=Decimal("7"),
                    price=Decimal("20"),
                    fee=Decimal("0"),
                    transaction_date=date(2026, 1, 1),
                    currency="SGD",
                ),
            ]
        )
        db.commit()

        unassigned_quantity, unassigned_cost = calculate_position_before(
            db,
            1,
            "PCT",
            "新加坡股",
            date(2026, 2, 1),
            broker_account_id=None,
        )
        assigned_quantity, assigned_cost = calculate_position_before(
            db,
            1,
            "PCT",
            "新加坡股",
            date(2026, 2, 1),
            broker_account_id=account.id,
        )

        assert (unassigned_quantity, unassigned_cost) == (
            Decimal("3"),
            Decimal("10"),
        )
        assert (assigned_quantity, assigned_cost) == (
            Decimal("7"),
            Decimal("20"),
        )
    finally:
        db.close()


# ---------------- trade_history.xlsx（规范格式）----------------


def ibkr_xlsx(*rows: dict) -> bytes:
    """构造 All Trades 表的 xlsx 字节流。"""
    import pandas as pd

    defaults = {
        "Date (HKT)": "2026-06-30 14:31",
        "Symbol": "1024",
        "Name": "KUAISHOU TECHNOLOGY",
        "Type": "STK",
        "Ccy": "HKD",
        "Side": "BUY",
        "Qty": 200,
        "Price": 41.36,
        "Net Amount": 8272.0,
        "Commission": 18.0,
        "Realized P&L": 0.0,
        "Exchange": "SEHK",
        "Trade ID": "t-0001",
    }
    frame = pd.DataFrame([{**defaults, **row} for row in rows] or [defaults])
    buffer = io.BytesIO()
    frame.to_excel(buffer, sheet_name="All Trades", index=False)
    return buffer.getvalue()


def test_ibkr_xlsx_stock_fills_become_trades(monkeypatch):
    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda symbol, market: None)
    contents = ibkr_xlsx(
        {"Trade ID": "t-1"},
        {
            "Trade ID": "t-2",
            "Symbol": "PCT",
            "Name": "PC PARTNER GROUP LTD",
            "Ccy": "SGD",
            "Side": "SELL",
            "Qty": 2000,
            "Price": 2.76,
            "Net Amount": 5520.0,
            "Commission": 4.416,
        },
    )

    rows, counts, total, errors = importer.parse_rows(contents, "trade_history.xlsx")

    assert errors == []
    assert total == 2
    trades = [row for row in rows if row.transaction_type and not row.skip_reason]
    assert len(trades) == 2

    hk = next(row for row in trades if row.price_currency == "HKD")
    assert hk.symbol == "01024"          # HKD 数字代码补足 5 位
    assert hk.market == "港股"
    assert hk.transaction_type == "BUY"
    assert hk.fee_in_price_currency == Decimal("18")   # 费用即 Commission（成交币种）

    sg = next(row for row in trades if row.price_currency == "SGD")
    assert sg.symbol == "PCT"
    assert sg.market == "新加坡股"
    assert sg.transaction_type == "SELL"
    assert sg.fee_in_price_currency == Decimal("4.416")


def test_ibkr_xlsx_options_skip_by_asset_type_even_with_plain_symbol(monkeypatch):
    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda symbol, market: None)
    # 期权行的 Symbol 是普通代码（3690），符号启发式认不出，靠 Type=OPT 判定
    contents = ibkr_xlsx({"Symbol": "3690", "Type": "OPT", "Trade ID": "t-opt"})

    rows, counts, total, errors = importer.parse_rows(contents, "trade_history.xlsx")

    assert errors == []
    assert len(rows) == 1
    assert rows[0].skip_reason == "option"


def test_ibkr_xlsx_fx_conversions_skip_as_fx(monkeypatch):
    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda symbol, market: None)
    contents = ibkr_xlsx(
        {
            "Symbol": "USD.HKD",
            "Name": "United States dollar",
            "Type": "CASH",
            "Side": "SELL",
            "Qty": 9800,
            "Price": 7.84241,
            "Net Amount": 76855.6,
            "Commission": 2.0,
            "Trade ID": "t-fx",
        }
    )

    rows, counts, total, errors = importer.parse_rows(contents, "trade_history.xlsx")

    assert errors == []
    assert len(rows) == 1
    assert rows[0].skip_reason == "fx"


def test_ibkr_xlsx_trade_id_disambiguates_identical_fills(monkeypatch):
    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda symbol, market: None)
    # 两笔除 Trade ID 外完全相同的成交必须得到不同 row_hash（跨上传去重的稳定标识）
    contents = ibkr_xlsx({"Trade ID": "t-a"}, {"Trade ID": "t-b"})

    rows, counts, total, errors = importer.parse_rows(contents, "trade_history.xlsx")

    assert errors == []
    assert len(rows) == 2
    assert rows[0].row_hash != rows[1].row_hash
    assert "trade_id=t-a" in rows[0].description


def test_ibkr_xlsx_missing_sheet_or_columns_raises():
    import pandas as pd

    buffer = io.BytesIO()
    pd.DataFrame([{"foo": 1}]).to_excel(buffer, sheet_name="Wrong Sheet", index=False)
    with pytest.raises(ValueError, match="All Trades"):
        importer.parse_rows(buffer.getvalue(), "trade_history.xlsx")

    buffer = io.BytesIO()
    pd.DataFrame([{"Date (HKT)": "2026-01-01"}]).to_excel(
        buffer, sheet_name="All Trades", index=False
    )
    with pytest.raises(ValueError, match="Missing required columns"):
        importer.parse_rows(buffer.getvalue(), "trade_history.xlsx")


def test_ibkr_xlsx_import_books_trades_and_archives_options(monkeypatch):
    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda symbol, market: None)
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    account = BrokerAccount(
        user_id=1,
        broker="IBKR",
        account_name="ibkr-xlsx",
        account_number_masked="U***67968",
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    try:
        contents = ibkr_xlsx(
            {"Trade ID": "t-1"},
            {"Symbol": "FXE", "Type": "OPT", "Ccy": "USD", "Trade ID": "t-2"},
        )
        result = importer.import_ibkr_activity(
            db, 1, contents, "trade_history.xlsx", broker_account_id=account.id
        )

        assert result["imported_transactions"] == 1
        assert result["skipped_option_rows"] == 1

        transaction = db.query(Transaction).one()
        assert transaction.symbol == "01024"
        assert transaction.market == "港股"
        assert transaction.currency == "HKD"
        assert transaction.broker_account_id == account.id

        batch = db.query(ImportBatch).one()
        assert batch.source_type == importer.SOURCE_TYPE_XLSX
        # 期权行归档保留（可审计），不生成交易
        archived = db.query(IbkrActivityFlow).filter_by(transaction_id=None).count()
        assert archived == 1
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_ibkr_xlsx_reimport_does_not_duplicate_archived_options(monkeypatch):
    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda symbol, market: None)
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    account = BrokerAccount(
        user_id=1,
        broker="IBKR",
        account_name="ibkr-xlsx-dedup",
        account_number_masked="U***67968",
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    try:
        contents = ibkr_xlsx(
            {"Trade ID": "t-1"},
            {"Symbol": "FXE", "Type": "OPT", "Ccy": "USD", "Trade ID": "t-2"},
        )
        importer.import_ibkr_activity(
            db, 1, contents, "trade_history.xlsx", broker_account_id=account.id
        )
        second = importer.import_ibkr_activity(
            db, 1, contents, "trade_history.xlsx", broker_account_id=account.id
        )

        assert second["imported_transactions"] == 0
        # 两次导入后：1 笔交易流水 + 1 条期权归档，均无重复
        assert db.query(IbkrActivityFlow).count() == 2
        assert db.query(Transaction).count() == 1
        assert (
            db.query(IbkrActivityFlow).filter_by(skip_reason="option").count() == 1
        )
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_ibkr_xlsx_option_archive_blocks_cross_account_dedup(monkeypatch):
    """期权归档判重必须校验账户归属：另一账户下的既有来源不能被静默视为重复。"""
    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda symbol, market: None)
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    account_a = BrokerAccount(
        user_id=1,
        broker="IBKR",
        account_name="ibkr-a",
        account_number_masked="U***11111",
    )
    account_b = BrokerAccount(
        user_id=1,
        broker="IBKR",
        account_name="ibkr-b",
        account_number_masked="U***22222",
    )
    db.add_all([account_a, account_b])
    db.commit()
    db.refresh(account_a)
    db.refresh(account_b)
    try:
        contents = ibkr_xlsx(
            {"Symbol": "FXE", "Type": "OPT", "Ccy": "USD", "Trade ID": "t-opt-x"}
        )
        # 先（比如误选）导入账户 A：期权归档到 A
        importer.import_ibkr_activity(
            db, 1, contents, "trade_history.xlsx", broker_account_id=account_a.id
        )
        assert (
            db.query(IbkrActivityFlow)
            .filter_by(skip_reason="option", broker_account_id=account_a.id)
            .count()
            == 1
        )

        # 同一文件再导账户 B：文件级 SHA 守卫先拦截
        with pytest.raises(ValueError, match="already imported into another broker account"):
            importer.import_ibkr_activity(
                db, 1, contents, "trade_history.xlsx", broker_account_id=account_b.id
            )

        # 不同文件（多一行无关成交，文件 SHA 不同）但含相同期权行：
        # 行级守卫必须阻塞，而非把另一账户的来源静默视为重复
        contents_v2 = ibkr_xlsx(
            {"Symbol": "FXE", "Type": "OPT", "Ccy": "USD", "Trade ID": "t-opt-x"},
            {"Trade ID": "t-new-stk"},
        )
        with pytest.raises(ValueError, match="已归属其他券商账户"):
            importer.import_ibkr_activity(
                db, 1, contents_v2, "trade_history_v2.xlsx", broker_account_id=account_b.id
            )

        # 账户 A 的归档原样保留，账户 B 未产生任何来源
        assert db.query(IbkrActivityFlow).filter_by(skip_reason="option").count() == 1
        assert (
            db.query(IbkrActivityFlow)
            .filter_by(broker_account_id=account_b.id)
            .count()
            == 0
        )
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


# ---------------------------------------------------------------------------
# 查名快速降级：单次尝试、批内并发缓存、连续失败熔断（导入预览不再近似卡死）
# ---------------------------------------------------------------------------


def test_name_lookup_empty_result_is_definitive_single_attempt(monkeypatch):
    """空结果 = 确定不在 Tushare（如 SGX 标的），单次查询不重试。"""
    import pandas as pd

    monkeypatch.setattr(importer, "_resolved_name_cache", {})
    calls = []

    def fake_query_once(api_name, **kwargs):
        calls.append(api_name)
        return pd.DataFrame()

    monkeypatch.setattr(importer, "tushare_query_once", fake_query_once)
    assert importer.lookup_tushare_security_name("ZZ9999", "港股") is None
    assert len(calls) == 1


def test_name_lookup_circuit_breaker_stops_after_consecutive_failures(monkeypatch):
    """token 缺失/网络故障时，连续失败 3 次后熔断，剩余标的不再逐个等待。"""
    monkeypatch.setattr(importer, "_resolved_name_cache", {})
    monkeypatch.setattr(importer, "NAME_LOOKUP_WORKERS", 1)
    attempts = []

    def failing_lookup(symbol, market):
        attempts.append(symbol)
        raise RuntimeError("未设置 TUSHARE_TOKEN")

    monkeypatch.setattr(importer, "lookup_tushare_security_name", failing_lookup)
    targets = [(f"TEST{i:02d}", "港股") for i in range(30)]
    results = importer.resolve_security_names(targets)

    assert len(attempts) == importer.NAME_LOOKUP_MAX_CONSECUTIVE_FAILURES
    assert len(results) == 30
    assert all(name is None for name in results.values())


def test_name_lookup_success_is_cached_across_batches(monkeypatch):
    """预览→导入两次解析同一文件，第二次不再发起外网查询。"""
    monkeypatch.setattr(importer, "_resolved_name_cache", {})
    calls = []

    def counting_lookup(symbol, market):
        calls.append(symbol)
        return "测试名称"

    monkeypatch.setattr(importer, "lookup_tushare_security_name", counting_lookup)
    targets = [("CACHE01", "港股")]
    first = importer.resolve_security_names(targets)
    second = importer.resolve_security_names(targets)

    assert first == second == {("CACHE01", "港股"): "测试名称"}
    assert len(calls) == 1


def test_name_lookup_overrides_never_hit_network(monkeypatch):
    monkeypatch.setattr(importer, "_resolved_name_cache", {})

    def exploding_lookup(symbol, market):
        raise AssertionError("名称覆盖命中不应触发外网查询")

    monkeypatch.setattr(importer, "lookup_tushare_security_name", exploding_lookup)
    key = ("01263", "港股")
    results = importer.resolve_security_names([key], name_overrides={key: "柏能集团"})
    assert results[key] == "柏能集团"
