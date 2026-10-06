"""格雷厄姆检验跨市场适配（PR-C）：港股/美股估值（TTM 判定 + 年报口径补充）、港股/美股分红
记录（现金流量表已付股息）、港股非流动借款、美股长期债务概念链、短历史文案，以及 A股 判定不变。

金样：
- tests/fixtures/graham/ashare_golden.json —— 改动前的生产代码对 5 只 A股 生产行的输出（输入已裁剪）；
- tests/fixtures/graham/hk_rows.json —— 生产 00700 / 00728 报表行（00728 2025 中报 EPS=2.0 是真实错误）。
"""

import json
from collections import namedtuple
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.timeutil import local_today
from app.database import SessionLocal
from app.models.security_price import SecurityPrice
from app.models.security_profile import SecurityProfileData
from app.services import edgar_facts, profile_store
from app.services import security_profile_service as svc
from app.services.edgar_facts import EDGAR_PIVOT_VERSION
from app.services.earnings_quality import (
    market_statements,
    pivot_rows_to_statements,
)
from app.services.graham_screen import compute_graham_screen, resolve_fx_rates
from app.services.portfolio.fx import ExchangeRateLookup
from app.services.report_statement_prompts import (
    EXPENSE_MAGNITUDE_FIELDS,
    STATEMENT_BUILD_VERSION,
    STATEMENT_FIELDS,
    STATEMENT_PROMPT_VERSION,
)
from app.services.report_statements import STATEMENT_EXTRACTOR_VERSION
from app.services.security_analysis_prompts import build_analysis_messages
from tests.analysis_fixtures import FULL_REPORT_JSON

FIXTURES = Path(__file__).parent / "fixtures" / "graham"
Rate = namedtuple("Rate", "from_currency to_currency rate effective_date")
CNY_HKD = 1.09  # 1 CNY = 1.09 HKD（测试常数）


def _by(result, key):
    return next(item for item in result["criteria"] if item["criterion"] == key)


def _current(row, kinds=("income", "balance", "cashflow")):
    """把裁剪行标成当前版本的 PDF 抽取行（读取侧版本门 + 现金流量表来源）。"""
    return {
        **row,
        "extractor_version": STATEMENT_EXTRACTOR_VERSION,
        "prompt_version": STATEMENT_PROMPT_VERSION,
        "build_version": STATEMENT_BUILD_VERSION,
        "source_by_kind": {kind: {"period_key": "x"} for kind in kinds},
    }


def _price(close, currency="HKD", *, day="2026-09-25", stale=False, age=2):
    return {
        "close": close,
        "currency": currency,
        "date": day,
        "stale": stale,
        "age_days": age,
        "source": "hkex-dayquot",
    }


def _hk_fixture(symbol):
    rows = json.loads((FIXTURES / "hk_rows.json").read_text(encoding="utf-8"))[symbol]
    annual = [_current(r) for r in rows if r["fp"] == "FY"]
    interim = [_current(r) for r in rows if r["fp"] == "H1"]
    return annual, interim


def _hk_screen(symbol, close, **valuation):
    annual, interim = _hk_fixture(symbol)
    statements = market_statements("港股", {"report_statements": annual, "yahoo_fundamentals": []})
    return compute_graham_screen(
        "港股",
        statements,
        valuation={
            "price": _price(close),
            "fx_rates": {"CNY": CNY_HKD},
            "interim_rows": interim,
            **valuation,
        },
    )


# ---------------------------------------------------------------------------- A股 不变


def test_a_share_verdicts_unchanged_against_pre_change_golden():
    """改动前的生产代码对 5 只 A股 生产行的逐项输出：verdict/value/脆弱性信号一字不差；reason 只有
    「披露历史短」这一处有意改写（600941 自 2018 年起才有数据）。"""
    golden = json.loads((FIXTURES / "ashare_golden.json").read_text(encoding="utf-8"))
    changed_reasons = []
    for case in golden["cases"]:
        inputs = case["inputs"]
        result = compute_graham_screen(
            "A股",
            market_statements("A股", inputs["statement_datasets"]),
            daily_basic_rows=inputs["daily_basic_rows"],
            dividend_rows=inputs["dividend_rows"],
        )
        assert result["as_of_year"] == case["expected"]["as_of_year"]
        assert result["fragility"] == case["expected"]["fragility"]
        for expected in case["expected"]["criteria"]:
            actual = _by(result, expected["criterion"])
            assert (actual["verdict"], actual["value"]) == (
                expected["verdict"],
                expected["value"],
            ), (
                case["symbol"],
                expected["criterion"],
            )
            if actual["reason"] != expected["reason"]:
                changed_reasons.append((case["symbol"], expected["criterion"]))
                assert "披露历史仅" in actual["reason"]
        # A股 估值仍是 daily_basic 快照；新增的只有参考口径
        pe = _by(result, "pe")
        assert pe["basis"]["label"].startswith("Tushare daily_basic")
        assert pe["supplement"]["basis"] == "年报口径，仅供参考，不参与判定"
    assert changed_reasons == [("600941", "earnings_stability"), ("600941", "earnings_growth")]


