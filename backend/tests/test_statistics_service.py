from datetime import date
from decimal import Decimal

from app.database import SessionLocal
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.transaction import Transaction
from app.services.statistics_service import calculate_fifo_pnl_per_symbol


def reset_tables(db):
    for model in (BrokerFundFlow, IbkrActivityFlow, Holding, CorporateAction, Transaction):
        db.query(model).delete()
    db.commit()


def add_transaction(db, **overrides):
    values = {
        "user_id": 1,
        "symbol": "AAPL",
        "name": "Apple",
        "market": "美股",
        "transaction_type": "BUY",
        "quantity": Decimal("100"),
        "price": Decimal("10"),
        "fee": Decimal("0"),
        "transaction_date": date(2026, 1, 1),
        "currency": "USD",
    }
    values.update(overrides)
    transaction = Transaction(**values)
    db.add(transaction)
    db.flush()
    return transaction


def test_fifo_pnl_tracks_partial_lot_cost_and_remaining_cost():
    db = SessionLocal()
    reset_tables(db)
    try:
        add_transaction(db, quantity=Decimal("100"), price=Decimal("10"), fee=Decimal("1"))
        add_transaction(
            db,
            quantity=Decimal("50"),
            price=Decimal("12"),
            transaction_date=date(2026, 1, 2),
        )
        add_transaction(
            db,
            transaction_type="SELL",
            quantity=Decimal("120"),
            price=Decimal("15"),
            fee=Decimal("2"),
            transaction_date=date(2026, 1, 3),
        )
        db.commit()

        result = calculate_fifo_pnl_per_symbol(db, 1, "AAPL", "美股")

        assert result["sold_cost"] == 1241.0
        assert result["realized_pnl"] == 557.0
        assert result["current_holdings_cost"] == 360.0
        assert result["buy_queue"] == [
            {
                "price": 12.0,
                "quantity": 30.0,
                "total_cost": 360.0,
                "date": "2026-01-02",
            }
        ]
    finally:
        db.close()
