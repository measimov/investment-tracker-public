"""利润质量客观指标（纯函数，无 DB/网络——藏利润/造假识别的客观基座）。

LLM 读摘要找"会计技巧"不可靠：先从已入库的三大报表/财务指标行预计算
客观指标喂给它。输入为 security_profile_data 的 payload 行（全字段，
非 LLM 白名单），年度合并报表口径（end_date=XXXX1231）。

指标与红旗语义（metric_semantics 随输出携带，向 LLM 解释阈值）：
- CFO/净利润：长期 <0.8 = 利润未转化为现金的红旗
- 应计利润率 (NI−CFO)/总资产：>0.1 偏高
- 应收/存货增速 − 营收增速差：>20pp = 塞货/压货信号
- 扣非净利占比：<70% = 依赖非经常性损益
- Beneish M-score（八因子）：> -1.78 提示存在盈余操纵可能（参考模型，
  非结论；杠杆率用总负债/总资产近似 LVGI，注明口径）
"""

from typing import Any, Dict, List, Optional

METRIC_SEMANTICS = {
    "cfo_ni_ratio": "经营现金流/归母净利润，逐年；长期低于 0.8 为利润质量红旗",
    "cfo_ni_ratio_5y": "近5年累计经营现金流/累计净利润；<0.8 红旗",
    "accruals_ratio": "(净利润−经营现金流)/总资产，逐年；>0.1 偏高",
    "receivable_vs_revenue_gap_pp": "应收增速−营收增速（百分点）；>20 为塞货信号",
    "inventory_vs_revenue_gap_pp": "存货增速−营收增速（百分点）；>20 为压货信号",
    "gross_margin_series": "毛利率逐年序列；异常跳变需关注",
    "net_margin_series": "净利率逐年序列",
    "recurring_profit_share": "扣非净利润/净利润；<0.7 = 依赖非经常性损益",
    "beneish_m_score": (
        "Beneish 八因子盈余操纵参考模型；M > -1.78 提示操纵可能。"
        "LVGI 以总负债/总资产近似；缺科目年份不计。仅供参考非结论。"
    ),
}


def _year_of(row: Dict[str, Any]) -> Optional[str]:
    """年度行的年份键：A股=末日 1231；美股财年可止于任意月（Apple 9 月末），
    以 fp=FY 标记年度行（pivot_rows_to_statements 透传该标记）。"""
    end_date = str(row.get("end_date") or "")
    if not end_date:
        return None
    if end_date.endswith("1231") or str(row.get("fp") or "") == "FY":
        return end_date[:4]
    return None


