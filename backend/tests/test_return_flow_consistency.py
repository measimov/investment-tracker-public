"""#271：XIRR、对账现金与 TTWR 曲线对「外部投入」的口径一致。

此前两处 XIRR 只看买卖与股息：转托管转入（期初建仓）与配股的股份在期末市值里，投入却从没
出现在现金流里，年化被系统性高估；对账推导现金漏掉配股认购款，有配股的账户永远对不平。
"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.cash_event import CashEvent
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.transaction import Transaction
from app.services.holding_service import recalculate_holdings
from app.services.portfolio.curve import corporate_action_inflows
from app.services.portfolio.fx import ExchangeRateLookup
from app.services.reconciliation_service import derive_account_cash_asof
from app.services.statistics import (
    calculate_current_holdings_performance,
    calculate_performance_summary,
)
from tests.helpers import reset_tables

RESET_MODELS = (Holding, CashEvent, CorporateAction, Transaction, BrokerAccount)


def _Rates():
    """HKD→CNY 恒为 0.9 的内存汇率表（真实的日期感知 lookup）。"""
    return ExchangeRateLookup(
        [
            SimpleNamespace(
                from_currency="HKD",
                to_currency="CNY",
                effective_date=date(2020, 1, 1),
                rate=Decimal("0.9"),
            ),
        ]
    )


@pytest.fixture
def db():
    session = SessionLocal()
    reset_tables(session, RESET_MODELS)
    try:
        yield session
    finally:
        reset_tables(session, RESET_MODELS)
        session.close()


def _action(**fields):
    base = {"symbol": "600000", "market": "A股", "currency": "CNY", "ex_date": date(2026, 3, 1)}
    base.update(fields)
    return SimpleNamespace(
        **{
            "adjusted_quantity": None,
            "cost_basis_adjustment": None,
            "adjusted_cost_per_share": None,
            "subscription_quantity": None,
            "subscription_price": None,
            "subscription_amount": None,
            **base,
        }
    )


def test_corporate_action_inflows_known_cost_rights_and_unknown():
    actions = [
        _action(
            action_type="OPENING_POSITION",
            adjusted_quantity=Decimal("100"),
            cost_basis_adjustment=Decimal("1000"),
        ),
        _action(
            action_type="OPENING_POSITION",
            adjusted_quantity=Decimal("50"),
            ex_date=date(2026, 4, 1),
        ),  # 成本未知
        _action(
            action_type="RIGHTS_ISSUE",
            subscription_quantity=Decimal("20"),
            subscription_price=Decimal("5"),
            subscription_amount=Decimal("110"),
            currency="HKD",
            market="港股",
            ex_date=date(2026, 5, 1),
        ),
        _action(action_type="CASH_DIVIDEND"),
    ]
    flows, unknown = corporate_action_inflows(
        actions, rate_lookup=_Rates(), fallback_currency=lambda market: "CNY"
    )
    assert flows == [
        (date(2026, 3, 1), Decimal("-1000")),
        (date(2026, 5, 1), Decimal("-99.0")),
    ]  # 110 HKD × 0.9
    assert [(row["date"], row["quantity"]) for row in unknown] == [("2026-04-01", 50.0)]

    in_range, _ = corporate_action_inflows(
        actions,
        rate_lookup=_Rates(),
        fallback_currency=lambda market: "CNY",
        start_date=date(2026, 4, 15),
        end_date=date(2026, 12, 31),
    )
    assert [flow_date for flow_date, _ in in_range] == [date(2026, 5, 1)]


def test_account_xirr_counts_the_opening_position_as_invested(db):
    """转托管转入 100 股、成本 1000，现价 11：投入 1000、期末 1100。修复前 XIRR 现金流里
    根本没有这 1000 的投入。"""
    account = BrokerAccount(user_id=1, broker="招商证券", account_name="A", base_currency="CNY")
    db.add(account)
    db.flush()
    db.add(
        CorporateAction(
            user_id=1,
            broker_account_id=account.id,
            symbol="600000",
            name="浦发银行",
            market="A股",
            currency="CNY",
            action_type="OPENING_POSITION",
            ex_date=date(2025, 1, 2),
            adjusted_quantity=Decimal("100"),
            cost_basis_adjustment=Decimal("1000"),
        )
    )
    db.commit()
    recalculate_holdings(db, 1, "600000", "A股")

    account_return = calculate_performance_summary(db, 1, {"600000:A股": 11.0})["account_return"]
    assert account_return["cash_flow_count"] == 2  # 期初投入 + 期末市值
    assert account_return["peak_invested_principal_cny"] == 1000.0
    assert account_return["annualized_return_rate"] is not None
    assert account_return["annualized_return_rate"] < 20  # 约 +10% 分摊到 1.7 年以上


def test_unknown_cost_opening_is_noted_and_still_listed_when_unpriced(db):
    db.add(
        CorporateAction(
            user_id=1,
            symbol="600000",
            name="浦发银行",
            market="A股",
            currency="CNY",
            action_type="OPENING_POSITION",
            ex_date=date(2025, 1, 2),
            adjusted_quantity=Decimal("100"),
        )
    )
    db.commit()
    recalculate_holdings(db, 1, "600000", "A股")

    current = calculate_current_holdings_performance(db, 1, {})
    # 成本未知按 0 成本入队：此前按「成本 > 0」判定缺价，这只持仓会从缺价告警里消失
    assert [row["symbol"] for row in current["unpriced_positions"]] == ["600000"]

    notes = calculate_performance_summary(db, 1, {"600000:A股": 11.0})["account_return"][
        "methodology_notes"
    ]
    assert any("成本未知" in note for note in notes)

    # PR #301 评审：methodology_notes 前端不读，年化偏高要出现在看板与统计页的 data_quality 里
    from app.services.statistics import calculate_performance_analytics
    from app.services.statistics.snapshot import _performance_warnings

    performance = calculate_performance_summary(db, 1, {"600000:A股": 11.0})
    assert performance["account_return"]["xirr_unknown_cost_count"] == 1
    warnings, _ = _performance_warnings(performance, has_missing_price_list=False)
    assert any("年化收益偏高" in warning for warning in warnings)
    # analytics 没有任何交易时直接返回空响应：补一笔买入让曲线真正建起来。买入在期初建仓
    # 之前，区间起点（钳到首笔交易日）才早于建仓日，建仓作为区间内流入计入 XIRR
    db.add(
        Transaction(
            user_id=1,
            symbol="600000",
            name="浦发银行",
            market="A股",
            transaction_type="BUY",
            quantity=Decimal("10"),
            price=Decimal("10"),
            fee=Decimal("0"),
            transaction_date=date(2024, 12, 2),
            currency="CNY",
        )
    )
    db.commit()
    analytics = calculate_performance_analytics(
        db, 1, {"600000:A股": 11.0}, start_date=date(2024, 12, 1), end_date=date(2026, 9, 1)
    )
    assert any("区间年化收益偏高" in w for w in analytics["data_quality"]["warnings"])


def test_reconciliation_cash_deducts_rights_subscription(db):
    account = BrokerAccount(user_id=1, broker="招商证券", account_name="A", base_currency="CNY")
    db.add(account)
    db.flush()
    db.add(
        CashEvent(
            user_id=1,
            broker_account_id=account.id,
            event_type="DEPOSIT",
            amount=Decimal("1000"),
            currency="CNY",
            event_date=date(2026, 1, 1),
        )
    )
    db.add(
        CorporateAction(
            user_id=1,
            broker_account_id=account.id,
            symbol="600000",
            name="浦发银行",
            market="A股",
            currency="CNY",
            action_type="RIGHTS_ISSUE",
            ex_date=date(2026, 2, 1),
            subscription_quantity=Decimal("30"),
            subscription_price=Decimal("5"),
            subscription_amount=Decimal("152"),
        )
    )
    db.commit()

    assert derive_account_cash_asof(db, 1, account.id, date(2026, 1, 31)) == {
        "CNY": Decimal("1000")
    }
    assert derive_account_cash_asof(db, 1, account.id, date(2026, 3, 1)) == {"CNY": Decimal("848")}


def test_reconciliation_attributes_unassigned_rights_to_the_sole_holder(db):
    """PR #301 评审：未指定账户的配股，持仓重放归到唯一持仓桶；现金同口径扣认购款，
    否则持仓对得上、现金却多出这笔款，账户永远 MISMATCHED。多个账户同时持有则不计入。"""
    holder = BrokerAccount(user_id=1, broker="招商证券", account_name="A", base_currency="CNY")
    other = BrokerAccount(user_id=1, broker="招商证券", account_name="B", base_currency="CNY")
    db.add_all([holder, other])
    db.flush()
    db.add(
        Transaction(
            user_id=1,
            broker_account_id=holder.id,
            symbol="600000",
            name="浦发银行",
            market="A股",
            transaction_type="BUY",
            quantity=Decimal("100"),
            price=Decimal("10"),
            fee=Decimal("0"),
            transaction_date=date(2026, 1, 5),
            currency="CNY",
        )
    )
    db.add(
        CorporateAction(
            user_id=1,
            broker_account_id=None,
            symbol="600000",
            name="浦发银行",
            market="A股",
            currency="CNY",
            action_type="RIGHTS_ISSUE",
            ex_date=date(2026, 2, 1),
            subscription_quantity=Decimal("30"),
            subscription_price=Decimal("5"),
        )
    )
    db.commit()

    assert derive_account_cash_asof(db, 1, holder.id, date(2026, 3, 1)) == {
        "CNY": Decimal("-1150")
    }  # 买入 1000 + 认购 150
    assert derive_account_cash_asof(db, 1, other.id, date(2026, 3, 1)) == {}

    # 另一个账户在除权日前也持有：无法归属，两边都不扣
    db.add(
        Transaction(
            user_id=1,
            broker_account_id=other.id,
            symbol="600000",
            name="浦发银行",
            market="A股",
            transaction_type="BUY",
            quantity=Decimal("10"),
            price=Decimal("10"),
            fee=Decimal("0"),
            transaction_date=date(2026, 1, 6),
            currency="CNY",
        )
    )
    db.commit()
    assert derive_account_cash_asof(db, 1, holder.id, date(2026, 3, 1)) == {"CNY": Decimal("-1000")}
