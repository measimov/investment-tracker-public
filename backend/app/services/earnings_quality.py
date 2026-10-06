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

跨年指标与报告币种：港股/美股透视行逐行带 currency，公司可能中途换报告币种（00799 2021 年起
USD→HKD）。本层没有汇率输入，币种切换处的跨年比值（营收/应收/存货增速 → 增速差、M-score 的
SGI）一律不计并在 per_year[year].currency_change 标注；近 5 年累计 CFO/净利润只累加与最新年度
同币种的年份。**币种未知不等于同币种**：任一侧（含两侧）行上无币种即按「无法确认相同」处理，
只有 A股（Tushare 报表按构造为人民币，STATEMENT_CURRENCY_BY_MARKET）由 market 参数补出币种。
同年内的比率（CFO/NI、应计率、毛利率、净利率）与币种无关。只在发生切换/无法确认时输出
currency_changes 等键——单币种序列（含 A股）的输出与改动前逐字节一致。
"""

from typing import Any, Callable, Dict, List, Optional, Tuple

from .edgar_facts import EDGAR_MISSING_OTHER_CURRENCY, EDGAR_ZERO_INFERENCE_MIN_VERSION
from .payload_versions import stored_version
from .periods import annual_by_year, annual_year_key, consecutive_run, coverage, years_adjacent

METRIC_SEMANTICS = {
    "cfo_ni_ratio": (
        "经营现金流/归母净利润，逐年；长期低于 0.8 为利润质量红旗。净利润 ≤ 0 的年份不计"
        "（per_year[年].ratio_unavailable 标 ni_non_positive）：负除负或正除负都不表示利润质量"
    ),
    "cfo_ni_ratio_5y": (
        "从最新年度往前**连续**财年（最多 5 年）累计经营现金流/累计净利润；<0.8 红旗；"
        "缺年、换币即停（cfo_ni_ratio_5y_note 说明）；累计净利润 ≤ 0 时不计"
    ),
    "accruals_ratio": "(净利润−经营现金流)/总资产，逐年；>0.1 偏高",
    "receivable_vs_revenue_gap_pp": "应收增速−营收增速（百分点）；>20 为塞货信号",
    "inventory_vs_revenue_gap_pp": "存货增速−营收增速（百分点）；>20 为压货信号",
    "gross_margin_series": "毛利率逐年序列；异常跳变需关注",
    "net_margin_series": "净利率逐年序列",
    "recurring_profit_share": (
        "扣非净利润/净利润；<0.7 = 依赖非经常性损益。净利润 ≤ 0 的年份不计（ratio_unavailable）"
    ),
    "beneish_m_score": (
        "Beneish 八因子盈余操纵参考模型；M > -1.78 提示操纵可能。"
        "LVGI 以总负债/总资产近似；缺科目年份不计（SGA 分项两年不一致也不计）。仅供参考非结论。"
    ),
}

# 比率在净利润 ≤ 0 时不计的原因码（#349：-100/-150 得 1.5 显示「健康」，其实现金流出大于亏损）
NI_NON_POSITIVE = "ni_non_positive"


def _year_of(row: Dict[str, Any]) -> Optional[str]:
    """年度行的财年键：A股=末日 1231；美股财年可止于任意月（Apple 9 月末），以 fp=FY 标记
    年度行（pivot_rows_to_statements 透传该标记）。键按财年而不是公历年取（periods，#350）。"""
    return annual_year_key(row)


