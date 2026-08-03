"""LLM 报告输入压缩层：字段裁剪、数组封顶、估算标签逐字保留。"""

from datetime import date
from decimal import Decimal

from app.database import SessionLocal
from app.models.corporate_action import CorporateAction
from app.models.exchange_rate import ExchangeRate
from app.models.holding import Holding
from app.models.security_price import SecurityPrice
from app.models.transaction import Transaction
from app.services.llm_report_input import (
    CHAR_BUDGET,
    PRIMARY_CAPS,
    _compact,
    _month_end_downsample,
    build_llm_report_input,
    serialize_input,
)


def _fake_snapshot(holding_count: int) -> dict:
    holdings_detail = [
        {
            "symbol": f"6{i:05d}",
            "name": f"标的{i}",
            "market": "A股",
            "currency": "CNY",
            "quantity": 100,
            "current_price": 10.0,
            "holdings_cost": 900.0,
            "holdings_cost_cny": 900.0,
            "market_value": 1000.0 + i,
            "market_value_cny": 1000.0 + i,
            "unrealized_pnl": 100.0,
            "unrealized_pnl_cny": 100.0,
            "unrealized_pnl_rate": 11.1,
        }
        for i in range(holding_count)
    ]
    return {
        "generated_at": "2026-07-30T00:00:00+00:00",
        "base_currency": "CNY",
        "prices": {
            "map": {"600000:A股": 10.0},
            "sources": {"600000:A股": "holding"},
            "freshness": {"600000:A股": {"source": "holding", "stale": False}},
            "stale_keys": [f"STALE{i}" for i in range(30)],
            "missing_keys": ["MISS1"],
        },
        "performance": {
            "current_performance": {
                "current_market_value_cny": sum(h["market_value_cny"] for h in holdings_detail),
                "holdings_detail": holdings_detail,
            },
            "realized_pnl": {
                "realized_pnl_cny": 1234.5,
                "sold_cost_cny": 10000.0,
                "realized_pnl_rate": 12.3,
                "data_quality": {"invalid_sell_event_count": 0},
                "trades_detail": [
                    {"symbol": f"S{i}", "market": "A股", "realized_pnl_cny": float(i * 10),
                     "realized_pnl_rate": 1.0}
                    for i in range(25)
                ],
                "closed_trades": [{"symbol": "S1", "date": "2026-01-01"}] * 5,
            },
            "dividend_summary": {
                "total_dividend_gross_cny": 100.0,
                "total_tax_cny": 10.0,
                "total_dividend_net_cny": 90.0,
                "missing_rate_currencies": [],
                "by_symbol": [
                    {"symbol": f"D{i}", "name": f"分红{i}", "total_net_cny": float(i), "count": 1}
                    for i in range(15)
                ],
            },
            "account_return": {
                "total_return_cny": 999.0,
                "calculation_status": "exact",
                "methodology_notes": [
                    "权益仓口径：仅统计投入证券的资金，口径内精确。",
                ],
            },
        },
        "markets": [{"market": "A股", "total_cost_cny": 1.0, "holdings_count": holding_count}],
        "recent_transactions": [{"id": 1, "symbol": "600000"}],
        "accounts": [
            {
                "id": 1,
                "account_name": "招商",
                "broker": "招商证券",
                "base_currency": "CNY",
                "latest_reconciliation": {
                    "snapshot_date": "2026-07-27",
                    "status": "MISMATCHED",
                    "all_scoped": False,
                    "scopes": [{"statement_scope": None, "status": "MISMATCHED",
                                "compared_at": "2026-07-29T00:00:00"}],
                },
            }
        ],
        "data_quality": {"warnings": ["某警告"], "stale_price_count": 30, "missing_price_count": 1},
    }


def _fake_analytics() -> dict:
    return {
        "date_range": {"start_date": "2023-01-01", "end_date": "2026-07-30"},
        "calculation_level": "daily_price_history",
        "metrics": {"total_return_rate": 45.3, "annualization_basis": "calendar_days"},
        "trade_skill": {"status": "experimental", "win_rate": 46.3},
        "range_summary": {"status": "experimental", "realized_pnl_cny": 1.0},
        "methodology": {"status": "experimental", "return_method": "ttwr_proxy"},
        "data_quality": {"warnings": ["行情告警"]},
        "curve": [
            {"date": f"2026-{month:02d}-{day:02d}", "cumulative_return_rate": float(month)}
            for month in range(1, 7)
            for day in (5, 15, 28)
        ],
        "benchmarks": [
            {
                "code": "000300.SH",
                "name": "沪深300",
                "status": "ok",
                "fx_basis": "index_native",
                "return_basis": "price_index_excl_dividends",
                "total_return_rate": 5.0,
                "max_drawdown_rate": -8.4,
                "comparison": {
                    "benchmark_total_return_rate": 5.0,
                    "excess_return_rate": 40.3,
                    "excess_basis": "arithmetic_pp",
                    "benchmark_max_drawdown_rate": -8.4,
                    "beta": None,
                },
                "points": [
                    {"date": f"2026-{month:02d}-{day:02d}",
                     "cumulative_return_rate": float(month) / 2}
                    for month in range(1, 7)
                    for day in (5, 15, 28)
                ],
            },
            {"code": "SPX", "name": "标普500", "status": "no_data"},
        ],
    }


