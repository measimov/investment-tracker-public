"""利润质量指标手算向量（纯函数）：CFO/NI、应计率、增速差、扣非占比、M-score。"""

from app.services.earnings_quality import (
    compute_earnings_quality,
    pivot_rows_to_statements,
)


def _income(year, revenue, ni, sell_exp=100.0, admin_exp=50.0):
    return {"end_date": f"{year}1231", "total_revenue": revenue,
            "n_income_attr_p": ni, "sell_exp": sell_exp, "admin_exp": admin_exp}


def _balance(year, assets, receivable, inventory, cur_assets, ppe, liab):
    return {"end_date": f"{year}1231", "total_assets": assets,
            "accounts_receiv": receivable, "inventories": inventory,
            "total_cur_assets": cur_assets, "fix_assets": ppe, "total_liab": liab}


def _cashflow(year, cfo, depreciation=80.0):
    return {"end_date": f"{year}1231", "n_cashflow_act": cfo,
            "depr_fa_coga_dpba": depreciation}


def _fina(year, gross_margin, net_margin, profit_dedt):
    return {"end_date": f"{year}1231", "grossprofit_margin": gross_margin,
            "netprofit_margin": net_margin, "profit_dedt": profit_dedt}


def test_core_ratios_hand_computed():
    """手算：2026 年 NI=200、CFO=150、总资产=2000 → CFO/NI=0.75、应计率=0.025；
    应收 100→150（+50%）营收 1000→1100（+10%）→ 增速差 40pp；扣非 160/200=0.8。"""
    result = compute_earnings_quality(
        income_rows=[_income(2026, 1100.0, 200.0), _income(2025, 1000.0, 180.0)],
        balancesheet_rows=[
            _balance(2026, 2000.0, 150.0, 300.0, 800.0, 600.0, 1000.0),
            _balance(2025, 1800.0, 100.0, 250.0, 700.0, 550.0, 900.0),
        ],
        cashflow_rows=[_cashflow(2026, 150.0), _cashflow(2025, 170.0)],
        fina_indicator_rows=[_fina(2026, 40.0, 18.0, 160.0), _fina(2025, 42.0, 18.0, 150.0)],
    )
    assert result["status"] == "ok"
    year = result["per_year"]["2026"]
    assert year["cfo_ni_ratio"] == 0.75
    assert year["accruals_ratio"] == 0.025  # (200-150)/2000
    assert year["receivable_vs_revenue_gap_pp"] == 40.0  # 50% - 10%
    assert year["inventory_vs_revenue_gap_pp"] == 10.0  # 20% - 10%
    assert year["recurring_profit_share"] == 0.8
    assert year["gross_margin"] == 40.0
    # 5 年累计（两年可得）：(150+170)/(200+180)
    assert result["cfo_ni_ratio_5y"] == round(320 / 380, 4)


def test_beneish_m_score_hand_computed():
    """M-score 因子手算：构造两年完整科目，逐因子对照公式。"""
    income = [_income(2026, 1100.0, 200.0, sell_exp=110.0, admin_exp=55.0),
              _income(2025, 1000.0, 180.0, sell_exp=100.0, admin_exp=50.0)]
    balance = [
        _balance(2026, 2000.0, 150.0, 300.0, 800.0, 600.0, 1000.0),
        _balance(2025, 1800.0, 100.0, 250.0, 700.0, 550.0, 900.0),
    ]
    cashflow = [_cashflow(2026, 150.0, depreciation=90.0),
                _cashflow(2025, 170.0, depreciation=80.0)]
    fina = [_fina(2026, 40.0, 18.0, 160.0), _fina(2025, 42.0, 18.0, 150.0)]

    result = compute_earnings_quality(
        income_rows=income, balancesheet_rows=balance,
        cashflow_rows=cashflow, fina_indicator_rows=fina,
    )
    entry = result["beneish_m_score"]["2026"]
    factors = entry["factors"]

    dsri = (150 / 1100) / (100 / 1000)
    gmi = 42.0 / 40.0
    aqi = (1 - (800 + 600) / 2000) / (1 - (700 + 550) / 1800)
    sgi = 1100 / 1000
    depi = (80 / (80 + 550)) / (90 / (90 + 600))
    sgai = (165 / 1100) / (150 / 1000)
    lvgi = (1000 / 2000) / (900 / 1800)
    tata = (200 - 150) / 2000
    assert factors["DSRI"] == round(dsri, 4)
    assert factors["GMI"] == round(gmi, 4)
    assert factors["AQI"] == round(aqi, 4)
    assert factors["SGI"] == round(sgi, 4)
    assert factors["DEPI"] == round(depi, 4)
    assert factors["SGAI"] == round(sgai, 4)
    assert factors["LVGI"] == round(lvgi, 4)
    assert factors["TATA"] == round(tata, 4)

    expected = (-4.84 + 0.92 * dsri + 0.528 * gmi + 0.404 * aqi + 0.892 * sgi
                + 0.115 * depi - 0.172 * sgai + 4.679 * tata - 0.327 * lvgi)
    assert entry["score"] == round(expected, 3)
    assert entry["flag"] == (expected > -1.78)


