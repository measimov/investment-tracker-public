"""已实现盈亏：同一标的的 currency 值不一致时不得重复累加。

calculate_realized_pnl_fifo 曾按 (symbol, market, currency) 三元组 distinct
取标的清单，却按 (symbol, market) 取 FIFO 结果——同一标的只要有两种 currency
取值，同一份 FIFO 队列就被迭代两次：已实现盈亏与 sold_cost 翻倍、
closed_trades 重复进而污染 trade_skill 的 sample_count / win_rate。

修复前实测（买入腿 CNY、卖出腿 HKD）：realized_pnl_cny=1000（应为 500）、
trades_detail 与 closed_trades 各 2 行（应各 1 行）。

同文件的 calculate_current_holdings_performance 早就注释并防住了同一个坑，
realized 路径漏掉了，这里补上回归网。

关于 NULL：迁移 20260807_0012（#144）把 transactions.currency 收紧为 NOT NULL
+ server_default，NULL 行在 DB 层已**不可能存在**——原先模拟"裸 SQL 修数留下
NULL"的两条用例随之删除，护栏从服务层的 `or "CNY"` 回退换成了数据库约束本身
（见 test_null_currency_is_rejected_by_schema）。
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.database import SessionLocal
from app.models.corporate_action import CorporateAction
from app.models.exchange_rate import ExchangeRate
from app.models.holding import Holding
from app.models.transaction import Transaction
from app.services.statistics import calculate_realized_pnl_fifo

from .helpers import add_transaction, reset_tables

RESET_MODELS = [Holding, CorporateAction, Transaction, ExchangeRate]


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        reset_tables(session, RESET_MODELS)
        yield session
    finally:
        reset_tables(session, RESET_MODELS)
        session.close()


def _seed_round_trip(db, *, buy_currency="CNY", sell_currency="CNY"):
    """一次完整买卖：买 100 @10、卖 100 @15 → 已实现盈亏 500、sold_cost 1000。"""
    add_transaction(
        db, symbol="600000", name="甲", market="A股", transaction_type="BUY",
        quantity=Decimal("100"), price=Decimal("10"),
        transaction_date=date(2026, 1, 2), currency=buy_currency,
    )
    add_transaction(
        db, symbol="600000", name="甲", market="A股", transaction_type="SELL",
        quantity=Decimal("100"), price=Decimal("15"),
        transaction_date=date(2026, 3, 2), currency=sell_currency,
    )
    db.commit()


def _seed_hkd_rate(db, rate="0.9"):
    db.add(ExchangeRate(
        from_currency="HKD", to_currency="CNY", rate=Decimal(rate),
        effective_date=date(2026, 1, 1), source="test", is_active=True,
    ))
    db.commit()


def test_mixed_currency_rows_do_not_double_count(db):
    """同一标的两条腿记了不同币种——修复前这会把整份 FIFO 结果算两遍。"""
    _seed_round_trip(db, buy_currency="CNY", sell_currency="HKD")
    # 必须显式给 HKD 汇率：缺汇率现在会被剔除（见下一条用例），
    # 否则这里测的就不是去重而是缺汇率兜底了。
    _seed_hkd_rate(db)

    result = calculate_realized_pnl_fifo(db, 1)

    # 修复前是 1000.0（同一份 FIFO 结果被算两遍）；currency 取时间序最后一个
    # 非空值 = HKD，故 500 HKD × 0.9 = 450
    assert result["realized_pnl_cny"] == pytest.approx(450.0)
    assert result["sold_cost_cny"] == pytest.approx(900.0)
    # 一个标的只能出现一行；重复累加时是 2
    assert len(result["trades_detail"]) == 1
    # 一次平仓只能产生一条 closed_trade；重复时是 2，会让 trade_skill 的
    # sample_count / win_rate 凭空翻倍
    assert len(result["closed_trades"]) == 1
    assert result["missing_rate_currencies"] == []


def test_missing_rate_is_excluded_and_reported_not_treated_as_cny(db):
    """缺汇率不得把外币原值混进 CNY 总额（此前是静默 1:1 混入）。"""
    _seed_round_trip(db, buy_currency="HKD", sell_currency="HKD")  # 不给 HKD 汇率

    result = calculate_realized_pnl_fifo(db, 1)

    # 旧行为：500 HKD 被当成 500 CNY 计入总额，且无任何提示
    assert result["realized_pnl_cny"] == pytest.approx(0.0)
    assert result["sold_cost_cny"] == pytest.approx(0.0)
    assert result["missing_rate_currencies"] == ["HKD"]
    warnings = result["data_quality"].get("warnings") or []
    assert any("HKD" in w for w in warnings), f"缺汇率必须进 warnings，实际：{warnings}"
    # 原币明细仍然保留，便于用户核对
    assert result["trades_detail"][0]["realized_pnl"] == pytest.approx(500.0)


def test_currency_takes_latest_non_null_value(db):
    """currency 是展示字段：取时间序最后一个非空值，与持仓重放逐事件覆盖同口径。"""
    _seed_round_trip(db, buy_currency="CNY", sell_currency="HKD")

    detail = calculate_realized_pnl_fifo(db, 1)["trades_detail"][0]

    assert detail["currency"] == "HKD"  # 卖出腿在后


def test_null_currency_is_rejected_by_schema(db):
    """currency 的 NULL 形态由 DB 约束直接拒绝（#144）。

    此前这里有两条用例用裸 SQL 把 currency 改成 NULL、测服务层的 CNY 回退；
    收紧 NOT NULL 后那个数据形态不可能存在，护栏升级为约束本身——裸 SQL 修数
    也写不进 NULL，服务层的回退成为纯防御。
    """
    from sqlalchemy.exc import IntegrityError

    _seed_round_trip(db)
    with pytest.raises(IntegrityError):
        db.execute(text("UPDATE transactions SET currency = NULL WHERE symbol = '600000'"))
        db.flush()
    db.rollback()


def test_single_currency_result_is_unchanged(db):
    """单币种（绝大多数情形）口径不得因去重改动而变化。"""
    _seed_round_trip(db)

    result = calculate_realized_pnl_fifo(db, 1)

    assert result["realized_pnl_cny"] == pytest.approx(500.0)
    assert result["sold_cost_cny"] == pytest.approx(1000.0)
    assert len(result["trades_detail"]) == 1
    assert len(result["closed_trades"]) == 1


def test_same_symbol_in_two_markets_stays_separate(db):
    """去重键是 (symbol, market)：同代码跨市场不得被并成一行。"""
    _seed_round_trip(db)
    add_transaction(
        db, symbol="600000", name="甲", market="港股", transaction_type="BUY",
        quantity=Decimal("10"), price=Decimal("10"),
        transaction_date=date(2026, 1, 2), currency="HKD",
    )
    add_transaction(
        db, symbol="600000", name="甲", market="港股", transaction_type="SELL",
        quantity=Decimal("10"), price=Decimal("20"),
        transaction_date=date(2026, 3, 2), currency="HKD",
    )
    db.add(ExchangeRate(
        from_currency="HKD", to_currency="CNY", rate=Decimal("0.9"),
        effective_date=date(2026, 1, 1), source="test", is_active=True,
    ))
    db.commit()

    result = calculate_realized_pnl_fifo(db, 1)

    assert len(result["trades_detail"]) == 2
    by_market = {row["market"]: row for row in result["trades_detail"]}
    assert by_market["A股"]["currency"] == "CNY"
    assert by_market["港股"]["currency"] == "HKD"
    # A股 500 + 港股 100×0.9 = 590
    assert result["realized_pnl_cny"] == pytest.approx(590.0)
