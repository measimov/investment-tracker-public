"""收益数字正确性逐个审计（阶段 3）：每个指标一组可手算的测试向量。

与冻结基线（test_statistics_snapshot）不同：基线防"变"，本文件证"对"——
所有期望值均为脱离实现、按定义手工推导的解析值。
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
from app.services.portfolio.fifo import calculate_fifo_pnl
from app.services.portfolio.metrics import calculate_risk_metrics, xirr
from app.services.statistics import (
    calculate_performance_analytics,
    calculate_performance_summary,
)
from tests.helpers import add_transaction, reset_tables


RESET_MODELS = (SecurityPrice, Holding, CorporateAction, Transaction, ExchangeRate)


def add_txn(db, **overrides):
    values = {"symbol": "600000", "name": "审计标的", "market": "A股", "currency": "CNY"}
    values.update(overrides)
    return add_transaction(db, **values)


def add_price(db, price_date, close, symbol="600000"):
    db.add(
        SecurityPrice(
            symbol=symbol,
            market="A股",
            ts_code=f"{symbol}.SH",
            price_date=price_date,
            currency="CNY",
            close_price=close,
            source="audit",
        )
    )


# ---------------------------------------------------------------------------
# 1. TTWR 链式收益：期中申购/赎回不得污染收益率（手算逐日核对）
# ---------------------------------------------------------------------------


def test_ttwr_chain_neutralizes_mid_period_flows():
    """D1 买 100@10；D2 涨到 11（+10%）；D3 再买 100@11 且收盘 12
    （日收益 = 2400/(1100+1100) − 1 = +9.0909%）；D4 卖 100@12、收盘 12
    （日收益 0）。累计 TTWR = 1.10 × 1.090909 − 1 = +20%，与出入金无关。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        add_txn(db, transaction_date=date(2026, 1, 1), quantity=Decimal("100"), price=Decimal("10"))
        add_txn(db, transaction_date=date(2026, 1, 3), quantity=Decimal("100"), price=Decimal("11"))
        add_txn(db, transaction_date=date(2026, 1, 4), transaction_type="SELL",
                quantity=Decimal("100"), price=Decimal("12"))
        for d, p in ((date(2026, 1, 1), Decimal("10")), (date(2026, 1, 2), Decimal("11")),
                     (date(2026, 1, 3), Decimal("12")), (date(2026, 1, 4), Decimal("12"))):
            add_price(db, d, p)
        db.commit()

        result = calculate_performance_analytics(
            db, 1, {"600000": 12},
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 4),
        )
        curve = result["curve"]
        by_date = {point["date"]: point for point in curve}

        assert by_date["2026-01-01"]["daily_return_rate"] == pytest.approx(0.0)
        assert by_date["2026-01-02"]["daily_return_rate"] == pytest.approx(10.0)
        assert by_date["2026-01-03"]["daily_return_rate"] == pytest.approx(100 * (2400 / 2200 - 1))
        assert by_date["2026-01-04"]["daily_return_rate"] == pytest.approx(0.0)
        # 链式累计：1.10 × (2400/2200) − 1 = 20%
        assert curve[-1]["cumulative_return_rate"] == pytest.approx(20.0)
        assert result["metrics"]["total_return_rate"] == pytest.approx(20.0)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 2. 风险指标：构造收益序列，全部期望值按定义解析推导
# ---------------------------------------------------------------------------


def _curve_point(day, daily_return, cumulative, drawdown):
    return {
        "date": f"2026-01-{day:02d}",
        "daily_return_rate": daily_return,
        "cumulative_return_rate": cumulative,
        "drawdown_rate": drawdown,
    }


