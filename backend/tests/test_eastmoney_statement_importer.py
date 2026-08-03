import hashlib
from datetime import date
from decimal import Decimal

import pytest

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.cash_event import CashEvent
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.import_batch import ImportBatch
from app.models.reconciliation_snapshot import ReconciliationSnapshot
from app.models.security_rule import SecurityRule
from app.models.transaction import Transaction
from app.services.eastmoney_statement_importer import (
    EastmoneyStatementContext,
    EastmoneyStatementPosition,
    import_eastmoney_statement,
    parse_table_rows,
    preview_eastmoney_statement,
)
from tests.helpers import reset_tables


RESET_MODELS = (
    BrokerFundFlow,
    IbkrActivityFlow,
    Holding,
    CorporateAction,
    Transaction,
    CashEvent,
    ReconciliationSnapshot,
    ImportBatch,
    BrokerAccount,
    SecurityRule,
)


def statement_context(
    *,
    statement_type="stock",
    positions=None,
    cash_balance="1000.00",
):
    return EastmoneyStatementContext(
        statement_type=statement_type,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 12, 31),
        positions=positions or [],
        cash_balances={"CNY": Decimal(cash_balance)},
    )


def patch_statement(monkeypatch, rows, context):
    monkeypatch.setattr(
        "app.services.eastmoney_statement_importer.read_eastmoney_statement_rows",
        lambda contents: (rows, len(rows)),
    )
    monkeypatch.setattr(
        "app.services.eastmoney_statement_importer.read_eastmoney_statement_context",
        lambda contents: context,
    )


def flow_row(
    date_value,
    business,
    symbol,
    name,
    quantity,
    price,
    amount,
    commission="0.00",
    stamp_tax="0.00",
    transfer_fee="0.00",
    balance="0.00",
):
    return {
        "发生日期": date_value,
        "买卖类别": business,
        "证券代码": symbol,
        "证券名称": name,
        "成交数量": quantity,
        "成交价格": price,
        "总发生金额": amount,
        "手续费": commission,
        "印花税": stamp_tax,
        "过户费": transfer_fee,
        "资金余额": balance,
    }


def sample_rows():
    return [
        (
            1,
            flow_row(
                "20260110",
                "证券买入",
                "600001",
                "合成股票甲",
                "200",
                "10.0000",
                "-2005.10",
                "5.00",
                "0.00",
                "0.10",
                "50000.00",
            ),
        ),
        (
            2,
            flow_row(
                "20260109",
                "证券卖出",
                "000002",
                "合成股票乙",
                "200",
                "20.0000",
                "3991.00",
                "5.00",
                "4.00",
                "0.00",
                "53991.00",
            ),
        ),
        (
            3,
            flow_row(
                "20260105",
                "证券买入",
                "510001",
                "合成ETF",
                "1000",
                "2.0000",
                "-2002.00",
                "2.00",
                "0.00",
                "0.00",
                "51989.00",
            ),
        ),
        (
            4,
            flow_row(
                "20260210",
                "红利入账",
                "600001",
                "合成股票甲",
                "200",
                "0.5000",
                "100.00",
                "0.00",
                "0.00",
                "0.00",
                "52089.00",
            ),
        ),
        (
            5,
            flow_row(
                "20260211",
                "股息红利差异扣税",
                "600001",
                "合成股票甲",
                "0",
                "0.0000",
                "-10.00",
                "0.00",
                "0.00",
                "0.00",
                "52079.00",
            ),
        ),
        (
            6,
            flow_row(
                "20260108",
                "融券回购",
                "204001",
                "合成回购",
                "1000",
                "1.5000",
                "-100050.00",
                "50.00",
                "0.00",
                "0.00",
                "152079.00",
            ),
        ),
    ]


def successful_stock_rows():
    rows = sample_rows()
    return [rows[index] for index in (0, 2, 3, 4, 5)]


