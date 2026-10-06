"""股息日期、手工税额和持仓币种的回归场景（#375 / #376）。"""

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from app.api.corporate_actions import _build_corporate_action_query
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.transaction import Transaction
from app.schemas.corporate_action import CashDividendCreate, CorporateActionCreate
from app.services.corporate_action_service import summarize_cash_dividends
from app.services.holding_service import recalculate_holdings
from app.services.portfolio.curve import corporate_action_curve_date
from app.services.portfolio.semantics import dividend_cash_date
from app.services.reconciliation_service import derive_account_cash_asof
from app.services.standard_import import import_standard_corporate_actions_dataframe
from tests.helpers import add_transaction, make_account, reset_tables

RESET_MODELS = (
    BrokerFundFlow,
    IbkrActivityFlow,
    Holding,
    CorporateAction,
    Transaction,
    BrokerAccount,
)


@pytest.fixture
def db():
    session = SessionLocal()
    reset_tables(session, RESET_MODELS)
    try:
        yield session
    finally:
        session.rollback()
        reset_tables(session, RESET_MODELS)
        session.close()


def test_cross_year_cash_dividend_list_count_summary_and_curve_use_payment_date(db):
    dividend = CorporateAction(
        user_id=1,
        symbol="600000",
        market="A股",
        action_type="CASH_DIVIDEND",
        ex_date=date(2025, 12, 30),
        payment_date=date(2026, 1, 5),
        total_dividend=Decimal("100"),
        tax_withheld=Decimal("10"),
        currency="CNY",
    )
    split = CorporateAction(
        user_id=1,
        symbol="600000",
        market="A股",
        action_type="STOCK_SPLIT",
        ex_date=date(2025, 12, 31),
        payment_date=date(2026, 1, 6),
        split_ratio="1:2",
    )
    fallback = CorporateAction(
        user_id=1,
        symbol="600000",
        market="A股",
        action_type="CASH_DIVIDEND",
        ex_date=date(2026, 1, 7),
        total_dividend=Decimal("50"),
        currency="CNY",
    )
    db.add_all([dividend, split, fallback])
    db.commit()
    january = _build_corporate_action_query(
        db, 1, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31)
    )
    assert january.count() == 2
    assert {row.id for row in january.all()} == {dividend.id, fallback.id}
    cash = summarize_cash_dividends(db, january.all())["cash_dividends"]
    assert cash["total_dividend"] == 150
    assert cash["net_dividend"] == 140
    december = _build_corporate_action_query(
        db, 1, start_date=date(2025, 12, 1), end_date=date(2025, 12, 31)
    )
    assert [row.id for row in december.all()] == [split.id]
    assert dividend_cash_date(dividend) == corporate_action_curve_date(dividend) == date(2026, 1, 5)
    assert corporate_action_curve_date(split) == date(2025, 12, 31)
    assert dividend_cash_date(fallback) == date(2026, 1, 7)


@pytest.mark.parametrize("explicit_tax,expected", [(None, "10"), ("0", "0"), ("7", "7")])
def test_standard_csv_tax_rate_only_derives_when_tax_is_missing(db, explicit_tax, expected):
    account = make_account(db, commit=True)
    row = {
        "symbol": "600000",
        "market": "A股",
        "action_type": "CASH_DIVIDEND",
        "receipt_confirmed": True,
        "payment_date": "2026-01-02",
        "ex_date": "2026-01-05",
        "total_dividend": "100",
        "tax_rate": "0.1",
    }
    if explicit_tax is not None:
        row["tax_withheld"] = explicit_tax
    import_standard_corporate_actions_dataframe(
        db, 1, pd.DataFrame([row]), broker_account_id=account.id
    )
    action = db.query(CorporateAction).one()
    assert action.tax_withheld == Decimal(expected)


def test_manual_and_shortcut_do_not_assume_a_tax_rate():
    base = dict(symbol="00700", market="港股", ex_date=date(2026, 1, 5), total_dividend="100")
    manual = CorporateActionCreate(**base, action_type="CASH_DIVIDEND", tax_withheld=None)
    shortcut = CashDividendCreate(**base, dividend_per_share="1").to_corporate_action()
    assert manual.tax_withheld is shortcut.tax_withheld is None
    assert manual.tax_rate is shortcut.tax_rate is None


@pytest.mark.parametrize(
    "market,symbol,currency",
    [
        ("港股", "00700", "HKD"),
        ("美股", "AAPL", "USD"),
        ("B股", "200001", "HKD"),
        ("B股", "900901", "USD"),
    ],
)
@pytest.mark.parametrize(
    "action_type,fields",
    [
        ("BONUS_ISSUE", {"distribution_ratio": "10:1"}),
        ("STOCK_SPLIT", {"split_ratio": "1:2"}),
    ],
)
def test_quantity_actions_preserve_trading_currency(
    db, market, symbol, currency, action_type, fields
):
    add_transaction(db, market=market, symbol=symbol, currency=currency)
    db.add(
        CorporateAction(
            user_id=1,
            symbol=symbol,
            market=market,
            action_type=action_type,
            ex_date=date(2026, 2, 1),
            currency="CNY",
            **fields,
        )
    )
    db.commit()
    holding = recalculate_holdings(db, 1, symbol, market)
    assert holding.currency == currency


@pytest.mark.parametrize("ambiguous", [False, True])
def test_unassigned_cash_dividend_uses_only_sole_holder_before_ex_date(db, ambiguous):
    first, second = make_account(db), make_account(db, broker="IBKR")
    add_transaction(db, broker_account_id=first.id, symbol="600000", market="A股", currency="CNY")
    add_transaction(
        db,
        broker_account_id=second.id,
        symbol="600000",
        market="A股",
        currency="CNY",
        transaction_date=date(2026, 1, 2) if ambiguous else date(2026, 2, 5),
    )
    db.add(
        CorporateAction(
            user_id=1,
            symbol="600000",
            market="A股",
            action_type="CASH_DIVIDEND",
            ex_date=date(2026, 2, 1),
            payment_date=date(2026, 2, 10),
            total_dividend=Decimal("100"),
            tax_withheld=Decimal("10"),
            currency="CNY",
        )
    )
    db.commit()
    assert derive_account_cash_asof(db, 1, first.id, date(2026, 2, 9))["CNY"] == -1000
    expected = -1000 if ambiguous else -910
    assert derive_account_cash_asof(db, 1, first.id, date(2026, 2, 10))["CNY"] == expected
    assert derive_account_cash_asof(db, 1, second.id, date(2026, 2, 10))["CNY"] == -1000
