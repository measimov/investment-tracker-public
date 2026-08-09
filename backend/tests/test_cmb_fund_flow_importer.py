import hashlib
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from fastapi import HTTPException

from app.api.import_export import validate_cmb_fund_flow_filename
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.cash_event import CashEvent
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.security_rule import SecurityRule
from app.models.import_batch import ImportBatch
from app.models.transaction import Transaction
from app.services import cmb_fund_flow_importer as importer
from app.services.cmb_fund_flow_importer import ParsedFlow, import_cmb_fund_flow
from tests.helpers import load_migration, reset_tables, run_migration

# skip_reason 列 + 历史孤儿回填。测试直接跑这个脚本本体，不复制它的 SQL。
MIGRATION_SKIP_REASON = "20260806_0011"


RESET_MODELS = (
    BrokerFundFlow,
    IbkrActivityFlow,
    Holding,
    CashEvent,
    CorporateAction,
    Transaction,
    ImportBatch,
    BrokerAccount,
    SecurityRule,
)


def parsed_flow(
    *,
    row_number: int,
    row_hash: str,
    business_name: str,
    trade_date: date,
    quantity: str,
    price: str,
    amount: str,
) -> ParsedFlow:
    return ParsedFlow(
        source_row_number=row_number,
        row_hash=row_hash,
        security_code="600000",
        security_name="浦发银行",
        currency="CNY",
        trade_date=trade_date,
        trade_price=Decimal(price),
        trade_quantity=Decimal(quantity),
        amount=Decimal(amount),
        cash_balance=Decimal("10000"),
        remaining_quantity=Decimal("100"),
        contract_number=f"contract-{row_number}",
        serial_number=f"serial-{row_number}",
        business_name=business_name,
        stamp_tax=Decimal("0"),
        commission=Decimal("1") if business_name == "证券买入" else Decimal("0"),
        handling_fee=Decimal("0"),
        management_fee=Decimal("0"),
        settlement_fee=Decimal("0"),
        transfer_fee=Decimal("0"),
        other_fee=Decimal("0"),
        shareholder_code="A123",
        notes=None,
    )


def sample_flows():
    return [
        parsed_flow(
            row_number=2,
            row_hash="a" * 64,
            business_name="证券买入",
            trade_date=date(2026, 1, 2),
            quantity="100",
            price="10",
            amount="-1001",
        ),
        parsed_flow(
            row_number=3,
            row_hash="b" * 64,
            business_name="股息入账",
            trade_date=date(2026, 5, 2),
            quantity="100",
            price="1",
            amount="100",
        ),
        parsed_flow(
            row_number=4,
            row_hash="c" * 64,
            business_name="股息红利税补缴",
            trade_date=date(2026, 5, 3),
            quantity="0",
            price="0",
            amount="-10",
        ),
    ]


def pdf_dataframe_row(**overrides):
    row = {
        "证券代码": "600000",
        "证券名称": "浦发银行",
        "币种": "人民币",
        "成交日期": "20240524",
        "成交价格": "10.00",
        "成交数量": "100.00",
        "PDF成交金额": "1000.00",
        "发生金额": "-1005.06",
        "流水号": "",
        "业务名称": "证券买入",
        "资金余额": "100.00",
        "剩余数量": "100.00",
        "佣金": "5.00",
        "印花税": "0.00",
        "其他费用": "0.06",
        "股东代码": "A123456789",
    }
    row.update(overrides)
    return row


def test_cmb_pdf_parser_extracts_rows_and_preserves_identical_trades(monkeypatch):
    header_positions = [
        ("发生日期", 21.2),
        ("市场", 75.8),
        ("币种", 146.0),
        ("银行代码", 195.3),
        ("证券账号", 262.0),
        ("证券代码", 312.7),
        ("证券名称", 352.4),
        ("业务标志", 392.0),
        ("发生数量", 473.7),
        ("成交均价", 513.3),
        ("成交金额", 559.2),
        ("佣金", 608.6),
        ("印花税", 643.4),
        ("其他费", 683.0),
        ("变动金额", 720.5),
        ("资金余额", 761.2),
        ("证券余额", 800.9),
    ]
    row_values = [
        ("20240524", 21.2),
        ("上海", 75.8),
        ("人民币", 146.0),
        ("招商银行", 195.3),
        ("A123456789", 262.0),
        ("600000", 312.7),
        ("浦发银行", 352.4),
        ("证券买入", 392.0),
        ("100.00", 474.6),
        ("10.00", 517.3),
        ("1000.00", 557.0),
        ("5.00", 605.9),
        ("0.00", 645.6),
        ("0.06", 685.2),
        ("-1005.06", 716.5),
        ("100.00", 757.2),
        ("100.00", 801.8),
    ]

    words = [{"text": text, "x0": x0, "top": 10.0} for text, x0 in header_positions]
    for top in (20.0, 30.0):
        words.extend({"text": text, "x0": x0, "top": top} for text, x0 in row_values)

    class FakePage:
        def extract_words(self, **kwargs):
            return words

    class FakePdf:
        pages = [FakePage()]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(importer, "ensure_pdf_is_readable", lambda contents: None)
    monkeypatch.setattr(importer.pdfplumber, "open", lambda source: FakePdf())

    rows, counts, total_rows, errors = importer.parse_rows(
        b"%PDF-fake",
        "cmb-statement.pdf",
    )

    assert total_rows == 2
    assert len(rows) == 2
    assert counts == {"证券买入": 2}
    assert errors == []
    assert rows[0].row_hash != rows[1].row_hash
    assert {row.transaction_type for row in rows} == {"BUY"}
    assert {row.currency for row in rows} == {"CNY"}
    assert {row.total_fee for row in rows} == {Decimal("5.06")}
    assert importer.SOURCE_TYPE == "cmb_statement_pdf"


def test_cmb_pdf_preview_rejects_misaligned_numbers_and_unattributed_tax(monkeypatch):
    dataframe = pd.DataFrame(
        [
            pdf_dataframe_row(佣金="not-a-number"),
            pdf_dataframe_row(业务名称="证券卖出", 发生金额="994.94"),
            pdf_dataframe_row(发生金额="-1000.00"),
            pdf_dataframe_row(
                证券代码="",
                证券名称="",
                业务名称="股息红利税补缴",
                成交价格="0.00",
                成交数量="0.00",
                PDF成交金额="0.00",
                发生金额="-10.00",
                佣金="0.00",
                其他费用="0.00",
            ),
        ]
    )
    monkeypatch.setattr(importer, "read_cmb_fund_flow", lambda contents, filename: dataframe)

    rows, counts, total_rows, errors, warnings = importer.parse_rows_with_warnings(
        b"%PDF-fake",
        "statement.pdf",
    )

    assert total_rows == 4
    assert len(rows) == 1
    assert rows[0].source_row_number == 5
    assert rows[0].security_code == ""
    assert not rows[0].is_dividend_tax
    assert counts == {"证券买入": 2, "证券卖出": 1, "股息红利税补缴": 1}
    assert errors == [
        "row 2: invalid PDF numeric fields: 佣金",
        "row 3: PDF trade quantity sign does not match 证券卖出",
        "row 4: PDF trade amount does not reconcile with value and fees",
    ]
    assert warnings == [
        "row 5: dividend tax missing security code; manual review required",
    ]
    result = importer.build_import_result(
        filename="statement.pdf",
        total_rows=total_rows,
        parsed_rows=rows,
        business_counts=counts,
        existing_hashes=set(),
        imported_transactions=0,
        imported_corporate_actions=0,
        imported_tax_adjustments=0,
        imported_cash_events=0,
        affected_symbols=0,
        errors=errors,
        warnings=warnings,
    )
    assert result["skipped_invalid_rows"] == 3
    assert result["skipped_non_trade_rows"] == 1
    assert result["errors"] == errors
    assert result["warnings"] == warnings


def test_cmb_pdf_accepts_valid_sell_and_enforces_amount_tolerance(monkeypatch):
    dataframe = pd.DataFrame(
        [
            pdf_dataframe_row(
                业务名称="证券卖出",
                成交数量="-100.00",
                发生金额="994.94",
            ),
            pdf_dataframe_row(发生金额="-1005.04"),
            pdf_dataframe_row(发生金额="-1005.039"),
        ]
    )
    monkeypatch.setattr(importer, "read_cmb_fund_flow", lambda contents, filename: dataframe)

    rows, _, total_rows, errors = importer.parse_rows(b"%PDF-fake", "statement.pdf")

    assert total_rows == 3
    assert [row.transaction_type for row in rows] == ["SELL", "BUY"]
    assert errors == [
        "row 4: PDF trade amount does not reconcile with value and fees",
    ]
    assert importer.parse_strict_pdf_decimal("1,234.56") == Decimal("1234.56")
    assert importer.parse_strict_pdf_decimal("1,2") is None


