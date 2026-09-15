"""graham_screen 纯函数金样：三市场固件逐准则断言，含 indeterminate 路径。

构造原则：每个准则至少一个 pass、一个 fail、一个 indeterminate 用例；
"数据不足绝不冒充判定"是本层的核心契约（港股年限、非 A股 估值、
港股 total_debt 含短债的不可归因场景）。
"""

from app.services.graham_screen import (
    GRAHAM_DEFENSIVE_THRESHOLDS,
    compute_graham_screen,
)


def _verdicts(result):
    return {item["criterion"]: item["verdict"] for item in result["criteria"]}


def _reasons(result):
    return {item["criterion"]: item["reason"] for item in result["criteria"]}


def _a_share_statements(years, *, loss_years=(), eps_first=1.0, eps_last=1.5):
    """A股年度报表固件：流动比率 2.5、长债 < 净流动资产、净利润为正。"""
    income, balance = [], []
    span = max(len(years) - 1, 1)
    for idx, year in enumerate(sorted(years)):
        eps = eps_first + (eps_last - eps_first) * idx / span
        profit = -1000.0 if year in loss_years else 10_000.0
        income.append({
            "end_date": f"{year}1231",
            "n_income_attr_p": profit,
            "basic_eps": eps,
            "operate_profit": 12_000.0,
            "int_exp": 1_000.0,
        })
        balance.append({
            "end_date": f"{year}1231",
            "total_cur_assets": 50_000.0,
            "total_cur_liab": 20_000.0,
            "total_assets": 100_000.0,
            "total_liab": 40_000.0,
            "money_cap": 30_000.0,
            "st_borr": 5_000.0,
            "lt_borr": 8_000.0,
            "bond_payable": 2_000.0,
            "non_cur_liab_due_1y": 1_000.0,
        })
    return {"income": income, "balancesheet": balance, "cashflow": [], "fina_indicator": []}


def _dividends(years):
    return [
        {"end_date": f"{year}1231", "div_proc": "实施", "cash_div_tax": 0.5}
        for year in years
    ]


DAILY_BASIC_CHEAP = [
    {"trade_date": "20260810", "pe_ttm": 9.0, "pb": 1.1, "total_mv": 50_000.0},
    {"trade_date": "20260801", "pe_ttm": 99.0, "pb": 9.0, "total_mv": 50_000.0},  # 旧行不得选中
]


class TestAShareFullPass:
    def test_ten_year_defensive_stock_passes_everything(self):
        years = range(2016, 2026)
        result = compute_graham_screen(
            "A股",
            _a_share_statements(years),
            daily_basic_rows=DAILY_BASIC_CHEAP,
            dividend_rows=_dividends(range(2019, 2026)),
        )
        assert result["status"] == "ok"
        assert result["as_of_year"] == "2025"
        verdicts = _verdicts(result)
        assert verdicts == {
            "current_ratio": "pass",
            "lt_debt_vs_net_current_assets": "pass",
            "earnings_stability": "pass",
            "dividend_record": "pass",
            "earnings_growth": "pass",
            "pe": "pass",
            "pb_or_product": "pass",
        }
        assert result["passed"] == 7
        assert result["failed"] == 0
        # 估值快照必须取 trade_date 最新一行（旧行 PE 99 不得混入）
        assert "9.00" in _reasons(result)["pe"]
        assert "2016→2025 连续 10 年" in _reasons(result)["earnings_stability"]

    def test_fragility_signals_and_market_cap_units(self):
        result = compute_graham_screen(
            "A股",
            _a_share_statements(range(2016, 2026)),
            daily_basic_rows=DAILY_BASIC_CHEAP,
            dividend_rows=_dividends(range(2019, 2026)),
        )
        fragility = result["fragility"]
        assert fragility["debt_to_assets"] == 0.4
        # 有息负债 16000 − 货币资金 30000 = 净现金 14000
        assert fragility["net_debt_to_assets"] == -0.14
        assert fragility["interest_coverage"] == 12.0
        # total_mv 单位万元：50000 万 = 5 亿元；净现金 14000 元 → 比例极小但符号为正
        assert fragility["net_cash_to_market_cap"] == round(14_000 / 500_000_000, 4)