def hk_flow_row(
    date_value,
    business,
    symbol="",
    name="",
    quantity="0",
    price="0.000",
    rate="0.00000",
    amount="0.03",
    commission="0.00",
    stamp_tax="0.00",
    management_fee="0.00",
    handling_fee="0.00",
    system_fee="0.00",
    settlement_fee="0.00",
    other_fee="0.00",
):
    return {
        "发生日期": date_value,
        "买卖类别": business,
        "证券代码": symbol,
        "证券名称": name,
        "成交数量": quantity,
        "成交价格": price,
        "结算汇率": rate,
        "总发生金额": amount,
        "手续费": commission,
        "印花税": stamp_tax,
        "交易征费": management_fee,
        "交易费": handling_fee,
        "系统费用": system_fee,
        "交收费": settlement_fee,
        "其他费用": other_fee,
        "_statement_type": "hk_connect",
        "_currency": "CNY",
    }


def hk_sample_rows():
    return [
        (
            1,
            hk_flow_row(
                "20260308",
                "港股通买入",
                "01234",
                "合成港股甲",
                "100",
                "80.000",
                "0.80000",
                "8000.00",
                "10.00",
                "8.00",
                "1.00",
                "2.00",
                "0.00",
                "1.00",
                "0.00",
            ),
        ),
        (
            2,
            hk_flow_row(
                "20260301",
                "港股通卖出",
                "05678",
                "合成港股乙",
                "100",
                "40.000",
                "0.80000",
                "4000.00",
                "5.00",
                "4.00",
                "0.50",
                "1.00",
                "0.00",
                "0.50",
                "0.00",
            ),
        ),
        (
            3,
            hk_flow_row(
                "20260221",
                "港股通买入",
                "05678",
                "合成港股乙",
                "60",
                "32.000",
                "0.80000",
                "1920.00",
                "3.00",
                "2.00",
                "0.20",
                "0.40",
                "0.00",
                "0.20",
                "0.00",
            ),
        ),
        (
            4,
            hk_flow_row(
                "20260212",
                "港股通买入",
                "05678",
                "合成港股乙",
                "40",
                "32.000",
                "0.80000",
                "1280.00",
                "2.00",
                "1.00",
                "0.10",
                "0.20",
                "0.00",
                "0.10",
                "0.00",
            ),
        ),
        (5, hk_flow_row("20260324", "港股通组合费", amount="0.10")),
        (6, hk_flow_row("20260323", "港股通组合费", amount="0.20")),
    ]


