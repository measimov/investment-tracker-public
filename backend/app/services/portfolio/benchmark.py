"""基准指数序列与超额收益（纯内核，无 DB 依赖）。

对比的是收益率曲线而非金额：基准收益率直接用指数点位原币计算、不折汇
（fx_basis="index_native"），且价格指数不含股息（return_basis 自述）。
调用方负责从存储加载指数收盘价并传入组合曲线的日期栅格。
"""

from datetime import date
from decimal import Decimal
from typing import Any, Dict, List


def build_benchmark_series(
    index_closes: Dict[date, Decimal],
    curve_dates: List[date],
) -> Dict[str, Any]:
    """基准累计收益序列：起点归一 + 逐点前向填充对齐组合曲线栅格。

    基点 = 曲线起点日或之前最近的指数收盘（alignment="on_or_before_start"）；
    起点前完全无数据时退化为区间内首个可得收盘作基点并标记
    （alignment="first_available"，附首段缺口天数）。指数休市日跟随组合
    栅格前向填充。完全无可用数据返回 {"status": "no_data"}。
    """
    if not index_closes or not curve_dates:
        return {"status": "no_data"}

    sorted_days = sorted(index_closes)
    range_start, range_end = curve_dates[0], curve_dates[-1]

    on_or_before = [day for day in sorted_days if day <= range_start]
    if on_or_before:
        base_date = on_or_before[-1]
        alignment = "on_or_before_start"
        gap_days = 0
    else:
        in_range = [day for day in sorted_days if day <= range_end]
        if not in_range:
            return {"status": "no_data"}
        base_date = in_range[0]
        alignment = "first_available"
        gap_days = (base_date - range_start).days

    base_close = Decimal(str(index_closes[base_date]))
    if base_close <= 0:
        return {"status": "no_data"}

    points: List[Dict[str, Any]] = []
    cursor = 0
    last_close: Decimal | None = None
    peak_factor = Decimal("0")
    max_drawdown = Decimal("0")
    for day in curve_dates:
        while cursor < len(sorted_days) and sorted_days[cursor] <= day:
            last_close = Decimal(str(index_closes[sorted_days[cursor]]))
            cursor += 1
        if last_close is None or day < base_date:
            continue  # first_available：基点前的栅格日无基准可言
        factor = last_close / base_close
        points.append({
            "date": day.isoformat(),
            "cumulative_return_rate": float((factor - Decimal("1")) * 100),
        })
        if factor > peak_factor:
            peak_factor = factor
        elif peak_factor > 0:
            drawdown = factor / peak_factor - Decimal("1")
            if drawdown < max_drawdown:
                max_drawdown = drawdown

    if not points:
        return {"status": "no_data"}

    return {
        "status": "ok",
        "alignment": alignment,
        "alignment_gap_days": gap_days,
        "base_date": base_date.isoformat(),
        "base_close": float(base_close),
        "fx_basis": "index_native",
        "return_basis": "price_index_excl_dividends",
        "points": points,
        "total_return_rate": points[-1]["cumulative_return_rate"],
        "max_drawdown_rate": float(max_drawdown * 100),
    }


def calculate_benchmark_comparison(
    portfolio_total_return_rate: float | None,
    benchmark_series: Dict[str, Any],
) -> Dict[str, Any] | None:
    """终点超额收益（算术差，百分点）。beta 预留后置（experimental）。

    仅在 alignment="on_or_before_start" 时计算：first_available 表示基准
    数据晚于组合区间起点，两条收益的计量区间不同，直接相减会把"组合多跑
    的头部区间"算进超额，财务含义错误——此时不产出 comparison，由调用方
    以 alignment 提示降级原因。
    """
    if benchmark_series.get("status") != "ok" or portfolio_total_return_rate is None:
        return None
    if benchmark_series.get("alignment") != "on_or_before_start":
        return None
    benchmark_rate = benchmark_series["total_return_rate"]
    return {
        "benchmark_total_return_rate": benchmark_rate,
        "excess_return_rate": round(float(portfolio_total_return_rate) - benchmark_rate, 4),
        "excess_basis": "arithmetic_pp",
        "benchmark_max_drawdown_rate": benchmark_series["max_drawdown_rate"],
        "beta": None,
    }
