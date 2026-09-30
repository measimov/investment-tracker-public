"""风险指标、交易能力指标与 XIRR（纯内核，无 DB 依赖）。"""

import math
from datetime import date
from decimal import Decimal
from bisect import bisect_right
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 年化天数基准：同一响应里会同时出现 TTWR 年化（metrics）与 XIRR 年化
# （range_summary），此前两者分别用 365 与 365.25，长区间下有可感知的系统性
# 差异且无任何字段说明。统一到 365.25（含闰年补偿，与 XIRR 的行业惯例一致），
# 并经 annualization_days_basis 字段自述。
DAYS_PER_YEAR = Decimal("365.25")
DAYS_PER_YEAR_FLOAT = float(DAYS_PER_YEAR)


def xirr(cash_flows: List[Tuple[date, Decimal]]) -> Optional[Decimal]:
    """Calculate annualized money-weighted return from dated cash flows.

    求解器内部用 float：Decimal 的分数次幂在千笔现金流 × 数十次二分下
    是秒级热点（实测占仪表盘响应 90%），而利率解的精度要求（1e-6 量级）
    远在 float 精度之内。同日现金流先合并，进一步减少幂运算次数。
    """
    merged: dict = {}
    for flow_date, amount in cash_flows:
        if amount != 0:
            merged[flow_date] = merged.get(flow_date, Decimal("0")) + amount
    flows = [(flow_date, amount) for flow_date, amount in merged.items() if amount != 0]
    if (
        not flows
        or not any(amount < 0 for _, amount in flows)
        or not any(amount > 0 for _, amount in flows)
    ):
        return None

    start_date = min(flow_date for flow_date, _ in flows)
    # 每项预计算 (log|amount|, 符号, 年数)；npv 在对数域求和。
    term_logs = [
        (
            math.log(abs(float(amount))),
            1.0 if amount > 0 else -1.0,
            (flow_date - start_date).days / DAYS_PER_YEAR_FLOAT,
        )
        for flow_date, amount in flows
    ]

    def npv(rate: float) -> float:
        # 数值稳定性：长跨度（数十年）× 极端利率下 base**years 会下溢/上溢。
        # 对数域求和（log-sum-exp）：log(term_i) = log|a_i| − years_i·log(1+r)，
        # 以最大项为基准缩放后相加——各项相对量级完整保留（逐项钳制会让两个
        # 极端项同值饱和后互相抵消，把搜索边界误判成根），任何中间量都有限。
        log_base = math.log1p(rate)
        shifted = [(log_amount - years * log_base, sign) for log_amount, sign, years in term_logs]
        max_log = max(log_value for log_value, _ in shifted)
        scaled = sum(sign * math.exp(log_value - max_log) for log_value, sign in shifted)
        if scaled == 0.0:
            return 0.0
        # 还原量级用于收敛判据；封顶只影响"远大于容差"的区域，符号不变。
        magnitude_log = min(max_log + math.log(abs(scaled)), 700.0)
        return math.copysign(math.exp(magnitude_log), scaled)

    low = -0.9999
    high = 1.0
    low_value = npv(low)
    high_value = npv(high)

    if low_value == 0:
        return Decimal(str(low))

    for _ in range(80):
        if low_value * high_value < 0:
            break
        high *= 2.0
        high_value = npv(high)
        if high > 1_000_000:
            return None

    for _ in range(120):
        mid = (low + high) / 2.0
        mid_value = npv(mid)
        if abs(mid_value) < 0.000001:
            return Decimal(str(mid))
        if low_value * mid_value <= 0:
            high = mid
            high_value = mid_value
        else:
            low = mid
            low_value = mid_value

    return Decimal(str((low + high) / 2.0))