def create_eastmoney_account(db, name="东方财富测试账户"):
    account = BrokerAccount(
        user_id=1,
        broker="东方财富证券",
        account_name=name,
        base_currency="CNY",
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def test_eastmoney_parse_imports_exchange_traded_funds_and_keeps_dividends():
    rows, business_counts, total_rows, errors = parse_table_rows(sample_rows())

    assert errors == []
    assert total_rows == 6
    assert business_counts["证券买入"] == 2
    assert len([row for row in rows if row.is_trade]) == 3
    assert rows[0].total_fee == Decimal("5.10")
    assert rows[2].skip_reason is None
    assert rows[2].is_trade
    assert rows[3].is_cash_dividend
    assert rows[4].is_dividend_tax
    assert rows[5].skip_reason == "unsupported"


def test_eastmoney_stock_trade_reconciliation_allows_displayed_average_price_rounding():
    rounded_rows = [
        (
            125,
            flow_row(
                "20260416",
                "证券卖出",
                "600003",
                "合成股票丙",
                "100",
                "10.0049",
                "998.39",
                "1.00",
                "1.00",
                "0.10",
            ),
        ),
        (
            218,
            flow_row(
                "20260401",
                "证券卖出",
                "510002",
                "合成ETF乙",
                "1000",
                "2.3456",
                "2344.60",
                "1.00",
            ),
        ),
    ]

    parsed, _, total_rows, errors = parse_table_rows(rounded_rows)

    assert total_rows == 2
    assert errors == []
    assert len(parsed) == 2
    assert all(flow.transaction_type == "SELL" for flow in parsed)

    outside_rounding_tolerance = rounded_rows[0][1].copy()
    outside_rounding_tolerance["总发生金额"] = "998.00"
    parsed, _, _, errors = parse_table_rows([(125, outside_rounding_tolerance)])

    assert parsed == []
    assert errors == [
        "row 125: PDF trade amount does not reconcile with value and fees",
    ]


def test_eastmoney_parses_hk_connect_rounding_and_native_currency():
    rows, business_counts, total_rows, errors = parse_table_rows(hk_sample_rows())

    assert errors == []
    assert total_rows == 6
    assert business_counts == {
        "港股通买入": 3,
        "港股通卖出": 1,
        "港股通组合费": 2,
    }
    assert len([row for row in rows if row.is_trade]) == 4
    assert len([row for row in rows if row.is_cash_fee]) == 2
    assert rows[0].currency == "CNY"
    assert rows[0].normalized_transaction_currency == "HKD"
    assert rows[0].normalized_transaction_price == Decimal("100")
    assert rows[0].total_fee == Decimal("22")
    assert rows[0].normalized_transaction_fee == Decimal("27.5")


def test_eastmoney_rejects_scope_conflict_and_malformed_numeric_field():
    conflicting = hk_flow_row(
        "20260308",
        "港股通买入",
        "600001",
        "合成股票甲",
        "100",
        "50.000",
        "0.86000",
        "5000.00",
    )
    malformed = hk_flow_row(
        "20260308",
        "港股通买入",
        "01234",
        "合成港股甲",
        "not-a-number",
        "80.000",
        "0.80000",
        "8000.00",
    )

    rows, _, total_rows, errors = parse_table_rows([(1, conflicting), (2, malformed)])

    assert total_rows == 2
    assert len(rows) == 1
    assert rows[0].skip_reason == "conflict"
    assert any("authority scope" in error for error in errors)
    assert any("成交数量" in error for error in errors)


def test_eastmoney_import_creates_transactions_and_corporate_actions(monkeypatch):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = create_eastmoney_account(db)
        context = statement_context(
            positions=[
                EastmoneyStatementPosition(
                    symbol="600001",
                    name="合成股票甲",
                    market="A股",
                    quantity=Decimal("200"),
                ),
                EastmoneyStatementPosition(
                    symbol="510001",
                    name="合成ETF",
                    market="A股",
                    quantity=Decimal("1000"),
                ),
                EastmoneyStatementPosition(
                    symbol="01234",
                    name="合成港股甲",
                    market="港股",
                    quantity=Decimal("999"),
                ),
            ]
        )
        patch_statement(monkeypatch, successful_stock_rows(), context)

        contents = b"%PDF"
        result = import_eastmoney_statement(
            db,
            1,
            contents,
            "eastmoney.pdf",
            broker_account_id=account.id,
        )

        assert result["broker"] == "东方财富证券"
        assert result["broker_account_id"] == account.id
        assert result["batch_status"] == "PARTIAL"
        assert result["total_rows"] == 5
        assert result["eligible_trade_rows"] == 2
        assert result["eligible_dividend_rows"] == 1
        assert result["eligible_tax_rows"] == 1
        assert result["imported_transactions"] == 2
        assert result["imported_corporate_actions"] == 1
        assert result["imported_tax_adjustments"] == 1
        assert result["skipped_cash_rows"] == 0
        assert result["skipped_unsupported_rows"] == 1
        assert result["statement_scope"] == "stock"
        assert result["reported_position_count"] == 2
        assert result["date_end"] == "2026-12-31"
        assert result["reconciliation_status"] == "MATCHED"

        transactions = db.query(Transaction).order_by(Transaction.id).all()
        assert [(txn.symbol, txn.transaction_type) for txn in transactions] == [
            ("600001", "BUY"),
            ("510001", "BUY"),
        ]
        assert transactions[0].fee == Decimal("5.10000000")
        assert {txn.broker_account_id for txn in transactions} == {account.id}
        assert {txn.import_batch_id for txn in transactions} == {result["import_batch_id"]}

        action = db.query(CorporateAction).one()
        assert action.symbol == "600001"
        assert action.action_type == "CASH_DIVIDEND"
        assert action.total_dividend == Decimal("100.00000000")
        assert action.tax_withheld == Decimal("10.00000000")
        assert action.net_dividend == Decimal("90.00000000")
        assert action.broker_account_id == account.id
        assert action.import_batch_id == result["import_batch_id"]

        flows = db.query(BrokerFundFlow).all()
        assert len(flows) == 5
        assert {flow.broker for flow in flows} == {"东方财富证券"}
        assert {flow.broker_account_id for flow in flows} == {account.id}
        assert {flow.import_batch_id for flow in flows} == {result["import_batch_id"]}
        assert {flow.statement_type for flow in flows} == {"stock"}
        assert sum(flow.corporate_action_id == action.id for flow in flows) == 2

        first_batch = db.get(ImportBatch, result["import_batch_id"])
        assert first_batch.source_sha256 == hashlib.sha256(contents).hexdigest()
        assert first_batch.row_count == 5
        assert first_batch.archived_count == 5
        assert first_batch.imported_count == 4
        assert first_batch.skipped_count == 1
        assert first_batch.status == "PARTIAL"
        assert first_batch.error_count >= 1
        snapshot = db.get(ReconciliationSnapshot, result["reconciliation_snapshot_id"])
        assert snapshot.snapshot_date == date(2026, 12, 31)
        assert snapshot.statement_scope == "stock"
        assert [position["market"] for position in snapshot.positions] == ["A股", "A股"]
        assert snapshot.import_batch_id == first_batch.id
        assert f"batch_id={first_batch.id}" in snapshot.notes

        duplicate = import_eastmoney_statement(
            db,
            1,
            contents,
            "renamed-eastmoney.pdf",
            broker_account_id=account.id,
        )
        assert duplicate["imported_transactions"] == 0
        assert duplicate["imported_corporate_actions"] == 0
        assert duplicate["imported_tax_adjustments"] == 0
        assert duplicate["duplicate_rows"] == 5
        assert duplicate["batch_status"] == "COMPLETED"
        assert duplicate["import_batch_id"] != result["import_batch_id"]
        assert duplicate["reconciliation_snapshot_id"] != snapshot.id
        assert db.query(Transaction).count() == 2
        assert db.query(CorporateAction).count() == 1
        duplicate_batch = db.get(ImportBatch, duplicate["import_batch_id"])
        duplicate_snapshot = db.get(
            ReconciliationSnapshot,
            duplicate["reconciliation_snapshot_id"],
        )
        assert duplicate_batch.imported_count == 0
        assert duplicate_batch.archived_count == 0
        assert duplicate_batch.duplicate_count == 5
        assert duplicate_batch.skipped_count == 0
        assert duplicate_snapshot.import_batch_id == duplicate_batch.id
        assert duplicate_snapshot.source_filename == "renamed-eastmoney.pdf"
        assert f"batch_id={duplicate_batch.id}" in duplicate_snapshot.notes
        assert db.query(ImportBatch).count() == 2
        assert db.query(ReconciliationSnapshot).count() == 2
    finally:
        db.close()


def test_eastmoney_missing_opening_position_rolls_back_entire_batch(monkeypatch):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = create_eastmoney_account(db)
        context = statement_context(
            positions=[
                EastmoneyStatementPosition(
                    symbol="600001",
                    name="合成股票甲",
                    market="A股",
                    quantity=Decimal("200"),
                ),
                EastmoneyStatementPosition(
                    symbol="510001",
                    name="合成ETF",
                    market="A股",
                    quantity=Decimal("1000"),
                ),
            ]
        )
        patch_statement(monkeypatch, sample_rows(), context)

        with pytest.raises(ValueError, match="缺少期初持仓"):
            import_eastmoney_statement(
                db,
                1,
                b"%PDF-missing-opening",
                "eastmoney-missing-opening.pdf",
                broker_account_id=account.id,
            )

        batch = db.query(ImportBatch).one()
        assert batch.status == "FAILED"
        assert batch.source_type == "eastmoney_stock_statement_pdf"
        assert batch.row_count == 6
        assert batch.period_end == date(2026, 12, 31)
        assert batch.imported_count == 0
        assert batch.archived_count == 0
        assert "缺少期初持仓" in batch.error_message
        assert db.query(Transaction).count() == 0
        assert db.query(CorporateAction).count() == 0
        assert db.query(BrokerFundFlow).count() == 0
        assert db.query(ReconciliationSnapshot).count() == 0
        assert db.query(Holding).count() == 0
    finally:
        db.close()


def test_eastmoney_mismatched_scope_reconciliation_rolls_back_canonical_rows(monkeypatch):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = create_eastmoney_account(db)
        source_rows = [sample_rows()[0]]
        context = statement_context(
            positions=[
                EastmoneyStatementPosition(
                    symbol="600001",
                    name="合成股票甲",
                    market="A股",
                    quantity=Decimal("100"),
                )
            ]
        )
        patch_statement(monkeypatch, source_rows, context)

        with pytest.raises(ValueError, match="持仓与账户交易记录不一致"):
            import_eastmoney_statement(
                db,
                1,
                b"%PDF-mismatch",
                "eastmoney-mismatch.pdf",
                broker_account_id=account.id,
            )

        batch = db.query(ImportBatch).one()
        assert batch.status == "FAILED"
        assert batch.imported_count == 0
        assert "reported': '100'" in batch.error_message
        assert "computed': '200" in batch.error_message
        assert db.query(Transaction).count() == 0
        assert db.query(BrokerFundFlow).count() == 0
        assert db.query(ReconciliationSnapshot).count() == 0
    finally:
        db.close()


def test_eastmoney_tax_requires_exactly_one_account_dividend_candidate(monkeypatch):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = create_eastmoney_account(db)
        for ex_date in (date(2026, 2, 9), date(2026, 2, 11)):
            db.add(
                CorporateAction(
                    user_id=1,
                    broker_account_id=account.id,
                    symbol="600001",
                    name="合成股票甲",
                    market="A股",
                    action_type="CASH_DIVIDEND",
                    ex_date=ex_date,
                    total_dividend=Decimal("100"),
                    tax_withheld=Decimal("0"),
                    net_dividend=Decimal("100"),
                    currency="CNY",
                )
            )
        db.commit()
        tax_rows = [sample_rows()[4]]
        patch_statement(monkeypatch, tax_rows, statement_context(positions=[]))

        result = import_eastmoney_statement(
            db,
            1,
            b"%PDF-ambiguous-tax",
            "eastmoney-tax.pdf",
            broker_account_id=account.id,
        )

        assert result["imported_tax_adjustments"] == 0
        assert result["archived_source_rows"] == 1
        assert result["batch_status"] == "PARTIAL"
        assert any("no account-scoped dividend" in error for error in result["errors"])
        actions = db.query(CorporateAction).order_by(CorporateAction.ex_date).all()
        assert [action.tax_withheld for action in actions] == [
            Decimal("0E-8"),
            Decimal("0E-8"),
        ]
        source = db.query(BrokerFundFlow).one()
        assert source.corporate_action_id is None
        batch = db.get(ImportBatch, result["import_batch_id"])
        assert batch.imported_count == 0
        assert batch.archived_count == 1
    finally:
        db.close()


def test_eastmoney_hk_import_preserves_all_rows_fees_and_snapshot(monkeypatch):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = create_eastmoney_account(db)
        context = statement_context(
            statement_type="hk_connect",
            positions=[
                EastmoneyStatementPosition(
                    symbol="01234",
                    name="合成港股甲",
                    market="港股",
                    quantity=Decimal("100"),
                    currency="HKD",
                ),
                EastmoneyStatementPosition(
                    symbol="600001",
                    name="合成股票甲",
                    market="A股",
                    quantity=Decimal("999"),
                ),
            ],
        )
        patch_statement(monkeypatch, hk_sample_rows(), context)

        result = import_eastmoney_statement(
            db,
            1,
            b"%PDF-hk",
            "eastmoney-hk.pdf",
            broker_account_id=account.id,
        )

        assert result["batch_status"] == "COMPLETED"
        assert result["total_rows"] == 6
        assert result["eligible_trade_rows"] == 4
        assert result["eligible_cash_rows"] == 2
        assert result["imported_transactions"] == 4
        assert result["imported_cash_events"] == 2
        assert result["reported_position_count"] == 1
        assert result["reconciliation_status"] == "MATCHED"
        assert db.query(BrokerFundFlow).count() == 6
        assert db.query(CashEvent).count() == 2

        fees = db.query(CashEvent).order_by(CashEvent.event_date.desc()).all()
        assert [(fee.event_type, fee.currency, fee.amount) for fee in fees] == [
            ("FEE", "CNY", Decimal("0.10000000")),
            ("FEE", "CNY", Decimal("0.20000000")),
        ]
        transactions = db.query(Transaction).order_by(Transaction.transaction_date.desc()).all()
        synthetic_hk = next(txn for txn in transactions if txn.symbol == "01234")
        assert synthetic_hk.currency == "HKD"
        assert synthetic_hk.price == Decimal("100.00000000")
        assert synthetic_hk.fee == Decimal("27.50000000")
        assert (
            db.query(Transaction)
            .filter(Transaction.symbol == "05678")
            .with_entities(Transaction.quantity)
            .count()
            == 3
        )
        snapshot = db.get(ReconciliationSnapshot, result["reconciliation_snapshot_id"])
        assert snapshot.status == "MATCHED"
        assert snapshot.snapshot_date == date(2026, 12, 31)
        assert snapshot.positions == [
            {
                "symbol": "01234",
                "name": "合成港股甲",
                "market": "港股",
                "quantity": "100",
                "currency": "HKD",
            }
        ]
        first_batch = db.get(ImportBatch, result["import_batch_id"])
        assert first_batch.imported_count == 6
        assert first_batch.archived_count == 6

        duplicate = import_eastmoney_statement(
            db,
            1,
            b"%PDF-hk",
            "renamed-hk.pdf",
            broker_account_id=account.id,
        )
        assert duplicate["duplicate_rows"] == 6
        assert duplicate["imported_transactions"] == 0
        assert duplicate["imported_cash_events"] == 0
        assert duplicate["reconciliation_snapshot_id"] != snapshot.id
        assert db.query(Transaction).count() == 4
        assert db.query(CashEvent).count() == 2
        assert db.query(BrokerFundFlow).count() == 6
        duplicate_batch = db.get(ImportBatch, duplicate["import_batch_id"])
        duplicate_snapshot = db.get(
            ReconciliationSnapshot,
            duplicate["reconciliation_snapshot_id"],
        )
        assert duplicate_batch.imported_count == 0
        assert duplicate_batch.archived_count == 0
        assert duplicate_snapshot.import_batch_id == duplicate_batch.id
        assert duplicate_snapshot.source_filename == "renamed-hk.pdf"
        assert f"batch_id={duplicate_batch.id}" in duplicate_snapshot.notes
        assert db.query(ReconciliationSnapshot).count() == 2
    finally:
        db.close()


def test_eastmoney_requires_account_and_rejects_same_file_across_accounts(monkeypatch):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        context = statement_context(
            statement_type="hk_connect",
            positions=[
                EastmoneyStatementPosition(
                    symbol="01234",
                    name="合成港股甲",
                    market="港股",
                    quantity=Decimal("100"),
                )
            ],
        )
        patch_statement(monkeypatch, hk_sample_rows(), context)

        with pytest.raises(ValueError, match="请选择东方财富券商账户"):
            import_eastmoney_statement(db, 1, b"%PDF-hk", "eastmoney-hk.pdf")
        assert db.query(ImportBatch).count() == 0

        first = create_eastmoney_account(db, "东方财富账户一")
        second = create_eastmoney_account(db, "东方财富账户二")
        first_result = import_eastmoney_statement(
            db,
            1,
            b"%PDF-hk",
            "eastmoney-hk.pdf",
            broker_account_id=first.id,
        )
        with pytest.raises(ValueError, match="already imported into another"):
            preview_eastmoney_statement(
                db,
                1,
                b"%PDF-hk",
                "eastmoney-hk.pdf",
                broker_account_id=second.id,
            )
        with pytest.raises(ValueError, match="already imported into another"):
            import_eastmoney_statement(
                db,
                1,
                b"%PDF-hk",
                "eastmoney-hk.pdf",
                broker_account_id=second.id,
            )

        assert first_result["imported_transactions"] == 4
        assert db.query(BrokerFundFlow).count() == 6
        assert db.query(ImportBatch).count() == 1
    finally:
        db.close()


def test_eastmoney_preview_does_not_create_import_batch(monkeypatch):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        patch_statement(monkeypatch, sample_rows(), statement_context())
        account = create_eastmoney_account(db)

        result = preview_eastmoney_statement(
            db,
            1,
            b"%PDF",
            "eastmoney.pdf",
            broker_account_id=account.id,
        )

        assert result["total_rows"] == 6
        assert result["eligible_trade_rows"] == 3
        assert db.query(ImportBatch).count() == 0
    finally:
        db.close()


def test_eastmoney_open_fund_subscription_books_as_buy():
    """开放基金申购（真实案例 161226 白银LOF）：申购费在佣金列，金额恒等式
    精确成立，映射为 BUY；不建模会让后续证券卖出撞期初持仓守卫。"""
    rows, business_counts, total_rows, errors = parse_table_rows(
        [
            (
                7,
                flow_row(
                    "20251225",
                    "开放基金申购",
                    "161226",
                    "白银LOF",
                    "256",
                    "1.9278",
                    "-498.47",
                    "4.95",
                ),
            )
        ]
    )

    assert errors == []
    assert len(rows) == 1
    assert rows[0].transaction_type == "BUY"
    assert rows[0].total_fee == Decimal("4.95")


def test_eastmoney_parser_version_tracks_booking_semantics():
    """入账口径变化必须升版（ImportBatch 按 parser/version 审计）。

    若本断言失败，说明你改了 parser 行为——请升级 PARSER_VERSION 并更新此处。
    """
    from app.services.eastmoney_statement_importer import PARSER_VERSION

    assert PARSER_VERSION == "7"


def test_excluded_security_archives_rows_and_passes_snapshot_gate(monkeypatch):
    """排除清单标的（货币基金 511880）：交易只归档不入账；对账单持仓节
    含该标的时，自动快照经统一比对过滤后仍 MATCHED，整批不被门禁拒绝。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = create_eastmoney_account(db)
        db.add(SecurityRule(rule_type="EXCLUDE", user_id=1, symbol="511880", market="A股", note="货币基金"))
        db.commit()

        rows = [
            (1, flow_row("20260110", "证券买入", "600001", "合成股票甲",
                         "200", "10.0000", "-2000.00", "0.00", "0.00", "0.00")),
            (2, flow_row("20260111", "证券买入", "511880", "银华日利",
                         "1000", "1.0000", "-1000.00", "0.00", "0.00", "0.00")),
        ]
        context = statement_context(
            positions=[
                EastmoneyStatementPosition(
                    symbol="600001", name="合成股票甲", market="A股", quantity=Decimal("200"),
                ),
                EastmoneyStatementPosition(
                    symbol="511880", name="银华日利", market="A股", quantity=Decimal("1000"),
                ),
            ]
        )
        patch_statement(monkeypatch, rows, context)

        result = import_eastmoney_statement(
            db, 1, b"%PDF", "eastmoney.pdf", broker_account_id=account.id,
        )

        assert result["imported_transactions"] == 1
        assert result["skipped_excluded_rows"] == 1
        assert result["reconciliation_status"] == "MATCHED"
        # 排除是预期跳过：正常行 + 排除行 = COMPLETED、零错误
        assert result["batch_status"] == "COMPLETED"
        batch = db.get(ImportBatch, result["import_batch_id"])
        assert batch.error_count == 0
        assert batch.error_message is None

        transactions = db.query(Transaction).all()
        assert [txn.symbol for txn in transactions] == ["600001"]

        flows = db.query(BrokerFundFlow).all()
        assert len(flows) == 2  # 排除行仍归档，审计链完整
        excluded_flow = next(f for f in flows if f.security_code == "511880")
        assert excluded_flow.transaction_id is None

        snapshot = db.get(ReconciliationSnapshot, result["reconciliation_snapshot_id"])
        # 快照保持对账单原貌（券商断言不丢），过滤发生在比对层
        assert {p["symbol"] for p in snapshot.positions} == {"600001", "511880"}
        assert snapshot.diff_detail["summary"]["excluded_symbols"] == [
            {"symbol": "511880", "market": "A股"}
        ]
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()