def test_a_share_supplement_static_and_three_year_average():
    golden = json.loads((FIXTURES / "ashare_golden.json").read_text(encoding="utf-8"))
    case = next(c for c in golden["cases"] if c["symbol"] == "000333")
    inputs = case["inputs"]
    result = compute_graham_screen(
        "A股",
        market_statements("A股", inputs["statement_datasets"]),
        daily_basic_rows=inputs["daily_basic_rows"],
        dividend_rows=inputs["dividend_rows"],
    )
    close = inputs["daily_basic_rows"][0]["close"]
    eps = {r["end_date"][:4]: r["basic_eps"] for r in inputs["statement_datasets"]["income"]}
    supplement = _by(result, "pe")["supplement"]
    assert supplement["static_pe"] == round(close / eps["2025"], 2)
    average = (eps["2025"] + eps["2024"] + eps["2023"]) / 3
    assert supplement["graham_avg3_pe"] == round(close / average, 2)


# ---------------------------------------------------------------------------- 港股估值（TTM）


def test_hk_ttm_rolls_latest_interim_and_converts_currency():
    """00700：TTM = 2025 年报 24.749 + 2026 中报 12.639 − 2025 中报 11.367（人民币）→ 折港币。"""
    result = _hk_screen("00700", 436.6)
    pe = _by(result, "pe")
    ttm_cny = 24.749 + 12.639 - 11.367
    assert pe["basis"]["eps_ttm"] == pytest.approx(ttm_cny * CNY_HKD, rel=1e-6)
    assert pe["value"] == pytest.approx(436.6 / (ttm_cny * CNY_HKD), abs=1e-4)
    assert pe["verdict"] == ("pass" if 436.6 / (ttm_cny * CNY_HKD) <= 15 else "fail")
    assert [c["period"] for c in pe["basis"]["components"]] == [
        "20251231|FY",
        "20260630|H1",
        "20250630|H1",
    ]
    assert [c["sign"] for c in pe["basis"]["components"]] == ["+", "+", "-"]
    assert "TTM = 20251231 年报 + 20260630 中报 − 上年同期中报" in pe["reason"]
    assert "价格 436.6 HKD（2026-09-25）" in pe["reason"]
    # 年报口径参考值：静态 PE 用 2025 年报 EPS，原著三年均值 PE 用 2023-2025
    supplement = pe["supplement"]
    assert supplement["static_pe"] == round(436.6 / (24.749 * CNY_HKD), 2)
    average = (24.749 + 20.938 + 12.186) / 3 * CNY_HKD
    assert supplement["graham_avg3_pe"] == round(436.6 / average, 2)


def test_hk_pb_uses_most_recent_balance_sheet_and_implied_shares():
    """MRQ：2026 中报的归母权益 ÷ 同一报告的隐含股数（净利 / EPS）。"""
    result = _hk_screen("00700", 436.6)
    pb = _by(result, "pb_or_product")
    shares = 114115000000.0 / 12.639
    bvps = 1135853000000.0 * CNY_HKD / shares
    assert pb["basis"]["bvps_period"] == "20260630|H1"
    assert pb["basis"]["bvps"] == pytest.approx(bvps, rel=1e-6)
    assert pb["value"] == pytest.approx(436.6 / bvps, abs=1e-4)
    assert pb["verdict"] == "fail"  # PB ≈ 2.97，PE×PB 超限


def test_hk_inconsistent_interim_eps_falls_back_to_annual():
    """00728 的 2025 中报 EPS 被映射成 2.0（净利 230 亿 → 隐含股数只有年报的 1/8）：
    不得滚动出负的 TTM，退回年报 EPS 并在依据里说明。"""
    result = _hk_screen("00728", 4.39)
    pe = _by(result, "pe")
    assert pe["verdict"] == "pass"
    assert pe["basis"]["method"] == "annual"
    assert pe["basis"]["eps_ttm"] == pytest.approx(0.36 * CNY_HKD)
    assert "中报数据不可用" in pe["basis"]["label"]
    assert "不自洽" in pe["basis"]["note"]


def test_hk_no_newer_interim_uses_annual():
    annual, interim = _hk_fixture("00700")
    statements = market_statements("港股", {"report_statements": annual})
    older = [r for r in interim if r["end_date"] < "20251231"]
    result = compute_graham_screen(
        "港股",
        statements,
        valuation={"price": _price(436.6), "fx_rates": {"CNY": CNY_HKD}, "interim_rows": older},
    )
    pe = _by(result, "pe")
    assert pe["basis"]["label"] == "20251231 年报（无更新中报）"
    assert pe["basis"]["method"] == "annual"


def test_hk_missing_prior_interim_does_not_roll():
    annual, interim = _hk_fixture("00700")
    statements = market_statements("港股", {"report_statements": annual})
    latest_only = [r for r in interim if r["end_date"] == "20260630"]
    result = compute_graham_screen(
        "港股",
        statements,
        valuation={
            "price": _price(436.6),
            "fx_rates": {"CNY": CNY_HKD},
            "interim_rows": latest_only,
        },
    )
    assert "缺上年同期中报" in _by(result, "pe")["basis"]["label"]


