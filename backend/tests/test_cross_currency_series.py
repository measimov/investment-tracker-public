"""报告币种中途切换的多年序列（纯函数）：格雷厄姆盈利增长按同一汇率折同一币种比较，利润质量的
跨年指标在切换处不计；单币种序列（A股 + 港股 00700/00728）的输出与改动前逐字节一致。

生产实例（2026-09-27，main 837b905）：
- 00799 2015-2020 以 USD、2021 起以 HKD 披露：旧口径 2016→2025 每股盈利 0.0537 → 0.5102 算出
  +850.1% 达标，按同一汇率 0.0537 USD ≈ 0.421 HKD，真实增幅约 +21% 不达标；
- 02669 2015-2022 以 HKD、2023 起以 CNY 披露。
"""

import json
import sys
from pathlib import Path

import pytest

from app.services.earnings_quality import compute_earnings_quality, market_statements
from app.services.graham_screen import compute_graham_screen

sys.path.insert(0, str(Path(__file__).parent))
from test_graham_cross_market import CNY_HKD, _hk_fixture, _price  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "graham"
# 生产 2026-09-25 汇率（USD→HKD 经 CNY 交叉：6.7126 × 1.16835503）
USD_HKD = 7.842699969396108
CNY_HKD_0925 = 1.1683550292578297

# 00799 年报本期行（生产 report_statements 裁剪）：年份 → (币种, 基本 EPS, 归母净利, 营收)
IGG_ROWS = {
    2025: ("HKD", 0.5102, 580_493_000.0, 5_497_009_000.0),
    2024: ("HKD", 0.5061, 580_676_000.0, 5_737_114_000.0),
    2023: ("HKD", 0.0625, 73_053_000.0, 5_265_911_000.0),
    2022: ("HKD", -0.4329, -503_589_000.0, 4_591_327_000.0),
    2021: ("HKD", 0.3158, 370_438_000.0, 6_050_894_000.0),
    2020: ("USD", 0.2223, 270_234_000.0, 704_128_000.0),
    2019: ("USD", 0.1319, 164_794_000.0, 667_648_000.0),
    2018: ("USD", 0.1467, 189_177_000.0, 748_785_000.0),
    2017: ("USD", 0.1172, 156_026_000.0, 607_253_000.0),
    2016: ("USD", 0.0537, 72_616_000.0, 322_087_000.0),
}
# 02669 年报本期行
CHINA_OVERSEAS_PS_ROWS = {
    2025: ("CNY", 0.4162, 1_366_779_000.0, 14_959_871_000.0),
    2024: ("CNY", 0.46, 1_510_918_000.0, 14_023_767_000.0),
    2023: ("CNY", 0.4084, 1_342_503_000.0, 13_051_250_000.0),
    2022: ("HKD", 0.3873, 1_273_146_000.0, 12_688_968_000.0),
    2021: ("HKD", 0.2993, 983_872_000.0, 9_442_035_000.0),
    2020: ("HKD", 0.213, 700_008_000.0, 6_544_877_000.0),
    2019: ("HKD", 0.1636, 537_840_000.0, 5_465_521_000.0),
    2018: ("HKD", 0.1223, 402_058_000.0, 4_154_670_000.0),
    2017: ("HKD", 0.0933, 306_760_000.0, 3_357_800_000.0),
    2016: ("HKD", 0.0723, 237_529_000.0, 3_296_695_000.0),
}


def _row(year, currency, eps, profit, revenue, **extra):
    return {
        "end_date": f"{year}1231",
        "fp": "FY",
        "currency": currency,
        "basic_eps": eps,
        "n_income_attr_p": profit,
        "total_revenue": revenue,
        **extra,
    }


def _statements(table):
    rows = [_row(year, *values) for year, values in sorted(table.items(), reverse=True)]
    return market_statements("港股", {"report_statements": rows, "yahoo_fundamentals": []})


def _growth(result):
    return next(c for c in result["criteria"] if c["criterion"] == "earnings_growth")


def _valuation(fx_rates, currency="HKD"):
    return {"price": _price(3.02, currency), "fx_rates": fx_rates, "interim_rows": []}


# ---------------------------------------------------------------------------- 单币种不变


def _eq(statements, market):
    return compute_earnings_quality(
        statements["income"],
        statements["balancesheet"],
        statements["cashflow"],
        statements["fina_indicator"],
        market=market,
    )


