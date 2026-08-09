from datetime import date
from decimal import Decimal

import pytest

from app.database import SessionLocal
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.transaction import Transaction
from app.services.holding_service import (
    recalculate_holdings,
    validate_no_oversell,
)
from app.services.statistics import (
    calculate_performance_summary,
    calculate_realized_pnl_fifo,
)
from app.services.statistics.fifo_results import fifo_results_for_user
from tests.helpers import add_transaction, reset_tables


RESET_MODELS = (BrokerFundFlow, IbkrActivityFlow, Holding, CorporateAction, Transaction)


def test_validate_no_oversell_rejects_excess_sell():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
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
    reset_tables(db, RESET_MODELS)
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
    reset_tables(db, RESET_MODELS)
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

        fifo_result = fifo_results_for_user(db, 1, {("AAPL", "美股")})[("AAPL", "美股")]
        realized_result = calculate_realized_pnl_fifo(db, 1)

        assert fifo_result["realized_pnl"] == 500.0
        assert len(fifo_result["invalid_sell_events"]) == 1
        invalid_event = fifo_result["invalid_sell_events"][0]
        assert invalid_event["date"] == "2026-01-03"
        assert invalid_event["symbol"] == "AAPL"
        assert invalid_event["market"] == "美股"
        assert invalid_event["sell_quantity"] == 50.0
        assert invalid_event["available_quantity"] == 0.0
        assert realized_result["data_quality"]["invalid_sell_event_count"] == 1
        assert realized_result["data_quality"]["invalid_sell_events"] == fifo_result[
            "invalid_sell_events"
        ]
        assert realized_result["data_quality"]["warnings"]
    finally:
        db.close()


def test_cash_dividend_does_not_overwrite_holding_identity():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
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
    reset_tables(db, RESET_MODELS)
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

        summary = calculate_performance_summary(db, 1, {"600000": 12})
        result = summary["account_return"]

        assert result["realized_trading_pnl_cny"] == 200.0
        assert result["unrealized_pnl_cny"] == 120.0
        assert result["net_dividend_income_cny"] == 30.0
        assert result["total_return"] == 350.0
        assert result["current_market_value_cny"] == 720.0
        assert result["net_invested_principal_cny"] == 370.0
        assert round(result["total_return_rate"], 2) == 94.59
        assert result["annualized_return_rate"] is not None
        assert summary["current_performance"]["unrealized_pnl_cny"] == 120.0
        assert summary["total_realized_return"]["total_realized_return"] == 230.0
    finally:
        db.close()
