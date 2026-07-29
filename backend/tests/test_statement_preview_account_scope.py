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
from app.models.transaction import Transaction
from app.services import cmb_fund_flow_importer as cmb_importer
from app.services import eastmoney_statement_importer as eastmoney_importer


def reset_tables(db):
    for model in (
        BrokerFundFlow,
        IbkrActivityFlow,
        Holding,
        CorporateAction,
        Transaction,
        CashEvent,
        ReconciliationSnapshot,
        ImportBatch,
        BrokerAccount,
    ):
        db.query(model).delete()
    db.commit()


def cmb_flow(row_hash: str) -> cmb_importer.ParsedFlow:
    return cmb_importer.ParsedFlow(
        source_row_number=2,
        row_hash=row_hash,
        security_code="600000",
        security_name="浦发银行",
        currency="CNY",
        trade_date=date(2026, 1, 2),
        trade_price=Decimal("10"),
        trade_quantity=Decimal("100"),
        amount=Decimal("-1001"),
        cash_balance=Decimal("10000"),
        remaining_quantity=Decimal("100"),
        contract_number="contract-2",
        serial_number="serial-2",
        business_name="证券买入",
        stamp_tax=Decimal("0"),
        commission=Decimal("1"),
        handling_fee=Decimal("0"),
        management_fee=Decimal("0"),
        settlement_fee=Decimal("0"),
        transfer_fee=Decimal("0"),
        other_fee=Decimal("0"),
        shareholder_code="A123",
        notes=None,
    )


def eastmoney_rows():
    return [
        (
            1,
            {
                "发生日期": "20260724",
                "买卖类别": "证券买入",
                "证券代码": "600000",
                "证券名称": "浦发银行",
                "成交数量": "100",
                "成交价格": "10.00",
                "总发生金额": "-1001.00",
                "手续费": "1.00",
                "印花税": "0.00",
                "过户费": "0.00",
                "资金余额": "10000.00",
                "_statement_type": "stock",
                "_currency": "CNY",
            },
        )
    ]


def test_cmb_preview_scopes_duplicates_to_validated_account(monkeypatch):
    db = SessionLocal()
    reset_tables(db)
    try:
        first_account = BrokerAccount(
            user_id=1,
            broker="招商证券",
            account_name="招商账户一",
            account_number_masked="****A123",
            base_currency="CNY",
        )
        second_account = BrokerAccount(
            user_id=1,
            broker="招商证券",
            account_name="招商账户二",
            account_number_masked="****A123",
            base_currency="CNY",
        )
        db.add_all([first_account, second_account])
        db.commit()
        db.refresh(first_account)
        db.refresh(second_account)

        flow = cmb_flow("a" * 64)
        db.add(
            cmb_importer.create_broker_fund_flow(
                user_id=1,
                broker_account_id=first_account.id,
                filename="existing.pdf",
                flow=flow,
            )
        )
        db.commit()
        monkeypatch.setattr(
            cmb_importer,
            "parse_rows",
            lambda contents, filename: ([flow], {"证券买入": 1}, 1, []),
        )

        first_preview = cmb_importer.preview_cmb_fund_flow(
            db,
            1,
            b"%PDF",
            "cmb.pdf",
            broker_account_id=first_account.id,
        )
        second_preview = cmb_importer.preview_cmb_fund_flow(
            db,
            1,
            b"%PDF",
            "cmb.pdf",
            broker_account_id=second_account.id,
        )

        assert first_preview["duplicate_rows"] == 1
        assert second_preview["duplicate_rows"] == 0
        assert db.query(ImportBatch).count() == 0
    finally:
        db.close()


def test_eastmoney_preview_scopes_duplicates_to_validated_account(monkeypatch):
    db = SessionLocal()
    reset_tables(db)
    try:
        first_account = BrokerAccount(
            user_id=1,
            broker="东方财富证券",
            account_name="东方财富账户一",
            base_currency="CNY",
        )
        second_account = BrokerAccount(
            user_id=1,
            broker="东方财富证券",
            account_name="东方财富账户二",
            base_currency="CNY",
        )
        db.add_all([first_account, second_account])
        db.commit()
        db.refresh(first_account)
        db.refresh(second_account)

        monkeypatch.setattr(
            eastmoney_importer,
            "read_eastmoney_statement_rows",
            lambda contents: (eastmoney_rows(), 1),
        )
        context = eastmoney_importer.EastmoneyStatementContext(
            statement_type="stock",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 26),
            positions=[],
            cash_balances={"CNY": Decimal("10000")},
        )
        monkeypatch.setattr(
            eastmoney_importer,
            "read_eastmoney_statement_context",
            lambda contents: context,
        )
        parsed_rows, _, _, _ = eastmoney_importer.parse_rows(b"%PDF", "eastmoney.pdf")
        db.add(
            eastmoney_importer.create_broker_fund_flow(
                user_id=1,
                broker_account_id=first_account.id,
                filename="existing.pdf",
                flow=parsed_rows[0],
            )
        )
        db.commit()

        first_preview = eastmoney_importer.preview_eastmoney_statement(
            db,
            1,
            b"%PDF",
            "eastmoney.pdf",
            broker_account_id=first_account.id,
        )
        second_preview = eastmoney_importer.preview_eastmoney_statement(
            db,
            1,
            b"%PDF",
            "eastmoney.pdf",
            broker_account_id=second_account.id,
        )

        assert first_preview["duplicate_rows"] == 1
        assert second_preview["duplicate_rows"] == 0
        assert db.query(ImportBatch).count() == 0
    finally:
        db.close()


@pytest.mark.parametrize(
    ("preview", "broker", "missing_message"),
    [
        (
            cmb_importer.preview_cmb_fund_flow,
            "招商证券",
            "broker_account_id is required",
        ),
        (
            eastmoney_importer.preview_eastmoney_statement,
            "东方财富证券",
            "请选择东方财富券商账户",
        ),
    ],
)
def test_statement_preview_rejects_missing_foreign_or_wrong_broker_account(
    preview,
    broker,
    missing_message,
):
    db = SessionLocal()
    reset_tables(db)
    try:
        wrong_broker = BrokerAccount(
            user_id=1,
            broker="东方财富证券" if broker == "招商证券" else "招商证券",
            account_name="错误券商",
            base_currency="CNY",
        )
        other_user = BrokerAccount(
            user_id=2,
            broker=broker,
            account_name="其他用户账户",
            base_currency="CNY",
        )
        db.add_all([wrong_broker, other_user])
        db.commit()

        with pytest.raises(ValueError, match=missing_message):
            preview(db, 1, b"%PDF", "statement.pdf")
        with pytest.raises(ValueError, match="belongs to"):
            preview(
                db,
                1,
                b"%PDF",
                "statement.pdf",
                broker_account_id=wrong_broker.id,
            )
        with pytest.raises(ValueError, match="not found"):
            preview(
                db,
                1,
                b"%PDF",
                "statement.pdf",
                broker_account_id=other_user.id,
            )
    finally:
        db.close()
