"""格雷厄姆防御型准则 × 塔勒布脆弱性信号（纯函数，无 DB/网络）。

LLM 自行心算估值/财务比率不可靠：与 earnings_quality 同模式，从已入库的
报表行/估值快照预计算，结构化结果连同语义词典喂给分析 prompt，模型只做
解读不做算术。

七项准则出自《聪明的投资者》防御型投资者标准，阈值集中在
GRAHAM_DEFENSIVE_THRESHOLDS（原著口径注释在旁，个别按市场现实调低）。
每项输出 verdict=pass/fail/indeterminate + reason：**数据不足绝不冒充判定**
——港股年度科目由披露易年报 PDF 抽取可达十年（雅虎只补近 3-5 年），覆盖不足十年的标的
"十年盈利稳定"这类准则只能给 indeterminate 并注明
覆盖年限；反过来有亏损年即可确定 fail，与覆盖年限无关。

塔勒布侧输出脆弱性信号（fragility）：杠杆、净债务、利息覆盖——识别
"下行无界"的结构（高杠杆 + 利息覆盖薄 = 对稳定环境的隐性依赖）；
净现金为负债的镜像，是下行保护的第一层。凸性/期权性属定性判断，
留给 LLM 从商业画像/财报摘要评估，不在本层伪造量化。
"""

from typing import Any, Dict, List, Optional

# 原著防御型标准；dividend_years_min 原著为 20 年不间断，A 股市场史与
# 注册制前的分红文化撑不起该口径，按"连续 5 年"起判、数值如实展示。
GRAHAM_DEFENSIVE_THRESHOLDS: Dict[str, float] = {
    "current_ratio_min": 2.0,
    "earnings_stability_years": 10,
    "dividend_years_min": 5,
    "earnings_growth_min_pct": 33.0,  # 原著：十年每股盈利累计增长至少 1/3
    "pe_max": 15.0,
    "pb_max": 1.5,
    "pe_pb_product_max": 22.5,
}

# 分红实施年度与最新报告年度的容许差：年报分红在次年实施
DIVIDEND_LAG_YEARS = 1

CRITERIA_SEMANTICS = {
    "current_ratio": "流动比率=流动资产/流动负债；防御型标准 ≥ 2",
    "lt_debt_vs_net_current_assets": "长期债务 ≤ 净流动资产（流动资产−流动负债）",
    "earnings_stability": "以最新报告年度为锚、逐年无缺口的连续十年归母净利润为正；"
    "任一年亏损即 fail，有缺年/覆盖不足为 indeterminate（缺数不冒充稳定）",
    "dividend_record": "锚定最新报告年度的连续现金分红年数（允许 1 年披露滞后；"
    "最近一次分红更早即视为中断 fail）",
    "earnings_growth": "锚定十年窗口 [最新报告年度-9, 最新报告年度] 首尾每股盈利"
    "（缺 EPS 端点用净利润）累计增幅 ≥ 1/3；任一端点缺失为 indeterminate",
    "pe": "市盈率 ≤ 15（优先 TTM）",
    "pb_or_product": "市净率 ≤ 1.5，或 PE×PB ≤ 22.5（放宽条款：盈利便宜可容忍 PB 略高）",
}

FRAGILITY_SEMANTICS = {
    "debt_to_assets": "总负债/总资产；杠杆越高对融资环境的隐性依赖越强",
    "net_debt_to_assets": "(有息负债−货币资金)/总资产；负值=净现金，"
    "是下行保护的第一层（塔勒布：先保证不死）",
    "interest_coverage": "息税前利润/利息支出；薄覆盖=利率或盈利小幅波动即可致损的脆弱结构",
    "net_cash_to_market_cap": "净现金/总市值（仅 A股有市值快照）；比例高=市价接近清算保护",
}


def _num(row: Optional[Dict[str, Any]], *fields: str) -> Optional[float]:
    if not row:
        return None
    for field in fields:
        value = row.get(field)
        if isinstance(value, (int, float)) and value == value:
            return float(value)
    return None