def test_compact_caps_arrays_and_preserves_estimate_labels():
    payload = _compact(_fake_snapshot(35), _fake_analytics(), [{"period": "2026-06"}] * 30,
                       PRIMARY_CAPS)

    # 持仓 cap 30 + 尾部合计行
    assert len(payload["holdings"]) == 31
    assert payload["holdings"][-1]["name"] == "其他5只合计"
    assert payload["holdings"][0]["weight_pct"] is not None

    # 明细数组被丢弃 / 封顶
    assert "trades_detail" not in serialize_input(payload)
    assert "closed_trades" not in serialize_input(payload)
    assert len(payload["realized_pnl"]["top_symbols"]) == 15
    assert len(payload["dividends"]["top_symbols"]) == 10
    assert len(payload["monthly"]) == 24

    # prices 冗余映射被丢弃，质量信号保留并封顶
    assert "map" not in serialize_input(payload["price_quality"])
    assert len(payload["price_quality"]["stale_keys"]) == 20
    assert payload["price_quality"]["stale_count"] == 30

    # 权益仓/实验标签逐字保留
    assert payload["account_return"]["calculation_status"] == "exact"
    assert "权益仓口径" in payload["account_return"]["methodology_notes"][0]
    assert payload["analytics"]["trade_skill"]["status"] == "experimental"
    assert payload["analytics"]["methodology"]["return_method"] == "ttwr_proxy"
    assert "estimate_semantics" in payload["meta"]

    # 对账红灯保留、compared_at 剔除
    rec = payload["accounts"][0]["latest_reconciliation"]
    assert rec["status"] == "MISMATCHED"
    assert "compared_at" not in serialize_input(rec)

    # 基准块：月末降采样封顶、comparison 原样保留、原始 points 不透传、
    # 语义字典含基准条目
    benchmarks = payload["analytics"]["benchmarks"]
    assert [b["code"] for b in benchmarks] == ["000300.SH", "SPX"]
    hs300 = benchmarks[0]
    assert hs300["comparison"]["excess_basis"] == "arithmetic_pp"
    assert len(hs300["curve_month_end"]) <= PRIMARY_CAPS["curve"]
    assert "points" not in serialize_input(hs300)
    assert benchmarks[1]["status"] == "no_data"
    assert "benchmark/基准" in payload["meta"]["estimate_semantics"]


def test_month_end_downsample_keeps_first_last_and_month_ends():
    curve = [
        {"date": f"2024-{m:02d}-{d:02d}", "cumulative_return_rate": float(m)}
        for m in range(1, 13)
        for d in range(1, 29)
    ]
    points = _month_end_downsample(curve, 36)
    assert points[0]["date"] == "2024-01-01"      # 首点
    assert points[-1]["date"] == "2024-12-28"     # 末点
    assert len(points) <= 13
    assert any(p["date"] == "2024-06-28" for p in points)  # 月末点

    # 800 点、超 cap 时均匀抽稀且首末保留
    long_curve = [
        {"date": f"20{20 + y}-{m:02d}-28", "cumulative_return_rate": 1.0}
        for y in range(5)
        for m in range(1, 13)
    ]
    points = _month_end_downsample(long_curve, 36)
    assert len(points) <= 36
    assert points[0]["date"] == long_curve[0]["date"]
    assert points[-1]["date"] == long_curve[-1]["date"]


def test_char_budget_triggers_secondary_shrink(monkeypatch):
    from app.services import llm_report_input as mod

    snapshot = _fake_snapshot(35)
    analytics = _fake_analytics()
    monkeypatch.setattr(mod, "build_portfolio_snapshot", lambda db, uid: snapshot)
    monkeypatch.setattr(
        mod, "calculate_performance_analytics", lambda db, uid, prices, **kw: analytics
    )
    monkeypatch.setattr(mod, "get_statistics_by_time", lambda db, uid, g: [{"period": "x"}] * 30)
    monkeypatch.setattr(mod, "CHAR_BUDGET", 1)  # 强制触发二级收缩

    payload = mod.build_llm_report_input(None, 1)
    assert len(payload["holdings"]) == 21  # 20 + 合计行
    assert len(payload["realized_pnl"]["top_symbols"]) == 10
    assert len(payload["monthly"]) == 12


def test_build_input_smoke_on_real_db():
    db = SessionLocal()
    for model in (SecurityPrice, Holding, CorporateAction, Transaction, ExchangeRate):
        db.query(model).delete()
    db.commit()
    try:
        db.add(Transaction(
            user_id=1, symbol="600000", name="冒烟标的", market="A股",
            transaction_type="BUY", quantity=Decimal("100"), price=Decimal("10"),
            fee=Decimal("0"), transaction_date=date(2026, 1, 5), currency="CNY",
        ))
        db.add(Holding(
            user_id=1, broker_account_id=None, symbol="600000", name="冒烟标的",
            market="A股", quantity=Decimal("100"), avg_cost=Decimal("10"),
            total_cost=Decimal("1000"), currency="CNY", current_price=Decimal("12"),
        ))
        db.commit()

        payload = build_llm_report_input(db, 1)
        # 必须是 JSON 原生类型：payload 要落 JSON 列，Decimal/date 残留会让
        # 报告行插入失败（实弹曾复现：LLM 调用成功但落库炸 Decimal）
        import json as json_module

        json_module.dumps(payload)  # 不带 default，任何非原生类型都会抛错
        assert set(payload) >= {
            "meta", "account_return", "holdings", "realized_pnl", "dividends",
            "price_quality", "markets", "accounts", "data_quality", "analytics", "monthly",
        }
        assert len(serialize_input(payload)) <= CHAR_BUDGET
        assert payload["holdings"][0]["symbol"] == "600000"
    finally:
        for model in (SecurityPrice, Holding, CorporateAction, Transaction, ExchangeRate):
            db.query(model).delete()
        db.commit()
        db.close()
