from datetime import date
from decimal import Decimal

import pytest

from app.database import SessionLocal
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.transaction import Transaction
from app.services.holding_service import calculate_realized_pnl, recalculate_holdings, validate_no_oversell
from app.services.statistics_service import calculate_account_total_return, calculate_fifo_pnl_per_symbol


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
    txn = Transaction(**values)
    db.add(txn)
    db.flush()
    return txn


def test_validate_no_oversell_rejects_excess_sell():
    db = SessionLocal()
    reset_tables(db)
    try:
        buy = add_transaction(db)
        sell = add_transaction(
            db,
            transaction_type="SELL",
            quantity=Decimal("150"),
            price=Decimal("12"),
            transaction_date=date(2026, 1, 2),
        )
        with pytest.raises(ValueError):
            validate_no_oversell([buy, sell])
    finally:
        db.close()


def test_recalculate_same_day_buy_before_sell():
    db = SessionLocal()
    reset_tables(db)
    try:
        add_transaction(
            db,
            transaction_type="SELL",
            quantity=Decimal("100"),
            price=Decimal("12"),
            transaction_date=date(2026, 1, 1),
        )
        add_transaction(
            db,
            transaction_type="BUY",
            quantity=Decimal("200"),
            price=Decimal("10"),
            transaction_date=date(2026, 1, 1),
        )
        db.commit()

        holding = recalculate_holdings(db, 1, "AAPL", "美股")

        assert holding.quantity == Decimal("100.00000000")
        assert holding.avg_cost == Decimal("10.00000000")
        assert holding.total_cost == Decimal("1000.00000000")
    finally:
        db.close()


def test_realized_pnl_ignores_oversell_rows_defensively():
    db = SessionLocal()
    reset_tables(db)
    try:
        add_transaction(db)
        add_transaction(
            db,
            transaction_type="SELL",
            quantity=Decimal("100"),
            price=Decimal("15"),
            transaction_date=date(2026, 1, 2),
        )
        add_transaction(
            db,
            transaction_type="SELL",
            quantity=Decimal("50"),
            price=Decimal("15"),
            transaction_date=date(2026, 1, 3),
        )
        db.commit()

        avg_cost_result = calculate_realized_pnl(db, 1, "AAPL", "美股")
        fifo_result = calculate_fifo_pnl_per_symbol(db, 1, "AAPL", "美股")

        assert avg_cost_result["capital_gain"] == 500.0
        assert fifo_result["realized_pnl"] == 500.0
    finally:
        db.close()


def test_cash_dividend_does_not_overwrite_holding_identity():
    db = SessionLocal()
    reset_tables(db)
    try:
        add_transaction(
            db,
            symbol="00883",
            name="中国海洋石油",
            market="港股",
            quantity=Decimal("1000"),
            price=Decimal("20"),
            transaction_date=date(2026, 1, 1),
            currency="HKD",
        )
        db.add(
            CorporateAction(
                user_id=1,
                symbol="00883",
                name="883(KYG...) 现金红利 HKD 0.02 每股",
                market="港股",
                action_type="CASH_DIVIDEND",
                ex_date=date(2026, 2, 1),
                total_dividend=Decimal("20"),
                net_dividend=Decimal("20"),
                currency="USD",
            )
        )
        db.commit()

        holding = recalculate_holdings(db, 1, "00883", "港股")

        assert holding.name == "中国海洋石油"
        assert holding.currency == "HKD"
    finally:
        db.close()


def test_account_total_return_combines_realized_unrealized_and_dividends():
    db = SessionLocal()
    reset_tables(db)
    try:
        add_transaction(
            db,
            symbol="600000",
            name="浦发银行",
            market="A股",
            quantity=Decimal("100"),
            price=Decimal("10"),
            transaction_date=date(2026, 1, 1),
            currency="CNY",
        )
        add_transaction(
            db,
            symbol="600000",
            name="浦发银行",
            market="A股",
            transaction_type="SELL",
            quantity=Decimal("40"),
            price=Decimal("15"),
            transaction_date=date(2026, 1, 10),
            currency="CNY",
        )
        db.add(
            CorporateAction(
                user_id=1,
                symbol="600000",
                name="浦发银行",
                market="A股",
                action_type="CASH_DIVIDEND",
                ex_date=date(2026, 1, 15),
                payment_date=date(2026, 1, 16),
                total_dividend=Decimal("30"),
                net_dividend=Decimal("30"),
                currency="CNY",
            )
        )
        db.commit()
        recalculate_holdings(db, 1, "600000", "A股")

        result = calculate_account_total_return(db, 1, {"600000": 12})

        assert result["realized_trading_pnl_cny"] == 200.0
        assert result["unrealized_pnl_cny"] == 120.0
        assert result["net_dividend_income_cny"] == 30.0
        assert result["total_return"] == 350.0
        assert result["current_market_value_cny"] == 720.0
        assert result["net_invested_principal_cny"] == 370.0
        assert round(result["total_return_rate"], 2) == 94.59
        assert result["annualized_return_rate"] is not None
    finally:
        db.close()