class TestFailPaths:
    def test_loss_year_fails_stability_regardless_of_coverage(self):
        result = compute_graham_screen(
            "A股",
            _a_share_statements(range(2023, 2026), loss_years={2024}),
            daily_basic_rows=DAILY_BASIC_CHEAP,
        )
        verdicts = _verdicts(result)
        assert verdicts["earnings_stability"] == "fail"
        assert "2024" in _reasons(result)["earnings_stability"]

    def test_pb_over_limit_but_product_clause_saves_it(self):
        rows = [{"trade_date": "20260810", "pe_ttm": 10.0, "pb": 2.0, "total_mv": 1.0}]
        result = compute_graham_screen(
            "A股", _a_share_statements(range(2016, 2026)), daily_basic_rows=rows,
        )
        assert _verdicts(result)["pb_or_product"] == "pass"
        assert "放宽条款" in _reasons(result)["pb_or_product"]

    def test_pb_and_product_both_over_fail(self):
        rows = [{"trade_date": "20260810", "pe_ttm": 20.0, "pb": 2.0, "total_mv": 1.0}]
        result = compute_graham_screen(
            "A股", _a_share_statements(range(2016, 2026)), daily_basic_rows=rows,
        )
        verdicts = _verdicts(result)
        assert verdicts["pe"] == "fail"
        assert verdicts["pb_or_product"] == "fail"

    def test_negative_pe_is_fail_not_cheap(self):
        rows = [{"trade_date": "20260810", "pe_ttm": -5.0, "pb": 0.8, "total_mv": 1.0}]
        result = compute_graham_screen(
            "A股", _a_share_statements(range(2016, 2026)), daily_basic_rows=rows,
        )
        assert _verdicts(result)["pe"] == "fail"
        assert "为负" in _reasons(result)["pe"]

    # ---- 评审 P1 回归：有记录 ≠ 记录连续/锚定当前 ----

    def test_loss_outside_anchored_window_does_not_fail(self):
        # 2014/2015 亏损、2016-2025 连续十年为正：亏损在锚定窗口外，应 pass
        # （years 最多取 12 年，窗口外旧亏损不得推翻已声明的"连续十年为正"语义）
        statements = _a_share_statements(range(2014, 2026), loss_years={2014, 2015})
        result = compute_graham_screen("A股", statements)
        stability = next(c for c in result["criteria"] if c["criterion"] == "earnings_stability")
        assert stability["verdict"] == "pass"
        assert "2016→2025 连续 10 年" in stability["reason"]

    def test_loss_inside_window_still_fails_and_names_window(self):
        statements = _a_share_statements(range(2014, 2026), loss_years={2016})
        result = compute_graham_screen("A股", statements)
        stability = next(c for c in result["criteria"] if c["criterion"] == "earnings_stability")
        assert stability["verdict"] == "fail"
        assert "2016→2025 窗口内" in stability["reason"]
        assert "2016" in stability["reason"]

    def test_growth_uses_anchored_ten_year_window_not_all_twelve_years(self):
        # 评审复现：2014 EPS=1、2015-2025 EPS=2 → 全段 2014→2025 是 +100%，
        # 但十年窗口 2016→2025 增长为 0，必须 fail
        statements = _a_share_statements(range(2014, 2026))
        for row in statements["income"]:
            row["basic_eps"] = 1.0 if row["end_date"].startswith("2014") else 2.0
        result = compute_graham_screen("A股", statements)
        growth = next(c for c in result["criteria"] if c["criterion"] == "earnings_growth")
        assert growth["verdict"] == "fail"
        assert "2016→2025" in growth["reason"]
        assert growth["value"] == 0.0

    def test_growth_endpoint_missing_is_indeterminate_not_shifted(self):
        # 窗口首年 2016 缺失：不得用 2017 或 2015 顶替端点
        statements = _a_share_statements(range(2014, 2026), eps_first=1.0, eps_last=3.0)
        statements["income"] = [
            row for row in statements["income"] if not row["end_date"].startswith("2016")
        ]
        result = compute_graham_screen("A股", statements)
        growth = next(c for c in result["criteria"] if c["criterion"] == "earnings_growth")
        assert growth["verdict"] == "indeterminate"
        assert "2016" in growth["reason"]

    def test_ten_positive_years_with_a_gap_is_indeterminate(self):
        # 2014-2018 + 2020-2025 共 11 个正利润年，但缺 2019 → 连续锚定只有 6 年
        statements = _a_share_statements(list(range(2014, 2019)) + list(range(2020, 2026)))
        result = compute_graham_screen("A股", statements)
        stability = next(c for c in result["criteria"] if c["criterion"] == "earnings_stability")
        assert stability["verdict"] == "indeterminate"
        assert "2019" in stability["reason"]
        assert "连续覆盖 6 年" in stability["reason"]

    def test_missing_latest_income_year_is_indeterminate(self):
        # 资产负债表到 2025，利润表只到 2024：末年缺失 → 以 2025 为锚连续覆盖 0 年
        statements = _a_share_statements(range(2015, 2026))
        statements["income"] = [row for row in statements["income"] if not row["end_date"].startswith("2025")]
        result = compute_graham_screen("A股", statements)
        stability = next(c for c in result["criteria"] if c["criterion"] == "earnings_stability")
        assert stability["verdict"] == "indeterminate"
        assert "2025" in stability["reason"]

    def test_stale_dividend_streak_fails_even_if_long(self):
        # 2015-2019 连续五年分红、2020-2025 停分：不得判 pass
        result = compute_graham_screen(
            "A股",
            _a_share_statements(range(2016, 2026)),
            dividend_rows=_dividends(range(2015, 2020)),
        )
        dividend = next(c for c in result["criteria"] if c["criterion"] == "dividend_record")
        assert dividend["verdict"] == "fail"
        assert "已中断" in dividend["reason"]

    def test_dividend_lag_one_year_is_tolerated(self):
        # 最新报告 2025，最近分红实施 2024（年报分红次年实施）→ 仍按连续计数
        result = compute_graham_screen(
            "A股",
            _a_share_statements(range(2016, 2026)),
            dividend_rows=_dividends(range(2019, 2025)),
        )
        dividend = next(c for c in result["criteria"] if c["criterion"] == "dividend_record")
        assert dividend["verdict"] == "pass"
        assert "连续现金分红 6 年" in dividend["reason"]

    def test_negative_pb_is_fail_not_margin_of_safety(self):
        rows = [{"trade_date": "20260810", "pe_ttm": 8.0, "pb": -0.5, "total_mv": 1.0}]
        result = compute_graham_screen(
            "A股", _a_share_statements(range(2016, 2026)), daily_basic_rows=rows,
        )
        pb = next(c for c in result["criteria"] if c["criterion"] == "pb_or_product")
        assert pb["verdict"] == "fail"
        assert "净资产非正" in pb["reason"]

    def test_short_dividend_streak_fails(self):
        result = compute_graham_screen(
            "A股",
            _a_share_statements(range(2016, 2026)),
            daily_basic_rows=DAILY_BASIC_CHEAP,
            dividend_rows=_dividends([2025, 2024, 2021]),  # 连续仅 2 年
        )
        assert _verdicts(result)["dividend_record"] == "fail"
        assert "连续现金分红 2 年" in _reasons(result)["dividend_record"]


