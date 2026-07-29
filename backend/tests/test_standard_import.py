from datetime import date
from decimal import Decimal

import pandas as pd

from app.api.import_export import (
    import_standard_corporate_actions_dataframe,
    import_standard_transactions_dataframe,
)
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.transaction import Transaction


def reset_tables(db):
    for model in (BrokerFundFlow, IbkrActivityFlow, Holding, CorporateAction, Transaction):
        db.query(model).delete()
    db.commit()


def standard_df(*rows):
    return pd.DataFrame(rows)


def test_standard_import_recalculates_holdings():
    db = SessionLocal()
    reset_tables(db)
    try:
        result = import_standard_transactions_dataframe(
            db,
            1,
            standard_df(
                {
                    "symbol": "600000",
                    "name": "浦发银行",
                    "market": "A股",
                    "transaction_type": "BUY",
                    "quantity": 100,
                    "price": 10,
                    "fee": 1,
                    "transaction_date": "2026-01-01",
                    "currency": "CNY",
                }
            ),
        )

        holding = db.query(Holding).filter_by(user_id=1, symbol="600000", market="A股").one()

        assert result["count"] == 1
        assert db.query(Transaction).count() == 1
        assert holding.quantity == Decimal("100.00000000")
        assert holding.total_cost == Decimal("1001.00000000")
    finally:
        db.close()


def test_standard_import_rolls_back_when_recalculation_fails():
    db = SessionLocal()
    reset_tables(db)
    try:
        try:
            import_standard_transactions_dataframe(
                db,
                1,
                standard_df(
                    {
                        "symbol": "600000",
                        "market": "A股",
                        "transaction_type": "SELL",
                        "quantity": 100,
                        "price": 10,
                        "transaction_date": date(2026, 1, 1),
                    }
                ),
            )
        except ValueError:
            pass

        assert db.query(Transaction).count() == 0
        assert db.query(Holding).count() == 0
    finally:
        db.close()


def test_standard_corporate_action_import_recalculates_holdings():
    db = SessionLocal()
    reset_tables(db)
    try:
        import_standard_transactions_dataframe(
            db,
            1,
            standard_df(
                {
                    "symbol": "00700",
                    "name": "腾讯控股",
                    "market": "港股",
                    "transaction_type": "BUY",
                    "quantity": 100,
                    "price": 300,
                    "fee": 10,
                    "transaction_date": "2026-01-01",
                    "currency": "HKD",
                }
            ),
        )

        result = import_standard_corporate_actions_dataframe(
            db,
            1,
            standard_df(
                {
                    "symbol": "00700",
                    "name": "腾讯控股",
                    "market": "港股",
                    "action_type": "CASH_DIVIDEND",
                    "ex_date": "2026-01-10",
                    "payment_date": "2026-01-10",
                    "total_dividend": "300",
                    "tax_withheld": "30",
                    "net_dividend": "270",
                    "currency": "HKD",
                },
                {
                    "symbol": "00700",
                    "name": "腾讯控股",
                    "market": "港股",
                    "action_type": "STOCK_SPLIT",
                    "ex_date": "2026-01-15",
                    "split_ratio": "1:2",
                    "currency": "HKD",
                },
            ),
        )

        holding = db.query(Holding).filter_by(user_id=1, symbol="00700", market="港股").one()
        dividend = db.query(CorporateAction).filter_by(
            user_id=1,
            symbol="00700",
            action_type="CASH_DIVIDEND",
        ).one()

        assert result == {
            "message": "Successfully imported 2 corporate actions",
            "count": 2,
            "affected_symbols": 1,
        }
        assert db.query(CorporateAction).count() == 2
        assert holding.quantity == Decimal("200.00000000")
        assert holding.total_cost == Decimal("30010.00000000")
        assert dividend.net_dividend == Decimal("270.00000000")
    finally:
        db.close()


def test_standard_corporate_action_import_rolls_back_on_invalid_row():
    db = SessionLocal()
    reset_tables(db)
    try:
        try:
            import_standard_corporate_actions_dataframe(
                db,
                1,
                standard_df(
                    {
                        "symbol": "00700",
                        "market": "港股",
                        "action_type": "CASH_DIVIDEND",
                        "ex_date": "2026-01-10",
                        "total_dividend": "300",
                        "net_dividend": "300",
                    },
                    {
                        "symbol": "00700",
                        "market": "港股",
                        "action_type": "UNSUPPORTED",
                        "ex_date": "2026-01-11",
                    },
                ),
            )
        except ValueError:
            pass

        assert db.query(CorporateAction).count() == 0
    finally:
        db.close()


def test_standard_import_attributes_broker_account():
    """标准导入带 broker_account_id：交易/公司行动/重算的持仓都落到该账户桶。"""
    db = SessionLocal()
    reset_tables(db)
    account = BrokerAccount(user_id=1, broker="HSBC", account_name="hsbc-probe")
    db.add(account)
    db.commit()
    db.refresh(account)
    try:
        import_standard_transactions_dataframe(
            db,
            1,
            standard_df(
                {
                    "symbol": "00700",
                    "name": "腾讯控股",
                    "market": "港股",
                    "transaction_type": "BUY",
                    "quantity": 100,
                    "price": 300,
                    "fee": 30,
                    "transaction_date": "2026-01-05",
                    "currency": "HKD",
                }
            ),
            broker_account_id=account.id,
        )
        import_standard_corporate_actions_dataframe(
            db,
            1,
            standard_df(
                {
                    "symbol": "00700",
                    "market": "港股",
                    "action_type": "CASH_DIVIDEND",
                    "ex_date": "2026-02-01",
                    "total_dividend": "160",
                    "net_dividend": "160",
                    "currency": "HKD",
                }
            ),
            broker_account_id=account.id,
        )

        txn = db.query(Transaction).one()
        action = db.query(CorporateAction).one()
        holding = db.query(Holding).filter_by(user_id=1, symbol="00700", market="港股").one()

        assert txn.broker_account_id == account.id
        assert action.broker_account_id == account.id
        assert holding.broker_account_id == account.id
    finally:
        reset_tables(db)
        db.query(BrokerAccount).filter_by(id=account.id).delete()
        db.commit()
        db.close()


def test_standard_import_without_account_lands_in_null_bucket():
    """不带 broker_account_id 时行为不变：落 NULL 账户桶。"""
    db = SessionLocal()
    reset_tables(db)
    try:
        import_standard_transactions_dataframe(
            db,
            1,
            standard_df(
                {
                    "symbol": "600000",
                    "name": "浦发银行",
                    "market": "A股",
                    "transaction_type": "BUY",
                    "quantity": 100,
                    "price": 10,
                    "fee": 1,
                    "transaction_date": "2026-01-01",
                    "currency": "CNY",
                }
            ),
        )
        txn = db.query(Transaction).one()
        holding = db.query(Holding).one()
        assert txn.broker_account_id is None
        assert holding.broker_account_id is None
    finally:
        reset_tables(db)
        db.close()