def test_missing_fields_degrade_gracefully():
    """缺前一年数据：比率类为 None、M-score 不计；无年度行 → no_data。"""
    result = compute_earnings_quality(
        income_rows=[_income(2026, 1100.0, 200.0)],
        balancesheet_rows=[_balance(2026, 2000.0, 150.0, 300.0, 800.0, 600.0, 1000.0)],
        cashflow_rows=[_cashflow(2026, 150.0)],
        fina_indicator_rows=[],
    )
    year = result["per_year"]["2026"]
    assert year["cfo_ni_ratio"] == 0.75
    assert year["receivable_vs_revenue_gap_pp"] is None  # 无上年
    assert year["recurring_profit_share"] is None  # 无 fina_indicator
    assert result["beneish_m_score"] == {}

    empty = compute_earnings_quality([], [], [], [])
    assert empty["status"] == "no_data"
    assert "metric_semantics" in empty


def test_quarterly_rows_are_ignored():
    """非年度行（中报/季报）不进入年度指标。"""
    result = compute_earnings_quality(
        income_rows=[
            _income(2026, 1100.0, 200.0),
            {"end_date": "20260630", "total_revenue": 500.0, "n_income_attr_p": 90.0},
        ],
        balancesheet_rows=[_balance(2026, 2000.0, 150.0, 300.0, 800.0, 600.0, 1000.0)],
        cashflow_rows=[_cashflow(2026, 150.0)],
        fina_indicator_rows=[],
    )
    assert result["years"] == ["2026"]


# ---------------------------------------------------------------------------
# EDGAR 透视行 → 报表形状映射（美股复用同一套指标函数）
# ---------------------------------------------------------------------------


def _edgar_fy(year, **fields):
    # Apple 式非日历财年（9 月末）——年度语义由 fp=FY 承载，不能依赖 1231 后缀
    return {"end_date": f"{year}0927", "fp": "FY", **fields}


def test_edgar_rows_map_to_statement_shape():
    """手算：FY 行映射为 A股报表伪行；毛利率/净利率由收入成本计算；
    SGA 挂 sell_exp 位；季度行剔除；扣非无对应概念留空。

    [实测回归] 财年止于非 12/31（Apple 9 月末）：指标层必须凭 fp=FY 识别
    年度行，否则 per_year 全空。"""
    rows = [
        _edgar_fy(
            2025, total_revenue=1000.0, cost_of_revenue=600.0,
            n_income_attr_p=200.0, sga_exp=120.0, total_assets=2000.0,
            total_liab=900.0, accounts_receiv=150.0, inventories=80.0,
            total_cur_assets=700.0, fix_assets=500.0,
            n_cashflow_act=260.0, depr_fa_coga_dpba=90.0,
        ),
        {"end_date": "20250628", "fp": "Q3", "total_revenue": 250.0},  # 季度行剔除
        _edgar_fy(2024, total_revenue=900.0, n_income_attr_p=150.0,
                  total_assets=1800.0, n_cashflow_act=180.0),
    ]
    stmts = pivot_rows_to_statements(rows)
    assert [r["end_date"] for r in stmts["income"]] == ["20250927", "20240927"]
    income = stmts["income"][0]
    assert income["sell_exp"] == 120.0  # SGA 合并科目挂 sell_exp 位
    assert income["admin_exp"] is None
    fina = stmts["fina_indicator"][0]
    assert fina["grossprofit_margin"] == 40.0  # (1000-600)/1000
    assert fina["netprofit_margin"] == 20.0
    assert fina["profit_dedt"] is None  # 无扣非概念
    # 缺 cost_of_revenue（2024）→ 毛利率留空而非报错
    assert stmts["fina_indicator"][1]["grossprofit_margin"] is None

    # 端到端：映射产物可直接进指标函数
    quality = compute_earnings_quality(
        stmts["income"], stmts["balancesheet"],
        stmts["cashflow"], stmts["fina_indicator"],
    )
    assert quality["per_year"]["2025"]["cfo_ni_ratio"] == 1.3  # 260/200
    assert quality["per_year"]["2025"]["gross_margin"] == 40.0