def _hk_statements(years, *, total_debt=10_000.0):
    income, balance = [], []
    for idx, year in enumerate(sorted(years)):
        income.append({
            "end_date": f"{year}1231", "fp": "FY",
            "n_income_attr_p": 8_000.0,
            "basic_eps": 1.0 + 0.2 * idx,
            "operating_income": 9_000.0,
            "int_exp": 500.0,
        })
        balance.append({
            "end_date": f"{year}1231", "fp": "FY",
            "total_cur_assets": 40_000.0,
            "total_cur_liab": 15_000.0,
            "total_assets": 90_000.0,
            "total_liab": 30_000.0,
            "money_cap": 20_000.0,
            "total_debt": total_debt,
        })
    return {"income": income, "balancesheet": balance, "cashflow": [], "fina_indicator": []}


class TestHkBoundaries:
    def test_short_history_all_positive_is_indeterminate_not_pass(self):
        result = compute_graham_screen("港股", _hk_statements(range(2022, 2026)))
        verdicts = _verdicts(result)
        assert verdicts["earnings_stability"] == "indeterminate"
        assert "不足原著 10 年" in _reasons(result)["earnings_stability"]
        # 无估值/分红数据源：如实 indeterminate，不冒充判定
        assert verdicts["pe"] == "indeterminate"
        assert verdicts["pb_or_product"] == "indeterminate"
        assert verdicts["dividend_record"] == "indeterminate"

    def test_total_debt_within_nca_gives_conservative_pass(self):
        # 含短债的合计都 ≤ 净流动资产（25000），长期债务必然满足 → 可判 pass
        result = compute_graham_screen("港股", _hk_statements(range(2022, 2026), total_debt=10_000.0))
        assert _verdicts(result)["lt_debt_vs_net_current_assets"] == "pass"

    def test_total_debt_exceeding_nca_cannot_be_attributed(self):
        # 合计超出但无法区分长短债 → indeterminate 而非 fail
        result = compute_graham_screen("港股", _hk_statements(range(2022, 2026), total_debt=30_000.0))
        assert _verdicts(result)["lt_debt_vs_net_current_assets"] == "indeterminate"
        assert "无法单独判定长期债务" in _reasons(result)["lt_debt_vs_net_current_assets"]


