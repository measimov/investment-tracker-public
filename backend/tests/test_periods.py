"""期间与覆盖约定（periods.py）及其在利润质量/格雷厄姆里的落点（#337 PR-2、#349、#350）。"""

import pytest

from app.services import graham_screen
from app.services.earnings_quality import (
    NI_NON_POSITIVE,
    compute_earnings_quality,
    pivot_rows_to_statements,
)
from app.services.periods import (
    annual_by_year,
    consecutive_run,
    coverage,
    fiscal_year_key,
    prior,
    years_adjacent,
)

# 「最接近 12/31 的星期六」结账的 52/53 周财年（#350 的整组样例）：年终日跨公历年
FIFTY_TWO_WEEK_ENDS = {
    "2019": "20191228",
    "2020": "20210102",
    "2021": "20220101",
    "2022": "20221231",
    "2023": "20231230",
    "2024": "20241228",
    "2025": "20260103",
}


# ---------------------------------------------------------------------------- 财年键


@pytest.mark.parametrize(("fiscal_year", "end_date"), sorted(FIFTY_TWO_WEEK_ENDS.items()))
def test_fiscal_year_key_52_53_week_year_end(fiscal_year, end_date):
    assert fiscal_year_key(end_date, "FY") == fiscal_year


@pytest.mark.parametrize(
    ("end_date", "fp", "expected"),
    [
        ("20251231", None, "2025"),  # A股 Tushare 年度行不带 fp
        ("20251231", "FY", "2025"),
        ("2025-12-31", None, "2025"),
        ("20250131", "FY", "2025"),  # 1 月底结账（零售业）：键与期末年份相同，不被推到上一年
        ("20250331", "FY", "2025"),  # 3 月财年（港股常见）
        ("20250630", "FY", "2025"),
        ("20250927", "FY", "2025"),  # Apple 型 9 月底 52/53 周
        ("20250630", None, None),  # A股 中报不是年度行
        ("20250630", "H1", None),
        ("", "FY", None),
        ("2025", "FY", None),  # 解析不了不猜
    ],
)
def test_fiscal_year_key_calendar_and_non_calendar(end_date, fp, expected):
    assert fiscal_year_key(end_date, fp) == expected


def test_annual_by_year_has_no_collision_for_52_53_week_rows():
    rows = [{"end_date": end, "fp": "FY"} for end in sorted(FIFTY_TWO_WEEK_ENDS.values())][::-1]
    by_year = annual_by_year(rows)
    assert sorted(by_year) == sorted(FIFTY_TWO_WEEK_ENDS)
    assert {year: row["end_date"] for year, row in by_year.items()} == FIFTY_TWO_WEEK_ENDS
    # 格雷厄姆与利润质量用的是同一个定义
    assert graham_screen._annual_by_year(rows) == by_year


# ---------------------------------------------------------------------------- 恰好上一期


def _row(end_date, fp="FY", **extra):
    return {"end_date": end_date, "fp": fp, **extra}


def test_prior_52_53_week_and_calendar():
    rows = [_row(end) for end in sorted(FIFTY_TWO_WEEK_ENDS.values(), reverse=True)]
    # 相邻年终日相隔 364 天（52 周）或 371 天（53 周：2024-12-28 → 2026-01-03）
    for newer, older in zip(rows, rows[1:]):
        assert prior(rows, newer) is older
    assert prior(rows, rows[-1]) is None


def test_prior_does_not_substitute_older_period_for_missing_year():
    rows = [_row("20251231"), _row("20231231")]
    assert prior(rows, rows[0]) is None  # 2024 缺失：不拿 2023 顶替
    assert prior(rows, rows[0], years=2) is rows[1]


def test_prior_requires_same_period_type():
    rows = [_row("20250630", "H1"), _row("20241231", "FY"), _row("20240630", "H1")]
    assert prior(rows, rows[0]) is rows[2]


# ---------------------------------------------------------------------------- 连续


def test_consecutive_run_stops_at_gap_unknown_or_value():
    def zero(row):
        paid = row.get("paid")
        return None if paid is None else paid == 0

    gap = [_row("20251231", paid=0), _row("20241231", paid=0), _row("20221231", paid=0)]
    run = consecutive_run(gap, zero)
    assert (run.count, run.stop_reason, run.stopped_at) == (2, "gap", gap[2])

    unknown = [_row("20251231", paid=0), _row("20241231", paid=None), _row("20231231", paid=0)]
    run = consecutive_run(unknown, zero)
    assert (run.count, run.stop_reason) == (1, "unknown")  # 不知道 ≠ 未派息，也 ≠ 派息

    value = [_row("20251231", paid=0), _row("20241231", paid=5)]
    run = consecutive_run(value, zero)
    assert (run.count, run.stop_reason) == (1, "value")

    run = consecutive_run(gap[:2], zero)
    assert (run.count, run.stop_reason, run.items) == (2, None, tuple(gap[:2]))