def test_hk_missing_fx_rate_is_indeterminate():
    result = _hk_screen("00700", 436.6, fx_rates={})
    pe = _by(result, "pe")
    assert pe["verdict"] == "indeterminate"
    assert "缺 CNY→HKD 汇率" in pe["reason"]
    assert _by(result, "pb_or_product")["verdict"] == "indeterminate"


def test_no_price_is_indeterminate():
    annual, _ = _hk_fixture("00700")
    result = compute_graham_screen(
        "港股",
        market_statements("港股", {"report_statements": annual}),
        valuation={"price": None},
    )
    for key in ("pe", "pb_or_product"):
        item = _by(result, key)
        assert item["verdict"] == "indeterminate"
        assert "无行情价格" in item["reason"]


def test_stale_price_still_computes_but_is_labelled():
    annual, interim = _hk_fixture("00700")
    result = compute_graham_screen(
        "港股",
        market_statements("港股", {"report_statements": annual}),
        valuation={
            "price": _price(436.6, stale=True, age=19),
            "fx_rates": {"CNY": CNY_HKD},
            "interim_rows": interim,
        },
    )
    pe = _by(result, "pe")
    assert pe["verdict"] in ("pass", "fail")
    assert "已陈旧 19 天" in pe["reason"]
    assert pe["basis"]["price_stale"] is True


def test_negative_ttm_eps_fails_as_loss():
    rows = [
        _current(
            {
                "end_date": "20251231",
                "fp": "FY",
                "currency": "HKD",
                "basic_eps": -0.5,
                "n_income_attr_p": -50.0,
                "total_hldr_eqy_exc_min_int": 1000.0,
            }
        ),
    ]
    result = compute_graham_screen(
        "港股",
        market_statements("港股", {"report_statements": rows}),
        valuation={"price": _price(5.0), "fx_rates": {}, "interim_rows": []},
    )
    pe = _by(result, "pe")
    assert pe["verdict"] == "fail"
    assert "亏损" in pe["reason"]
    assert pe["supplement"]["static_pe"] is None


def test_latest_annual_without_eps_is_indeterminate_not_stale_year():
    """最新年报缺 EPS 时不拿更早年份顶替（那不是 TTM）。"""
    rows = [
        _current({"end_date": "20251231", "fp": "FY", "currency": "HKD", "n_income_attr_p": -10.0}),
        _current(
            {
                "end_date": "20241231",
                "fp": "FY",
                "currency": "HKD",
                "basic_eps": 0.3,
                "n_income_attr_p": 30.0,
            }
        ),
    ]
    result = compute_graham_screen(
        "港股",
        market_statements("港股", {"report_statements": rows}),
        valuation={"price": _price(5.0), "fx_rates": {}, "interim_rows": []},
    )
    pe = _by(result, "pe")
    assert pe["verdict"] == "indeterminate"
    assert "20251231 年报缺基本每股盈利" in pe["reason"]


def test_resolve_fx_rates_crosses_via_cny():
    lookup = ExchangeRateLookup(
        [
            Rate("HKD", "CNY", Decimal("0.92"), date(2026, 9, 1)),
            Rate("USD", "CNY", Decimal("7.10"), date(2026, 9, 1)),
        ]
    )
    rates = resolve_fx_rates({"CNY", "USD", "HKD", None, "EUR"}, "HKD", date(2026, 9, 25), lookup)
    assert rates["HKD"] == 1.0
    assert rates["CNY"] == pytest.approx(1 / 0.92)
    assert rates["USD"] == pytest.approx(7.10 / 0.92)
    assert "EUR" not in rates


# ---------------------------------------------------------------------------- 美股估值


def _us_rows(*, form="10-K", quarters=True, version=EDGAR_PIVOT_VERSION):
    annual = []
    for year, eps in ((2025, 4.0), (2024, 3.0), (2023, 2.0)):
        annual.append(
            {
                "end_date": f"{year}0927",
                "fp": "FY",
                "form": form,
                "currency": "USD",
                "basic_eps": eps,
                "n_income_attr_p": eps * 1000.0,
                "total_cur_assets": 5000.0,
                "total_cur_liab": 2000.0,
                "total_hldr_eqy_exc_min_int": 20000.0 + year,
                "n_cashflow_act": 900.0,
                "edgar_chain_version": version,
            }
        )
    interim = []
    if quarters:
        interim = [
            {
                "end_date": "20251227",
                "fp": "Q1",
                "currency": "USD",
                "basic_eps": 1.5,
                "n_income_attr_p": 1500.0,
                "total_hldr_eqy_exc_min_int": 26000.0,
            },
            {
                "end_date": "20241228",
                "fp": "Q1",
                "currency": "USD",
                "basic_eps": 1.0,
                "n_income_attr_p": 1000.0,
            },
            # 只有时点科目的比较列行（不参与 TTM）
            {"end_date": "20260101", "fp": "Q1", "currency": "USD", "total_cur_assets": 1.0},
        ]
    return annual, interim