def test_single_currency_outputs_byte_identical_to_pre_change():
    """A股 5 只 + 港股 00700/00728（单币种）的格雷厄姆与利润质量完整输出 = 改动前的金样。"""
    golden = json.loads((FIXTURES / "single_currency_golden.json").read_text(encoding="utf-8"))
    ashare = json.loads((FIXTURES / "ashare_golden.json").read_text(encoding="utf-8"))
    actual = {}
    for case in ashare["cases"]:
        inputs = case["inputs"]
        statements = market_statements("A股", inputs["statement_datasets"])
        actual[f"A股:{case['symbol']}"] = {
            "graham": compute_graham_screen(
                "A股",
                statements,
                daily_basic_rows=inputs["daily_basic_rows"],
                dividend_rows=inputs["dividend_rows"],
            ),
            "earnings_quality": _eq(statements, "A股"),
        }
    for symbol, close in (("00700", 436.6), ("00728", 3.2)):
        annual, interim = _hk_fixture(symbol)
        statements = market_statements(
            "港股", {"report_statements": annual, "yahoo_fundamentals": []}
        )
        actual[f"港股:{symbol}"] = {
            "graham": compute_graham_screen(
                "港股",
                statements,
                valuation={
                    "price": _price(close),
                    "fx_rates": {"CNY": CNY_HKD},
                    "interim_rows": interim,
                },
            ),
            "earnings_quality": _eq(statements, "港股"),
        }
    normalized = json.loads(json.dumps(actual, ensure_ascii=False, sort_keys=True))
    assert normalized == golden["cases"]


# ---------------------------------------------------------------------------- 格雷厄姆盈利增长


def test_usd_to_hkd_growth_compared_in_constant_currency_flips_to_fail():
    """00799：2016 年 0.0537 USD 按价格日汇率 ≈ 0.4212 HKD，对 2025 年 0.5102 HKD 仅 +21%。"""
    result = compute_graham_screen(
        "港股",
        _statements(IGG_ROWS),
        valuation=_valuation({"USD": USD_HKD, "HKD": 1.0}),
    )
    growth = _growth(result)
    expected = (0.5102 / (0.0537 * USD_HKD) - 1) * 100
    assert growth["verdict"] == "fail"
    assert growth["value"] == pytest.approx(expected, abs=1e-4)
    assert 20 < growth["value"] < 23
    assert "2016 年以 USD 披露，按同一汇率折 HKD 比较" in growth["reason"]
    basis = growth["basis"]
    assert basis["method"] == "constant_currency"
    assert (basis["first_currency"], basis["currency"]) == ("USD", "HKD")
    assert basis["first_value_converted"] == pytest.approx(0.0537 * USD_HKD, abs=1e-6)
    assert basis["fx_rate_date"] == "2026-09-25"
    # 旧口径（不折算）是 +850% 达标——正是这次要堵的
    assert (0.5102 / 0.0537 - 1) * 100 > 800


def test_hkd_to_cny_growth_converted_into_latest_currency():
    """02669：2016 年 HKD 折成 2025 年的 CNY 再比（首尾共用价格日汇率）。"""
    result = compute_graham_screen(
        "港股",
        _statements(CHINA_OVERSEAS_PS_ROWS),
        valuation=_valuation({"CNY": CNY_HKD_0925, "HKD": 1.0}),
    )
    growth = _growth(result)
    first_cny = 0.0723 / CNY_HKD_0925
    assert growth["verdict"] == "pass"
    assert growth["value"] == pytest.approx((0.4162 / first_cny - 1) * 100, abs=1e-4)
    assert "2016 年以 HKD 披露，按同一汇率折 CNY 比较" in growth["reason"]
    assert growth["basis"]["currency"] == "CNY"


def test_growth_independent_of_price_currency():
    """同一汇率折算：增幅与用哪种价格币种做中转无关（汇率变动不计入增长）。"""
    hkd = _growth(
        compute_graham_screen(
            "港股",
            _statements(IGG_ROWS),
            valuation=_valuation({"USD": USD_HKD, "HKD": 1.0}),
        )
    )
    cny = _growth(
        compute_graham_screen(
            "港股",
            _statements(IGG_ROWS),
            valuation=_valuation({"USD": 6.7126, "HKD": 0.85590422}, currency="CNY"),
        )
    )
    assert hkd["value"] == pytest.approx(cny["value"], rel=1e-6)


@pytest.mark.parametrize(
    "valuation",
    [
        _valuation({"HKD": 1.0}),  # 缺 USD 汇率
        {"price": None},  # 无行情价 → 无汇率可用
        None,
    ],
)
def test_missing_fx_is_indeterminate(valuation):
    result = compute_graham_screen("港股", _statements(IGG_ROWS), valuation=valuation)
    growth = _growth(result)
    assert growth["verdict"] == "indeterminate"
    assert "缺 USD→HKD 汇率" in growth["reason"]
    assert "无法按同一币种比较增幅" in growth["reason"]


