"""基准内核手算向量：基点归一、前向填充、超额收益、回撤、降级。"""

from datetime import date
from decimal import Decimal

from app.services.portfolio.benchmark import (
    build_benchmark_series,
    calculate_benchmark_comparison,
)


def _d(day: int) -> date:
    return date(2026, 1, day)


def test_base_point_on_or_before_start():
    """基点取起点日或之前最近收盘：起点 1/5，基点应为 1/3 的 100。"""
    closes = {_d(3): Decimal("100"), _d(6): Decimal("110"), _d(7): Decimal("121")}
    series = build_benchmark_series(closes, [_d(5), _d(6), _d(7)])

    assert series["status"] == "ok"
    assert series["alignment"] == "on_or_before_start"
    assert series["base_date"] == "2026-01-03"
    # 1/5 前向填充基点值 → 0%；1/6 → +10%；1/7 → +21%
    assert [p["cumulative_return_rate"] for p in series["points"]] == [0.0, 10.0, 21.0]
    assert series["total_return_rate"] == 21.0


def test_first_available_fallback_with_gap():
    """起点前无数据：退化为区间内首个收盘作基点，标记缺口天数。"""
    closes = {_d(8): Decimal("200"), _d(9): Decimal("210")}
    series = build_benchmark_series(closes, [_d(5), _d(8), _d(9)])

    assert series["alignment"] == "first_available"
    assert series["alignment_gap_days"] == 3
    assert series["base_date"] == "2026-01-08"
    # 基点前的栅格日（1/5）无点；1/8 → 0%；1/9 → +5%
    assert [p["date"] for p in series["points"]] == ["2026-01-08", "2026-01-09"]
    assert series["points"][-1]["cumulative_return_rate"] == 5.0

    # [评审回归] 计量区间不一致：不得与全区间组合收益相减产出超额
    assert calculate_benchmark_comparison(20.0, series) is None


def test_forward_fill_on_index_closed_days():
    """指数休市日跟随组合栅格前向填充最近收盘。"""
    closes = {_d(5): Decimal("100"), _d(8): Decimal("120")}
    series = build_benchmark_series(closes, [_d(5), _d(6), _d(7), _d(8)])

    rates = {p["date"]: p["cumulative_return_rate"] for p in series["points"]}
    assert rates["2026-01-06"] == 0.0  # 填充 1/5 收盘
    assert rates["2026-01-07"] == 0.0
    assert rates["2026-01-08"] == 20.0


def test_benchmark_max_drawdown():
    """峰值因子法回撤：100→130→104 → 回撤 = 104/130−1 = −20%。"""
    closes = {_d(5): Decimal("100"), _d(6): Decimal("130"), _d(7): Decimal("104")}
    series = build_benchmark_series(closes, [_d(5), _d(6), _d(7)])

    assert series["max_drawdown_rate"] == float(
        (Decimal("104") / Decimal("130") - 1) * 100
    )


def test_no_data_paths():
    assert build_benchmark_series({}, [_d(5)]) == {"status": "no_data"}
    assert build_benchmark_series({_d(5): Decimal("100")}, []) == {"status": "no_data"}
    # 区间后才有数据 → 无可用点
    assert build_benchmark_series({_d(9): Decimal("100")}, [_d(5), _d(6)]) == {
        "status": "no_data"
    }
    # 非法基点（0 值）
    assert build_benchmark_series({_d(3): Decimal("0")}, [_d(5)]) == {"status": "no_data"}


def test_comparison_arithmetic_excess():
    """超额收益 = 组合终点收益 − 基准终点收益（算术差，百分点）。"""
    closes = {_d(3): Decimal("100"), _d(7): Decimal("105")}
    series = build_benchmark_series(closes, [_d(5), _d(7)])

    comparison = calculate_benchmark_comparison(8.2, series)
    assert comparison["benchmark_total_return_rate"] == 5.0
    assert comparison["excess_return_rate"] == 3.2
    assert comparison["excess_basis"] == "arithmetic_pp"
    assert comparison["beta"] is None

    assert calculate_benchmark_comparison(None, series) is None
    assert calculate_benchmark_comparison(8.2, {"status": "no_data"}) is None