def test_risk_metrics_match_hand_computed_definitions():
    """收益序列 [+1%, −2%, +3%, 0%]、跨度 4 天、rf=0：
    - 累计 = 1.01×0.98×1.03×1.00 − 1 = 1.949394%
    - 年化 = 1.01949394^(365.25/4) − 1
    - 样本波动率(n−1) = sqrt(0.0013/3)，年化因子 = sqrt(365.25×4/4)
    - 夏普 = 0.005/std × sqrt(365.25)
    - 索提诺分母按全样本 N：sqrt(0.0004/4) = 0.01
    - 卡玛 = 年化 / |maxDD|

    年化天数基准统一为 365.25（issue #138）：此前风险指标用 365、XIRR 用
    365.25，同一响应里两个"年化"基准不同且无字段说明——本文件下方的 XIRR
    用例（:153）当时就已经写着 365.25，两个基准在同一份手算文档里并存。
    """
    cumulative = [1.0, -1.02, 1.949394, 1.949394]
    curve = [
        _curve_point(1, None, 0.0, 0.0),
        _curve_point(2, 1.0, cumulative[0], 0.0),
        _curve_point(3, -2.0, cumulative[1], -2.0),
        _curve_point(4, 3.0, cumulative[2], 0.0),
        _curve_point(5, 0.0, cumulative[3], 0.0),
    ]
    metrics = calculate_risk_metrics(curve, Decimal("0"), "daily_price_history")

    mean = 0.005
    std = (0.0013 / 3) ** 0.5              # 样本方差（n−1）
    annual_factor = 365.25 ** 0.5          # periods/year = 365.25×4样本/4天 = 365.25
    assert metrics["observation_span_days"] == 4
    assert metrics["risk_sample_count"] == 4
    assert metrics["total_return_rate"] == pytest.approx(1.949394)
    assert metrics["annualization_days_basis"] == 365.25
    assert metrics["annualized_return_rate"] == pytest.approx(
        (1.01949394 ** (365.25 / 4) - 1) * 100, rel=1e-6
    )
    assert metrics["annualized_volatility"] == pytest.approx(std * annual_factor * 100, rel=1e-9)
    assert metrics["sharpe_ratio"] == pytest.approx(mean / std * annual_factor, rel=1e-9)
    # 索提诺：唯一下行样本 −2% → sqrt(0.0004/4)=0.01
    assert metrics["sortino_ratio"] == pytest.approx(mean / 0.01 * annual_factor, rel=1e-9)
    assert metrics["max_drawdown_rate"] == pytest.approx(-2.0)
    assert metrics["calmar_ratio"] == pytest.approx(
        ((1.01949394 ** (365.25 / 4) - 1)) / 0.02, rel=1e-6
    )


# ---------------------------------------------------------------------------
# 3. XIRR：解析可验的单流入/流出
# ---------------------------------------------------------------------------


def test_xirr_matches_analytic_solution():
    # 1 年（365 天，除以 365.25 年化）：-1000 → +1100 ≈ +10%
    rate = xirr([(date(2025, 1, 1), Decimal("-1000")), (date(2026, 1, 1), Decimal("1100"))])
    assert float(rate) == pytest.approx(0.10, abs=2e-4)
    # 零收益
    rate = xirr([(date(2025, 1, 1), Decimal("-1000")), (date(2026, 1, 1), Decimal("1000"))])
    assert float(rate) == pytest.approx(0.0, abs=1e-6)
    # 两年翻倍（2024 闰年共 731 天，年化基 365.25）：r = 2^(365.25/731) − 1
    rate = xirr([(date(2024, 1, 1), Decimal("-1000")), (date(2026, 1, 1), Decimal("2000"))])
    assert float(rate) == pytest.approx(2 ** (365.25 / 731) - 1, abs=2e-4)


# ---------------------------------------------------------------------------
# 4. FIFO 已实现盈亏：费用进成本/出净额（手算）
# ---------------------------------------------------------------------------