def test_us_ttm_from_quarters_and_mrq_equity():
    annual, interim = _us_rows()
    result = compute_graham_screen(
        "美股",
        pivot_rows_to_statements(annual),
        valuation={
            "price": _price(60.0, "USD"),
            "fx_rates": {"USD": 1.0},
            "interim_rows": interim,
            "annual_form": "10-K",
        },
    )
    pe = _by(result, "pe")
    assert pe["basis"]["eps_ttm"] == pytest.approx(4.0 + 1.5 - 1.0)
    assert pe["value"] == pytest.approx(60 / 4.5, abs=1e-4)
    assert pe["basis"]["label"] == "TTM = 20250927 年报 + 年报后 1 个单季 − 上年同期单季"
    pb = _by(result, "pb_or_product")
    assert pb["basis"]["bvps_period"] == "20251227|Q1"
    assert pb["basis"]["bvps"] == pytest.approx(26000.0 / 1000.0)


def test_us_20f_annual_with_ads_ratio():
    """20-F 发行人（拼多多）：无季报 → 最新年报；EDGAR 每股按普通股，价格按 ADS（1:4）。"""
    annual, _ = _us_rows(form="20-F", quarters=False)
    result = compute_graham_screen(
        "美股",
        pivot_rows_to_statements(annual),
        valuation={
            "price": _price(77.57, "USD"),
            "fx_rates": {"USD": 1.0},
            "interim_rows": [],
            "annual_form": "20-F",
            "share_ratio": 4,
            "share_ratio_note": "1 ADS = 4 股",
        },
    )
    pe = _by(result, "pe")
    assert pe["basis"]["label"] == "20250927 年报（20-F 发行人不披露季报）"
    assert pe["basis"]["eps_ttm"] == pytest.approx(16.0)
    assert pe["value"] == pytest.approx(77.57 / 16.0, abs=1e-4)
    assert pe["supplement"]["static_pe"] == round(77.57 / 16.0, 2)
    pb = _by(result, "pb_or_product")
    assert pb["basis"]["bvps"] == pytest.approx(22025.0 / 1000.0 * 4)


@pytest.mark.parametrize("share_ratio", [0.125, 0.00125])
def test_model_message_preserves_fractional_ads_valuation_basis(share_ratio):
    annual, _ = _us_rows(form="20-F", quarters=False)
    result = compute_graham_screen(
        "美股",
        pivot_rows_to_statements(annual),
        valuation={
            "price": _price(77.57, "USD"),
            "fx_rates": {"USD": 1.0},
            "interim_rows": [],
            "annual_form": "20-F",
            "share_ratio": share_ratio,
            "share_ratio_note": f"1 ADS = {share_ratio} 股",
        },
    )
    message = build_analysis_messages({"meta": {"market": "美股"}, "graham_screen": result})[1][
        "content"
    ]
    sent = json.loads(message.split("```json\n", 1)[1].split("\n```", 1)[0])["graham_screen"]
    for criterion in ("pe", "pb_or_product"):
        original = _by(result, criterion)
        displayed = _by(sent, criterion)
        assert original["basis"]["share_ratio"] == share_ratio
        assert displayed["basis"] == original["basis"]
        assert displayed["verdict"] == original["verdict"]


def test_us_20f_without_registered_ads_ratio_is_indeterminate():
    annual, _ = _us_rows(form="20-F", quarters=False)
    result = compute_graham_screen(
        "美股",
        pivot_rows_to_statements(annual),
        valuation={
            "price": _price(10.0, "USD"),
            "fx_rates": {"USD": 1.0},
            "share_ratio_missing": True,
            "annual_form": "20-F",
        },
    )
    for key in ("pe", "pb_or_product"):
        assert _by(result, key)["verdict"] == "indeterminate"
        assert "ADS" in _by(result, key)["reason"]


# ---------------------------------------------------------------------------- 分红记录


def _hk_div_rows(amounts, *, cashflow=True):
    """amounts: {year: 已付股息 | None(现金流量表在而未列)}；cashflow=False 表示没有现金流量表。"""
    rows = []
    for year, amount in amounts.items():
        row = {
            "end_date": f"{year}1231",
            "fp": "FY",
            "currency": "HKD",
            "n_income_attr_p": 100.0,
            "basic_eps": 1.0,
        }
        if cashflow:
            row["n_cashflow_act"] = 120.0
        if amount is not None:
            row["div_paid_owners"] = amount
        rows.append(_current(row, kinds=("income", "cashflow") if cashflow else ("income",)))
    return rows


def _dividend(rows, market="港股", **kwargs):
    statements = market_statements(market, {"report_statements": rows})
    return _by(compute_graham_screen(market, statements, **kwargs), "dividend_record")


def test_hk_dividend_consecutive_years_pass():
    item = _dividend(_hk_div_rows({y: 50.0 for y in range(2019, 2026)}))
    assert item["verdict"] == "pass"
    assert "连续 7 年支付股东股息" in item["reason"]


def test_hk_dividend_not_listed_counts_as_zero_and_breaks_streak():
    amounts = {y: 50.0 for y in range(2022, 2026)}
    amounts[2021] = None  # 现金流量表在而未列已付股息 → 0
    amounts[2020] = 50.0
    item = _dividend(_hk_div_rows(amounts))
    assert item["verdict"] == "fail"
    assert "2021 年现金流量表未列已付股息" in item["reason"]