def calculate_trade_skill_metrics(realized: Dict[str, Any]) -> Dict[str, Any]:
    # Prefer per-closing-lot samples (one record per SELL matched against buy
    # lots) so win rate / payoff / profit factor are per trade, not per symbol
    # (issue #43). Fall back to the per-symbol aggregate when closed-lot detail is
    # unavailable.
    closed_trades = realized.get("closed_trades")
    if closed_trades is not None:
        sample_unit = "closed_trade"
        sample_description = "One record per closing trade (SELL matched to buy lots)."
        samples = [
            Decimal(str(item.get("realized_pnl_cny", item.get("realized_pnl", 0))))
            for item in closed_trades
            if Decimal(str(item.get("matched_cost_cny", item.get("matched_cost", 0)))) > 0
        ]
    else:
        sample_unit = "realized_symbol"
        sample_description = "One aggregated realized result per symbol."
        samples = [
            Decimal(str(item.get("realized_pnl_cny", item.get("realized_pnl", 0))))
            for item in realized.get("trades_detail", [])
            if Decimal(str(item.get("sold_cost_cny", item.get("sold_cost", 0)))) > 0
        ]

    winners = [value for value in samples if value > 0]
    losers = [value for value in samples if value < 0]
    active_samples = [value for value in samples if value != 0]

    total_profit = sum(winners, Decimal("0"))
    total_loss = sum(losers, Decimal("0"))
    avg_win = total_profit / Decimal(len(winners)) if winners else Decimal("0")
    avg_loss = total_loss / Decimal(len(losers)) if losers else Decimal("0")
    win_rate = (
        Decimal(len(winners)) / Decimal(len(active_samples)) if active_samples else Decimal("0")
    )
    loss_rate = (
        Decimal(len(losers)) / Decimal(len(active_samples)) if active_samples else Decimal("0")
    )
    expectancy = win_rate * avg_win + loss_rate * avg_loss

    # profit_factor is None when there are no losing trades; has_losses lets the
    # caller tell "no losses yet" apart from "no samples" (issue #43).
    has_losses = total_loss < 0
    profit_factor = float(total_profit / abs(total_loss)) if has_losses else None

    payoff_ratio = None
    if avg_loss < 0:
        payoff_ratio = float(avg_win / abs(avg_loss))

    return {
        "sample_unit": sample_unit,
        "status": "experimental",
        "sample_description": sample_description,
        "sample_count": len(active_samples),
        "winning_count": len(winners),
        "losing_count": len(losers),
        "has_losses": has_losses,
        # 无样本时是「无从计算」而不是 0%（#218：区间无平仓时显示 0.00% 像是全亏）；
        # expectancy 等其余字段保持原口径（无样本时为 0）
        "win_rate": float(win_rate * Decimal("100")) if active_samples else None,
        "average_win_cny": float(avg_win),
        "average_loss_cny": float(avg_loss),
        "payoff_ratio": payoff_ratio,
        "profit_factor": profit_factor,
        "expectancy_cny": float(expectancy),
    }