@pytest.mark.parametrize("unknown_years", [(2016,), (2025,), (2016, 2025)])
@pytest.mark.parametrize("market", ["港股", "美股"])
def test_unknown_endpoint_currency_is_indeterminate(unknown_years, market):
    """任一端（含两端同时）行上无币种 → 不可判定。PR #242 评审 P2：两端都是 None 时曾被当成
    同币种，00799 数据照算出 +850% 达标——两个未知证明不了同币种。"""
    table = dict(IGG_ROWS)
    for year in unknown_years:
        table[year] = (None, *IGG_ROWS[year][1:])
    growth = _growth(
        compute_graham_screen(
            market,
            _statements(table),
            valuation=_valuation({"USD": USD_HKD, "HKD": 1.0}),
        )
    )
    assert growth["verdict"] == "indeterminate"
    assert growth["value"] is None
    assert "首尾币种无法确认一致" in growth["reason"]
    assert "币种未知" in growth["reason"]


def test_hk_rows_without_any_currency_never_compared():
    """整段都没有币种的港股序列：不是「单币种」，不能直接比较（旧口径 +850% 达标）。"""
    table = {year: (None, *values[1:]) for year, values in IGG_ROWS.items()}
    growth = _growth(compute_graham_screen("港股", _statements(table), valuation=None))
    assert growth["verdict"] == "indeterminate"
    assert "2016 年 币种未知、2025 年 币种未知" in growth["reason"]


def test_a_share_rows_without_currency_resolved_by_market():
    """A股 Tushare 行按构造为人民币、行上不带 currency：显式市场例外，照常直接比较。"""
    income = [
        {"end_date": f"{year}1231", "basic_eps": values[1], "n_income_attr_p": values[2]}
        for year, values in sorted(IGG_ROWS.items(), reverse=True)
    ]
    statements = {"income": income, "balancesheet": [], "cashflow": [], "fina_indicator": []}
    growth = _growth(compute_graham_screen("A股", statements))
    assert growth["verdict"] == "pass"
    assert growth["value"] == pytest.approx((0.5102 / 0.0537 - 1) * 100, abs=1e-4)
    assert "basis" not in growth


def test_same_currency_endpoints_unchanged_even_if_middle_years_switch():
    """只看首尾：两端同币种（中间年份换过币种）照旧直接比较，不带折算依据。"""
    table = {
        year: ("HKD" if year in (2016, 2025) else "USD", *values[1:])
        for year, values in IGG_ROWS.items()
    }
    growth = _growth(
        compute_graham_screen(
            "港股",
            _statements(table),
            valuation=_valuation({"USD": USD_HKD, "HKD": 1.0}),
        )
    )
    assert growth["value"] == pytest.approx((0.5102 / 0.0537 - 1) * 100, abs=1e-4)
    assert "basis" not in growth
    assert "同一汇率" not in growth["reason"]


def test_three_year_average_supplement_uses_one_rate_for_all_years():
    """三年平均 PE 补充值：跨币种年份逐行按同一组汇率折价格币种（既有行为，钉住）。"""
    table = {
        2025: ("HKD", 0.5, 1.0e8, 1.0e9),
        2024: ("USD", 0.05, 1.0e7, 1.0e8),
        2023: ("USD", 0.05, 1.0e7, 1.0e8),
    }
    result = compute_graham_screen(
        "港股",
        _statements(table),
        valuation=_valuation({"USD": USD_HKD, "HKD": 1.0}),
    )
    supplement = next(c for c in result["criteria"] if c["criterion"] == "pe")["supplement"]
    average = (0.5 + 0.05 * USD_HKD * 2) / 3
    assert supplement["graham_avg3_pe"] == round(3.02 / average, 2)


# ---------------------------------------------------------------------------- 利润质量


def _full_row(year, currency, scale, revenue_growth=1.1):
    """科目齐全、同一 scale 的年度行：M-score 所需八因子都可算。"""
    k = scale * (revenue_growth ** (year - 2016))
    return _row(
        year,
        currency,
        0.1 * k,
        100.0 * k,
        1000.0 * k,
        cost_of_revenue=600.0 * k,
        accounts_receiv=150.0 * k,
        inventories=200.0 * k,
        total_cur_assets=800.0 * k,
        fix_assets=600.0 * k,
        total_assets=2000.0 * k,
        total_liab=1000.0 * k,
        sga_exp=120.0 * k,
        n_cashflow_act=90.0 * k,
        depr_fa_coga_dpba=80.0 * k,
    )


def _quality(rows):
    statements = market_statements("港股", {"report_statements": rows, "yahoo_fundamentals": []})
    return _eq(statements, "港股")