def test_hk_dividend_missing_cashflow_year_is_indeterminate():
    rows = _hk_div_rows({y: 50.0 for y in range(2022, 2026)})
    rows += _hk_div_rows({2021: None}, cashflow=False)  # 该年没有现金流量表
    item = _dividend(rows)
    assert item["verdict"] == "indeterminate"
    assert "2021 年无现金流量表数据" in item["reason"]


def test_hk_dividend_interrupted_is_fail():
    amounts = {y: 50.0 for y in range(2016, 2022)}
    amounts.update({2022: None, 2023: None, 2024: None, 2025: None})
    item = _dividend(_hk_div_rows(amounts))
    assert item["verdict"] == "fail"
    assert "已中断" in item["reason"]


def test_hk_dividend_unknown_for_yahoo_only_rows():
    """雅虎行不带某序列不能推断公司没付：无任何可知年份 → indeterminate。"""
    yahoo = [
        {
            "end_date": f"{y}1231",
            "fp": "FY",
            "currency": "HKD",
            "n_income_attr_p": 1.0,
            "n_cashflow_act": 2.0,
        }
        for y in range(2022, 2026)
    ]
    statements = market_statements("港股", {"yahoo_fundamentals": yahoo})
    item = _by(compute_graham_screen("港股", statements), "dividend_record")
    assert item["verdict"] == "indeterminate"
    assert "待报表重抽" in item["reason"]


def test_us_dividend_absent_concept_is_zero_only_for_current_chain():
    annual, _ = _us_rows(quarters=False)
    statements = pivot_rows_to_statements(annual)  # 无 dividend_absent_means_zero → 不可知
    assert all(row["div_paid_status"] is None for row in statements["cashflow"])
    item = _by(
        compute_graham_screen("美股", market_statements("美股", {"edgar_companyfacts": annual})),
        "dividend_record",
    )
    assert item["verdict"] == "fail"
    assert "现金流量表均未列已付股东股息" in item["reason"]
    old, _ = _us_rows(quarters=False, version=None)
    item = _by(
        compute_graham_screen("美股", market_statements("美股", {"edgar_companyfacts": old})),
        "dividend_record",
    )
    assert item["verdict"] == "indeterminate"
    assert "待重新同步 EDGAR" in item["reason"]


# ---------------------------------------------------------------------------- 长期债务


def _hk_balance(**fields):
    base = {
        "end_date": "20251231",
        "fp": "FY",
        "currency": "HKD",
        "n_income_attr_p": 100.0,
        "total_cur_assets": 40000.0,
        "total_cur_liab": 15000.0,
    }
    base.update(fields)
    return market_statements("港股", {"report_statements": [_current(base)]})


def test_hk_lt_borr_decides_long_term_debt():
    passing = compute_graham_screen("港股", _hk_balance(total_debt=30000.0, lt_borr=20000.0))
    item = _by(passing, "lt_debt_vs_net_current_assets")
    assert item["verdict"] == "pass"
    assert "长期债务（非流动借款）" in item["reason"]
    failing = compute_graham_screen("港股", _hk_balance(total_debt=40000.0, lt_borr=26000.0))
    assert _by(failing, "lt_debt_vs_net_current_assets")["verdict"] == "fail"
    # 没有 lt_borr：退回含短债合计，超出时仍不可归因
    legacy = compute_graham_screen("港股", _hk_balance(total_debt=30000.0))
    assert _by(legacy, "lt_debt_vs_net_current_assets")["verdict"] == "indeterminate"


def test_hk_negative_net_current_assets_fails_without_attribution():
    result = compute_graham_screen(
        "港股",
        _hk_balance(total_cur_assets=10000.0, total_cur_liab=15000.0, total_debt=5000.0),
    )
    item = _by(result, "lt_debt_vs_net_current_assets")
    assert item["verdict"] == "fail"
    assert "净流动资产为负" in item["reason"]


def test_hk_fragility_net_debt_uses_borrowings_when_no_total():
    result = compute_graham_screen(
        "港股",
        _hk_balance(lt_borr=8000.0, st_borr=2000.0, money_cap=4000.0, total_assets=90000.0),
    )
    assert result["fragility"]["net_debt_basis"] == "流动+非流动借款"
    assert result["fragility"]["net_debt_to_assets"] == round((10000.0 - 4000.0) / 90000.0, 4)


def test_us_lt_debt_absent_now_but_reported_before_is_zero():
    annual, _ = _us_rows(quarters=False)
    annual[1]["lt_debt"] = 700.0  # 2024 年报过长期债务，2025 未报
    item = _by(
        compute_graham_screen("美股", pivot_rows_to_statements(annual)),
        "lt_debt_vs_net_current_assets",
    )
    assert item["verdict"] == "pass"
    assert "往年报过" in item["reason"]
    # 旧概念链抓的行：缺概念不代表没有债务
    old, _ = _us_rows(quarters=False, version=None)
    old[1]["lt_debt"] = 700.0
    item = _by(
        compute_graham_screen("美股", pivot_rows_to_statements(old)),
        "lt_debt_vs_net_current_assets",
    )
    assert item["verdict"] == "indeterminate"


