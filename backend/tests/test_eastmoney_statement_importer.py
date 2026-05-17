from decimal import Decimal

from app.database import SessionLocal
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.transaction import Transaction
from app.services.eastmoney_statement_importer import (
    import_eastmoney_statement,
    parse_table_rows,
)


def reset_tables(db):
    for model in (BrokerFundFlow, IbkrActivityFlow, Holding, CorporateAction, Transaction):
        db.query(model).delete()
    db.commit()


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
                "20260310",
                "证券买入",
                "600660",
                "福耀玻璃",
                "200",
                "58.4600",
                "-11697.12",
                "5.00",
                "0.00",
                "0.12",
                "99217.68",
            ),
        ),
        (
            2,
            flow_row(
                "20260309",
                "证券卖出",
                "000333",
                "美的集团",
                "200",
                "75.1800",
                "15023.48",
                "5.00",
                "7.52",
                "0.00",
                "75559.23",
            ),
        ),
        (
            3,
            flow_row(
                "20260105",
                "证券买入",
                "513120",
                "HK创新药",
                "10000",
                "1.2610",
                "-12611.51",
                "1.51",
                "0.00",
                "0.00",
                "1896.77",
            ),
        ),
        (
            4,
            flow_row(
                "20260512",
                "红利入账",
                "600660",
                "福耀玻璃",
                "600",
                "1.2000",
                "720.00",
                "0.00",
                "0.00",
                "0.00",
                "107719.02",
            ),
        ),
        (
            5,
            flow_row(
                "20260513",
                "股息红利差异扣税",
                "600660",
                "福耀玻璃",
                "0",
                "0.0000",
                "-72.00",
                "0.00",
                "0.00",
                "0.00",
                "107647.02",
            ),
        ),
        (
            6,
            flow_row(
                "20260511",
                "融券回购",
                "204028",
                "GC028",
                "3000",
                "1.3900",
                "-300060.00",
                "60.00",
                "0.00",
                "0.00",
                "111571.59",
            ),
        ),
    ]


def test_eastmoney_parse_skips_funds_and_keeps_dividends():
    rows, business_counts, total_rows, errors = parse_table_rows(sample_rows())

    assert errors == []
    assert total_rows == 6
    assert business_counts["证券买入"] == 2
    assert len([row for row in rows if row.is_trade]) == 2
    assert rows[0].total_fee == Decimal("5.12")
    assert rows[2].skip_reason == "fund"
    assert rows[3].is_cash_dividend
    assert rows[4].is_dividend_tax
    assert rows[5].skip_reason == "unsupported"


def test_eastmoney_import_creates_transactions_and_corporate_actions(monkeypatch):
    db = SessionLocal()
    reset_tables(db)
    try:
        monkeypatch.setattr(
            "app.services.eastmoney_statement_importer.read_eastmoney_statement_rows",
            lambda contents: (sample_rows(), len(sample_rows())),
        )

        result = import_eastmoney_statement(db, 1, b"%PDF", "eastmoney.pdf")

        assert result["broker"] == "东方财富证券"
        assert result["total_rows"] == 6
        assert result["eligible_trade_rows"] == 2
        assert result["eligible_dividend_rows"] == 1
        assert result["eligible_tax_rows"] == 1
        assert result["imported_transactions"] == 2
        assert result["imported_corporate_actions"] == 1
        assert result["imported_tax_adjustments"] == 1
        assert result["skipped_cash_rows"] == 1

        transactions = db.query(Transaction).order_by(Transaction.id).all()
        assert [(txn.symbol, txn.transaction_type) for txn in transactions] == [
            ("600660", "BUY"),
            ("000333", "SELL"),
        ]
        assert transactions[0].fee == Decimal("5.12000000")

        action = db.query(CorporateAction).one()
        assert action.symbol == "600660"
        assert action.action_type == "CASH_DIVIDEND"
        assert action.total_dividend == Decimal("720.00000000")
        assert action.tax_withheld == Decimal("72.00000000")
        assert action.net_dividend == Decimal("648.00000000")

        flows = db.query(BrokerFundFlow).all()
        assert len(flows) == 4
        assert {flow.broker for flow in flows} == {"东方财富证券"}

        duplicate = import_eastmoney_statement(db, 1, b"%PDF", "eastmoney.pdf")
        assert duplicate["imported_transactions"] == 0
        assert duplicate["imported_corporate_actions"] == 0
        assert duplicate["imported_tax_adjustments"] == 0
        assert duplicate["duplicate_rows"] == 4
        assert db.query(Transaction).count() == 2
        assert db.query(CorporateAction).count() == 1
    finally:
        db.close()