def test_cmb_pdf_rejects_trade_value_that_only_reconciles_with_shifted_columns(
    monkeypatch,
):
    dataframe = pd.DataFrame(
        [
            pdf_dataframe_row(
                PDF成交金额="500.00",
                发生金额="-505.06",
            )
        ]
    )
    monkeypatch.setattr(importer, "read_cmb_fund_flow", lambda contents, filename: dataframe)

    rows, _, total_rows, errors = importer.parse_rows(b"%PDF-fake", "statement.pdf")

    assert total_rows == 1
    assert rows == []
    assert errors == [
        "row 2: PDF trade value does not reconcile with quantity "
        "and displayed average price"
    ]


def test_cmb_pdf_preserves_but_flags_invalid_dividend_and_tax_signs(monkeypatch):
    dataframe = pd.DataFrame(
        [
            pdf_dataframe_row(
                业务名称="股息入账",
                成交价格="0.00",
                成交数量="0.00",
                PDF成交金额="0.00",
                发生金额="0.00",
                佣金="0.00",
                其他费用="0.00",
            ),
            pdf_dataframe_row(
                业务名称="股息红利税补缴",
                成交价格="0.00",
                成交数量="0.00",
                PDF成交金额="0.00",
                发生金额="10.00",
                佣金="0.00",
                其他费用="0.00",
            ),
        ]
    )
    monkeypatch.setattr(importer, "read_cmb_fund_flow", lambda contents, filename: dataframe)

    rows, _, total_rows, errors = importer.parse_rows(b"%PDF-fake", "statement.pdf")

    assert total_rows == 2
    assert len(rows) == 2
    assert not rows[0].is_cash_dividend
    assert not rows[1].is_dividend_tax
    assert errors == [
        "row 2: dividend amount must be positive",
        "row 3: dividend tax amount must be negative",
    ]


def test_cmb_pdf_rejects_negative_trade_values_and_fees(monkeypatch):
    dataframe = pd.DataFrame(
        [
            pdf_dataframe_row(
                佣金="-5.00",
                发生金额="-995.06",
            ),
            pdf_dataframe_row(
                PDF成交金额="-1000.00",
                发生金额="994.94",
            ),
        ]
    )
    monkeypatch.setattr(importer, "read_cmb_fund_flow", lambda contents, filename: dataframe)

    rows, _, total_rows, errors = importer.parse_rows(b"%PDF-fake", "statement.pdf")

    assert total_rows == 2
    assert rows == []
    assert errors == [
        "row 2: PDF fee fields must be non-negative",
        "row 3: PDF trade value must be non-negative",
    ]


def test_cmb_row_hash_ignores_display_name_but_includes_fee_breakdown(monkeypatch):
    current = pd.DataFrame([pdf_dataframe_row()])
    monkeypatch.setattr(importer, "read_cmb_fund_flow", lambda contents, filename: current)
    original, _, _, _ = importer.parse_rows(b"%PDF-fake", "statement.pdf")

    current = pd.DataFrame([pdf_dataframe_row(证券名称="浦发银行股份")])
    renamed, _, _, _ = importer.parse_rows(b"%PDF-fake", "statement.pdf")

    current = pd.DataFrame(
        [
            pdf_dataframe_row(
                佣金="6.00",
                发生金额="-1006.06",
            )
        ]
    )
    fee_changed, _, _, _ = importer.parse_rows(b"%PDF-fake", "statement.pdf")

    assert original[0].row_hash == renamed[0].row_hash
    assert original[0].row_hash != fee_changed[0].row_hash


def test_cmb_missing_business_name_is_preserved_for_manual_review(monkeypatch):
    dataframe = pd.DataFrame([pdf_dataframe_row(业务名称="")])
    monkeypatch.setattr(importer, "read_cmb_fund_flow", lambda contents, filename: dataframe)

    rows, counts, total_rows, errors = importer.parse_rows(b"%PDF-fake", "statement.pdf")

    assert total_rows == 1
    assert len(rows) == 1
    assert rows[0].business_name == "__MISSING__"
    assert counts == {"__MISSING__": 1}
    assert errors == ["row 2: missing business name; manual review required"]


def test_cmb_pdf_is_the_only_supported_source_at_api_and_service_boundaries():
    validate_cmb_fund_flow_filename("statement.PDF")

    for excel_filename in ("fund-flow.xls", "fund-flow.xlsx"):
        with pytest.raises(HTTPException, match="PDF"):
            validate_cmb_fund_flow_filename(excel_filename)

        with pytest.raises(ValueError, match="PDF"):
            import_cmb_fund_flow(None, 1, b"workbook", excel_filename)