def test_edgar_chain_covers_convertible_debt_and_dividends():
    chains = edgar_facts.EDGAR_CONCEPT_CHAINS
    assert chains["lt_debt"][:2] == ("LongTermDebtNoncurrent", "LongTermDebt")
    for concept in (
        "ConvertibleDebtNoncurrent",
        "ConvertibleNotesPayableNoncurrent",
        "LongTermDebtAndCapitalLeaseObligations",
    ):
        assert concept in chains["lt_debt"]
    # v4（#351）：普通股股息概念排前；合计概念（含优先股与非控股股息）只作最后兜底
    assert chains["div_paid_owners"][0] == "PaymentsOfDividendsCommonStock"
    assert chains["div_paid_owners"][-1] == "PaymentsOfDividends"


def test_statement_prompt_fields_for_graham():
    assert {"lt_borr", "st_borr"} <= set(STATEMENT_FIELDS["balance"])
    assert "div_paid_owners" in STATEMENT_FIELDS["cashflow"]
    assert "div_paid_owners" in EXPENSE_MAGNITUDE_FIELDS
    assert STATEMENT_PROMPT_VERSION >= 5


# ---------------------------------------------------------------------------- 短历史文案


def test_short_history_message_depends_on_confirmation():
    rows = [
        _current(
            {
                "end_date": f"{y}1231",
                "fp": "FY",
                "currency": "HKD",
                "n_income_attr_p": 10.0,
                "basic_eps": 0.1,
            }
        )
        for y in range(2019, 2026)
    ]
    statements = market_statements("港股", {"report_statements": rows})
    confirmed = compute_graham_screen("港股", statements, history_confirmed=True)
    assert _by(confirmed, "earnings_stability")["reason"].startswith(
        "披露历史仅 7 年（最早 2019），不足原著十年"
    )
    assert _by(confirmed, "earnings_stability")["verdict"] == "indeterminate"
    unconfirmed = compute_graham_screen("港股", statements, history_confirmed=False)
    assert "已取得的年度数据仅 7 年" in _by(unconfirmed, "earnings_growth")["reason"]


# ---------------------------------------------------------------------------- 取数（DB）


@pytest.fixture
def db():
    session = SessionLocal()
    symbols = ("09999", "ZZTEST")

    def clean():
        session.query(SecurityProfileData).filter(SecurityProfileData.symbol.in_(symbols)).delete(
            synchronize_session=False
        )
        session.query(SecurityPrice).filter(SecurityPrice.symbol.in_(symbols)).delete(
            synchronize_session=False
        )
        session.commit()

    clean()
    try:
        yield session
    finally:
        clean()
        session.close()


def test_compute_graham_for_hk_loads_price_interim_and_fx(db, monkeypatch):
    annual, interim = _hk_fixture("00700")
    for row in annual + interim:
        profile_store.upsert_profile_row(
            db, "09999", "港股", "report_statements", f"{row['end_date']}|{row['fp']}", row
        )
    db.add(
        SecurityPrice(
            symbol="09999",
            market="港股",
            price_date=local_today() - timedelta(days=20),
            currency="HKD",
            close_price=Decimal("400"),
            source="test",
        )
    )
    db.add(
        SecurityPrice(
            symbol="09999",
            market="港股",
            price_date=local_today() - timedelta(days=9),
            currency="HKD",
            close_price=Decimal("436.6"),
            source="hkex-dayquot",
        )
    )
    db.commit()
    lookup = ExchangeRateLookup(
        [Rate("HKD", "CNY", Decimal("1") / Decimal(str(CNY_HKD)), date(2026, 1, 1))]
    )
    monkeypatch.setattr(svc, "_rate_lookup_for", lambda _db, _markets: lookup)

    result = svc.compute_graham_for(db, "09999", "港股")
    pe = _by(result, "pe")
    assert pe["basis"]["price"] == 436.6
    assert pe["basis"]["price_stale"] is True  # 9 天 > 7 天
    assert "已陈旧 9 天" in pe["reason"]
    assert pe["basis"]["method"] == "ttm"
    assert pe["basis"]["eps_ttm"] == pytest.approx((24.749 + 12.639 - 11.367) * CNY_HKD, rel=1e-6)
    # 批量摘要与单标的同口径
    summary = svc.graham_summaries_for(db, [("09999", "港股")])[("09999", "港股")]
    assert summary == {
        "passed": result["passed"],
        "failed": result["failed"],
        "indeterminate": result["indeterminate"],
        "total": 7,
        "as_of_year": "2025",
    }


def test_load_graham_inputs_includes_us_quarters(db):
    annual, interim = _us_rows()
    for row in annual + interim:
        profile_store.upsert_profile_row(
            db, "ZZTEST", "美股", "edgar_companyfacts", f"{row['end_date']}|{row['fp']}", row
        )
    db.commit()
    inputs = svc.load_graham_inputs(db, "ZZTEST", "美股")
    assert [r["fp"] for r in inputs["statement_datasets"]["edgar_companyfacts"]] == ["FY"] * 3
    assert sorted(r["end_date"] for r in inputs["interim_rows"]) == [
        "20241228",
        "20251227",
        "20260101",
    ]
    # 无行情价：估值不可判定而不是报错
    result = svc.compute_graham_for(db, "ZZTEST", "美股")
    assert _by(result, "pe")["verdict"] == "indeterminate"


# ---------------------------------------------------------------------------- PR #232 评审 P2