def _annual_by_year(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """年度行按财年键索引；同年取首见（调用方已按最新排序）。"""
    return annual_by_year(rows)


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


# 数据源按构造就确定币种、行上不带 currency 的市场：A股 Tushare 三大报表（合并报表口径，单位
# 人民币元；格雷厄姆 A股 估值路径同样按 CNY 处理）。**只有这里列出的市场**能把「行上无币种」
# 解释为已知币种；港股/美股透视行逐行带 currency，缺失即未知（PDF 抽取允许 currency=None，
# merge_hk_statement_rows 也刻意不猜）——两端都未知不能证明同币种
STATEMENT_CURRENCY_BY_MARKET: Dict[str, str] = {"A股": "CNY"}


def statement_currency(row: Optional[Dict[str, Any]], market: Optional[str]) -> Optional[str]:
    """报表行的币种：行上 currency 优先，缺失时只对 STATEMENT_CURRENCY_BY_MARKET 的市场按市场
    补出；其余返回 None（未知）。"""
    currency = (row or {}).get("currency")
    if currency:
        return str(currency)
    if row is None:
        return None
    return STATEMENT_CURRENCY_BY_MARKET.get(market or "")


def _currency_of(market: Optional[str], *rows: Optional[Dict[str, Any]]) -> Optional[str]:
    """该年度各表行的报告币种（取第一个已知的；全部未知 → None）。"""
    for row in rows:
        currency = statement_currency(row, market)
        if currency:
            return currency
    return None


def _currency_change(
    tables: List[Dict[str, Dict[str, Any]]], year: str, prev: str, market: Optional[str]
) -> Optional[str]:
    """year 与 prev 两年的报告币种不同或**无法确认相同**（任一侧未知，含两侧都未知）→
    "USD→HKD" / "未知→HKD"；确认同币种或 prev 年无行 → None。

    跨年比值（增速、SGI）在币种切换处不能直接相除——00799 2020 年营收 704.1M USD、2021 年
    6,050.9M HKD，直接相除是 +759% 的"增长"。本层无汇率输入，切换处的跨年指标一律不计并标注。"""
    prev_rows = [table.get(prev) for table in tables]
    if not any(prev_rows):
        return None
    ours = _currency_of(market, *(table.get(year) for table in tables))
    theirs = _currency_of(market, *prev_rows)
    if ours and ours == theirs:
        return None
    return f"{theirs or '未知'}→{ours or '未知'}"


def _dividend_status(row: Dict[str, Any], absent_means_zero: Optional[Callable]) -> Optional[str]:
    """已付股东股息的来源状态：reported / not_listed（现金流量表在而未列，按 0 计）/
    other_currency_only（EDGAR：只以非报告币种披露，不混币取数 → 不可知）/ None（不可知）。"""
    if row.get("div_paid_owners") is not None:
        return "reported"
    if edgar_missing_other_currency(row, "div_paid_owners"):
        return EDGAR_MISSING_OTHER_CURRENCY
    if absent_means_zero is not None and absent_means_zero(row):
        return "not_listed"
    return None


def edgar_missing_other_currency(row: Optional[Dict[str, Any]], field: str) -> bool:
    """EDGAR 透视行上该科目为空是因为只有非报告币种的事实（而不是发行人没报这个概念）。"""
    reasons = (row or {}).get("edgar_missing_reasons") or {}
    return reasons.get(field) == EDGAR_MISSING_OTHER_CURRENCY


def edgar_zero_inference_allowed(row: Optional[Dict[str, Any]]) -> bool:
    """该 EDGAR 透视行由支持「缺概念 → 0」推断的概念链抓取（edgar_chain_version ≥ 2）。"""
    return stored_version(row, "edgar_chain_version") >= EDGAR_ZERO_INFERENCE_MIN_VERSION


def _edgar_absent_means_zero(row: Dict[str, Any]) -> bool:
    return (
        edgar_zero_inference_allowed(row)
        and row.get("n_cashflow_act") is not None
        and not edgar_missing_other_currency(row, "div_paid_owners")
    )


def _hk_absent_means_zero(row: Dict[str, Any]) -> bool:
    # 只有当前版本 PDF 抽取行（带 prompt_version，读取侧已过版本门）且现金流量表确实被映射；
    # 雅虎行不带某序列不能推断公司没付
    return bool(row.get("prompt_version")) and "cashflow" in (row.get("source_by_kind") or {})


def pivot_rows_to_statements(
    pivot_rows: List[Dict[str, Any]],
    *,
    dividend_absent_means_zero: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """EDGAR/Yahoo 透视行（fp=FY，科目名已对齐）→ A股报表形状的伪行，
    供同一套指标函数复用。

    毛利率由 total_revenue/cost_of_revenue 计算；扣非占比无对应概念留空。
    港股（Yahoo）自 PR-F 起补齐成本/应收/存货/流动资产/固定资产/折旧/SGA，
    毛利率、增速差与 Beneish M-score 全部可算；年度行由披露易 PDF 抽取可达十年，雅虎补缺仅近 3-5 年，
    M-score 需要上一年做基期，因此最早那一年天然留空。
    """
    income, balance, cashflow, fina = [], [], [], []
    for row in pivot_rows:
        if str(row.get("fp")) != "FY":
            continue
        end_date = str(row.get("end_date") or "")
        # fp=FY 标记透传：美股财年不一定止于 12/31，_year_of 依赖它识别年度行；
        # currency 供格雷厄姆估值把每股盈利/净资产折成价格币种
        base = {"end_date": end_date, "fp": "FY", "currency": row.get("currency")}
        income.append(
            {
                **base,
                "total_revenue": row.get("total_revenue"),
                "n_income_attr_p": row.get("n_income_attr_p"),
                "sell_exp": row.get("sga_exp"),  # SGA 合并科目挂 sell_exp 位
                "admin_exp": None,
                # graham_screen 消费：盈利增长（EPS 优先）与利息覆盖
                "basic_eps": row.get("basic_eps"),
                "diluted_eps": row.get("diluted_eps"),
                "int_exp": row.get("int_exp"),
                "operating_income": row.get("operating_income"),
            }
        )
        balance.append(
            {
                **base,
                "total_assets": row.get("total_assets"),
                "total_liab": row.get("total_liab"),
                "accounts_receiv": row.get("accounts_receiv"),
                "inventories": row.get("inventories"),
                "total_cur_assets": row.get("total_cur_assets"),
                "fix_assets": row.get("fix_assets"),
                # graham_screen 消费：财务强度与净债务
                "total_cur_liab": row.get("total_cur_liab"),
                "total_debt": row.get("total_debt"),  # 港股 合计口径（PDF 映射 / Yahoo）
                "lt_borr": row.get("lt_borr"),  # 港股 PDF：非流动借款（准则 2 的长期债务）
                "st_borr": row.get("st_borr"),  # 港股 PDF：流动借款
                "lt_debt": row.get("lt_debt"),  # 美股 EDGAR 长期债务口径
                "money_cap": row.get("money_cap"),
                # graham_screen 消费：MRQ 每股净资产；EDGAR 概念链版本（「本期无长债 → 0」的前提）
                "total_hldr_eqy_exc_min_int": row.get("total_hldr_eqy_exc_min_int"),
                "edgar_chain_version": row.get("edgar_chain_version"),
                # 只以非报告币种披露的科目（lt_debt 为空时不得按「缺概念 → 0」）
                "edgar_missing_reasons": row.get("edgar_missing_reasons"),
            }
        )
        cashflow.append(
            {
                **base,
                "n_cashflow_act": row.get("n_cashflow_act"),
                "depr_fa_coga_dpba": row.get("depr_fa_coga_dpba"),
                # graham_screen 消费：港股/美股的分红记录（已付本公司股东股息，量级）
                "div_paid_owners": (
                    abs(row["div_paid_owners"])
                    if isinstance(row.get("div_paid_owners"), (int, float))
                    else None
                ),
                "div_paid_status": _dividend_status(row, dividend_absent_means_zero),
            }
        )
        revenue = row.get("total_revenue")
        cost = row.get("cost_of_revenue")
        net_income = row.get("n_income_attr_p")
        gross_margin = (
            (revenue - cost) / revenue * 100
            if isinstance(revenue, (int, float)) and revenue and isinstance(cost, (int, float))
            else None
        )
        net_margin = (
            net_income / revenue * 100
            if isinstance(revenue, (int, float))
            and revenue
            and isinstance(net_income, (int, float))
            else None
        )
        fina.append(
            {
                **base,
                "grossprofit_margin": gross_margin,
                "netprofit_margin": net_margin,
                "profit_dedt": None,  # 无扣非概念
            }
        )
    return {
        "income": income,
        "balancesheet": balance,
        "cashflow": cashflow,
        "fina_indicator": fina,
    }


_STATEMENT_META_KEYS = frozenset(
    {
        "end_date",
        "fp",
        "currency",
        "is_comparative",
        "source_period_key",
        "source_report_type",
        "source_end_date",
        "source_url",
        "source_fingerprint",
        "source_pages",
        "source_by_kind",
        "extractor_version",
        "prompt_version",
        "validation",
        "currency_by_kind",
        "unit_by_kind",
        "derived_fields",
        "comparative_evidence",
    }
)


def merge_hk_statement_rows(datasets: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """港股透视行 = 年报/中报 PDF 抽取行（report_statements，官方一手）优先，Yahoo 补缺。
    **合并粒度是科目而不是整行**：只处理了中报时 `20251231|FY` 可能只有资产负债表比较列，
    整行屏蔽 Yahoo 会让本来有的营收/利润/现金流全部变 None（PR #201 评审 P2）。**双方币种
    已确认且一致**才逐科目合并；任一侧币种未知或不同，PDF 行原样保留、不补数，Yahoo 只在 PDF
    没有该期时整行补入。"""
    from .report_statement_checks import rederive_fields, scrub_suspect_fields

    by_period: Dict[tuple, Dict[str, Any]] = {}
    for row in datasets.get("report_statements", []) or []:
        key = (str(row.get("end_date")), str(row.get("fp") or "FY"))
        # 校验存疑的科目先置空，让 Yahoo 补——存疑值不得进利润质量/格雷厄姆/分析输入
        by_period[key] = scrub_suspect_fields(row)
    for row in datasets.get("yahoo_fundamentals", []) or []:
        key = (str(row.get("end_date")), str(row.get("fp") or "FY"))
        pdf = by_period.get(key)
        if pdf is None:
            by_period[key] = dict(row)
            continue
        pdf_currency, yahoo_currency = pdf.get("currency"), row.get("currency")
        # 双方币种都已确认且一致才能逐科目补数；任一侧未知或不同 → PDF 行原样、不借 Yahoo 的
        # 金额也不借它的币种标签（parsed.currency 允许为 None，贴 Yahoo 币种就是猜）
        if not pdf_currency or not yahoo_currency or pdf_currency != yahoo_currency:
            continue
        for field, value in row.items():
            if field in _STATEMENT_META_KEYS or value is None:
                continue
            if pdf.get(field) is None:
                pdf[field] = value
        # 清洗时随输入一起失效的派生科目（FCF/分项合计）在可信补缺后重新推导
        rederive_fields(pdf)
    merged = list(by_period.values())
    merged.sort(key=lambda row: str(row.get("end_date") or ""), reverse=True)
    return merged


def market_statements(
    market: str, datasets: Dict[str, List[Dict[str, Any]]]
) -> Dict[str, List[Dict[str, Any]]]:
    """按市场从档案数据集取报表行（compute_earnings_quality 的统一入口）：
    A股=Tushare 三大报表+指标原行；美股/港股=透视行映射为伪行。

    分析输入与 profile API 共用本函数——两处口径必须一致，否则详情页
    指标与 LLM 输入会对不上。
    """
    if market == "美股":
        return pivot_rows_to_statements(
            datasets.get("edgar_companyfacts", []),
            dividend_absent_means_zero=_edgar_absent_means_zero,
        )
    if market == "港股":
        return pivot_rows_to_statements(
            merge_hk_statement_rows(datasets),
            dividend_absent_means_zero=_hk_absent_means_zero,
        )
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
    market: Optional[str] = None,
) -> Dict[str, Any]:
    """从年度报表行计算利润质量指标；数据不足的指标为 None/缺省。

    market 决定行上无 currency 时能否按市场确定币种（STATEMENT_CURRENCY_BY_MARKET，仅 A股）；
    其余情况币种未知的相邻年份不做跨年比较。"""
    income = _annual_by_year(income_rows)
    balance = _annual_by_year(balancesheet_rows)
    cashflow = _annual_by_year(cashflow_rows)
    fina = _annual_by_year(fina_indicator_rows)

    years = sorted(set(income) | set(cashflow), reverse=True)[:max_years]
    if not years:
        return {"status": "no_data", "metric_semantics": METRIC_SEMANTICS}

    per_year: Dict[str, Dict[str, Any]] = {}
    currency_changes: List[Dict[str, str]] = []
    tables = [income, balance, cashflow]
    for year in years:
        ni = _num(income.get(year), "n_income_attr_p", "n_income")
        cfo = _num(cashflow.get(year), "n_cashflow_act")
        assets = _num(balance.get(year), "total_assets")
        revenue = _num(income.get(year), "total_revenue", "revenue")
        prev = str(int(year) - 1)
        change = _currency_change(tables, year, prev, market)
        if change:
            currency_changes.append({"year": year, "change": change})
        # 币种切换处的跨年比值不计（上年值置空 → 增速为 None）
        prev_income = None if change else income.get(prev)
        prev_balance = None if change else balance.get(prev)
        revenue_prev = _num(prev_income, "total_revenue", "revenue")
        receivable = _num(balance.get(year), "accounts_receiv")
        receivable_prev = _num(prev_balance, "accounts_receiv")
        inventory = _num(balance.get(year), "inventories")
        inventory_prev = _num(prev_balance, "inventories")
        deducted = _num(fina.get(year), "profit_dedt")

        revenue_growth = _growth_pct(revenue, revenue_prev)
        receivable_growth = _growth_pct(receivable, receivable_prev)
        inventory_growth = _growth_pct(inventory, inventory_prev)

        # 净利润 ≤ 0 时 CFO/NI 与扣非占比的方向失真（负除负、正除负），不计并注明原因
        ni_positive = ni is not None and ni > 0
        unavailable = {}
        if ni is not None and not ni_positive:
            if cfo is not None:
                unavailable["cfo_ni_ratio"] = NI_NON_POSITIVE
            if deducted is not None:
                unavailable["recurring_profit_share"] = NI_NON_POSITIVE
        per_year[year] = {
            "cfo_ni_ratio": _round(_ratio(cfo, ni)) if ni_positive else None,
            "accruals_ratio": _round(
                _ratio((ni - cfo) if ni is not None and cfo is not None else None, assets)
            ),
            "receivable_vs_revenue_gap_pp": _round(
                receivable_growth - revenue_growth
                if receivable_growth is not None and revenue_growth is not None
                else None,
                2,
            ),
            "inventory_vs_revenue_gap_pp": _round(
                inventory_growth - revenue_growth
                if inventory_growth is not None and revenue_growth is not None
                else None,
                2,
            ),
            "gross_margin": _num(fina.get(year), "grossprofit_margin"),
            "net_margin": _num(fina.get(year), "netprofit_margin"),
            "recurring_profit_share": _round(_ratio(deducted, ni)) if ni_positive else None,
        }
        if unavailable:
            per_year[year]["ratio_unavailable"] = unavailable
        if change:
            per_year[year]["currency_change"] = change

    m_scores = {
        year: score
        for year in years
        if not per_year[year].get("currency_change")
        and (score := _beneish_m_score(income, balance, cashflow, fina, year)) is not None
    }

    cumulative = _cumulative_cfo_ni(years, income, cashflow, market)
    result = {
        "status": "ok",
        "years": years,
        "per_year": per_year,
        "cfo_ni_ratio_5y": cumulative["ratio"],
        "beneish_m_score": m_scores,
        "metric_semantics": METRIC_SEMANTICS,
    }
    # 以下键只在报告币种中途切换时出现：单币种序列（含 A股）的输出与改动前逐字节一致
    if currency_changes:
        result["currency_changes"] = currency_changes
        result["currency_change_note"] = (
            "报告币种在序列中途切换或无法确认相同（年份=切换后第一年；「未知」=行上无币种）："
            "该年相对上一年的增速差与 Beneish "
            "M-score 不计（不同币种金额不能直接相除，本层不做汇率折算）；同年内的比率"
            "（CFO/净利润、应计率、毛利率、净利率）不受影响"
        )
    if cumulative["note"]:
        result["cfo_ni_ratio_5y_years"] = cumulative["years"]
        result["cfo_ni_ratio_5y_note"] = cumulative["note"]
    if cumulative["unavailable"]:
        result["cfo_ni_ratio_5y_unavailable"] = cumulative["unavailable"]
    return result


CUMULATIVE_YEARS = 5


def _cumulative_cfo_ni(
    years: List[str],
    income: Dict[str, Dict[str, Any]],
    cashflow: Dict[str, Dict[str, Any]],
    market: Optional[str],
) -> Dict[str, Any]:
    """近 5 年累计 CFO/净利润：从最新一个 CFO 与净利润都有的财年起，往前只累加**连续**财年。

    缺年（整年无行）、某年缺 CFO 或净利润、报告币种不同或无法确认相同即停——缺的那年情况未知，
    跨过它累加会把 7 年的跨度当成 5 年；USD 与 HKD 金额相加没有意义。累计净利润 ≤ 0 时比率
    方向失真，不计（#349）。"""

    def values(year: str):
        return (
            _num(income.get(year), "n_income_attr_p", "n_income"),
            _num(cashflow.get(year), "n_cashflow_act"),
        )

    start = next((i for i, year in enumerate(years) if None not in values(year)), None)
    empty = {"ratio": None, "years": 0, "note": None, "unavailable": None}
    if start is None:
        return empty
    first = years[start]
    first_currency = _currency_of(market, income.get(first), cashflow.get(first))

    def accumulable(year: str) -> Optional[bool]:
        if None in values(year):
            return None
        if year == first:
            return True
        # 币种未知的年份不能与任何年份相加（首年未知则只取这一年）
        currency = _currency_of(market, income.get(year), cashflow.get(year))
        return currency is not None and currency == first_currency

    run = consecutive_run(years[start:], accumulable, adjacent=years_adjacent)
    taken = run.items[:CUMULATIVE_YEARS]
    cum_ni = sum(values(year)[0] for year in taken)
    cum_cfo = sum(values(year)[1] for year in taken)
    out = dict(empty, years=len(taken))
    if cum_ni > 0:
        out["ratio"] = _round(cum_cfo / cum_ni)
    else:
        out["unavailable"] = NI_NON_POSITIVE
    if len(taken) >= CUMULATIVE_YEARS or run.stop_reason is None:
        return out
    stopped = run.stopped_at
    if run.stop_reason == "value":
        out["note"] = (
            f"近 5 年累计只含与最新年度同币种（{first_currency or '币种未知'}）的 {len(taken)} 年；"
            f"{stopped} 年起币种不同或无法确认相同，不混币累计"
        )
    elif run.stop_reason == "gap":
        missing = coverage([taken[-1], stopped])["missing"]
        out["note"] = (
            f"近 5 年累计只含 {first} 年往前连续的 {len(taken)} 年；"
            f"缺 {'、'.join(missing)} 年的数据，不跨缺年累计"
        )
    else:
        out["note"] = (
            f"近 5 年累计只含 {first} 年往前连续的 {len(taken)} 年；"
            f"{stopped} 年缺净利润或经营现金流，不跨缺年累计"
        )
    return out


def _sga_pair(cur: Dict[str, Any], prev: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    """两年的销售+管理费用。两年**披露的分项必须一致**才相加（#349）：缺一个分项按 0 补会让
    上年少算一整块费用，SGAI 由 1.03 变成 3.33。港股/美股透视行只有合并 SGA（挂在 sell_exp 位），
    两年都只有这一项，照常可比。"""
    parts = ("sell_exp", "admin_exp")
    cur_values = {field: _num(cur, field) for field in parts}
    prev_values = {field: _num(prev, field) for field in parts}
    present = {field for field, value in cur_values.items() if value is not None}
    prev_present = {field for field, value in prev_values.items() if value is not None}
    if not present or present != prev_present:
        return None, None
    return (
        sum(cur_values[field] for field in present),
        sum(prev_values[field] for field in present),
    )


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
    sga, sga_prev = _sga_pair(cur_i, prev_i)
    total_liab = _num(cur_b, "total_liab")
    total_liab_prev = _num(prev_b, "total_liab")
    ni = _num(cur_i, "n_income_attr_p", "n_income")
    cfo = _num(cur_c, "n_cashflow_act")

    dsri = _ratio(_ratio(receivable, revenue), _ratio(receivable_prev, revenue_prev))
    gmi = _ratio(gross_margin_prev, gross_margin)
    aqi = None
    if all(v is not None for v in (cur_assets, ppe, total_assets)) and total_assets:
        soft_cur = 1 - (cur_assets + ppe) / total_assets
        if (
            all(v is not None for v in (prev_assets_cur, ppe_prev, total_assets_prev))
            and total_assets_prev
        ):
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
        "DSRI": dsri,
        "GMI": gmi,
        "AQI": aqi,
        "SGI": sgi,
        "DEPI": depi,
        "SGAI": sgai,
        "LVGI": lvgi,
        "TATA": tata,
    }
    if any(value is None for value in factors.values()):
        return None
    score = (
        -4.84
        + 0.92 * dsri
        + 0.528 * gmi
        + 0.404 * aqi
        + 0.892 * sgi
        + 0.115 * depi
        - 0.172 * sgai
        + 4.679 * tata
        - 0.327 * lvgi
    )
    return {
        "score": round(score, 3),
        "flag": score > -1.78,
        "factors": {name: round(value, 4) for name, value in factors.items()},
    }