def test_consecutive_run_over_fiscal_year_keys():
    run = consecutive_run(["2025", "2024", "2022"], lambda _: True, adjacent=years_adjacent)
    assert (run.count, run.stop_reason, run.stopped_at) == (2, "gap", "2022")


def test_coverage_reports_missing_years():
    assert coverage(["2025", "2021", "2023", "2024"]) == {
        "first": "2021",
        "last": "2025",
        "count": 4,
        "missing": ["2022"],
    }
    assert coverage([]) == {"first": None, "last": None, "count": 0, "missing": []}


# ---------------------------------------------------------------------------- 利润质量落点


def _a_rows(year, ni, cfo, *, deducted=None, sell=100.0, admin=50.0, revenue=1000.0):
    end = f"{year}1231"
    return (
        {
            "end_date": end,
            "total_revenue": revenue,
            "n_income_attr_p": ni,
            "sell_exp": sell,
            "admin_exp": admin,
        },
        {
            "end_date": end,
            "total_assets": 2000.0,
            "accounts_receiv": 100.0,
            "inventories": 200.0,
            "total_cur_assets": 800.0,
            "fix_assets": 600.0,
            "total_liab": 1000.0,
        },
        {"end_date": end, "n_cashflow_act": cfo, "depr_fa_coga_dpba": 80.0},
        {
            "end_date": end,
            "grossprofit_margin": 40.0,
            "netprofit_margin": 10.0,
            "profit_dedt": deducted,
        },
    )


def _eq(*years):
    tables = list(zip(*years))
    return compute_earnings_quality(*[list(table) for table in tables], market="A股")


@pytest.mark.parametrize(
    ("ni", "cfo", "deducted", "unavailable"),
    [
        # #349 的四种组合：净利 ≤ 0 时比率方向失真，一律不计并写原因
        (-100.0, -150.0, None, {"cfo_ni_ratio": NI_NON_POSITIVE}),  # 1.5「健康」→ 不计
        (-100.0, 80.0, None, {"cfo_ni_ratio": NI_NON_POSITIVE}),  # -0.8 标红 → 不计
        (-100.0, None, -300.0, {"recurring_profit_share": NI_NON_POSITIVE}),  # 3.0 → 不计
        (-100.0, None, 50.0, {"recurring_profit_share": NI_NON_POSITIVE}),  # -0.5 → 不计
        (
            0.0,
            50.0,
            0.0,
            {"cfo_ni_ratio": NI_NON_POSITIVE, "recurring_profit_share": NI_NON_POSITIVE},
        ),
    ],
)
def test_ratios_not_computed_when_net_income_non_positive(ni, cfo, deducted, unavailable):
    result = _eq(_a_rows(2025, ni, cfo, deducted=deducted))
    year = result["per_year"]["2025"]
    assert year["cfo_ni_ratio"] is None
    assert year["recurring_profit_share"] is None
    assert year["ratio_unavailable"] == unavailable
    if cfo is not None:
        assert year["accruals_ratio"] is not None  # 应计率与净利符号无关，照算


def test_profitable_year_has_no_unavailable_marker():
    result = _eq(_a_rows(2025, 200.0, 150.0, deducted=160.0))
    year = result["per_year"]["2025"]
    assert year["cfo_ni_ratio"] == 0.75
    assert year["recurring_profit_share"] == 0.8
    assert "ratio_unavailable" not in year


def test_cumulative_ratio_not_computed_when_cumulative_net_income_non_positive():
    result = _eq(_a_rows(2025, -300.0, 50.0), _a_rows(2024, 100.0, 120.0))
    assert result["cfo_ni_ratio_5y"] is None
    assert result["cfo_ni_ratio_5y_unavailable"] == NI_NON_POSITIVE
    # 单个亏损年份不影响累计为正的窗口
    result = _eq(_a_rows(2025, -50.0, 50.0), _a_rows(2024, 150.0, 120.0))
    assert result["cfo_ni_ratio_5y"] == 1.7  # 170 / 100
    assert "cfo_ni_ratio_5y_unavailable" not in result