def _parse_curve_date(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def forward_filled_rates(
    points: Sequence[Tuple[date, Decimal]], dates: Sequence[Optional[date]]
) -> List[Optional[Decimal]]:
    """每个日期取「当日或之前最近一个发布值」；早于首个发布值或日期缺失 → None。"""
    ordered = sorted(points, key=lambda item: item[0])
    keys = [item[0] for item in ordered]
    result: List[Optional[Decimal]] = []
    for day in dates:
        if day is None:
            result.append(None)
            continue
        index = bisect_right(keys, day) - 1
        result.append(ordered[index][1] if index >= 0 else None)
    return result


def calculate_risk_metrics(
    curve: List[Dict[str, Any]],
    risk_free_rate: Decimal,
    calculation_level: str,
    risk_free_points: Optional[Sequence[Tuple[date, Decimal]]] = None,
) -> Dict[str, Any]:
    """风险指标。无风险利率两种口径：

    - 常量 `risk_free_rate`（年化 %）：按平均观测频率摊到每期（历史口径，逐字节不变）；
    - `risk_free_points` = [(发布日, 年化 %)] 日序列（#200，SHIBOR 3M）：每个收益点按其日期
      向前填充取当期年化利率，再按**该点与上一点的实际日历间隔**计息
      （rate/100 × gap/365.25）——曲线日期网格不规则，平均频率会把长间隔少算、短间隔多算。
      此时输出的 `risk_free_rate` 是各收益点所用利率的均值；早于序列首值的点按 0 计并在
      `risk_free_missing_points` 里如实报数。
    """
    returns = [
        Decimal(str(point["daily_return_rate"])) / Decimal("100")
        for point in curve
        if point.get("daily_return_rate") is not None
    ]
    # 每个收益点的 (日期, 上一曲线点日期)：逐期无风险收益按实际间隔计息要用
    return_dates: List[Tuple[Optional[date], Optional[date]]] = []
    previous_date: Optional[date] = None
    for point in curve:
        point_date = _parse_curve_date(point.get("date"))
        if point.get("daily_return_rate") is not None:
            return_dates.append((point_date, previous_date))
        previous_date = point_date
    series_rates: Optional[List[Optional[Decimal]]] = None
    if risk_free_points:
        series_rates = forward_filled_rates(risk_free_points, [d for d, _ in return_dates])
        known = [rate for rate in series_rates if rate is not None]
        if known:
            risk_free_rate = sum(known, Decimal("0")) / Decimal(len(known))
    final_return_rate = Decimal(str(curve[-1]["cumulative_return_rate"])) if curve else Decimal("0")
    max_drawdown_rate = min(
        [Decimal(str(point.get("drawdown_rate", 0))) for point in curve],
        default=Decimal("0"),
    )

    # Elapsed calendar span of the curve. The return series lives on an irregular
    # date grid (union of price/event/boundary dates), so annualization is driven
    # by real elapsed time, not by a fixed 252/12 period count (issue #40).
    span_days = 0
    if curve:
        try:
            start_d = date.fromisoformat(str(curve[0]["date"]))
            end_d = date.fromisoformat(str(curve[-1]["date"]))
            span_days = (end_d - start_d).days
        except (ValueError, KeyError, TypeError):
            span_days = 0

    metrics = {
        "total_return_rate": float(final_return_rate),
        "annualized_return_rate": None,
        "annualized_volatility": None,
        "sharpe_ratio": None,
        "sortino_ratio": None,
        "max_drawdown_rate": float(max_drawdown_rate),
        "calmar_ratio": None,
        "risk_free_rate": float(risk_free_rate),
        "risk_free_basis": "series" if series_rates is not None else "constant",
        "risk_sample_count": len(returns),
        "annualization_basis": "calendar_days",
        # 与 range_summary 的 XIRR 同基准；此前两者 365 vs 365.25 不一致且无说明
        "annualization_days_basis": DAYS_PER_YEAR_FLOAT,
        "observation_span_days": span_days,
    }

    if series_rates is not None:
        metrics["risk_free_missing_points"] = sum(1 for rate in series_rates if rate is None)

    if len(returns) < 2:
        return metrics

    # Event-level curves are a sparse, irregular union of trade/corporate-action
    # dates; a period-based annualization has no defensible frequency, so only the
    # cumulative return and max drawdown are reported for them (issue #40).
    if calculation_level != "daily_price_history" or span_days <= 0:
        metrics["annualization_basis"] = "none"
        return metrics

    # Annualize the realized cumulative return over actual calendar time rather than
    # by a fixed 252-period assumption tied to the sample count (issue #40).
    total_growth = Decimal("1") + final_return_rate / Decimal("100")
    if total_growth > 0:
        annualized = total_growth ** (DAYS_PER_YEAR / Decimal(span_days)) - Decimal("1")
        metrics["annualized_return_rate"] = float(annualized * Decimal("100"))
    else:
        annualized = None

    # Scale volatility by the true observation frequency (average calendar gap
    # between observations) instead of a fixed sqrt(252) (issue #40).
    periods_per_year = DAYS_PER_YEAR * Decimal(len(returns)) / Decimal(span_days)
    annual_factor = periods_per_year.sqrt()

    average_return = sum(returns, Decimal("0")) / Decimal(len(returns))
    variance = sum((value - average_return) ** 2 for value in returns) / Decimal(len(returns) - 1)
    volatility = variance.sqrt()
    metrics["annualized_volatility"] = float(volatility * annual_factor * Decimal("100"))

    if series_rates is None:
        period_risk_free = (risk_free_rate / Decimal("100")) / periods_per_year
        excess_returns = [value - period_risk_free for value in returns]
    else:
        average_gap = Decimal(span_days) / Decimal(len(returns))
        excess_returns = []
        for value, rate, (point_date, prior_date) in zip(returns, series_rates, return_dates):
            if point_date is not None and prior_date is not None:
                gap = Decimal((point_date - prior_date).days)
            else:
                gap = average_gap
            accrued = (rate or Decimal("0")) / Decimal("100") * gap / DAYS_PER_YEAR
            excess_returns.append(value - accrued)
    average_excess = sum(excess_returns, Decimal("0")) / Decimal(len(excess_returns))

    # Sharpe uses the dispersion of the *excess* returns so numerator and
    # denominator share one basis. Under a constant risk-free rate this equals the
    # raw-return stddev, but it is stated explicitly for correctness (issue #41).
    excess_variance = sum((value - average_excess) ** 2 for value in excess_returns) / Decimal(
        len(excess_returns) - 1
    )
    excess_volatility = excess_variance.sqrt()
    if excess_volatility > 0:
        metrics["sharpe_ratio"] = float((average_excess / excess_volatility) * annual_factor)

    # Sortino downside deviation divides by the TOTAL observation count N — periods
    # that met the target contribute a zero to the sum of squares rather than being
    # dropped from the denominator (issue #41). Dividing by the downside count alone
    # systematically inflated the deviation and understated the ratio.
    downside_variance = sum(min(value, Decimal("0")) ** 2 for value in excess_returns) / Decimal(
        len(excess_returns)
    )
    downside_deviation = downside_variance.sqrt()
    if downside_deviation > 0:
        metrics["sortino_ratio"] = float((average_excess / downside_deviation) * annual_factor)

    if annualized is not None and max_drawdown_rate < 0:
        metrics["calmar_ratio"] = float(annualized / (abs(max_drawdown_rate) / Decimal("100")))

    return metrics