def _us_statements(years):
    income, balance = [], []
    for idx, year in enumerate(sorted(years)):
        income.append({
            "end_date": f"{year}0930", "fp": "FY",  # 美股财年不止于 12/31
            "n_income_attr_p": 5_000.0,
            "basic_eps": 2.0 + 0.5 * idx,
            "operating_income": 6_000.0,
            "int_exp": 300.0,
        })
        balance.append({
            "end_date": f"{year}0930", "fp": "FY",
            "total_cur_assets": 30_000.0,
            "total_cur_liab": 12_000.0,
            "total_assets": 80_000.0,
            "total_liab": 35_000.0,
            "money_cap": 15_000.0,
            "lt_debt": 9_000.0,
        })
    return {"income": income, "balancesheet": balance, "cashflow": [], "fina_indicator": []}


class TestUsMarket:
    def test_lt_debt_basis_is_annotated(self):
        result = compute_graham_screen("美股", _us_statements(range(2016, 2026)))
        verdicts = _verdicts(result)
        assert verdicts["lt_debt_vs_net_current_assets"] == "pass"
        assert "仅长期债务" in _reasons(result)["lt_debt_vs_net_current_assets"]
        assert result["fragility"]["net_debt_basis"].startswith("仅长期债务")

    def test_eps_growth_computed_from_fiscal_year_rows(self):
        result = compute_graham_screen("美股", _us_statements(range(2016, 2026)))
        growth = next(
            c for c in result["criteria"] if c["criterion"] == "earnings_growth"
        )
        assert growth["verdict"] == "pass"  # 2.0 → 6.5，远超 33%
        assert "每股盈利" in growth["reason"]


class TestDegenerateInputs:
    def test_no_statements_returns_no_data(self):
        result = compute_graham_screen(
            "A股", {"income": [], "balancesheet": [], "cashflow": [], "fina_indicator": []},
        )
        assert result["status"] == "no_data"
        assert "criteria_semantics" in result

    def test_missing_balance_fields_indeterminate(self):
        statements = {
            "income": [{"end_date": "20251231", "n_income_attr_p": 100.0}],
            "balancesheet": [{"end_date": "20251231"}],
            "cashflow": [],
            "fina_indicator": [],
        }
        result = compute_graham_screen("A股", statements)
        verdicts = _verdicts(result)
        assert verdicts["current_ratio"] == "indeterminate"
        assert verdicts["lt_debt_vs_net_current_assets"] == "indeterminate"
        assert result["fragility"] == {}

    def test_growth_falls_back_to_net_profit_when_eps_sparse(self):
        # EPS 只有 3 年、净利润有 10 年：应回退净利润口径计算而非 indeterminate
        statements = _a_share_statements(range(2016, 2026), eps_first=1.0, eps_last=2.0)
        for idx, row in enumerate(statements["income"]):
            if idx < 7:
                row.pop("basic_eps")
            row["n_income_attr_p"] = 10_000.0 + 1_000.0 * idx  # 期末/期初 = 1.9
        result = compute_graham_screen("A股", statements)
        growth = next(c for c in result["criteria"] if c["criterion"] == "earnings_growth")
        assert growth["verdict"] == "pass"
        assert "净利润" in growth["reason"]

    def test_growth_needs_positive_base(self):
        statements = _a_share_statements(range(2016, 2026), eps_first=-1.0, eps_last=2.0)
        result = compute_graham_screen("A股", statements)
        assert _verdicts(result)["earnings_growth"] == "indeterminate"
        assert "非正" in _reasons(result)["earnings_growth"]

    def test_thresholds_exported_for_ui(self):
        result = compute_graham_screen("A股", _a_share_statements(range(2016, 2026)))
        assert result["thresholds"] == GRAHAM_DEFENSIVE_THRESHOLDS