def test_cumulative_ratio_stops_at_missing_year():
    """缺年即停：2025、2024 连续，2022 与之隔着缺失的 2023，不跨缺年累计。"""
    result = _eq(
        _a_rows(2025, 100.0, 90.0),
        _a_rows(2024, 100.0, 90.0),
        _a_rows(2022, 100.0, 300.0),
    )
    assert result["cfo_ni_ratio_5y"] == 0.9
    assert result["cfo_ni_ratio_5y_years"] == 2
    assert "缺 2023 年" in result["cfo_ni_ratio_5y_note"]


def test_cumulative_ratio_stops_at_year_missing_cash_flow():
    result = _eq(
        _a_rows(2025, 100.0, 90.0),
        _a_rows(2024, 100.0, None),
        _a_rows(2023, 100.0, 300.0),
    )
    assert result["cfo_ni_ratio_5y"] == 0.9
    assert result["cfo_ni_ratio_5y_years"] == 1
    assert "2024 年缺净利润或经营现金流" in result["cfo_ni_ratio_5y_note"]


def test_cumulative_ratio_full_window_has_no_note():
    result = _eq(*[_a_rows(year, 100.0, 90.0) for year in range(2025, 2018, -1)])
    assert result["cfo_ni_ratio_5y"] == 0.9
    assert "cfo_ni_ratio_5y_note" not in result
    assert "cfo_ni_ratio_5y_years" not in result


def test_sgai_requires_same_components_in_both_years():
    """#349-2：上年缺管理费用时按 0 补会让 SGAI 由约 1 变成 3.3，M-score 仍当有效分输出。"""
    complete = _eq(_a_rows(2025, 100.0, 90.0), _a_rows(2024, 100.0, 90.0))
    assert complete["beneish_m_score"]["2025"]["factors"]["SGAI"] == 1.0
    partial = _eq(_a_rows(2025, 100.0, 90.0), _a_rows(2024, 100.0, 90.0, admin=None))
    assert "2025" not in partial["beneish_m_score"]


def test_pivot_rows_single_sga_component_still_scored():
    """港股/美股透视行只有合并 SGA（挂在 sell_exp 位），两年分项一致，M-score 照常可算。"""

    def fy(year, **fields):
        base = {
            "end_date": f"{year}1231",
            "fp": "FY",
            "currency": "USD",
            "total_revenue": 1000.0,
            "cost_of_revenue": 600.0,
            "n_income_attr_p": 100.0,
            "sga_exp": 150.0,
            "total_assets": 2000.0,
            "total_liab": 1000.0,
            "accounts_receiv": 100.0,
            "total_cur_assets": 800.0,
            "fix_assets": 600.0,
            "n_cashflow_act": 90.0,
            "depr_fa_coga_dpba": 80.0,
        }
        return {**base, **fields}

    statements = pivot_rows_to_statements([fy(2025), fy(2024)])
    result = compute_earnings_quality(
        statements["income"],
        statements["balancesheet"],
        statements["cashflow"],
        statements["fina_indicator"],
        market="美股",
    )
    assert result["beneish_m_score"]["2025"]["factors"]["SGAI"] == 1.0


def test_earnings_quality_52_53_week_year_over_year():
    """#350：营收每年 +10%；按公历年取键时 FY2021/FY2022 撞键，「同比」变成 FY2022 对 FY2020。"""
    income, balance, cashflow = [], [], []
    revenue = 1000.0
    for year in sorted(FIFTY_TWO_WEEK_ENDS):
        end = FIFTY_TWO_WEEK_ENDS[year]
        base = {"end_date": end, "fp": "FY", "currency": "USD"}
        income.append({**base, "total_revenue": revenue, "n_income_attr_p": revenue / 10})
        balance.append({**base, "total_assets": revenue * 2, "accounts_receiv": revenue / 10})
        cashflow.append({**base, "n_cashflow_act": revenue / 10})
        revenue *= 1.1
    result = compute_earnings_quality(
        income[::-1], balance[::-1], cashflow[::-1], [], market="美股"
    )
    assert result["years"] == sorted(FIFTY_TWO_WEEK_ENDS, reverse=True)
    for year in result["years"][:-1]:
        # 应收与营收同增 10%，增速差恒为 0；错年比较会得到 +10pp 之类的假信号
        assert result["per_year"][year]["receivable_vs_revenue_gap_pp"] == 0.0
    assert "cfo_ni_ratio_5y_note" not in result
