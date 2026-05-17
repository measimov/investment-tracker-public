from datetime import date
from decimal import Decimal

import pandas as pd

from app.api.import_export import import_standard_transactions_dataframe
from app.database import SessionLocal
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