def _year_of(row: Dict[str, Any]) -> Optional[str]:
    end_date = str(row.get("end_date") or "")
    if not end_date:
        return None
    if end_date.endswith("1231") or str(row.get("fp") or "") == "FY":
        return end_date[:4]
    return None


def _annual_by_year(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    by_year: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        year = _year_of(row)
        if year and year not in by_year:
            by_year[year] = row
    return by_year


def _criterion(
    key: str,
    verdict: str,
    reason: str,
    value: Optional[float] = None,
) -> Dict[str, Any]:
    return {
        "criterion": key,
        "value": round(value, 4) if isinstance(value, float) else value,
        "verdict": verdict,
        "reason": reason,
    }


def _total_debt(balance_row: Optional[Dict[str, Any]], market: str) -> Optional[Dict[str, Any]]:
    """有息负债合计与口径说明；无法可靠合计时返回 None（宁缺毋错）。

    - 港股：Yahoo 直接给 total_debt 合计；
    - 美股：EDGAR 概念链只有长期债务（lt_debt），短债概念在各公司间过于分裂，
      合计只含长期口径并注明（净现金因此偏乐观，方向性标注给 LLM）；
    - A股：短借+长借+应付债券+一年内到期非流动负债，可得项求和、缺项注明。
    """
    if balance_row is None:
        return None
    if market == "港股":
        value = _num(balance_row, "total_debt")
        return {"value": value, "basis": "total_debt(含短债)"} if value is not None else None
    if market == "美股":
        value = _num(balance_row, "lt_debt")
        return {"value": value, "basis": "仅长期债务(EDGAR 短债概念不统一)"} if value is not None else None
    parts = {
        "st_borr": _num(balance_row, "st_borr"),
        "lt_borr": _num(balance_row, "lt_borr"),
        "bond_payable": _num(balance_row, "bond_payable"),
        "non_cur_liab_due_1y": _num(balance_row, "non_cur_liab_due_1y"),
    }
    present = {name: value for name, value in parts.items() if value is not None}
    if not present:
        return None
    missing = sorted(set(parts) - set(present))
    basis = "短借+长借+应付债券+一年内到期非流动负债"
    if missing:
        basis += f"（缺 {'/'.join(missing)}，按 0 计）"
    return {"value": sum(present.values()), "basis": basis}


def _latest_daily_basic(daily_basic_rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    dated = [row for row in daily_basic_rows if row.get("trade_date")]
    if not dated:
        return None
    return max(dated, key=lambda row: str(row["trade_date"]))


def _dividend_years(dividend_rows: List[Dict[str, Any]]) -> List[int]:
    """有实施现金分红的年度集合（A股 dividend 数据集口径）。"""
    years = set()
    for row in dividend_rows:
        if str(row.get("div_proc") or "") != "实施":
            continue
        cash_div = _num(row, "cash_div_tax", "cash_div")
        end_date = str(row.get("end_date") or "")
        if cash_div and cash_div > 0 and len(end_date) >= 4:
            years.add(int(end_date[:4]))
    return sorted(years, reverse=True)


def compute_graham_screen(
    market: str,
    statements: Dict[str, List[Dict[str, Any]]],
    daily_basic_rows: Optional[List[Dict[str, Any]]] = None,
    dividend_rows: Optional[List[Dict[str, Any]]] = None,
    *,
    max_years: int = 12,
) -> Dict[str, Any]:
    """七准则 + 脆弱性信号。statements 为 earnings_quality.market_statements 的输出。"""
    thresholds = GRAHAM_DEFENSIVE_THRESHOLDS
    income = _annual_by_year(statements.get("income", []))
    balance = _annual_by_year(statements.get("balancesheet", []))

    years = sorted(set(income) | set(balance), reverse=True)[:max_years]
    if not years:
        return {
            "status": "no_data",
            "criteria_semantics": CRITERIA_SEMANTICS,
            "fragility_semantics": FRAGILITY_SEMANTICS,
            "thresholds": thresholds,
        }
    latest = years[0]
    latest_balance = balance.get(latest)
    latest_income = income.get(latest)

    criteria: List[Dict[str, Any]] = []

    # 1. 流动比率
    cur_assets = _num(latest_balance, "total_cur_assets")
    cur_liab = _num(latest_balance, "total_cur_liab")
    if cur_assets is not None and cur_liab not in (None, 0):
        ratio = cur_assets / cur_liab
        criteria.append(_criterion(
            "current_ratio",
            "pass" if ratio >= thresholds["current_ratio_min"] else "fail",
            f"{latest} 年流动比率 {ratio:.2f}（阈值 ≥ {thresholds['current_ratio_min']}）",
            ratio,
        ))
    else:
        criteria.append(_criterion(
            "current_ratio", "indeterminate", "缺流动资产或流动负债科目",
        ))

    # 2. 长期债务 ≤ 净流动资产
    debt_info = _total_debt(latest_balance, market)
    if cur_assets is not None and cur_liab is not None and debt_info is not None:
        net_current_assets = cur_assets - cur_liab
        debt = debt_info["value"]
        if market == "港股" and debt > net_current_assets:
            # total_debt 含短债（短债已在流动负债里扣过一次），超出时无法归因于长期债务
            criteria.append(_criterion(
                "lt_debt_vs_net_current_assets", "indeterminate",
                f"总有息负债 {debt:,.0f} > 净流动资产 {net_current_assets:,.0f}，"
                "但数据源只有含短债的合计，无法单独判定长期债务",
                debt,
            ))
        else:
            verdict = "pass" if debt <= net_current_assets else "fail"
            criteria.append(_criterion(
                "lt_debt_vs_net_current_assets", verdict,
                f"{latest} 年有息负债（{debt_info['basis']}）{debt:,.0f} "
                f"{'≤' if verdict == 'pass' else '>'} 净流动资产 {net_current_assets:,.0f}",
                debt,
            ))
    else:
        criteria.append(_criterion(
            "lt_debt_vs_net_current_assets", "indeterminate", "缺债务或流动项科目",
        ))

    # 3. 盈利稳定（亏损年一票 fail；年限不足且全为正 → indeterminate）
    profit_series = [
        (year, _num(income.get(year), "n_income_attr_p", "n_income"))
        for year in years
    ]
    known = [(year, value) for year, value in profit_series if value is not None]
    required_years = int(thresholds["earnings_stability_years"])
    anchor_year = int(latest)
    # 连续覆盖检查（评审 P1）：只数"有 10 条为正的记录"会把缺年冒充成稳定
    # 记录。pass 必须是以最新报告年度为锚、逐年无缺口的连续 N 年；有缺口或
    # 末年缺失一律 indeterminate 并点名缺年。
    # 亏损年触发 fail 也只看锚定窗口 [anchor-9, anchor]（评审 P1 二轮）：
    # years 最多取 12 年，窗口外的旧亏损（如 2014 亏、2016-2025 十年全正）
    # 不该推翻"以最新年度为锚的连续十年为正"这一已声明语义。
    window_floor = anchor_year - required_years + 1
    loss_years = [
        year for year, value in known
        if value <= 0 and window_floor <= int(year) <= anchor_year
    ]
    known_years = {year for year, _ in known}
    contiguous_span = 0
    for offset in range(required_years):
        if str(anchor_year - offset) in known_years:
            contiguous_span += 1
        else:
            break
    missing_years = sorted(
        str(anchor_year - offset)
        for offset in range(required_years)
        if str(anchor_year - offset) not in known_years
    )
    if not known:
        criteria.append(_criterion("earnings_stability", "indeterminate", "无净利润数据"))
    elif loss_years:
        criteria.append(_criterion(
            "earnings_stability", "fail",
            f"{window_floor}→{anchor_year} 窗口内存在亏损/零利润年度："
            f"{'、'.join(sorted(loss_years))}",
        ))
    elif contiguous_span >= required_years:
        criteria.append(_criterion(
            "earnings_stability", "pass",
            f"{anchor_year - required_years + 1}→{anchor_year} 连续 {required_years} 年"
            "净利润均为正",
        ))
    else:
        criteria.append(_criterion(
            "earnings_stability", "indeterminate",
            f"以 {anchor_year} 为锚仅连续覆盖 {contiguous_span} 年（缺 "
            f"{'、'.join(missing_years[:4])}{'…' if len(missing_years) > 4 else ''}），"
            f"不足原著 {required_years} 年连续口径",
        ))

    # 4. 股息记录（连续年数，仅 A股 有 dividend 数据集）
    # 锚定最新报告年度（评审 P1）：只从"最近一次分红"往回数，2015-2019 分过
    # 五年、之后停分的公司也会判 pass。年报分红在次年实施，允许 1 年披露
    # 滞后；最新实施年度更早即视为分红已中断 → fail（有记录但不连续到当下）。
    div_years = _dividend_years(dividend_rows or [])
    if div_years:
        anchor_year = int(latest)
        latest_div_year = div_years[0]
        if latest_div_year < anchor_year - DIVIDEND_LAG_YEARS:
            criteria.append(_criterion(
                "dividend_record", "fail",
                f"最近一次现金分红为 {latest_div_year} 年，距最新报告年度 {anchor_year} "
                f"已中断（允许 {DIVIDEND_LAG_YEARS} 年披露滞后）",
                0.0,
            ))
        else:
            consecutive = 1
            for idx in range(1, len(div_years)):
                if div_years[idx] == div_years[idx - 1] - 1:
                    consecutive += 1
                else:
                    break
            verdict = "pass" if consecutive >= thresholds["dividend_years_min"] else "fail"
            criteria.append(_criterion(
                "dividend_record", verdict,
                f"截至 {latest_div_year} 年连续现金分红 {consecutive} 年"
                f"（阈值 ≥ {int(thresholds['dividend_years_min'])} 年；原著为 20 年）",
                float(consecutive),
            ))
    else:
        criteria.append(_criterion(
            "dividend_record", "indeterminate",
            "无分红实施记录数据" + ("" if market == "A股" else "（本市场无分红数据源）"),
        ))

    # 5. 盈利增长：**锚定十年窗口 [anchor-9, anchor]**（评审 P1 三轮）：years 最多
    # 保留 12 年，反转取首尾会用 11 年区间套十年阈值——2014 EPS=1、2015-2025
    # EPS=2 会算出 2014→2025 +100% pass，而十年窗口 2016→2025 增长为 0 应 fail。
    # 端点要求：窗口首年（anchor-9）与末年（anchor）都必须有值——首年缺则
    # indeterminate（不用更早/更晚的年份顶替，那会改变判定窗口）。
    # 优先 EPS（净利润增长可能只是增发摊薄的幻象）；EPS 端点不齐才回退净利润。
    growth_first_year = anchor_year - required_years + 1

    def _growth_endpoints(*fields: str):
        first = _num(income.get(str(growth_first_year)), *fields)
        last = _num(income.get(str(anchor_year)), *fields)
        return first, last

    first, last = _growth_endpoints("basic_eps", "diluted_eps")
    growth_basis = "每股盈利"
    if first is None or last is None:
        first, last = _growth_endpoints("n_income_attr_p", "n_income")
        growth_basis = "净利润（缺 EPS 端点，未剔除股本变动）"
    if first is None or last is None:
        criteria.append(_criterion(
            "earnings_growth", "indeterminate",
            f"十年窗口端点 {growth_first_year}/{anchor_year} 缺{growth_basis}数据，"
            "无法按十年口径计算增幅",
        ))
    elif first <= 0:
        criteria.append(_criterion(
            "earnings_growth", "indeterminate",
            f"期初（{growth_first_year}）{growth_basis}非正，增幅无意义",
        ))
    else:
        growth_pct = (last / first - 1) * 100
        verdict = "pass" if growth_pct >= thresholds["earnings_growth_min_pct"] else "fail"
        criteria.append(_criterion(
            "earnings_growth", verdict,
            f"{growth_first_year}→{anchor_year} {growth_basis}累计增幅 {growth_pct:.1f}%"
            f"（阈值 ≥ {thresholds['earnings_growth_min_pct']:.0f}%，十年窗口）",
            growth_pct,
        ))

    # 6/7. 估值（A股 daily_basic 快照；其余市场无估值数据源）
    basic = _latest_daily_basic(daily_basic_rows or [])
    pe = _num(basic, "pe_ttm", "pe")
    pb = _num(basic, "pb")
    trade_date = str((basic or {}).get("trade_date") or "")
    if pe is not None:
        verdict = "pass" if 0 < pe <= thresholds["pe_max"] else "fail"
        reason = f"PE {pe:.2f}（阈值 ≤ {thresholds['pe_max']:.0f}，快照 {trade_date}）"
        if pe <= 0:
            verdict, reason = "fail", f"PE 为负（亏损），快照 {trade_date}"
        criteria.append(_criterion("pe", verdict, reason, pe))
    else:
        criteria.append(_criterion(
            "pe", "indeterminate",
            "无估值快照" + ("" if market == "A股" else "（本市场无估值数据源）"),
        ))
    if pb is not None:
        if pb <= 0:
            # 负 PB = 净资产为负，不是"价格低于账面价值"的安全边际（评审 P1；
            # 与上面负 PE 判 fail 同口径）
            criteria.append(_criterion(
                "pb_or_product", "fail",
                f"PB {pb:.2f} 为负/零（净资产非正），不构成账面价值安全边际，快照 {trade_date}",
                pb,
            ))
        elif pb <= thresholds["pb_max"]:
            criteria.append(_criterion(
                "pb_or_product", "pass",
                f"PB {pb:.2f} ≤ {thresholds['pb_max']}（快照 {trade_date}）", pb,
            ))
        elif pe is not None and pe > 0 and pe * pb <= thresholds["pe_pb_product_max"]:
            criteria.append(_criterion(
                "pb_or_product", "pass",
                f"PB {pb:.2f} 超限但 PE×PB {pe * pb:.1f} ≤ "
                f"{thresholds['pe_pb_product_max']}（放宽条款）", pb,
            ))
        else:
            criteria.append(_criterion(
                "pb_or_product", "fail",
                f"PB {pb:.2f} > {thresholds['pb_max']}"
                + (f" 且 PE×PB {pe * pb:.1f} 超限" if pe is not None and pe > 0 else ""),
                pb,
            ))
    else:
        criteria.append(_criterion(
            "pb_or_product", "indeterminate",
            "无估值快照" + ("" if market == "A股" else "（本市场无估值数据源）"),
        ))

    # 塔勒布脆弱性信号
    fragility: Dict[str, Any] = {}
    total_assets = _num(latest_balance, "total_assets")
    total_liab = _num(latest_balance, "total_liab")
    money_cap = _num(latest_balance, "money_cap")
    if total_assets and total_liab is not None:
        fragility["debt_to_assets"] = round(total_liab / total_assets, 4)
    if debt_info is not None and money_cap is not None and total_assets:
        fragility["net_debt_to_assets"] = round(
            (debt_info["value"] - money_cap) / total_assets, 4
        )
        fragility["net_debt_basis"] = debt_info["basis"]
    ebit = _num(latest_income, "operating_income", "operate_profit", "ebit")
    interest = _num(latest_income, "int_exp", "fin_exp")
    if ebit is not None and interest is not None:
        if interest > 0:
            fragility["interest_coverage"] = round(ebit / interest, 2)
        else:
            fragility["interest_coverage_note"] = "利息支出为零或净收益，覆盖倍数无意义"
    total_mv = _num(basic, "total_mv")
    if (
        market == "A股"
        and total_mv
        and debt_info is not None
        and money_cap is not None
    ):
        # daily_basic 总市值单位为万元，报表科目单位为元
        fragility["net_cash_to_market_cap"] = round(
            (money_cap - debt_info["value"]) / (total_mv * 10000), 4
        )

    passed = sum(1 for c in criteria if c["verdict"] == "pass")
    failed = sum(1 for c in criteria if c["verdict"] == "fail")
    return {
        "status": "ok",
        "as_of_year": latest,
        "criteria": criteria,
        "passed": passed,
        "failed": failed,
        "indeterminate": len(criteria) - passed - failed,
        "fragility": fragility,
        "thresholds": thresholds,
        "criteria_semantics": CRITERIA_SEMANTICS,
        "fragility_semantics": FRAGILITY_SEMANTICS,
    }
