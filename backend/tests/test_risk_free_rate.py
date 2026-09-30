"""#200 无风险利率：SHIBOR 3M 日序列进夏普/索提诺，常量口径逐字节不变。

解析层用真实响应裁剪的金样（中国货币网 ShiborHis、美国财政部国库券 XML）；
内核只验证计息口径；同步编排对外呼打桩。
"""

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models.reference_rate import ReferenceRate
from app.services import chinamoney_source, reference_rate_service, treasury_source
from app.services.chinamoney_source import ChinamoneyError
from app.services.portfolio.metrics import calculate_risk_metrics, forward_filled_rates

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_shibor_history_takes_term_and_sorts():
    payload = json.loads((FIXTURES / "chinamoney" / "shibor_hist.json").read_text())
    points = chinamoney_source.parse_shibor_history(payload, "3M")
    assert points[0] == (date(2026, 9, 20), Decimal("1.4310"))  # 周日调休工作日照常发布
    assert points[-1] == (date(2026, 9, 24), Decimal("1.4300"))
    assert [p[0] for p in points] == sorted(p[0] for p in points)
    on = chinamoney_source.parse_shibor_history(payload, "ON")
    assert on[-1] == (date(2026, 9, 24), Decimal("1.3640"))


def test_parse_shibor_history_rejects_refusal_and_unknown_term():
    payload = {
        "head": {"rep_code": "200"},
        "data": {"message": "只提供一年历史数据查询及下载"},
        "records": [],
    }
    with pytest.raises(ChinamoneyError):
        chinamoney_source.parse_shibor_history(payload)
    with pytest.raises(ValueError):
        chinamoney_source.parse_shibor_history(payload, "5Y")


def test_parse_treasury_bill_rates_13wk_yield():
    xml = (FIXTURES / "treasury" / "bill_rates_202609.xml").read_text()
    assert treasury_source.parse_bill_rates(xml) == [
        (date(2026, 9, 23), Decimal("4.14")),
        (date(2026, 9, 24), Decimal("4.18")),
        (date(2026, 9, 25), Decimal("4.18")),
    ]
    with pytest.raises(treasury_source.TreasuryError):
        treasury_source.parse_bill_rates("<html>maintenance</html>")


def test_forward_filled_rates():
    points = [(date(2026, 1, 5), Decimal("1.5")), (date(2026, 1, 2), Decimal("1.4"))]
    assert forward_filled_rates(
        points, [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 4), date(2026, 1, 9), None]
    ) == [None, Decimal("1.4"), Decimal("1.4"), Decimal("1.5"), None]


def _curve(returns, gaps):
    """规则或不规则日期网格上的曲线：首点无收益，其后每点一个日收益率（%）。"""
    day = date(2026, 1, 1)
    curve = [
        {
            "date": day.isoformat(),
            "daily_return_rate": None,
            "cumulative_return_rate": 0,
            "drawdown_rate": 0,
        }
    ]
    cumulative = Decimal("1")
    for ret, gap in zip(returns, gaps):
        day += timedelta(days=gap)
        cumulative *= 1 + Decimal(str(ret)) / 100
        curve.append(
            {
                "date": day.isoformat(),
                "daily_return_rate": ret,
                "cumulative_return_rate": float((cumulative - 1) * 100),
                "drawdown_rate": 0,
            }
        )
    return curve


RETURNS = [0.8, -0.5, 1.2, -0.9, 0.3, 0.6, -1.1, 0.4, 0.9, -0.2]


def test_constant_series_on_regular_grid_equals_constant_rate():
    curve = _curve(RETURNS, [1] * len(RETURNS))
    constant = calculate_risk_metrics(curve, Decimal("2"), "daily_price_history")
    series = calculate_risk_metrics(
        curve,
        Decimal("0"),
        "daily_price_history",
        risk_free_points=[(date(2025, 12, 31), Decimal("2"))],
    )
    assert constant["risk_free_basis"] == "constant" and series["risk_free_basis"] == "series"
    assert series["risk_free_rate"] == 2.0
    assert series["sharpe_ratio"] == pytest.approx(constant["sharpe_ratio"], rel=1e-12)
    assert series["sortino_ratio"] == pytest.approx(constant["sortino_ratio"], rel=1e-12)
    assert series["risk_free_missing_points"] == 0


def test_series_accrues_by_actual_gap_and_reports_missing_points():
    # 不规则网格：长间隔的点应当多扣无风险收益 → 夏普低于按平均频率摊的常量口径
    gaps = [1, 1, 1, 7, 1, 1, 1, 1, 7, 1]
    curve = _curve(RETURNS, gaps)
    series = calculate_risk_metrics(
        curve,
        Decimal("0"),
        "daily_price_history",
        risk_free_points=[(date(2026, 1, 6), Decimal("3"))],  # 前 3 个收益点（1/2–1/4）早于序列首值
    )
    assert series["risk_free_missing_points"] == 3
    assert series["risk_free_rate"] == 3.0  # 只对有值的点求均值
    zero = calculate_risk_metrics(curve, Decimal("0"), "daily_price_history")
    assert series["sharpe_ratio"] < zero["sharpe_ratio"]