def test_fifo_realized_pnl_includes_fees_hand_computed():
    """买 100@10 费 5（成本 1005，每股 10.05）；卖 50@12 费 3：
    卖出净额 = 600 − 3 = 597；匹配成本 = 50 × 10.05 = 502.5；
    已实现 = 94.5；剩余持仓成本 = 502.5。"""
    from types import SimpleNamespace

    transactions = [
        SimpleNamespace(id=1, transaction_type="BUY", transaction_date=date(2026, 1, 1),
                        quantity=Decimal("100"), price=Decimal("10"), fee=Decimal("5")),
        SimpleNamespace(id=2, transaction_type="SELL", transaction_date=date(2026, 1, 5),
                        quantity=Decimal("50"), price=Decimal("12"), fee=Decimal("3")),
    ]
    result = calculate_fifo_pnl("600000", "A股", transactions, [])
    assert float(result["realized_pnl"]) == pytest.approx(94.5)
    assert float(result["sold_cost"]) == pytest.approx(502.5)
    assert float(result["current_holdings_cost"]) == pytest.approx(502.5)
    trade = result["closed_trades"][0]
    assert trade["proceeds"] == pytest.approx(597.0)
    assert trade["matched_cost"] == pytest.approx(502.5)
    assert trade["realized_pnl"] == pytest.approx(94.5)
    assert result["invalid_sell_events"] == []


# ---------------------------------------------------------------------------
# 5. 证券持仓收益（performance-summary）：总收益构成与本金口径
# ---------------------------------------------------------------------------


def test_performance_summary_composition_hand_computed():
    """买 100@10（无费用），现价 12，税后股息 30：
    已实现 0 + 未实现 200 + 股息 30 = 总收益 230；
    净投入本金 = 市值 1200 − 总收益 230 = 970（= 1000 投入 − 30 股息回流）；
    总收益率 = 230/970。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        add_txn(db)
        db.add(CorporateAction(
            user_id=1, symbol="600000", name="审计标的", market="A股",
            action_type="CASH_DIVIDEND", ex_date=date(2026, 2, 1),
            payment_date=date(2026, 2, 1), total_dividend=Decimal("30"),
            tax_withheld=Decimal("0"), net_dividend=Decimal("30"), currency="CNY",
        ))
        db.add(Holding(
            user_id=1, broker_account_id=None, symbol="600000", name="审计标的",
            market="A股", quantity=Decimal("100"), avg_cost=Decimal("10"),
            total_cost=Decimal("1000"), currency="CNY",
        ))
        db.commit()

        summary = calculate_performance_summary(db, 1, {"600000:A股": 12})
        account = summary["account_return"]
        assert account["realized_trading_pnl_cny"] == pytest.approx(0.0)
        assert account["unrealized_pnl_cny"] == pytest.approx(200.0)
        assert account["net_dividend_income_cny"] == pytest.approx(30.0)
        assert account["total_return_cny"] == pytest.approx(230.0)
        assert account["net_invested_principal_cny"] == pytest.approx(970.0)
        assert account["total_return_rate"] == pytest.approx(230 / 970 * 100)

        dividends = summary["dividend_summary"]
        assert dividends["total_dividend_net_cny"] == pytest.approx(30.0)

        current = summary["current_performance"]
        assert current["current_market_value_cny"] == pytest.approx(1200.0)
        assert current["unrealized_pnl_cny"] == pytest.approx(200.0)
        assert current["unrealized_pnl_rate"] == pytest.approx(20.0)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 6. 股息摘要：税前/税后/税额口径
# ---------------------------------------------------------------------------


def test_dividend_summary_gross_tax_net_hand_computed():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        add_txn(db)
        db.add(CorporateAction(
            user_id=1, symbol="600000", name="审计标的", market="A股",
            action_type="CASH_DIVIDEND", ex_date=date(2026, 3, 1),
            total_dividend=Decimal("100"), tax_withheld=Decimal("20"),
            net_dividend=None, currency="CNY",  # net 缺省 → gross − tax
        ))
        db.commit()

        summary = calculate_performance_summary(db, 1, {"600000:A股": 10})
        dividends = summary["dividend_summary"]
        assert dividends["total_dividend_gross_cny"] == pytest.approx(100.0)
        assert dividends["total_tax_cny"] == pytest.approx(20.0)
        assert dividends["total_dividend_net_cny"] == pytest.approx(80.0)
    finally:
        db.close()
