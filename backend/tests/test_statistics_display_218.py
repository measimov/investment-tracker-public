"""#218 仪表盘/统计页展示修复的后端部分：持仓排行按标的合并、胜率无样本为 None、
快照聚合缺汇率与缺价警告去重。

这些改动都只改「展示形状」，不应改变任何既有指标数值——每个用例都同时断言
既有数值字段与改动前的口径逐字段一致。
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.cash_event import CashEvent
from app.models.corporate_action import CorporateAction
from app.models.exchange_rate import ExchangeRate
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.reconciliation_snapshot import ReconciliationSnapshot
from app.models.security_price import SecurityPrice
from app.models.transaction import Transaction
from app.services.holding_service import recalculate_holdings
from app.services.portfolio.metrics import calculate_trade_skill_metrics
from app.services.statistics import (
    build_portfolio_snapshot,
    calculate_performance_summary,
    get_holdings_cost_breakdown,
    get_summary_statistics,
)
from app.services.statistics.aggregates import UNPRICED_POSITIONS_WARNING
from tests.helpers import make_account, reset_tables

RESET_MODELS = (
    BrokerFundFlow,
    IbkrActivityFlow,
    ReconciliationSnapshot,
    CashEvent,
    SecurityPrice,
    Holding,
    CorporateAction,
    Transaction,
    BrokerAccount,
    ExchangeRate,
)


def _rate(db, currency, rate):
    db.add(ExchangeRate(
        from_currency=currency, to_currency="CNY", rate=Decimal(rate),
        effective_date=date(2020, 1, 1), is_active=True,
    ))


def _buy(db, account_id, symbol, market, quantity, price, currency, name=None):
    db.add(Transaction(
        user_id=1, broker_account_id=account_id, symbol=symbol, name=name or symbol,
        market=market, transaction_type="BUY", quantity=Decimal(quantity),
        price=Decimal(price), fee=Decimal("0"),
        transaction_date=date(2026, 1, 5), currency=currency,
    ))


# ---------------------------------------------------------------------------
# 持仓排行
# ---------------------------------------------------------------------------


def test_cost_breakdown_merges_accounts_and_sorts_by_cny_cost():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        _rate(db, "HKD", "0.9")
        a = make_account(db, "A")
        b = make_account(db, "B")
        # 600000 分在两个账户：合并成一行
        _buy(db, a.id, "600000", "A股", "100", "10", "CNY")
        _buy(db, b.id, "600000", "A股", "300", "12", "CNY")
        # 港股原币成本 HK$5,000（¥4,500）< A 股 ¥4,600，但原币数字更大：
        # 旧实现按原币排序会把它排第一
        _buy(db, a.id, "00700", "港股", "10", "500", "HKD")
        # 缺汇率：THB 原币成本最大，但无法折 CNY，排最后
        _buy(db, a.id, "PTT", "泰股", "1000", "100", "THB")
        db.commit()
        for symbol, market in (("600000", "A股"), ("00700", "港股"), ("PTT", "泰股")):
            recalculate_holdings(db, 1, symbol, market)

        holding_rows = db.query(Holding).filter(Holding.user_id == 1).all()
        assert len([h for h in holding_rows if h.symbol == "600000"]) == 2

        rows = get_holdings_cost_breakdown(db, 1)
        assert [row["symbol"] for row in rows] == ["600000", "00700", "PTT"]

        merged = rows[0]
        assert merged["account_count"] == 2
        assert merged["quantity"] == 400.0
        assert merged["total_cost"] == 4600.0
        assert merged["avg_cost"] == 11.5
        assert merged["total_cost_cny"] == 4600.0
        assert merged["missing_rate"] is False

        hk = rows[1]
        assert hk["account_count"] == 1
        assert hk["total_cost"] == 5000.0
        assert hk["total_cost_cny"] == 4500.0
        # 单账户行的原字段与 Holding 行逐字段相同（合并前的口径）
        hk_holding = next(h for h in holding_rows if h.symbol == "00700")
        assert hk["quantity"] == float(hk_holding.quantity)
        assert hk["avg_cost"] == float(hk_holding.avg_cost)
        assert hk["total_cost"] == float(hk_holding.total_cost)
        assert hk["currency"] == "HKD"

        thb = rows[2]
        assert thb["total_cost_cny"] is None
        assert thb["missing_rate"] is True

        # 排行的 CNY 成本与概览同口径：有汇率的行加总 == summary 的 CNY 总成本
        summary = get_summary_statistics(db, 1)
        priced_total = sum(row["total_cost_cny"] for row in rows if row["total_cost_cny"] is not None)
        assert round(priced_total, 2) == summary["total_invested_cny"]
        assert summary["missing_rate_currencies"] == ["THB"]
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


# ---------------------------------------------------------------------------
# 胜率
# ---------------------------------------------------------------------------


def test_win_rate_is_none_without_samples_and_unchanged_with_samples():
    empty = calculate_trade_skill_metrics({"closed_trades": []})
    assert empty["sample_count"] == 0
    assert empty["win_rate"] is None
    # 其余字段保持原口径
    assert empty["expectancy_cny"] == 0.0
    assert empty["average_win_cny"] == 0.0
    assert empty["payoff_ratio"] is None
    assert empty["profit_factor"] is None

    # 只有保本平仓（pnl == 0）同样算无有效样本
    flat = calculate_trade_skill_metrics(
        {"closed_trades": [{"realized_pnl_cny": 0, "matched_cost_cny": 100}]}
    )
    assert flat["sample_count"] == 0
    assert flat["win_rate"] is None

    mixed = calculate_trade_skill_metrics({
        "closed_trades": [
            {"realized_pnl_cny": 30, "matched_cost_cny": 100},
            {"realized_pnl_cny": -10, "matched_cost_cny": 100},
            {"realized_pnl_cny": 20, "matched_cost_cny": 100},
        ]
    })
    assert mixed["sample_count"] == 3
    assert round(mixed["win_rate"], 6) == round(200 / 3, 6)
    assert mixed["payoff_ratio"] == 2.5
    assert mixed["profit_factor"] == 5.0

    all_losing = calculate_trade_skill_metrics(
        {"closed_trades": [{"realized_pnl_cny": -10, "matched_cost_cny": 100}]}
    )
    # 有样本全亏仍是 0%，不能与「无样本」混为一谈
    assert all_losing["win_rate"] == 0.0


# ---------------------------------------------------------------------------
# 组合快照警告
# ---------------------------------------------------------------------------


def test_snapshot_aggregates_realized_and_dividend_missing_rates():
    """已实现/股息缺汇率时看板也要报（此前只转述持仓表现那一块）。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = make_account(db, "A")
        # 持仓：CNY，有价——持仓表现本身不缺汇率
        _buy(db, account.id, "600000", "A股", "100", "10", "CNY")
        # 已清仓的 THB 标的：已实现盈亏缺汇率
        _buy(db, account.id, "PTT", "泰股", "100", "10", "THB")
        db.add(Transaction(
            user_id=1, broker_account_id=account.id, symbol="PTT", name="PTT", market="泰股",
            transaction_type="SELL", quantity=Decimal("100"), price=Decimal("12"),
            fee=Decimal("0"), transaction_date=date(2026, 1, 8), currency="THB",
        ))
        # SGD 股息：股息汇总缺汇率
        db.add(CorporateAction(
            user_id=1, symbol="D05", name="DBS", market="新加坡股", action_type="CASH_DIVIDEND",
            ex_date=date(2026, 1, 9), payment_date=date(2026, 1, 9),
            total_dividend=Decimal("50"), tax_withheld=Decimal("0"),
            net_dividend=Decimal("50"), currency="SGD",
        ))
        db.commit()
        recalculate_holdings(db, 1, "600000", "A股")
        recalculate_holdings(db, 1, "PTT", "泰股")
        row = db.query(Holding).filter(Holding.symbol == "600000").one()
        row.current_price = Decimal("11")
        row.price_updated_at = datetime.now(timezone.utc)
        db.commit()

        snapshot = build_portfolio_snapshot(db, 1)
        quality = snapshot["data_quality"]
        assert quality["missing_rate_currencies"] == ["SGD", "THB"]
        rate_warnings = [w for w in quality["warnings"] if "对 CNY 的汇率" in w]
        assert len(rate_warnings) == 1
        assert "SGD" in rate_warnings[0] and "THB" in rate_warnings[0]

        # 数值不受影响：与直接计算的业绩摘要一致
        prices = snapshot["prices"]["map"]
        direct = calculate_performance_summary(db, 1, prices)
        assert snapshot["performance"]["realized_pnl"]["realized_pnl_cny"] == (
            direct["realized_pnl"]["realized_pnl_cny"]
        )
        assert snapshot["performance"]["account_return"]["total_return_cny"] == (
            direct["account_return"]["total_return_cny"]
        )
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_snapshot_reports_missing_prices_once_with_symbol_list():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = make_account(db, "A")
        _buy(db, account.id, "600000", "A股", "100", "10", "CNY")
        _buy(db, account.id, "NOPX", "A股", "100", "10", "CNY")
        db.commit()
        recalculate_holdings(db, 1, "600000", "A股")
        recalculate_holdings(db, 1, "NOPX", "A股")
        row = db.query(Holding).filter(Holding.symbol == "600000").one()
        row.current_price = Decimal("11")
        row.price_updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()

        snapshot = build_portfolio_snapshot(db, 1)
        # 持仓表现块仍带泛泛那条（统计页与 LLM 输入原样可见）
        current_warnings = snapshot["performance"]["current_performance"]["data_quality"][
            "warnings"
        ]
        assert UNPRICED_POSITIONS_WARNING in current_warnings
        # 看板只出列出具体标的的那一条
        warnings = snapshot["data_quality"]["warnings"]
        assert UNPRICED_POSITIONS_WARNING not in warnings
        price_warnings = [w for w in warnings if "估值价格" in w]
        assert len(price_warnings) == 1
        assert "NOPX" in price_warnings[0]
        assert snapshot["data_quality"]["missing_price_count"] == 1
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()