def test_cmb_import_links_batch_account_and_keeps_duplicate_attempt(monkeypatch):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = BrokerAccount(
            user_id=1,
            broker="招商证券",
            account_name="招商测试账户",
            base_currency="CNY",
            account_number_masked="****A123",
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        monkeypatch.setattr(
            importer,
            "parse_rows",
            lambda contents, filename, **kwargs: (
                sample_flows(),
                {"证券买入": 1, "股息入账": 1, "股息红利税补缴": 1, "其他": 1},
                4,
                [],
            ),
        )

        contents = b"%PDF-cmb-statement"
        result = import_cmb_fund_flow(
            db,
            1,
            contents,
            "cmb.pdf",
            broker_account_id=account.id,
        )

        assert result["batch_status"] == "PARTIAL"
        assert result["broker_account_id"] == account.id
        assert result["imported_transactions"] == 1
        assert result["imported_corporate_actions"] == 1
        assert result["imported_tax_adjustments"] == 1

        transaction = db.query(Transaction).one()
        assert transaction.broker_account_id == account.id
        assert transaction.import_batch_id == result["import_batch_id"]
        action = db.query(CorporateAction).one()
        assert action.broker_account_id == account.id
        assert action.import_batch_id == result["import_batch_id"]
        assert action.net_dividend == Decimal("90.00000000")
        flows = db.query(BrokerFundFlow).all()
        assert len(flows) == 3
        assert {flow.import_batch_id for flow in flows} == {result["import_batch_id"]}
        assert {flow.broker_account_id for flow in flows} == {account.id}
        assert {flow.statement_type for flow in flows} == {"cmb_statement_pdf"}
        assert sum(flow.transaction_id is not None for flow in flows) == 1
        assert sum(flow.corporate_action_id == action.id for flow in flows) == 2

        batch = db.get(ImportBatch, result["import_batch_id"])
        assert batch.source_sha256 == hashlib.sha256(contents).hexdigest()
        assert batch.source_type == "cmb_statement_pdf"
        assert batch.parser_name == "cmb_statement"
        assert batch.row_count == 4
        assert batch.archived_count == 3
        assert batch.imported_count == 3
        assert batch.skipped_count == 1
        assert batch.error_count == 1

        duplicate = import_cmb_fund_flow(
            db,
            1,
            contents,
            "cmb.pdf",
            broker_account_id=account.id,
        )
        assert duplicate["batch_status"] == "PARTIAL"
        assert duplicate["duplicate_rows"] == 3
        assert duplicate["imported_transactions"] == 0
        assert duplicate["import_batch_id"] != result["import_batch_id"]
        duplicate_batch = db.get(ImportBatch, duplicate["import_batch_id"])
        assert duplicate_batch.archived_count == 0
        assert duplicate_batch.imported_count == 0
        assert db.query(ImportBatch).count() == 2
        assert db.query(Transaction).count() == 1
    finally:
        db.close()


def test_cmb_import_preserves_unresolved_and_unsupported_source_rows(monkeypatch):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = BrokerAccount(
            user_id=1,
            broker="招商证券",
            account_name="招商审计账户",
            base_currency="CNY",
            account_number_masked="****A123",
        )
        db.add(account)
        db.commit()
        db.refresh(account)

        unsupported = parsed_flow(
            row_number=2,
            row_hash="d" * 64,
            business_name="银行转证券",
            trade_date=date(2026, 1, 2),
            quantity="0",
            price="0",
            amount="1000",
        )
        unresolved_tax = parsed_flow(
            row_number=3,
            row_hash="e" * 64,
            business_name="股息红利税补缴",
            trade_date=date(2026, 1, 3),
            quantity="0",
            price="0",
            amount="-10",
        )
        unresolved_tax.security_code = ""
        monkeypatch.setattr(
            importer,
            "parse_rows",
            lambda contents, filename, **kwargs: (
                [unsupported, unresolved_tax],
                {"银行转证券": 1, "股息红利税补缴": 1},
                2,
                ["row 3: dividend tax missing security code; manual review required"],
            ),
        )

        preview = importer.preview_cmb_fund_flow(
            db,
            1,
            b"%PDF-audit-rows",
            "cmb.pdf",
            broker_account_id=account.id,
        )
        assert preview["errors"] == []
        assert preview["warnings"] == [
            "row 3: dividend tax missing security code; manual review required"
        ]

        result = import_cmb_fund_flow(
            db,
            1,
            b"%PDF-audit-rows",
            "cmb.pdf",
            broker_account_id=account.id,
        )

        assert result["batch_status"] == "PARTIAL"
        assert result["imported_transactions"] == 0
        assert result["imported_corporate_actions"] == 0
        assert result["imported_tax_adjustments"] == 0
        assert result["errors"] == []
        assert result["warnings"] == [
            "row 3: dividend tax missing security code; manual review required"
        ]
        rows = db.query(BrokerFundFlow).order_by(BrokerFundFlow.source_row_number).all()
        assert len(rows) == 2
        assert {row.business_name for row in rows} == {"银行转证券", "股息红利税补缴"}
        assert all(row.transaction_id is None for row in rows)
        assert all(row.corporate_action_id is None for row in rows)
        batch = db.get(ImportBatch, result["import_batch_id"])
        assert batch.archived_count == 2
        assert batch.imported_count == 0
        assert batch.skipped_count == 2
    finally:
        db.close()


def test_cmb_product_dividend_becomes_linked_interest_cash_event(monkeypatch):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = BrokerAccount(
            user_id=1,
            broker="招商证券",
            account_name="招商现金收益账户",
            base_currency="CNY",
            account_number_masked="****A123",
        )
        db.add(account)
        db.commit()

        interest_flow = parsed_flow(
            row_number=2,
            row_hash="i" * 64,
            business_name="产品红利发放",
            trade_date=date(2026, 5, 2),
            quantity="0",
            price="0",
            amount="7.50",
        )
        interest_flow.security_code = "880013"
        interest_flow.security_name = "天添利"
        interest_flow.is_cash_management_symbol = True  # 表驱动后由 parse 按规则标注
        monkeypatch.setattr(
            importer,
            "parse_rows",
            lambda contents, filename, **kwargs: (
                [interest_flow],
                {"产品红利发放": 1},
                1,
                [],
            ),
        )

        first = import_cmb_fund_flow(
            db,
            1,
            b"%PDF-cash-interest",
            "cmb.pdf",
            broker_account_id=account.id,
        )

        assert first["batch_status"] == "COMPLETED"
        assert first["eligible_cash_rows"] == 1
        assert first["imported_cash_events"] == 1
        assert first["imported_corporate_actions"] == 0
        cash_event = db.query(CashEvent).one()
        source = db.query(BrokerFundFlow).one()
        assert cash_event.event_type == "INTEREST"
        assert cash_event.amount == Decimal("7.50000000")
        assert cash_event.broker_account_id == account.id
        assert source.cash_event_id == cash_event.id
        assert source.corporate_action_id is None
        batch = db.get(ImportBatch, first["import_batch_id"])
        assert batch.archived_count == 1
        assert batch.imported_count == 1
        assert batch.skipped_count == 0

        duplicate = import_cmb_fund_flow(
            db,
            1,
            b"%PDF-cash-interest",
            "cmb.pdf",
            broker_account_id=account.id,
        )
        assert duplicate["duplicate_rows"] == 1
        assert duplicate["imported_cash_events"] == 0
        assert db.query(CashEvent).count() == 1
        assert db.query(BrokerFundFlow).count() == 1
    finally:
        db.close()


def test_cmb_same_rows_import_once_per_broker_account(monkeypatch):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        first_account = BrokerAccount(
            user_id=1,
            broker="招商证券",
            account_name="招商账户一",
            base_currency="CNY",
            account_number_masked="****A123",
        )
        second_account = BrokerAccount(
            user_id=1,
            broker="招商证券",
            account_name="招商账户二",
            base_currency="CNY",
            account_number_masked="****A123",
        )
        db.add_all([first_account, second_account])
        db.commit()
        db.refresh(first_account)
        db.refresh(second_account)
        monkeypatch.setattr(
            importer,
            "parse_rows",
            lambda contents, filename, **kwargs: (
                sample_flows(),
                {"证券买入": 1, "股息入账": 1, "股息红利税补缴": 1},
                3,
                [],
            ),
        )

        first = import_cmb_fund_flow(
            db,
            1,
            b"%PDF-same-rows-first-account",
            "first.pdf",
            broker_account_id=first_account.id,
        )
        second = import_cmb_fund_flow(
            db,
            1,
            b"%PDF-same-rows-second-account",
            "second.pdf",
            broker_account_id=second_account.id,
        )

        assert first["imported_transactions"] == 1
        assert second["imported_transactions"] == 1
        assert second["duplicate_rows"] == 0
        assert db.query(Transaction).count() == 2
        assert db.query(CorporateAction).count() == 2
        assert db.query(BrokerFundFlow).count() == 6
        assert {flow.broker_account_id for flow in db.query(BrokerFundFlow).all()} == {
            first_account.id,
            second_account.id,
        }
    finally:
        db.close()


def test_cmb_formal_import_requires_broker_account():
    with pytest.raises(
        ValueError,
        match="broker_account_id is required",
    ):
        import_cmb_fund_flow(None, 1, b"%PDF", "cmb.pdf")


def test_cmb_failed_parse_leaves_failed_batch(monkeypatch):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = BrokerAccount(
            user_id=1,
            broker="招商证券",
            account_name="招商测试账户",
            base_currency="CNY",
            account_number_masked="****A123",
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        monkeypatch.setattr(
            importer,
            "parse_rows",
            lambda contents, filename, **kwargs: (_ for _ in ()).throw(ValueError("broken PDF")),
        )

        with pytest.raises(ValueError, match="broken PDF"):
            import_cmb_fund_flow(
                db,
                1,
                b"%PDF-broken",
                "broken.pdf",
                broker_account_id=account.id,
            )

        batch = db.query(ImportBatch).one()
        assert batch.status == "FAILED"
        assert batch.imported_count == 0
        assert "broken PDF" in batch.error_message
        assert db.query(Transaction).count() == 0
        assert db.query(BrokerFundFlow).count() == 0
    finally:
        db.close()


def test_cmb_import_rejects_unowned_or_wrong_broker_account(monkeypatch):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        wrong_broker = BrokerAccount(
            user_id=1,
            broker="IBKR",
            account_name="错误券商",
            base_currency="USD",
        )
        other_user = BrokerAccount(
            user_id=2,
            broker="招商证券",
            account_name="其他用户账户",
            base_currency="CNY",
            account_number_masked="****A123",
        )
        db.add_all([wrong_broker, other_user])
        db.commit()

        with pytest.raises(ValueError, match="belongs to IBKR"):
            import_cmb_fund_flow(
                db,
                1,
                b"unused",
                "cmb.pdf",
                broker_account_id=wrong_broker.id,
            )
        with pytest.raises(ValueError, match="not found"):
            import_cmb_fund_flow(
                db,
                1,
                b"unused",
                "cmb.pdf",
                broker_account_id=other_user.id,
            )

        assert db.query(ImportBatch).count() == 0
    finally:
        db.close()


def test_cmb_unassigned_tax_matches_only_unassigned_dividend():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = BrokerAccount(
            user_id=1,
            broker="招商证券",
            account_name="招商测试账户",
            base_currency="CNY",
            account_number_masked="****A123",
        )
        db.add(account)
        db.flush()
        unassigned = CorporateAction(
            user_id=1,
            broker_account_id=None,
            symbol="600000",
            market="A股",
            action_type="CASH_DIVIDEND",
            ex_date=date(2026, 5, 2),
            total_dividend=Decimal("100"),
            currency="CNY",
        )
        assigned = CorporateAction(
            user_id=1,
            broker_account_id=account.id,
            symbol="600000",
            market="A股",
            action_type="CASH_DIVIDEND",
            ex_date=date(2026, 5, 2),
            total_dividend=Decimal("200"),
            currency="CNY",
        )
        db.add_all([unassigned, assigned])
        db.commit()

        tax_flow = sample_flows()[2]
        assert (
            importer.find_dividend_for_tax(db, 1, tax_flow, "A股", broker_account_id=None).id
            == unassigned.id
        )
        assert (
            importer.find_dividend_for_tax(db, 1, tax_flow, "A股", broker_account_id=account.id).id
            == assigned.id
        )
    finally:
        db.close()


def test_cmb_tax_is_left_unlinked_when_account_has_multiple_candidate_dividends(
    monkeypatch,
):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = BrokerAccount(
            user_id=1,
            broker="招商证券",
            account_name="招商多股息账户",
            base_currency="CNY",
            account_number_masked="****A123",
        )
        db.add(account)
        db.flush()
        db.add_all(
            [
                CorporateAction(
                    user_id=1,
                    broker_account_id=account.id,
                    symbol="600000",
                    market="A股",
                    action_type="CASH_DIVIDEND",
                    ex_date=date(2026, 4, 1),
                    total_dividend=Decimal("50"),
                    tax_withheld=Decimal("0"),
                    net_dividend=Decimal("50"),
                    currency="CNY",
                ),
                CorporateAction(
                    user_id=1,
                    broker_account_id=account.id,
                    symbol="600000",
                    market="A股",
                    action_type="CASH_DIVIDEND",
                    ex_date=date(2026, 5, 2),
                    total_dividend=Decimal("100"),
                    tax_withheld=Decimal("0"),
                    net_dividend=Decimal("100"),
                    currency="CNY",
                ),
            ]
        )
        db.commit()

        tax_flow = sample_flows()[2]
        monkeypatch.setattr(
            importer,
            "parse_rows",
            lambda contents, filename, **kwargs: (
                [tax_flow],
                {"股息红利税补缴": 1},
                1,
                [],
            ),
        )

        result = import_cmb_fund_flow(
            db,
            1,
            b"%PDF-ambiguous-tax",
            "cmb.pdf",
            broker_account_id=account.id,
        )

        assert result["batch_status"] == "PARTIAL"
        assert result["imported_tax_adjustments"] == 0
        assert result["errors"] == []
        assert "expected exactly one account-scoped dividend" in result["warnings"][0]
        source = db.query(BrokerFundFlow).one()
        assert source.corporate_action_id is None
        assert {
            action.tax_withheld for action in db.query(CorporateAction).order_by(CorporateAction.id)
        } == {Decimal("0E-8")}
    finally:
        db.close()


def test_cmb_preview_reports_masks_and_formal_import_requires_full_coverage(
    monkeypatch,
):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = BrokerAccount(
            user_id=1,
            broker="招商证券",
            account_name="招商掩码账户",
            base_currency="CNY",
            account_number_masked="****A123 / ****B456 / ****C789",
        )
        db.add(account)
        db.commit()

        flows = []
        for row_number, shareholder_code in enumerate(
            ("035167A123", "98Z054B456", "A71712C789"),
            start=2,
        ):
            flow = parsed_flow(
                row_number=row_number,
                row_hash=str(row_number) * 64,
                business_name="银行转存",
                trade_date=date(2026, 1, row_number),
                quantity="0",
                price="0",
                amount="100",
            )
            flow.shareholder_code = shareholder_code
            flows.append(flow)
        monkeypatch.setattr(
            importer,
            "parse_rows",
            lambda contents, filename, **kwargs: (
                flows,
                {"银行转存": 3},
                3,
                [],
            ),
        )

        preview = importer.preview_cmb_fund_flow(
            db,
            1,
            b"%PDF-account-masks",
            "cmb.pdf",
            broker_account_id=account.id,
        )
        assert preview["source_account_masks"] == [
            "****A123",
            "****B456",
            "****C789",
        ]
        assert db.query(BrokerFundFlow).count() == 0

        account.account_number_masked = "****A123 / ****B456"
        db.commit()
        with pytest.raises(ValueError, match="账户掩码未覆盖"):
            importer.preview_cmb_fund_flow(
                db,
                1,
                b"%PDF-account-masks-wrong-account",
                "cmb.pdf",
                broker_account_id=account.id,
            )

        with pytest.raises(ValueError, match="账户掩码未覆盖"):
            import_cmb_fund_flow(
                db,
                1,
                b"%PDF-account-masks",
                "cmb.pdf",
                broker_account_id=account.id,
            )

        batch = db.query(ImportBatch).one()
        assert batch.status == "FAILED"
        assert "****C789" in batch.error_message
        assert db.query(BrokerFundFlow).count() == 0
    finally:
        db.close()


def test_cmb_precommit_account_oversell_rolls_back_entire_batch(monkeypatch):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = BrokerAccount(
            user_id=1,
            broker="招商证券",
            account_name="招商缺期初账户",
            base_currency="CNY",
            account_number_masked="****A123",
        )
        db.add(account)
        db.commit()
        sell_flow = parsed_flow(
            row_number=2,
            row_hash="7" * 64,
            business_name="证券卖出",
            trade_date=date(2026, 1, 2),
            quantity="-100",
            price="10",
            amount="1000",
        )
        monkeypatch.setattr(
            importer,
            "parse_rows",
            lambda contents, filename, **kwargs: (
                [sell_flow],
                {"证券卖出": 1},
                1,
                [],
            ),
        )

        with pytest.raises(ValueError, match="持仓预检失败"):
            import_cmb_fund_flow(
                db,
                1,
                b"%PDF-missing-opening-position",
                "cmb.pdf",
                broker_account_id=account.id,
            )

        batch = db.query(ImportBatch).one()
        assert batch.status == "FAILED"
        assert batch.row_count == 1
        assert batch.archived_count == 0
        assert batch.imported_count == 0
        assert batch.skipped_count == 1
        assert db.query(Transaction).count() == 0
        assert db.query(BrokerFundFlow).count() == 0
        assert db.query(Holding).count() == 0
    finally:
        db.close()


def test_cmb_preview_rejects_exact_file_already_imported_to_other_account(
    monkeypatch,
):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        first_account = BrokerAccount(
            user_id=1,
            broker="招商证券",
            account_name="招商账户一",
            base_currency="CNY",
            account_number_masked="****A123",
        )
        second_account = BrokerAccount(
            user_id=1,
            broker="招商证券",
            account_name="招商账户二",
            base_currency="CNY",
            account_number_masked="****A123",
        )
        db.add_all([first_account, second_account])
        db.commit()
        monkeypatch.setattr(
            importer,
            "parse_rows",
            lambda contents, filename, **kwargs: (
                sample_flows(),
                {"证券买入": 1, "股息入账": 1, "股息红利税补缴": 1},
                3,
                [],
            ),
        )
        contents = b"%PDF-cross-account"
        import_cmb_fund_flow(
            db,
            1,
            contents,
            "cmb.pdf",
            broker_account_id=first_account.id,
        )

        with pytest.raises(ValueError, match="already imported into another"):
            importer.preview_cmb_fund_flow(
                db,
                1,
                contents,
                "cmb.pdf",
                broker_account_id=second_account.id,
            )
    finally:
        db.close()


# ---------------- 沪港通（普通对账单）与流水明细节划界 ----------------


def hk_connect_row(**overrides):
    """真实 2025 对账单里的沪港通买入行（03115 安硕恒生指数）。"""
    row = pdf_dataframe_row(
        市场="沪港通",
        证券代码="03115",
        证券名称="安硕恒生指数",
        股东代码="A717124108",
        成交价格="73.04",
        成交数量="600.00",
        PDF成交金额="41294.04",
        佣金="123.88",
        印花税="0.00",
        其他费用="5.40",
        发生金额="-41423.32",
    )
    row.update(overrides)
    return row


def test_cmb_hk_connect_trade_derives_settlement_rate_and_books_hkd(monkeypatch):
    dataframe = pd.DataFrame([hk_connect_row()])
    monkeypatch.setattr(importer, "read_cmb_fund_flow", lambda contents, filename: dataframe)

    rows, counts, total_rows, errors, warnings = importer.parse_rows_with_warnings(
        b"%PDF-fake", "statement.pdf"
    )

    assert errors == []
    assert len(rows) == 1
    flow = rows[0]
    expected_rate = (Decimal("41294.04") / (Decimal("600") * Decimal("73.04"))).quantize(
        Decimal("0.00000001")
    )
    assert flow.is_hk_connect
    assert flow.settlement_rate == expected_rate
    assert flow.effective_currency == "HKD"
    # 费用列为 CNY，按推导汇率换回 HKD
    expected_fee = (Decimal("129.28") / expected_rate).quantize(Decimal("0.00000001"))
    assert flow.effective_fee == expected_fee
    # 非港股通行为不受影响
    assert flow.currency == "CNY"


def test_cmb_hk_connect_settlement_rate_outside_band_is_error(monkeypatch):
    # 成交金额 CNY 与 数量×HKD价格 之比落在 0.5~1.5 之外 → 阻塞错误
    dataframe = pd.DataFrame(
        [hk_connect_row(PDF成交金额="1000.00", 发生金额="-1129.28")]
    )
    monkeypatch.setattr(importer, "read_cmb_fund_flow", lambda contents, filename: dataframe)

    rows, counts, total_rows, errors, warnings = importer.parse_rows_with_warnings(
        b"%PDF-fake", "statement.pdf"
    )

    assert len(rows) == 0
    assert len(errors) == 1
    assert "settlement rate" in errors[0]


def test_cmb_hk_connect_amount_check_still_enforced(monkeypatch):
    # 汇率合理但 变动金额 != -(成交金额+费用) → 仍然阻塞（全 CNY 校验保留）
    dataframe = pd.DataFrame([hk_connect_row(发生金额="-40000.00")])
    monkeypatch.setattr(importer, "read_cmb_fund_flow", lambda contents, filename: dataframe)

    rows, counts, total_rows, errors, warnings = importer.parse_rows_with_warnings(
        b"%PDF-fake", "statement.pdf"
    )

    assert len(rows) == 0
    assert len(errors) == 1
    assert "does not reconcile with value and fees" in errors[0]


def test_cmb_hk_connect_fee_row_is_preserved_for_archive(monkeypatch):
    # 港股通组合费收取：无证券代码、数量价格为 0，不映射业务 → 保留归档，不报错
    dataframe = pd.DataFrame(
        [
            hk_connect_row(
                证券代码="",
                证券名称="",
                业务名称="港股通组合费收取",
                成交价格="0.00",
                成交数量="0.00",
                PDF成交金额="0.06",
                佣金="0.00",
                其他费用="0.00",
                发生金额="-0.06",
            )
        ]
    )
    monkeypatch.setattr(importer, "read_cmb_fund_flow", lambda contents, filename: dataframe)

    rows, counts, total_rows, errors, warnings = importer.parse_rows_with_warnings(
        b"%PDF-fake", "statement.pdf"
    )

    assert errors == []
    assert len(rows) == 1
    assert rows[0].transaction_type is None
    assert rows[0].settlement_rate is None


def _flow_section_words():
    """两页文档：p1 流水明细一行有效行 + 未回业务节一行幽灵行；p2 是未回业务续页。"""
    header_positions = [
        ("发生日期", 21.2), ("市场", 75.8), ("币种", 146.0), ("银行代码", 195.3),
        ("证券账号", 262.0), ("证券代码", 312.7), ("证券名称", 352.4), ("业务标志", 392.0),
        ("发生数量", 473.7), ("成交均价", 513.3), ("成交金额", 559.2), ("佣金", 608.6),
        ("印花税", 643.4), ("其他费", 683.0), ("变动金额", 720.5), ("资金余额", 761.2),
        ("证券余额", 800.9),
    ]
    valid_row = [
        ("20240524", 21.2), ("上海", 75.8), ("人民币", 146.0), ("招商银行", 195.3),
        ("A123456789", 262.0), ("600000", 312.7), ("浦发银行", 352.4), ("证券买入", 392.0),
        ("100.00", 474.6), ("10.00", 517.3), ("1000.00", 557.0), ("5.00", 605.9),
        ("0.00", 645.6), ("0.06", 685.2), ("-1005.06", 716.5), ("100.00", 757.2),
        ("100.00", 801.8),
    ]
    # 幽灵行：日期 + 落进"市场"列的股东账号（未回业务/配号表的版式错位）
    ghost_row = [("20251219", 21.2), ("0351671471", 75.8)]

    page1 = [{"text": "流水明细", "x0": 21.2, "top": 5.0}]
    page1 += [{"text": t, "x0": x, "top": 10.0} for t, x in header_positions]
    page1 += [{"text": t, "x0": x, "top": 20.0} for t, x in valid_row]
    page1 += [{"text": "未回业务流水明细", "x0": 21.2, "top": 28.0}]
    page1 += [{"text": t, "x0": x, "top": 35.0} for t, x in ghost_row]
    # p2：未回业务续页，无任何节标题
    page2 = [{"text": t, "x0": x, "top": 12.0} for t, x in ghost_row]
    return page1, page2


def test_cmb_flow_section_state_machine_excludes_ghost_rows(monkeypatch):
    page1, page2 = _flow_section_words()

    class FakePage:
        def __init__(self, words):
            self._words = words

        def extract_words(self, **kwargs):
            return self._words

    class FakePdf:
        pages = [FakePage(page1), FakePage(page2)]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(importer, "ensure_pdf_is_readable", lambda contents: None)
    monkeypatch.setattr(importer.pdfplumber, "open", lambda source: FakePdf())

    rows, counts, total_rows, errors = importer.parse_rows(b"%PDF-fake", "statement.pdf")

    # 只剩流水明细节内那 1 行；p1 节后幽灵行与 p2 续页幽灵行都被排除
    assert total_rows == 1
    assert len(rows) == 1
    assert errors == []
    assert rows[0].security_code == "600000"


def hk_connect_parsed_flow(**overrides):
    kwargs = dict(
        source_row_number=9,
        row_hash="hk-flow-1",
        security_code="03115",
        security_name="安硕恒生指数",
        currency="CNY",
        trade_date=date(2025, 1, 3),
        trade_price=Decimal("73.04"),
        trade_quantity=Decimal("600"),
        amount=Decimal("-41423.32"),
        cash_balance=Decimal("-2376.24"),
        remaining_quantity=Decimal("600"),
        contract_number=None,
        serial_number=None,
        business_name="证券买入",
        stamp_tax=Decimal("0"),
        commission=Decimal("123.88"),
        handling_fee=Decimal("0"),
        management_fee=Decimal("0"),
        settlement_fee=Decimal("0"),
        transfer_fee=Decimal("0"),
        other_fee=Decimal("5.40"),
        shareholder_code="A123",
        notes=None,
        market_text="沪港通",
        settlement_rate=(
            Decimal("41294.04") / (Decimal("600") * Decimal("73.04"))
        ).quantize(Decimal("0.00000001")),
    )
    kwargs.update(overrides)
    return ParsedFlow(**kwargs)


def test_cmb_hk_connect_import_books_hkd_transaction_with_converted_fee(monkeypatch):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    account = BrokerAccount(
        user_id=1,
        broker="招商证券",
        account_name="招商测试账户",
        base_currency="CNY",
        account_number_masked="****A123",
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    try:
        flow = hk_connect_parsed_flow()
        monkeypatch.setattr(
            importer,
            "parse_rows",
            lambda contents, filename, **kwargs: ([flow], {"证券买入": 1}, 1, []),
        )

        result = import_cmb_fund_flow(
            db, 1, b"%PDF-cmb", "cmb.pdf", broker_account_id=account.id
        )

        assert result["imported_transactions"] == 1
        transaction = db.query(Transaction).one()
        assert transaction.market == "港股"
        assert transaction.currency == "HKD"
        assert transaction.price == Decimal("73.04000000")
        assert transaction.quantity == Decimal("600.00000000")
        expected_fee = (Decimal("129.28") / flow.settlement_rate).quantize(
            Decimal("0.00000001")
        )
        assert transaction.fee == expected_fee
        assert "推导结算汇率" in transaction.notes

        stored = db.query(BrokerFundFlow).one()
        assert stored.settlement_rate == flow.settlement_rate
        assert stored.transaction_id == transaction.id
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_cmb_open_fund_subscription_books_as_buy(monkeypatch):
    """开放基金申购：份额×净值与成交金额有舍入差（容差内），映射为 BUY。

    真实案例 161225 白银LOF：不建模会让后续"证券卖出"撞持仓预检。
    """
    dataframe = pd.DataFrame(
        [
            pdf_dataframe_row(
                证券代码="161225",
                证券名称="白银LOF",
                业务名称="开放基金申购",
                成交价格="2.51",
                成交数量="392.00",
                PDF成交金额="984.51",
                发生金额="-999.29",
                佣金="14.78",
                印花税="0.00",
                其他费用="0.00",
            )
        ]
    )
    monkeypatch.setattr(importer, "read_cmb_fund_flow", lambda contents, filename: dataframe)

    rows, counts, total_rows, errors, warnings = importer.parse_rows_with_warnings(
        b"%PDF-fake", "statement.pdf"
    )

    assert errors == []
    assert len(rows) == 1
    assert rows[0].transaction_type == "BUY"
    assert rows[0].total_fee == Decimal("14.78")


def test_cmb_ipo_allotment_books_as_buy_without_value_reconciliation(monkeypatch):
    """新股入账：零现金行（缴款另记），发行价在价格列，视同买入且跳过金额对账。"""
    dataframe = pd.DataFrame(
        [
            pdf_dataframe_row(
                证券代码="123266",
                证券名称="测试转债",
                业务名称="新股入账",
                成交价格="100.00",
                成交数量="10.00",
                PDF成交金额="0.00",
                发生金额="0.00",
                佣金="0.00",
                印花税="0.00",
                其他费用="0.00",
            )
        ]
    )
    monkeypatch.setattr(importer, "read_cmb_fund_flow", lambda contents, filename: dataframe)

    rows, counts, total_rows, errors, warnings = importer.parse_rows_with_warnings(
        b"%PDF-fake", "statement.pdf"
    )

    assert errors == []
    assert len(rows) == 1
    flow = rows[0]
    assert flow.transaction_type == "BUY"
    assert flow.trade_price == Decimal("100.00")
    assert flow.total_fee == Decimal("0")


def test_cmb_allotment_rejects_negative_quantity_and_nonzero_cash(monkeypatch):
    """新股入账语义校验：负数量与非零现金行必须阻塞，不得静默成仓。"""
    negative_qty = pdf_dataframe_row(
        证券代码="123266",
        证券名称="测试转债",
        业务名称="新股入账",
        成交价格="100.00",
        成交数量="-10.00",
        PDF成交金额="0.00",
        发生金额="0.00",
        佣金="0.00",
        其他费用="0.00",
    )
    nonzero_cash = pdf_dataframe_row(
        证券代码="123266",
        证券名称="测试转债",
        业务名称="新股入账",
        成交价格="100.00",
        成交数量="10.00",
        PDF成交金额="1000.00",
        发生金额="-1000.00",
        佣金="0.00",
        其他费用="0.00",
    )
    missing_code = pdf_dataframe_row(
        证券代码="",
        证券名称="",
        业务名称="新股入账",
        成交价格="100.00",
        成交数量="10.00",
        PDF成交金额="0.00",
        发生金额="0.00",
        佣金="0.00",
        其他费用="0.00",
    )
    dataframe = pd.DataFrame([negative_qty, nonzero_cash, missing_code])
    monkeypatch.setattr(importer, "read_cmb_fund_flow", lambda contents, filename: dataframe)

    rows, counts, total_rows, errors, warnings = importer.parse_rows_with_warnings(
        b"%PDF-fake", "statement.pdf"
    )

    assert len(rows) == 0
    assert len(errors) == 3
    assert "quantity and issue price must be positive" in errors[0]
    assert "zero-cash row" in errors[1]
    assert "missing security code" in errors[2]


def test_cmb_parser_version_tracks_booking_semantics():
    """入账口径变化必须升版（ImportBatch 按 parser/version 审计）。

    若本断言失败，说明你改了 parser 行为——请升级 PARSER_VERSION 并更新此处。
    """
    assert importer.PARSER_VERSION == "11"


def test_cmb_excluded_security_rows_archive_without_booking(monkeypatch):
    """排除清单标的：交易与股息只归档不入账，正常标的不受影响；
    预览与正式导入的 skipped_excluded_rows 口径一致。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = BrokerAccount(
            user_id=1,
            broker="招商证券",
            account_name="招商测试账户",
            base_currency="CNY",
            account_number_masked="****A123",
        )
        db.add(account)
        db.add(SecurityRule(rule_type="EXCLUDE", user_id=1, symbol="511880", market="A股", note="货币基金"))
        db.commit()
        db.refresh(account)

        normal_buy = parsed_flow(
            row_number=2, row_hash="e" * 64, business_name="证券买入",
            trade_date=date(2026, 3, 2), quantity="100", price="10", amount="-1001",
        )
        excluded_buy = parsed_flow(
            row_number=3, row_hash="f" * 64, business_name="证券买入",
            trade_date=date(2026, 3, 3), quantity="1000", price="1", amount="-1001",
        )
        excluded_buy.security_code = "511880"
        excluded_buy.security_name = "银华日利"
        excluded_dividend = parsed_flow(
            row_number=4, row_hash="d" * 64, business_name="股息入账",
            trade_date=date(2026, 3, 10), quantity="0", price="0", amount="8.88",
        )
        excluded_dividend.security_code = "511880"
        flows = [normal_buy, excluded_buy, excluded_dividend]
        monkeypatch.setattr(
            importer,
            "parse_rows",
            lambda contents, filename, **kwargs: (
                flows,
                {"证券买入": 2, "股息入账": 1},
                3,
                [],
            ),
        )

        result = import_cmb_fund_flow(
            db, 1, b"%PDF-cmb", "cmb.pdf", broker_account_id=account.id,
        )

        assert result["imported_transactions"] == 1
        assert result["imported_corporate_actions"] == 0
        assert result["skipped_excluded_rows"] == 2
        # 排除是预期跳过：不得把批次拖成 PARTIAL（前端把 PARTIAL 当导入异常）
        assert result["batch_status"] == "COMPLETED"
        batch = db.get(ImportBatch, result["import_batch_id"])
        assert batch.error_count == 0
        assert batch.error_message is None

        transaction = db.query(Transaction).one()
        assert transaction.symbol == "600000"
        assert db.query(CorporateAction).count() == 0

        flows_db = db.query(BrokerFundFlow).all()
        assert len(flows_db) == 3  # 排除行仍归档，审计链完整
        for flow in flows_db:
            if flow.security_code == "511880":
                assert flow.transaction_id is None
                assert flow.corporate_action_id is None
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_cmb_excluded_rows_do_not_mask_genuinely_invalid_rows(monkeypatch):
    """排除行 + 真正无效行：批次仍为 PARTIAL——预期跳过的抵扣
    只作用于排除行本身，不掩盖真实的数据问题。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = BrokerAccount(
            user_id=1,
            broker="招商证券",
            account_name="招商测试账户",
            base_currency="CNY",
            account_number_masked="****A123",
        )
        db.add(account)
        db.add(SecurityRule(rule_type="EXCLUDE", user_id=1, symbol="511880", market="A股"))
        db.commit()
        db.refresh(account)

        excluded_buy = parsed_flow(
            row_number=2, row_hash="a1" * 32, business_name="证券买入",
            trade_date=date(2026, 3, 3), quantity="1000", price="1", amount="-1001",
        )
        excluded_buy.security_code = "511880"
        invalid_trade = parsed_flow(
            row_number=3, row_hash="b1" * 32, business_name="证券买入",
            trade_date=date(2026, 3, 4), quantity="100", price="0", amount="-100",
        )
        monkeypatch.setattr(
            importer,
            "parse_rows",
            lambda contents, filename, **kwargs: (
                [excluded_buy, invalid_trade],
                {"证券买入": 2},
                2,
                [],
            ),
        )

        result = import_cmb_fund_flow(
            db, 1, b"%PDF-cmb", "cmb.pdf", broker_account_id=account.id,
        )

        assert result["skipped_excluded_rows"] == 1
        assert result["batch_status"] == "PARTIAL"
        batch = db.get(ImportBatch, result["import_batch_id"])
        assert batch.error_count >= 1
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def _cmb_account(db, name="招商账户"):
    account = BrokerAccount(
        user_id=1, broker="招商证券", account_name=name,
        base_currency="CNY", account_number_masked="****A123",
    )
    db.add(account)
    db.commit()
    return account


def test_cmb_precheck_accepts_sell_covered_by_ratio_only_bonus(monkeypatch):
    """ratio-only 送股必须计入预检，否则整批被误拒。

    分红同步接受送转建议时刻意写 broker_account_id=None（送转是比例行动，
    作用于所有账户桶），而预检原来既按账户过滤、又裸读 shares_received——
    这条最常见的路径在预检里贡献 0 股，后续卖出必然误报「缺少期初持仓」。
    """
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = _cmb_account(db)
        db.add(Transaction(
            user_id=1, symbol="600000", name="浦发银行", market="A股",
            transaction_type="BUY", quantity=Decimal("100"), price=Decimal("10"),
            fee=Decimal("0"), transaction_date=date(2026, 1, 1), currency="CNY",
            broker_account_id=account.id,
        ))
        db.add(CorporateAction(
            user_id=1, symbol="600000", name="浦发银行", market="A股",
            action_type="STOCK_DIVIDEND", distribution_ratio="10:3",
            ex_date=date(2026, 1, 5), currency="CNY", broker_account_id=None,
        ))
        db.commit()

        sell_flow = parsed_flow(
            row_number=2, row_hash="b" * 64, business_name="证券卖出",
            trade_date=date(2026, 1, 6), quantity="-130", price="10", amount="1300",
        )
        monkeypatch.setattr(
            importer, "parse_rows",
            lambda contents, filename, **kwargs: ([sell_flow], {"证券卖出": 1}, 1, []),
        )

        # 修复前：预检只看到 100 股 → ValueError("持仓预检失败")，整批拒绝
        result = import_cmb_fund_flow(
            db, 1, b"%PDF-ratio-bonus", "cmb.pdf", broker_account_id=account.id,
        )
        batch = db.get(ImportBatch, result["import_batch_id"])
        assert batch.status == "COMPLETED", batch.error_message
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_cmb_precheck_counts_transfer_in_instead_of_crashing(monkeypatch):
    """转入腿必须计入，且不得再触发 AttributeError。

    预检原来的分支链是 if BUY / elif SELL / elif event.action_type ——
    TRANSFER_* 会落进第三支，而 Transaction 没有 action_type 列，
    只要该账户有过一次转仓，整个导入直接崩溃。
    """
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        source = _cmb_account(db, "转出账户")
        target = _cmb_account(db, "转入账户")
        db.add(Transaction(
            user_id=1, symbol="600000", name="浦发银行", market="A股",
            transaction_type="BUY", quantity=Decimal("100"), price=Decimal("10"),
            fee=Decimal("0"), transaction_date=date(2026, 1, 1), currency="CNY",
            broker_account_id=source.id,
        ))
        out_leg = Transaction(
            user_id=1, symbol="600000", name="浦发银行", market="A股",
            transaction_type="TRANSFER_OUT", quantity=Decimal("100"), price=Decimal("10"),
            fee=Decimal("0"), transaction_date=date(2026, 1, 2), currency="CNY",
            broker_account_id=source.id,
        )
        in_leg = Transaction(
            user_id=1, symbol="600000", name="浦发银行", market="A股",
            transaction_type="TRANSFER_IN", quantity=Decimal("100"), price=Decimal("10"),
            fee=Decimal("0"), transaction_date=date(2026, 1, 2), currency="CNY",
            broker_account_id=target.id,
        )
        db.add_all([out_leg, in_leg])
        db.flush()
        out_leg.linked_transaction_id = in_leg.id
        in_leg.linked_transaction_id = out_leg.id
        db.commit()

        sell_flow = parsed_flow(
            row_number=2, row_hash="c" * 64, business_name="证券卖出",
            trade_date=date(2026, 1, 3), quantity="-100", price="10", amount="1000",
        )
        monkeypatch.setattr(
            importer, "parse_rows",
            lambda contents, filename, **kwargs: ([sell_flow], {"证券卖出": 1}, 1, []),
        )

        # 修复前：AttributeError('Transaction' object has no attribute 'action_type')
        result = import_cmb_fund_flow(
            db, 1, b"%PDF-transfer-in", "cmb.pdf", broker_account_id=target.id,
        )
        batch = db.get(ImportBatch, result["import_batch_id"])
        assert batch.status == "COMPLETED", batch.error_message
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


# ---------------------------------------------------------------------------
# issue #132 子项 B：未归属红利税行的重导恢复（对齐 IBKR 既有机制）
#
# 此前招商把找不到唯一股息的税行无链接归档、并把 row_hash 记入判重：
# 之后即使补齐股息、重导同一对账单也会被跳过，tax_withheld 永远缺失。
# ---------------------------------------------------------------------------


def _cmb_account(db, name="招商税行恢复账户"):
    account = BrokerAccount(
        user_id=1, broker="招商证券", account_name=name,
        base_currency="CNY", account_number_masked="****A123",
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def _tax_only_flows():
    return [
        parsed_flow(
            row_number=1, row_hash="d1" * 32, business_name="股息红利税补缴",
            trade_date=date(2026, 5, 3), quantity="0", price="0", amount="-10",
        )
    ]


def _dividend_and_tax_flows():
    return [
        parsed_flow(
            row_number=1, row_hash="e2" * 32, business_name="股息入账",
            trade_date=date(2026, 5, 2), quantity="100", price="1", amount="100",
        ),
        parsed_flow(
            row_number=2, row_hash="d1" * 32, business_name="股息红利税补缴",
            trade_date=date(2026, 5, 3), quantity="0", price="0", amount="-10",
        ),
    ]


def _patch_parse(monkeypatch, flows, counts):
    monkeypatch.setattr(
        importer, "parse_rows",
        lambda contents, filename, **kwargs: (flows, counts, len(flows), []),
    )


def test_cmb_unattributed_tax_is_recovered_on_reimport(monkeypatch):
    """税行先到、股息后到：重导必须就地转正，而不是被 hash 判重跳过。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = _cmb_account(db)

        _patch_parse(monkeypatch, _tax_only_flows(), {"股息红利税补缴": 1})
        first = import_cmb_fund_flow(db, 1, b"%PDF-1", "cmb.pdf", broker_account_id=account.id)

        assert first["imported_tax_adjustments"] == 0
        orphan = db.query(BrokerFundFlow).one()
        orphan_id = orphan.id
        assert orphan.skip_reason == "unattributed_tax"
        assert orphan.corporate_action_id is None
        assert db.query(CorporateAction).count() == 0

        _patch_parse(
            monkeypatch, _dividend_and_tax_flows(), {"股息入账": 1, "股息红利税补缴": 1}
        )
        second = import_cmb_fund_flow(db, 1, b"%PDF-2", "cmb.pdf", broker_account_id=account.id)

        assert second["imported_tax_adjustments"] == 1
        action = db.query(CorporateAction).one()
        assert action.tax_withheld == Decimal("10.00000000")
        assert action.net_dividend == Decimal("90.00000000")

        recovered = db.query(BrokerFundFlow).filter_by(row_hash="d1" * 32).one()
        assert recovered.id == orphan_id, "必须就地转正，不得插新行"
        assert recovered.skip_reason is None
        assert recovered.corporate_action_id == action.id
        assert "attributed during account-scoped re-import" in (recovered.notes or "")
    finally:
        db.close()


def test_cmb_recovered_tax_is_not_applied_twice(monkeypatch):
    """转正后再导同一文件：税额不得二次叠加。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = _cmb_account(db, "招商幂等账户")

        _patch_parse(monkeypatch, _tax_only_flows(), {"股息红利税补缴": 1})
        import_cmb_fund_flow(db, 1, b"%PDF-1", "cmb.pdf", broker_account_id=account.id)
        _patch_parse(
            monkeypatch, _dividend_and_tax_flows(), {"股息入账": 1, "股息红利税补缴": 1}
        )
        import_cmb_fund_flow(db, 1, b"%PDF-2", "cmb.pdf", broker_account_id=account.id)

        action = db.query(CorporateAction).one()
        assert action.tax_withheld == Decimal("10.00000000")

        third = import_cmb_fund_flow(db, 1, b"%PDF-3", "cmb.pdf", broker_account_id=account.id)

        db.refresh(action)
        assert action.tax_withheld == Decimal("10.00000000"), "重导不得叠加税额"
        assert third["imported_tax_adjustments"] == 0
        assert db.query(BrokerFundFlow).filter_by(row_hash="d1" * 32).count() == 1
    finally:
        db.close()


def test_cmb_unattributed_tax_is_not_duplicated_when_still_unmatched(monkeypatch):
    """仍找不到股息时重导：保持未归属、不重复建行。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = _cmb_account(db, "招商仍未归属账户")

        _patch_parse(monkeypatch, _tax_only_flows(), {"股息红利税补缴": 1})
        import_cmb_fund_flow(db, 1, b"%PDF-1", "cmb.pdf", broker_account_id=account.id)
        import_cmb_fund_flow(db, 1, b"%PDF-2", "cmb.pdf", broker_account_id=account.id)

        rows = db.query(BrokerFundFlow).filter_by(row_hash="d1" * 32).all()
        assert len(rows) == 1, "未归属税行不得重复建行"
        assert rows[0].skip_reason == "unattributed_tax"
    finally:
        db.close()


def test_cmb_pre_migration_orphan_is_recoverable_after_backfill(monkeypatch):
    """迁移前就存在的孤儿税行（skip_reason=NULL）升级后必须能被重导转正。

    这是复审点名的核心场景：只加列不回填的话，历史孤儿仍是 NULL →
    get_existing_hashes 继续当它已入账 → 重导跳过 → 永久失联。
    这里构造"迁移前形态"的行（先 downgrade 掉该列），再跑**真实**迁移脚本，
    验证它变成可恢复状态并真的被转正。
    """
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = _cmb_account(db, "招商存量孤儿账户")
        legacy_flow = _tax_only_flows()[0]

        # 迁移前形态：无链接归档、skip_reason 为 NULL
        db.add(
            importer.create_broker_fund_flow(
                user_id=1,
                broker_account_id=account.id,
                filename="legacy.pdf",
                flow=legacy_flow,
                import_batch_id=None,
            )
        )
        db.commit()
        orphan = db.query(BrokerFundFlow).one()
        orphan_id = orphan.id
        assert orphan.skip_reason is None, "构造的是迁移前形态"

        # 真实迁移：先退回迁移前（列不存在），再重跑 upgrade 完成回填
        run_migration(db, MIGRATION_SKIP_REASON, "downgrade")
        run_migration(db, MIGRATION_SKIP_REASON, "upgrade")
        db.commit()
        db.expire_all()
        orphan = db.query(BrokerFundFlow).one()
        assert orphan.skip_reason == "unattributed_tax", "回填未生效"

        # 补齐股息后重导：必须就地转正，而不是被判重跳过
        _patch_parse(
            monkeypatch, _dividend_and_tax_flows(), {"股息入账": 1, "股息红利税补缴": 1}
        )
        result = import_cmb_fund_flow(
            db, 1, b"%PDF-after-migration", "cmb.pdf", broker_account_id=account.id
        )

        assert result["imported_tax_adjustments"] == 1, "存量孤儿未被恢复"
        action = db.query(CorporateAction).one()
        assert action.tax_withheld == Decimal("10.00000000")
        recovered = db.query(BrokerFundFlow).filter_by(row_hash=legacy_flow.row_hash).one()
        assert recovered.id == orphan_id
        assert recovered.corporate_action_id == action.id
        assert recovered.skip_reason is None
    finally:
        db.close()


def test_migration_backfill_does_not_touch_non_tax_rows():
    """回填必须精确到税业务行：其余无链接行（如申购配号）不得被误标。

    误标会让它们在重导时被当成"未入账"而反复处理。
    """
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = _cmb_account(db, "招商非税行账户")
        other = parsed_flow(
            row_number=1, row_hash="f9" * 32, business_name="申购配号",
            trade_date=date(2026, 5, 3), quantity="0", price="0", amount="0",
        )
        db.add(
            importer.create_broker_fund_flow(
                user_id=1, broker_account_id=account.id,
                filename="legacy.pdf", flow=other, import_batch_id=None,
            )
        )
        db.commit()

        run_migration(db, MIGRATION_SKIP_REASON, "downgrade")
        run_migration(db, MIGRATION_SKIP_REASON, "upgrade")
        db.commit()
        db.expire_all()

        row = db.query(BrokerFundFlow).one()
        assert row.skip_reason is None, "非税业务行不得被回填标记"
    finally:
        db.close()


def test_migration_backfill_note_is_appended_once_across_downgrade_cycles():
    """反复 downgrade → upgrade 后，审计备注只能有一份。

    downgrade 是**删列**，skip_reason 随之全部归 NULL，而 notes 里的痕迹留着——
    只用 `skip_reason IS NULL` 做幂等守卫的话，每轮 upgrade 都会把同一句备注
    再追加一遍。此前的测试复制迁移 SQL 且只跑一次 UPDATE，看不见这个循环。
    """
    migration = load_migration(MIGRATION_SKIP_REASON)
    note = migration.BACKFILL_NOTE

    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = _cmb_account(db, "招商备注幂等账户")
        db.add(
            importer.create_broker_fund_flow(
                user_id=1,
                broker_account_id=account.id,
                filename="legacy.pdf",
                flow=_tax_only_flows()[0],
                import_batch_id=None,
            )
        )
        db.commit()

        for _ in range(3):
            run_migration(db, MIGRATION_SKIP_REASON, "downgrade")
            run_migration(db, MIGRATION_SKIP_REASON, "upgrade")
        db.commit()
        db.expire_all()

        row = db.query(BrokerFundFlow).one()
        assert row.skip_reason == "unattributed_tax", "回填标记应在每轮 upgrade 后恢复"
        assert row.notes.count(note) == 1, f"审计备注被重复追加：{row.notes!r}"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# issue #132 子项 C：预览/导入对称
#
# 整批一票否决的持仓预检此前只在 commit 通道跑，preview 无对应物：用户拿到
# 干净预览、正式导入却被整批拒绝，"先看 /preview" 的契约在这类失败上失效。
# ---------------------------------------------------------------------------


def _oversell_flow():
    return parsed_flow(
        row_number=1, row_hash="c7" * 32, business_name="证券卖出",
        trade_date=date(2026, 3, 2), quantity="-130", price="10", amount="1300",
    )


def _seed_holding_transaction(db, account, quantity="100"):
    db.add(Transaction(
        user_id=1, symbol="600000", name="浦发银行", market="A股",
        transaction_type="BUY", quantity=Decimal(quantity), price=Decimal("10"),
        fee=Decimal("0"), transaction_date=date(2026, 1, 1), currency="CNY",
        broker_account_id=account.id,
    ))
    db.commit()


def test_cmb_preview_reports_the_position_precheck_that_import_would_fail_on(monkeypatch):
    """预览必须预报整批拒绝，且理由与导入通道逐字一致。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = _cmb_account(db, "招商预览预检账户")
        _seed_holding_transaction(db, account)
        _patch_parse(monkeypatch, [_oversell_flow()], {"证券卖出": 1})

        preview = importer.preview_cmb_fund_flow(
            db, 1, b"%PDF-preview-oversell", "cmb.pdf", broker_account_id=account.id,
        )
        blocking = [error for error in preview["errors"] if "持仓预检失败" in error]
        assert blocking, f"预览没有预报整批拒绝：{preview['errors']}"

        # 对称性：导入通道给出的必须是同一条理由
        with pytest.raises(ValueError, match="持仓预检失败") as excinfo:
            import_cmb_fund_flow(
                db, 1, b"%PDF-preview-oversell", "cmb.pdf", broker_account_id=account.id,
            )
        assert blocking[0] == str(excinfo.value)
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_cmb_preview_stays_clean_when_a_ratio_only_bonus_covers_the_sell(monkeypatch):
    """预检在预览里同样要看见 ratio-only 送股，否则变成整批误报。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = _cmb_account(db, "招商预览送股账户")
        _seed_holding_transaction(db, account)
        db.add(CorporateAction(
            user_id=1, symbol="600000", name="浦发银行", market="A股",
            action_type="STOCK_DIVIDEND", distribution_ratio="10:3",
            ex_date=date(2026, 1, 5), currency="CNY", broker_account_id=None,
        ))
        db.commit()
        _patch_parse(monkeypatch, [_oversell_flow()], {"证券卖出": 1})

        preview = importer.preview_cmb_fund_flow(
            db, 1, b"%PDF-preview-bonus", "cmb.pdf", broker_account_id=account.id,
        )
        assert not [error for error in preview["errors"] if "持仓预检失败" in error]

        result = import_cmb_fund_flow(
            db, 1, b"%PDF-preview-bonus", "cmb.pdf", broker_account_id=account.id,
        )
        assert db.get(ImportBatch, result["import_batch_id"]).status == "COMPLETED"
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_cmb_prospective_transactions_match_what_import_actually_books(monkeypatch):
    """防漂移：预览构造的"待入账交易"条数必须等于导入真正建的交易数。

    两边共用 flow.becomes_transaction，这条断言钉住的是"共用"本身——
    哪天有人在导入循环里加了新分支却没同步谓词，这里直接红。
    """
    flows = [
        parsed_flow(row_number=1, row_hash="a1" * 32, business_name="证券买入",
                    trade_date=date(2026, 3, 1), quantity="100", price="10", amount="-1000"),
        parsed_flow(row_number=2, row_hash="a2" * 32, business_name="股息入账",
                    trade_date=date(2026, 3, 2), quantity="0", price="0", amount="50"),
        parsed_flow(row_number=3, row_hash="a3" * 32, business_name="股息红利税补缴",
                    trade_date=date(2026, 3, 3), quantity="0", price="0", amount="-5"),
        # 价格为 0 的买卖行：只归档，不入账
        parsed_flow(row_number=4, row_hash="a4" * 32, business_name="证券买入",
                    trade_date=date(2026, 3, 4), quantity="10", price="0", amount="0"),
    ]
    counts = {"证券买入": 2, "股息入账": 1, "股息红利税补缴": 1}

    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = _cmb_account(db, "招商谓词一致账户")
        _patch_parse(monkeypatch, flows, counts)

        expected = importer.prospective_transactions(flows, set())
        result = import_cmb_fund_flow(
            db, 1, b"%PDF-predicate", "cmb.pdf", broker_account_id=account.id,
        )
        assert len(expected) == result["imported_transactions"] == 1
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_cmb_prospective_transaction_sorts_after_persisted_ones(monkeypatch):
    """排序键整键碰撞时，替身必须排在既有交易之后。

    招商 PDF 常常没有流水号与合同编号，行号又按每份对账单从头计数，因此
    "同日 + 空流水 + 空合同 + 同行号"的整键碰撞是现实的（手工录入的交易
    没有关联流水行，同样落在空值那一档）。碰撞时 id 位决定次序：用 0 会把
    替身排到既有交易之前，而正式导入 flush 拿到的真 id 排在之后——两边会
    指向不同的首笔超卖并报出不同余量。
    """
    from dataclasses import replace

    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = _cmb_account(db, "招商排序碰撞账户")
        for txn_type, quantity in (("BUY", "100"), ("SELL", "30")):
            db.add(Transaction(
                user_id=1, symbol="600000", name="浦发银行", market="A股",
                transaction_type=txn_type, quantity=Decimal(quantity),
                price=Decimal("10"), fee=Decimal("0"),
                transaction_date=date(2026, 2, 1), currency="CNY",
                broker_account_id=account.id,
            ))
        db.commit()

        # 本批：同日再卖 90，且流水号/合同号/行号与既有交易的空值档完全撞上。
        #   替身排在后（正确）：100 −30 → 卖 90 撞 70
        #   替身排在前（用 0）：100 −90 → 既有的卖 30 撞 10
        collided = replace(
            parsed_flow(
                row_number=0, row_hash="cc" * 32, business_name="证券卖出",
                trade_date=date(2026, 2, 1), quantity="-90", price="10", amount="900",
            ),
            serial_number=None,
            contract_number=None,
        )
        _patch_parse(monkeypatch, [collided], {"证券卖出": 1})

        preview = importer.preview_cmb_fund_flow(
            db, 1, b"%PDF-collision", "cmb.pdf", broker_account_id=account.id,
        )
        oversell = [error for error in preview["errors"] if "持仓预检失败" in error]
        assert oversell, preview["errors"]
        assert "卖出 90" in oversell[0], oversell[0]
        assert "可用数量仅 70" in oversell[0], oversell[0]

        with pytest.raises(ValueError, match="持仓预检失败") as excinfo:
            import_cmb_fund_flow(
                db, 1, b"%PDF-collision", "cmb.pdf", broker_account_id=account.id,
            )
        assert oversell[0] == str(excinfo.value), "预览与导入必须报同一条理由"
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()
