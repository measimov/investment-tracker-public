"""统计口径冻结快照（组合引擎抽取的行为保险丝）。

在把 statistics_service 的重放/FIFO/曲线/指标逻辑迁入 services/portfolio/
纯函数内核之前，先用一个覆盖多市场、多币种、四类公司行动的固定场景，
把所有统计入口的完整响应逐字段冻结下来。重构后的任何数值或结构变化都会
在这里失败。

重新生成基线（仅在有意变更口径时）:
    UPDATE_SNAPSHOTS=1 pytest tests/test_statistics_snapshot.py
"""

import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models.corporate_action import CorporateAction
from app.models.exchange_rate import ExchangeRate
from app.models.holding import Holding
from app.models.security_price import SecurityPrice
from app.models.transaction import Transaction
from app.services.holding_service import recalculate_holdings
from app.services.statistics_service import (
    calculate_performance_analytics,
    calculate_performance_summary,
    get_holdings_cost_breakdown,
    get_statistics_by_market,
    get_statistics_by_time,
    get_summary_statistics,
)

SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "statistics_baseline.json"

CURRENT_PRICES = {
    "AAPL:美股": 16.0,
    "0700:港股": 320.0,
    "600519:A股": 1550.0,
}


def _reset(db):
    for model in (SecurityPrice, Holding, CorporateAction, Transaction, ExchangeRate):
        db.query(model).delete()
    db.commit()


def _seed_scenario(db):
    """多市场 + 多币种 + 全部四类会改变持仓的公司行动 + 现金股息。"""
    rates = [
        ("USD", "CNY", "7.0", date(2025, 1, 1)),
        ("USD", "CNY", "7.2", date(2025, 6, 1)),
        ("HKD", "CNY", "0.9", date(2025, 1, 1)),
    ]
    for from_c, to_c, rate, effective in rates:
        db.add(ExchangeRate(
            from_currency=from_c,
            to_currency=to_c,
            rate=Decimal(rate),
            effective_date=effective,
            is_active=True,
            source="manual",
        ))

    transactions = [
        # AAPL(美股/USD): 两笔买入 → 1:2 拆股 → 卖出
        ("AAPL", "Apple", "美股", "BUY", "100", "10", "1", date(2025, 1, 10), "USD"),
        ("AAPL", "Apple", "美股", "BUY", "50", "12", "1", date(2025, 3, 1), "USD"),
        ("AAPL", "Apple", "美股", "SELL", "80", "15", "2", date(2025, 6, 10), "USD"),
        # 0700(港股/HKD): 买入 → 配股
        ("0700", "腾讯控股", "港股", "BUY", "200", "300", "10", date(2025, 2, 1), "HKD"),
        # 600519(A股/CNY): 买入 → 10:2 送股 → 卖出
        ("600519", "贵州茅台", "A股", "BUY", "10", "1500", "5", date(2025, 4, 1), "CNY"),
        ("600519", "贵州茅台", "A股", "SELL", "5", "1600", "5", date(2025, 8, 1), "CNY"),
    ]
    for symbol, name, market, txn_type, qty, price, fee, txn_date, currency in transactions:
        db.add(Transaction(
            user_id=1,
            symbol=symbol,
            name=name,
            market=market,
            transaction_type=txn_type,
            quantity=Decimal(qty),
            price=Decimal(price),
            fee=Decimal(fee),
            transaction_date=txn_date,
            currency=currency,
        ))

    db.add(CorporateAction(
        user_id=1, symbol="AAPL", name="Apple", market="美股",
        action_type="STOCK_SPLIT", ex_date=date(2025, 4, 15),
        split_ratio="1:2", currency="USD",
    ))
    db.add(CorporateAction(
        user_id=1, symbol="AAPL", name="Apple", market="美股",
        action_type="CASH_DIVIDEND", ex_date=date(2025, 5, 15),
        payment_date=date(2025, 5, 20),
        total_dividend=Decimal("50"), tax_withheld=Decimal("5"),
        net_dividend=Decimal("45"), currency="USD",
    ))
    db.add(CorporateAction(
        user_id=1, symbol="0700", name="腾讯控股", market="港股",
        action_type="RIGHTS_ISSUE", ex_date=date(2025, 3, 10),
        subscription_quantity=Decimal("20"), subscription_price=Decimal("250"),
        currency="HKD",
    ))
    db.add(CorporateAction(
        user_id=1, symbol="600519", name="贵州茅台", market="A股",
        action_type="BONUS_ISSUE", ex_date=date(2025, 5, 1),
        distribution_ratio="10:2", currency="CNY",
    ))

    price_history = {
        ("AAPL", "美股", "USD"): [
            (date(2025, 1, 31), "11"), (date(2025, 2, 28), "11.5"),
            (date(2025, 3, 31), "12.5"), (date(2025, 4, 30), "6.5"),
            (date(2025, 5, 30), "7"), (date(2025, 6, 30), "7.5"),
            (date(2025, 9, 30), "8"), (date(2025, 12, 31), "8.2"),
        ],
        ("0700", "港股", "HKD"): [
            (date(2025, 1, 31), "290"), (date(2025, 2, 28), "305"),
            (date(2025, 3, 31), "310"), (date(2025, 6, 30), "315"),
            (date(2025, 9, 30), "318"), (date(2025, 12, 31), "322"),
        ],
        ("600519", "A股", "CNY"): [
            (date(2025, 4, 30), "1520"), (date(2025, 5, 30), "1280"),
            (date(2025, 6, 30), "1300"), (date(2025, 9, 30), "1450"),
            (date(2025, 12, 31), "1560"),
        ],
    }
    for (symbol, market, currency), rows in price_history.items():
        for price_date, close in rows:
            db.add(SecurityPrice(
                symbol=symbol,
                market=market,
                price_date=price_date,
                close_price=Decimal(close),
                currency=currency,
                source="test",
            ))

    db.commit()

    for symbol, market in [("AAPL", "美股"), ("0700", "港股"), ("600519", "A股")]:
        recalculate_holdings(db, 1, symbol, market)