def test_constant_path_output_is_unchanged_apart_from_basis_field():
    curve = _curve(RETURNS, [1] * len(RETURNS))
    metrics = calculate_risk_metrics(curve, Decimal("0"), "daily_price_history")
    assert "risk_free_missing_points" not in metrics
    assert metrics["risk_free_rate"] == 0.0


@pytest.fixture
def db():
    session = SessionLocal()
    session.query(ReferenceRate).delete()
    session.commit()
    yield session
    session.rollback()
    session.query(ReferenceRate).delete()
    session.commit()
    session.close()


def test_sync_series_backfills_head_then_tail_and_is_idempotent(db, monkeypatch):
    calls = []

    def fake_fetch(start, end):
        calls.append((start, end))
        return [
            (start + timedelta(days=i), Decimal("1.5"))
            for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).weekday() < 5
        ]

    spec = reference_rate_service.SERIES["SHIBOR_3M"]
    monkeypatch.setitem(
        reference_rate_service.SERIES,
        "SHIBOR_3M",
        reference_rate_service.SeriesSpec(spec.label, spec.currency, spec.source, fake_fetch),
    )
    today = date(2026, 9, 28)
    first = reference_rate_service.sync_series(db, "SHIBOR_3M", start=date(2026, 9, 1), today=today)
    assert first["error"] is None and calls == [(date(2026, 9, 1), today)]
    assert first["written"] == 20

    # 目标起点提前 → 补前段 + 补尾（尾部重复值不改写）
    calls.clear()
    second = reference_rate_service.sync_series(
        db, "SHIBOR_3M", start=date(2026, 8, 25), today=today
    )
    assert calls == [(date(2026, 8, 25), date(2026, 8, 31)), (date(2026, 9, 18), today)]
    assert second["written"] == 5

    points = reference_rate_service.load_points(
        db, "SHIBOR_3M", date(2026, 9, 6), date(2026, 9, 10)
    )
    # 区间首日（周日）之前最近一个发布值一并带上，供向前填充
    assert points[0][0] == date(2026, 9, 4)
    assert [p[0] for p in points[1:]] == [
        date(2026, 9, 7),
        date(2026, 9, 8),
        date(2026, 9, 9),
        date(2026, 9, 10),
    ]


def test_sync_series_failure_is_reported_not_raised(db, monkeypatch):
    def boom(start, end):
        raise ChinamoneyError("系统繁忙")

    spec = reference_rate_service.SERIES["SHIBOR_3M"]
    monkeypatch.setitem(
        reference_rate_service.SERIES,
        "SHIBOR_3M",
        reference_rate_service.SeriesSpec(spec.label, spec.currency, spec.source, boom),
    )
    outcome = reference_rate_service.sync_series(
        db, "SHIBOR_3M", start=date(2026, 9, 1), today=date(2026, 9, 28)
    )
    assert outcome["written"] == 0 and "系统繁忙" in outcome["error"]


def test_analytics_defaults_to_series_and_explicit_rate_overrides(db):
    from app.services.statistics import calculate_performance_analytics
    from tests.test_statistics_snapshot import CURRENT_PRICES, _reset, _seed_scenario

    try:
        _reset(db)
        _seed_scenario(db)
        reference_rate_service.upsert_points(
            db,
            "SHIBOR_3M",
            "cfets-shibor",
            [(date(2024, 12, 31), Decimal("1.6")), (date(2025, 6, 2), Decimal("1.4"))],
        )
        kwargs = dict(start_date=date(2025, 1, 1), end_date=date(2025, 12, 31))
        default = calculate_performance_analytics(db, 1, CURRENT_PRICES, **kwargs)
        assert default["risk_free"]["basis"] == "series"
        assert default["risk_free"]["label"] == "SHIBOR 3M"
        assert default["risk_free"]["published_points"] == 1  # 区间内只有 6/2；12/31 是前值
        assert 1.4 <= default["risk_free"]["average"] <= 1.6
        assert default["metrics"]["risk_free_basis"] == "series"

        explicit = calculate_performance_analytics(
            db, 1, CURRENT_PRICES, risk_free_rate=Decimal("0"), **kwargs
        )
        assert explicit["risk_free"]["basis"] == "constant"
        assert explicit["metrics"]["risk_free_rate"] == 0.0
        if explicit["metrics"]["sharpe_ratio"] is not None:
            assert default["metrics"]["sharpe_ratio"] < explicit["metrics"]["sharpe_ratio"]
    finally:
        _reset(db)