def test_earnings_quality_skips_cross_currency_pair_and_marks_switch_year():
    """00799 形态：2021 年起 HKD（金额 ≈ USD × 7.8）。切换年的增速差与 M-score 不计，其余年份照算。"""
    rows = [
        _full_row(year, "HKD" if year >= 2021 else "USD", 7.8 if year >= 2021 else 1.0)
        for year in range(2025, 2016, -1)
    ]
    result = _quality(rows)
    switch = result["per_year"]["2021"]
    assert switch["currency_change"] == "USD→HKD"
    assert switch["receivable_vs_revenue_gap_pp"] is None
    assert switch["inventory_vs_revenue_gap_pp"] is None
    assert "2021" not in result["beneish_m_score"]
    # 同年内的比率不受影响
    assert switch["cfo_ni_ratio"] == 0.9
    assert switch["gross_margin"] == pytest.approx(40.0)
    # 切换前后的同币种年份照常：同比增长一致 → 增速差为 0，M-score 可算
    for year in ("2025", "2020"):
        assert result["per_year"][year]["receivable_vs_revenue_gap_pp"] == 0.0
        assert "currency_change" not in result["per_year"][year]
        assert year in result["beneish_m_score"]
    assert result["currency_changes"] == [{"year": "2021", "change": "USD→HKD"}]
    assert "不计" in result["currency_change_note"]
    # 最新 5 年（2021-2025）同为 HKD：累计照算，不带截断说明
    assert result["cfo_ni_ratio_5y"] == 0.9
    assert "cfo_ni_ratio_5y_note" not in result


def test_five_year_cfo_ni_stops_at_currency_switch():
    """02669 形态：2023 年起 CNY、此前 HKD——近 5 年累计只含 CNY 的 3 年，不把 HKD 与 CNY 相加。"""
    rows = [
        _full_row(year, "CNY" if year >= 2023 else "HKD", 1.0) for year in range(2025, 2016, -1)
    ]
    # 让 HKD 年份的 CFO/NI 明显不同：混进来累计值就会偏离 0.9
    for row in rows:
        if row["currency"] == "HKD":
            row["n_cashflow_act"] = row["n_income_attr_p"] * 2
    result = _quality(rows)
    assert result["cfo_ni_ratio_5y"] == 0.9
    assert result["cfo_ni_ratio_5y_years"] == 3
    assert "CNY" in result["cfo_ni_ratio_5y_note"] and "2022" in result["cfo_ni_ratio_5y_note"]
    assert result["currency_changes"] == [{"year": "2023", "change": "HKD→CNY"}]


def test_single_currency_quality_has_no_currency_keys():
    rows = [_full_row(year, "HKD", 1.0) for year in range(2025, 2016, -1)]
    result = _quality(rows)
    assert not {
        "currency_changes",
        "currency_change_note",
        "cfo_ni_ratio_5y_note",
        "cfo_ni_ratio_5y_years",
    } & set(result)
    assert all("currency_change" not in item for item in result["per_year"].values())


def test_quality_unknown_currency_adjacent_to_known_is_not_compared():
    """港股某年行上无币种：它与相邻两年都无法确认同币种 → 两处跨年指标都不计；
    近 5 年累计在未知年份处截断（不把未知币种金额加进来）。"""
    rows = [_full_row(year, "HKD", 1.0) for year in range(2025, 2016, -1)]
    unknown = next(row for row in rows if row["end_date"] == "20231231")
    unknown["currency"] = None
    result = _quality(rows)
    assert result["per_year"]["2024"]["currency_change"] == "未知→HKD"
    assert result["per_year"]["2023"]["currency_change"] == "HKD→未知"
    for year in ("2024", "2023"):
        assert result["per_year"][year]["receivable_vs_revenue_gap_pp"] is None
        assert year not in result["beneish_m_score"]
    assert result["per_year"]["2025"]["receivable_vs_revenue_gap_pp"] == 0.0
    assert "2025" in result["beneish_m_score"]
    assert result["cfo_ni_ratio_5y_years"] == 2
    assert "2023 年起币种不同或无法确认相同" in result["cfo_ni_ratio_5y_note"]


def test_quality_both_unknown_is_not_same_currency_except_a_share():
    """两年都无币种：港股/未声明市场 → 无法确认同币种，跨年指标不计；A股 → 按市场即人民币，照算。"""
    rows = [_full_row(year, None, 1.0) for year in range(2025, 2016, -1)]
    statements = market_statements("港股", {"report_statements": rows, "yahoo_fundamentals": []})
    for market in ("港股", None):
        result = _eq(statements, market)
        assert result["per_year"]["2025"]["currency_change"] == "未知→未知"
        assert result["per_year"]["2025"]["receivable_vs_revenue_gap_pp"] is None
        assert result["beneish_m_score"] == {}
        assert result["cfo_ni_ratio_5y_years"] == 1
    a_share = _eq(statements, "A股")
    assert a_share["per_year"]["2025"]["receivable_vs_revenue_gap_pp"] == 0.0
    assert "2025" in a_share["beneish_m_score"]
    assert not {"currency_changes", "cfo_ni_ratio_5y_note"} & set(a_share)
