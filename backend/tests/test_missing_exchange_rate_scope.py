"""缺汇率口径：所有 CNY 汇总路径都必须剔除并报告，绝不按原值当人民币。

issue #129 统一了四处汇总，但 PR #148 复审发现**区间股息**（analytics 的
range_summary.dividend_net_cny）还留着一处旧兜底：100 THB 被当成 100 CNY
计入区间收益，且没有任何警告。这个文件把每条折算路径逐一钉死，避免再漏。
"""

from datetime import date
from decimal import Decimal

import pytest

from app.database import SessionLocal
from app.models.corporate_action import CorporateAction
from app.models.exchange_rate import ExchangeRate
from app.models.holding import Holding
from app.models.security_price import SecurityPrice
from app.models.transaction import Transaction
from app.services.statistics import (
    calculate_current_holdings_performance,
    calculate_performance_analytics,
    get_dividend_summary,
    get_statistics_by_market,
    get_summary_statistics,
)

from .helpers import add_transaction, reset_tables

RESET_MODELS = [SecurityPrice, Holding, CorporateAction, Transaction, ExchangeRate]


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        reset_tables(session, RESET_MODELS)
        yield session
    finally:
        reset_tables(session, RESET_MODELS)
        session.close()


def _thb_holding(db, *, with_transaction=False):
    """一只泰铢标的：THB 没有对 CNY 的汇率。

    with_transaction=True 时补一笔买入——持仓表现的数量来自 FIFO 队列，
    没有交易的话仓位是 0，市值断言就失去意义。
    """
    if with_transaction:
        add_transaction(
            db, symbol="PTT", name="泰国国家石油", market="美股",
            transaction_type="BUY", quantity=Decimal("100"), price=Decimal("10"),
            transaction_date=date(2026, 1, 5), currency="THB",
        )
    db.add(Holding(
        user_id=1, symbol="PTT", name="泰国国家石油", market="美股",
        quantity=Decimal("100"), avg_cost=Decimal("10"), total_cost=Decimal("1000"),
        currency="THB", current_price=Decimal("12"),
    ))
    db.commit()


def test_summary_statistics_excludes_and_reports(db):
    _thb_holding(db)

    result = get_summary_statistics(db, 1)

    # 旧行为：1000 THB 直接当成 1000 CNY
    assert result["total_invested_cny"] == 0.0
    assert result["missing_rate_currencies"] == ["THB"]
    # 原币明细仍保留，用户能看出这 1000 是 THB
    assert result["total_invested_by_currency"]["THB"] == 1000.0


def test_statistics_by_market_excludes_and_reports(db):
    _thb_holding(db)

    rows = get_statistics_by_market(db, 1)

    assert len(rows) == 1
    assert rows[0]["total_cost_cny"] == 0.0
    assert rows[0]["missing_rate_currencies"] == ["THB"]


def test_current_holdings_performance_excludes_and_reports(db):
    _thb_holding(db, with_transaction=True)

    result = calculate_current_holdings_performance(db, 1, {"PTT:美股": 12})

    assert result["current_market_value_cny"] == 0.0
    assert result["missing_rate_currencies"] == ["THB"]
    warnings = result["data_quality"].get("warnings") or []
    assert any("THB" in w for w in warnings), f"缺汇率必须进 warnings：{warnings}"
    # 原币市值仍在明细里
    assert result["holdings_detail"][0]["market_value"] == pytest.approx(1200.0)


def test_dividend_summary_excludes_and_reports(db):
    db.add(CorporateAction(
        user_id=1, symbol="PTT", name="泰国国家石油", market="美股",
        action_type="CASH_DIVIDEND", ex_date=date(2026, 3, 1),
        total_dividend=Decimal("100"), tax_withheld=Decimal("0"),
        net_dividend=Decimal("100"), currency="THB",
    ))
    db.commit()

    result = get_dividend_summary(db, 1)

    assert result["total_dividend_net_cny"] == 0.0
    # total_dividend_net 是 CNY 的向后兼容别名，同样为 0
    assert result["total_dividend_net"] == 0.0
    assert result["missing_rate_currencies"] == ["THB"]
    # 原币净额在 by_symbol 明细里保留，用户能看出这 100 是 THB
    assert result["by_symbol"][0]["total_net"] == pytest.approx(100.0)
    assert result["by_symbol"][0]["currency"] == "THB"


def test_range_dividend_excludes_and_reports(db):
    """PR #148 复审复现的那条：100 THB 曾被当成 100 CNY 计入区间收益。"""
    add_transaction(
        db, symbol="PTT", name="泰国国家石油", market="美股",
        transaction_type="BUY", quantity=Decimal("100"), price=Decimal("10"),
        transaction_date=date(2026, 1, 5), currency="THB",
    )
    db.add(CorporateAction(
        user_id=1, symbol="PTT", name="泰国国家石油", market="美股",
        action_type="CASH_DIVIDEND", ex_date=date(2026, 3, 1),
        payment_date=date(2026, 3, 1),
        total_dividend=Decimal("100"), tax_withheld=Decimal("0"),
        net_dividend=Decimal("100"), currency="THB",
    ))
    db.add(Holding(
        user_id=1, symbol="PTT", name="泰国国家石油", market="美股",
        quantity=Decimal("100"), avg_cost=Decimal("10"), total_cost=Decimal("1000"),
        currency="THB", current_price=Decimal("12"),
    ))
    db.commit()

    analytics = calculate_performance_analytics(
        db, 1, {"PTT:美股": 12},
        start_date=date(2026, 1, 1), end_date=date(2026, 6, 30),
    )

    # 旧行为：dividend_net_cny == 100.0（100 THB 当成 100 CNY），且无任何提示
    assert analytics["range_summary"]["dividend_net_cny"] == 0.0
    warnings = analytics["data_quality"].get("warnings") or []
    assert any("THB" in w for w in warnings), (
        f"区间股息缺汇率必须进 analytics warnings：{warnings}"
    )


def test_rates_present_means_no_warning(db):
    """有汇率时不得误报——守住正常路径。"""
    _thb_holding(db)
    db.add(ExchangeRate(
        from_currency="THB", to_currency="CNY", rate=Decimal("0.2"),
        effective_date=date(2026, 1, 1), source="test", is_active=True,
    ))
    db.commit()

    summary = get_summary_statistics(db, 1)
    performance = calculate_current_holdings_performance(db, 1, {"PTT:美股": 12})

    assert summary["total_invested_cny"] == pytest.approx(200.0)
    assert summary["missing_rate_currencies"] == []
    assert performance["missing_rate_currencies"] == []
    assert not any(
        "THB" in w for w in (performance["data_quality"].get("warnings") or [])
    )
