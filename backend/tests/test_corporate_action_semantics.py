"""Issue #47: the three position replays must agree on corporate-action fields."""

from datetime import date
from decimal import Decimal

import pytest

from app.database import SessionLocal
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.transaction import Transaction
from app.schemas.corporate_action import CorporateActionCreate
from app.services.holding_service import recalculate_holdings
from app.services.statistics_service import _get_fifo_results_for_user
from tests.helpers import reset_tables


RESET_MODELS = (BrokerFundFlow, IbkrActivityFlow, Holding, CorporateAction, Transaction)


def _seed_position(db, **action_fields):
    db.add(Transaction(
        user_id=1, symbol="600000", name="浦发银行", market="A股",
        transaction_type="BUY", quantity=Decimal("100"), price=Decimal("10"),
        fee=Decimal("0"), transaction_date=date(2026, 1, 1), currency="CNY",
    ))
    db.add(CorporateAction(
        user_id=1, symbol="600000", name="浦发银行", market="A股",
        ex_date=date(2026, 1, 10), currency="CNY", **action_fields,
    ))
    db.commit()


def _replayed_quantities(db):
    holding = recalculate_holdings(db, 1, "600000", "A股")
    fifo = _get_fifo_results_for_user(db, 1, {("600000", "A股")})[("600000", "A股")]
    fifo_quantity = sum(Decimal(str(b["quantity"])) for b in fifo["buy_queue"])
    return Decimal(str(holding.quantity)), fifo_quantity


@pytest.mark.parametrize(
    "action_fields,expected",
    [
        # Bonus with only distribution_ratio (10 songs 3): 100 -> 130
        ({"action_type": "STOCK_DIVIDEND", "distribution_ratio": "10:3"}, Decimal("130")),
        # Bonus with only shares_received: 100 + 30 -> 130
        ({"action_type": "STOCK_DIVIDEND", "shares_received": Decimal("30")}, Decimal("130")),
        # Both provided: distribution_ratio wins (10:2 -> 120, not +30)
        (
            {
                "action_type": "STOCK_DIVIDEND",
                "distribution_ratio": "10:2",
                "shares_received": Decimal("30"),
            },
            Decimal("120"),
        ),
        # Split with only split_ratio 1:2 -> 200
        ({"action_type": "STOCK_SPLIT", "split_ratio": "1:2"}, Decimal("200")),
        # Split with only new_shares -> 200
        ({"action_type": "STOCK_SPLIT", "new_shares": Decimal("200")}, Decimal("200")),
        # Reverse split 10:1 -> 10
        ({"action_type": "REVERSE_SPLIT", "split_ratio": "10:1"}, Decimal("10")),
    ],
)
def test_holding_and_fifo_replays_agree(action_fields, expected):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        _seed_position(db, **action_fields)
        holding_qty, fifo_qty = _replayed_quantities(db)
        assert holding_qty == expected
        assert fifo_qty == expected
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_ttwr_curve_position_matches_holding_for_ratio_only_bonus():
    """A distribution_ratio-only bonus must not trigger a terminal mismatch."""
    from app.services.statistics_service import calculate_performance_analytics

    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        _seed_position(db, action_type="STOCK_DIVIDEND", distribution_ratio="10:3")
        recalculate_holdings(db, 1, "600000", "A股")

        analytics = calculate_performance_analytics(db, 1, {"600000": 10})

        assert analytics["data_quality"]["terminal_position_mismatches"] == []
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_schema_requires_a_quantity_field():
    base = dict(
        symbol="600000", market="A股", ex_date=date(2026, 1, 10), currency="CNY"
    )
    with pytest.raises(ValueError):
        CorporateActionCreate(action_type="STOCK_DIVIDEND", **base)
    with pytest.raises(ValueError):
        CorporateActionCreate(action_type="STOCK_SPLIT", **base)
    # Valid when one usable field is present.
    CorporateActionCreate(
        action_type="STOCK_DIVIDEND", distribution_ratio="10:3", **base
    )
    CorporateActionCreate(action_type="STOCK_SPLIT", new_shares=Decimal("200"), **base)