def _us_quarter_rows(quarters):
    """FY 2025-09-27 EPS=4；quarters: [(期末, EPS)]，同时给出上年同期单季。"""
    annual = [
        {
            "end_date": "20250927",
            "fp": "FY",
            "form": "10-K",
            "currency": "USD",
            "basic_eps": 4.0,
            "n_income_attr_p": 4000.0,
            "total_hldr_eqy_exc_min_int": 30000.0,
        }
    ]
    interim = []
    for end, eps in quarters:
        prior = f"{int(end[:4]) - 1}{end[4:]}"
        interim.append(
            {
                "end_date": end,
                "fp": "Q",
                "currency": "USD",
                "basic_eps": eps,
                "n_income_attr_p": eps * 1000.0,
            }
        )
        interim.append(
            {
                "end_date": prior,
                "fp": "Q",
                "currency": "USD",
                "basic_eps": eps / 2,
                "n_income_attr_p": eps * 500.0,
            }
        )
    return annual, interim


def _us_ttm(quarters):
    annual, interim = _us_quarter_rows(quarters)
    result = compute_graham_screen(
        "美股",
        pivot_rows_to_statements(annual),
        valuation={
            "price": _price(70.0, "USD"),
            "fx_rates": {"USD": 1.0},
            "interim_rows": interim,
            "annual_form": "10-K",
        },
    )
    return _by(result, "pe")


def test_us_ttm_missing_first_quarter_does_not_roll():
    """评审复现：FY 2025-09-27，库里只有 2026-03-28 的 Q2（缺 2025-12 Q1）——不得把
    「年报 + Q2 − 上年 Q2」当 TTM（EPS=5、PE=14 pass）。"""
    pe = _us_ttm([("20260328", 2.0)])
    assert pe["basis"]["method"] == "annual"
    assert pe["basis"]["eps_ttm"] == pytest.approx(4.0)
    assert "单季不连续" in pe["basis"]["label"]
    assert "20250927 至 20260328 间隔 182 天" in pe["basis"]["note"]


def test_us_ttm_missing_middle_quarter_does_not_roll():
    pe = _us_ttm([("20251227", 1.0), ("20260627", 1.0)])  # 缺 2026-03 Q2
    assert pe["basis"]["method"] == "annual"
    assert "20251227 至 20260627 间隔 182 天" in pe["basis"]["note"]


def test_us_ttm_contiguous_52_53_week_quarters_roll():
    # 13 周 + 13 周 + 14 周（53 周财年的末季）：91 / 91 / 98 天都算连续
    pe = _us_ttm([("20251227", 1.0), ("20260328", 1.0), ("20260704", 1.0)])
    assert pe["basis"]["method"] == "ttm"
    assert pe["basis"]["eps_ttm"] == pytest.approx(4.0 + 3.0 - 1.5)
    assert pe["basis"]["label"] == "TTM = 20250927 年报 + 年报后 3 个单季 − 上年同期单季"


def test_hk_interim_path_requires_adjacent_half_year():
    """港股只滚最新一期中报（6 个月累计值，不存在缺季问题）；中报与年报不衔接时不滚动。"""
    annual, interim = _hk_fixture("00700")
    statements = market_statements(
        "港股", {"report_statements": [r for r in annual if r["end_date"] < "20251231"]}
    )
    result = compute_graham_screen(
        "港股",
        statements,
        valuation={"price": _price(436.6), "fx_rates": {"CNY": CNY_HKD}, "interim_rows": interim},
    )
    pe = _by(result, "pe")
    # 最新年报 2024-12-31，一年内最新中报 2025-06-30（181 天）可滚动；2026 中报超出一年不取
    assert pe["basis"]["method"] == "ttm"
    assert [c["period"] for c in pe["basis"]["components"]][1] == "20250630|H1"


def test_hk_dividend_recent_years_unknown_is_not_interruption():
    """评审复现：2017–2021 年报有现金流量表且支付股息，2022–2025 只有利润表 → 最近已知的正值
    年份不证明之后没付，判 indeterminate 而非「已中断」。"""
    rows = _hk_div_rows({y: 50.0 for y in range(2017, 2022)})
    rows += _hk_div_rows({y: None for y in range(2022, 2026)}, cashflow=False)
    item = _dividend(rows)
    assert item["verdict"] == "indeterminate"
    assert "已知最近一次支付股东股息为 2021 年" in item["reason"]
    assert "2024、2025 年无现金流量表已付股息数据" in item["reason"]


def test_hk_dividend_only_early_zero_years_is_indeterminate():
    rows = _hk_div_rows({2017: None, 2018: None})  # 早年现金流量表在而未列（按 0）
    rows += _hk_div_rows({y: None for y in range(2019, 2026)}, cashflow=False)
    item = _dividend(rows)
    assert item["verdict"] == "indeterminate"


def test_hk_dividend_window_known_zero_is_interruption():
    rows = _hk_div_rows({y: 50.0 for y in range(2017, 2022)})
    rows += _hk_div_rows({y: None for y in range(2022, 2024)}, cashflow=False)
    rows += _hk_div_rows({2024: 0.0, 2025: None})  # 锚定窗口 2024–2025 已知为 0
    item = _dividend(rows)
    assert item["verdict"] == "fail"
    assert "已中断" in item["reason"]