def _annual_by_year(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """年度行（end_date=XXXX1231 或 fp=FY）按年份索引；同年取首见（调用方已按最新排序）。"""
    by_year: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        year = _year_of(row)
        if year and year not in by_year:
            by_year[year] = row
    return by_year


def _num(row: Optional[Dict[str, Any]], *fields: str) -> Optional[float]:
    if not row:
        return None
    for field in fields:
        value = row.get(field)
        if isinstance(value, (int, float)) and value == value:
            return float(value)
    return None


def _ratio(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def _growth_pct(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous in (None, 0):
        return None
    return (current / previous - 1) * 100


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    return round(value, digits) if value is not None else None


def pivot_rows_to_statements(
    pivot_rows: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    """EDGAR/Yahoo 透视行（fp=FY，科目名已对齐）→ A股报表形状的伪行，
    供同一套指标函数复用。

    毛利率由 total_revenue/cost_of_revenue 计算；扣非占比无对应概念留空。
    港股（Yahoo）自 PR-F 起补齐成本/应收/存货/流动资产/固定资产/折旧/SGA，
    毛利率、增速差与 Beneish M-score 全部可算；受数据源限制只有近 3-5 年，
    M-score 需要上一年做基期，因此最早那一年天然留空。
    """
    income, balance, cashflow, fina = [], [], [], []
    for row in pivot_rows:
        if str(row.get("fp")) != "FY":
            continue
        end_date = str(row.get("end_date") or "")
        # fp=FY 标记透传：美股财年不一定止于 12/31，_year_of 依赖它识别年度行
        base = {"end_date": end_date, "fp": "FY"}
        income.append({
            **base,
            "total_revenue": row.get("total_revenue"),
            "n_income_attr_p": row.get("n_income_attr_p"),
            "sell_exp": row.get("sga_exp"),  # SGA 合并科目挂 sell_exp 位
            "admin_exp": None,
        })
        balance.append({
            **base,
            "total_assets": row.get("total_assets"),
            "total_liab": row.get("total_liab"),
            "accounts_receiv": row.get("accounts_receiv"),
            "inventories": row.get("inventories"),
            "total_cur_assets": row.get("total_cur_assets"),
            "fix_assets": row.get("fix_assets"),
        })
        cashflow.append({
            **base,
            "n_cashflow_act": row.get("n_cashflow_act"),
            "depr_fa_coga_dpba": row.get("depr_fa_coga_dpba"),
        })
        revenue = row.get("total_revenue")
        cost = row.get("cost_of_revenue")
        net_income = row.get("n_income_attr_p")
        gross_margin = (
            (revenue - cost) / revenue * 100
            if isinstance(revenue, (int, float)) and revenue
            and isinstance(cost, (int, float))
            else None
        )
        net_margin = (
            net_income / revenue * 100
            if isinstance(revenue, (int, float)) and revenue
            and isinstance(net_income, (int, float))
            else None
        )
        fina.append({
            **base,
            "grossprofit_margin": gross_margin,
            "netprofit_margin": net_margin,
            "profit_dedt": None,  # 无扣非概念
        })
    return {
        "income": income, "balancesheet": balance,
        "cashflow": cashflow, "fina_indicator": fina,
    }


def market_statements(
    market: str, datasets: Dict[str, List[Dict[str, Any]]]
) -> Dict[str, List[Dict[str, Any]]]:
    """按市场从档案数据集取报表行（compute_earnings_quality 的统一入口）：
    A股=Tushare 三大报表+指标原行；美股/港股=透视行映射为伪行。

    分析输入与 profile API 共用本函数——两处口径必须一致，否则详情页
    指标与 LLM 输入会对不上。
    """
    if market == "美股":
        return pivot_rows_to_statements(datasets.get("edgar_companyfacts", []))
    if market == "港股":
        return pivot_rows_to_statements(datasets.get("yahoo_fundamentals", []))
    return {
        key: datasets.get(key, [])
        for key in ("income", "balancesheet", "cashflow", "fina_indicator")
    }


def compute_earnings_quality(
    income_rows: List[Dict[str, Any]],
    balancesheet_rows: List[Dict[str, Any]],
    cashflow_rows: List[Dict[str, Any]],
    fina_indicator_rows: List[Dict[str, Any]],
    *,
    max_years: int = 8,
) -> Dict[str, Any]:
    """从年度报表行计算利润质量指标；数据不足的指标为 None/缺省。"""
    income = _annual_by_year(income_rows)
    balance = _annual_by_year(balancesheet_rows)
    cashflow = _annual_by_year(cashflow_rows)
    fina = _annual_by_year(fina_indicator_rows)

    years = sorted(set(income) | set(cashflow), reverse=True)[:max_years]
    if not years:
        return {"status": "no_data", "metric_semantics": METRIC_SEMANTICS}

    per_year: Dict[str, Dict[str, Any]] = {}
    cum_cfo = cum_ni = 0.0
    cum_years = 0
    for year in years:
        ni = _num(income.get(year), "n_income_attr_p", "n_income")
        cfo = _num(cashflow.get(year), "n_cashflow_act")
        assets = _num(balance.get(year), "total_assets")
        revenue = _num(income.get(year), "total_revenue", "revenue")
        prev = str(int(year) - 1)
        revenue_prev = _num(income.get(prev), "total_revenue", "revenue")
        receivable = _num(balance.get(year), "accounts_receiv")
        receivable_prev = _num(balance.get(prev), "accounts_receiv")
        inventory = _num(balance.get(year), "inventories")
        inventory_prev = _num(balance.get(prev), "inventories")
        deducted = _num(fina.get(year), "profit_dedt")

        revenue_growth = _growth_pct(revenue, revenue_prev)
        receivable_growth = _growth_pct(receivable, receivable_prev)
        inventory_growth = _growth_pct(inventory, inventory_prev)

        per_year[year] = {
            "cfo_ni_ratio": _round(_ratio(cfo, ni)),
            "accruals_ratio": _round(
                _ratio((ni - cfo) if ni is not None and cfo is not None else None, assets)
            ),
            "receivable_vs_revenue_gap_pp": _round(
                receivable_growth - revenue_growth
                if receivable_growth is not None and revenue_growth is not None
                else None, 2,
            ),
            "inventory_vs_revenue_gap_pp": _round(
                inventory_growth - revenue_growth
                if inventory_growth is not None and revenue_growth is not None
                else None, 2,
            ),
            "gross_margin": _num(fina.get(year), "grossprofit_margin"),
            "net_margin": _num(fina.get(year), "netprofit_margin"),
            "recurring_profit_share": _round(_ratio(deducted, ni)),
        }
        if ni is not None and cfo is not None and cum_years < 5:
            cum_ni += ni
            cum_cfo += cfo
            cum_years += 1

    m_scores = {
        year: score
        for year in years
        if (score := _beneish_m_score(income, balance, cashflow, fina, year)) is not None
    }

    return {
        "status": "ok",
        "years": years,
        "per_year": per_year,
        "cfo_ni_ratio_5y": _round(_ratio(cum_cfo, cum_ni)) if cum_years else None,
        "beneish_m_score": m_scores,
        "metric_semantics": METRIC_SEMANTICS,
    }


def _beneish_m_score(
    income: Dict[str, Dict[str, Any]],
    balance: Dict[str, Dict[str, Any]],
    cashflow: Dict[str, Dict[str, Any]],
    fina: Dict[str, Dict[str, Any]],
    year: str,
) -> Optional[Dict[str, Any]]:
    """八因子 M-score；任一必需因子缺失则该年不计（返回 None）。

    M = -4.84 + 0.92·DSRI + 0.528·GMI + 0.404·AQI + 0.892·SGI + 0.115·DEPI
        − 0.172·SGAI + 4.679·TATA − 0.327·LVGI
    """
    prev = str(int(year) - 1)
    cur_i, prev_i = income.get(year), income.get(prev)
    cur_b, prev_b = balance.get(year), balance.get(prev)
    cur_c = cashflow.get(year)
    cur_f, prev_f = fina.get(year), fina.get(prev)
    if not (cur_i and prev_i and cur_b and prev_b and cur_c):
        return None

    revenue = _num(cur_i, "total_revenue", "revenue")
    revenue_prev = _num(prev_i, "total_revenue", "revenue")
    receivable = _num(cur_b, "accounts_receiv")
    receivable_prev = _num(prev_b, "accounts_receiv")
    gross_margin = _num(cur_f, "grossprofit_margin")
    gross_margin_prev = _num(prev_f, "grossprofit_margin")
    cur_assets = _num(cur_b, "total_cur_assets")
    prev_assets_cur = _num(prev_b, "total_cur_assets")
    ppe = _num(cur_b, "fix_assets")
    ppe_prev = _num(prev_b, "fix_assets")
    total_assets = _num(cur_b, "total_assets")
    total_assets_prev = _num(prev_b, "total_assets")
    depreciation = _num(cur_c, "depr_fa_coga_dpba")
    depreciation_prev = _num(cashflow.get(prev), "depr_fa_coga_dpba")
    sga = None
    sga_prev = None
    sell = _num(cur_i, "sell_exp")
    admin = _num(cur_i, "admin_exp")
    if sell is not None or admin is not None:
        sga = (sell or 0.0) + (admin or 0.0)
    sell_prev = _num(prev_i, "sell_exp")
    admin_prev = _num(prev_i, "admin_exp")
    if sell_prev is not None or admin_prev is not None:
        sga_prev = (sell_prev or 0.0) + (admin_prev or 0.0)
    total_liab = _num(cur_b, "total_liab")
    total_liab_prev = _num(prev_b, "total_liab")
    ni = _num(cur_i, "n_income_attr_p", "n_income")
    cfo = _num(cur_c, "n_cashflow_act")

    dsri = _ratio(_ratio(receivable, revenue), _ratio(receivable_prev, revenue_prev))
    gmi = _ratio(gross_margin_prev, gross_margin)
    aqi = None
    if all(v is not None for v in (cur_assets, ppe, total_assets)) and total_assets:
        soft_cur = 1 - (cur_assets + ppe) / total_assets
        if all(v is not None for v in (prev_assets_cur, ppe_prev, total_assets_prev)) and total_assets_prev:
            soft_prev = 1 - (prev_assets_cur + ppe_prev) / total_assets_prev
            aqi = _ratio(soft_cur, soft_prev)
    sgi = _ratio(revenue, revenue_prev)
    depi = None
    if all(v is not None for v in (depreciation, ppe, depreciation_prev, ppe_prev)):
        rate_cur = _ratio(depreciation, depreciation + ppe)
        rate_prev = _ratio(depreciation_prev, depreciation_prev + ppe_prev)
        depi = _ratio(rate_prev, rate_cur)
    sgai = _ratio(_ratio(sga, revenue), _ratio(sga_prev, revenue_prev))
    lvgi = _ratio(_ratio(total_liab, total_assets), _ratio(total_liab_prev, total_assets_prev))
    tata = _ratio((ni - cfo) if ni is not None and cfo is not None else None, total_assets)

    factors = {
        "DSRI": dsri, "GMI": gmi, "AQI": aqi, "SGI": sgi,
        "DEPI": depi, "SGAI": sgai, "LVGI": lvgi, "TATA": tata,
    }
    if any(value is None for value in factors.values()):
        return None
    score = (
        -4.84 + 0.92 * dsri + 0.528 * gmi + 0.404 * aqi + 0.892 * sgi
        + 0.115 * depi - 0.172 * sgai + 4.679 * tata - 0.327 * lvgi
    )
    return {
        "score": round(score, 3),
        "flag": score > -1.78,
        "factors": {name: round(value, 4) for name, value in factors.items()},
    }