def _rounded(value, places=6):
    """递归把浮点数收敛到固定精度，消除跨平台的最后一位抖动。

    Decimal 也归一为 float：现网响应里 holdings_detail.current_price 是原生
    Decimal（FastAPI 序列化时才转数字），冻结时统一口径。
    """
    if isinstance(value, Decimal):
        return round(float(value), places)
    if isinstance(value, float):
        return round(value, places)
    if isinstance(value, dict):
        return {key: _rounded(item, places) for key, item in value.items()}
    if isinstance(value, list):
        return [_rounded(item, places) for item in value]
    return value


# 来自无 ORDER BY 查询/无序去重循环的列表：顺序无契约，冻结前按内容排序。
# 其余列表（curve 按日期升序、statistics_by_month/by_year 按 period、
# holdings_cost_breakdown 按成本降序）顺序即契约，保持原样让快照能捕获
# 排序回归。
_ORDER_INSENSITIVE_KEYS = {
    "statistics_by_market",
    "holdings_detail",
    "trades_detail",
    "closed_trades",
    "by_symbol",
    "unpriced_positions",
    "missing_price_history",
}


def _canonicalize(value, key=None):
    """仅对白名单路径的字典列表按内容排序，消除堆表行序抖动。"""
    if isinstance(value, dict):
        return {item_key: _canonicalize(item, item_key) for item_key, item in value.items()}
    if isinstance(value, list):
        items = [_canonicalize(item) for item in value]
        if (
            key in _ORDER_INSENSITIVE_KEYS
            and items
            and all(isinstance(item, dict) for item in items)
        ):
            items.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
        return items
    return value


def _normalize(snapshot):
    """剔除依赖 date.today() 的易变字段。"""
    account = snapshot["performance_summary"]["account_return"]
    account["annualized_return_rate"] = "<volatile:today-dependent>"
    return snapshot


def _build_snapshot(db):
    summary = calculate_performance_summary(db, 1, dict(CURRENT_PRICES))
    analytics = calculate_performance_analytics(
        db,
        1,
        dict(CURRENT_PRICES),
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
    )
    snapshot = {
        "summary_statistics": get_summary_statistics(db, 1),
        "statistics_by_market": get_statistics_by_market(db, 1),
        "statistics_by_month": get_statistics_by_time(db, 1, "month"),
        "statistics_by_year": get_statistics_by_time(db, 1, "year"),
        "holdings_cost_breakdown": get_holdings_cost_breakdown(db, 1),
        "performance_summary": summary,
        "performance_analytics": analytics,
    }
    return _canonicalize(_rounded(_normalize(snapshot)))


def test_statistics_outputs_match_frozen_baseline():
    db = SessionLocal()
    try:
        _reset(db)
        _seed_scenario(db)
        snapshot = _build_snapshot(db)
    finally:
        _reset(db)
        db.close()

    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        pytest.skip(f"Snapshot regenerated at {SNAPSHOT_PATH}")

    assert SNAPSHOT_PATH.exists(), (
        f"缺少基线文件 {SNAPSHOT_PATH}；先运行 UPDATE_SNAPSHOTS=1 pytest {__file__} 生成"
    )
    expected = json.loads(SNAPSHOT_PATH.read_text())
    assert snapshot == expected