# 「安全边际充足」接真实计算输出（不是手写 screen）


MARGIN = (
    '{"tags":["安全边际充足"],"risk_level":"medium","summary":"s","report_markdown":"'
    + FULL_REPORT_JSON
    + '"}'
)


def _a_share_statements_all_pass():
    income = [
        {"end_date": f"{y}1231", "n_income_attr_p": 100.0, "basic_eps": 1.0 + (y - 2016) * 0.1}
        for y in range(2016, 2026)
    ]
    balance = [
        {
            "end_date": f"{y}1231",
            "total_cur_assets": 50000.0,
            "total_cur_liab": 20000.0,
            "lt_borr": 1000.0,
            "total_assets": 100000.0,
            "total_liab": 30000.0,
            "money_cap": 20000.0,
        }
        for y in range(2016, 2026)
    ]
    return {"income": income, "balancesheet": balance, "cashflow": [], "fina_indicator": []}


def test_margin_of_safety_a_share_snapshot_from_real_output():
    """评审复现：A股 快照 basis 也带 price（收盘价），不得被当成估算型估值拒绝。"""
    from app.services.graham_screen import _daily_basic_valuation
    from app.services.security_analysis_prompts import (
        margin_of_safety_allowed,
        parse_analysis_output,
    )

    pe, pb = _daily_basic_valuation(
        "A股",
        {"pe_ttm": 10.0, "pb": 1.0, "trade_date": "20260925"},
        [],
    )
    screen = {
        "status": "ok",
        "criteria": [
            pe,
            pb,
            {"criterion": "current_ratio", "verdict": "pass"},
            {"criterion": "lt_debt_vs_net_current_assets", "verdict": "pass"},
        ],
    }
    assert pe["basis"]["valuation_method"] == "snapshot"
    assert margin_of_safety_allowed(screen, "A股")
    # 完整计算链：compute_graham_screen（快照带收盘价）→ 解析层标签校验
    result = compute_graham_screen(
        "A股",
        _a_share_statements_all_pass(),
        daily_basic_rows=[{"trade_date": "20260925", "close": 12.0, "pe_ttm": 10.0, "pb": 1.0}],
    )
    assert _by(result, "pe")["basis"]["price"] == 12.0
    assert all(
        _by(result, key)["verdict"] == "pass"
        for key in ("pe", "pb_or_product", "current_ratio", "lt_debt_vs_net_current_assets")
    )
    assert parse_analysis_output(MARGIN, market="A股", graham_screen=result)["tags"] == [
        "安全边际充足"
    ]


def _hk_all_pass_screen(**price_kwargs):
    rows = [
        _current(
            {
                "end_date": "20251231",
                "fp": "FY",
                "currency": "HKD",
                "basic_eps": 1.0,
                "n_income_attr_p": 100.0,
                "total_hldr_eqy_exc_min_int": 1000.0,
                "total_cur_assets": 40000.0,
                "total_cur_liab": 15000.0,
                "lt_borr": 1000.0,
            }
        )
    ]
    return compute_graham_screen(
        "港股",
        market_statements("港股", {"report_statements": rows}),
        valuation={"price": _price(9.0, **price_kwargs), "fx_rates": {}, "interim_rows": []},
    )


def test_margin_of_safety_hk_estimated_from_real_output():
    from app.services.security_analysis_prompts import parse_analysis_output

    result = _hk_all_pass_screen()
    assert _by(result, "pe")["basis"]["valuation_method"] == "estimated"
    assert parse_analysis_output(MARGIN, market="港股", graham_screen=result)["tags"] == [
        "安全边际充足"
    ]
    stale = _hk_all_pass_screen(stale=True, age=15)
    assert _by(stale, "pe")["verdict"] == "pass"  # 陈价照算、照判
    with pytest.raises(ValueError, match="安全边际充足"):
        parse_analysis_output(MARGIN, market="港股", graham_screen=stale)
    # 估算型结果即使调用方没传 market，也按 basis.valuation_method 走估算口径
    with pytest.raises(ValueError, match="安全边际充足"):
        parse_analysis_output(MARGIN, graham_screen=stale)


def test_margin_of_safety_us_rejects_unrolled_ttm_from_real_output():
    from app.services.security_analysis_prompts import parse_analysis_output

    annual, interim = _us_quarter_rows([("20260328", 2.0)])  # 缺首季 → 退回年报并 note
    annual[0].update({"total_cur_assets": 50000.0, "total_cur_liab": 20000.0, "lt_debt": 100.0})
    result = compute_graham_screen(
        "美股",
        pivot_rows_to_statements(annual),
        valuation={
            "price": _price(30.0, "USD", day="2026-06-30"),
            "fx_rates": {"USD": 1.0},
            "interim_rows": interim,
            "annual_form": "10-K",
        },
    )
    assert all(
        _by(result, key)["verdict"] == "pass"
        for key in ("pe", "pb_or_product", "current_ratio", "lt_debt_vs_net_current_assets")
    )
    with pytest.raises(ValueError, match="安全边际充足"):
        parse_analysis_output(MARGIN, market="美股", graham_screen=result)
