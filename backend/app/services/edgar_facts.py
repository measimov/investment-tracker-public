"""SEC EDGAR companyfacts（XBRL）→ 每 (期末, fp) 一行的透视（#281，由 security_profile_service 迁出）。

概念兜底链、分项求和兜底、按报告币种取数、期间长度校验与年度/季度分类封顶都在这里，
版本常量 `EDGAR_PIVOT_VERSION` 与它所版本化的透视代码同处一个模块——改透视口径就在这里
bump，不必再跨文件找（此前常量在 earnings_quality、代码在 security_profile_service）。
模块顶层只依赖标准库；网络请求在 `fetch_edgar_companyfacts` 内按需导入 report_fetchers。
"""

import re
from datetime import date
from typing import Any, Dict, List, Optional

from ..core.logging import get_app_logger

logger = get_app_logger(__name__)

# EDGAR 透视行的版本（行上 `edgar_chain_version`）。v2：长期债务扩展链与已付股息链；
# v3：按**报告币种**取数（中概 20-F 发行人为 CNY，此前只取 USD 单位——那只是最新一两年的
# 便利折算，多年序列残缺且逐年折算率不同）。部署后美股档案需重新同步一次才会换成 v3 行
EDGAR_PIVOT_VERSION = 3
# 「现金流量表在而无股息概念 → 0」「本期无长期债务概念而往年有 → 0」只对 v2+ 的行成立——
# 旧链抓的行缺这些概念只说明当时没抓，不说明公司没有（v3 只改取数币种，不影响这条前提）。
# 股息（本模块）与长期债务（graham_screen）共用这一个阈值与 edgar_zero_inference_allowed
EDGAR_ZERO_INFERENCE_MIN_VERSION = 2
# 行上 `edgar_missing_reasons[field]` 的取值：该科目在报告币种下为空、但发行人用**别的币种**
# 披露过这个概念。只有不带这个标记的空值才是「发行人没报这个概念」——股息/长期债务的
# 「缺概念 → 0」推断只对后者成立（透视层 security_profile_service._mark_other_currency_gaps 写入）
EDGAR_MISSING_OTHER_CURRENCY = "other_currency_only"


# EDGAR XBRL 概念兜底链：同一财务概念在不同公司/年份用不同 tag，
# 取链上首个有值者；银行等特殊行业末位取不到就留空（走"数据不足"）。
EDGAR_CONCEPT_CHAINS: Dict[str, tuple] = {
    "total_revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ),
    "cost_of_revenue": ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"),
    "operating_income": ("OperatingIncomeLoss",),
    "n_income_attr_p": ("NetIncomeLoss",),
    "total_assets": ("Assets",),
    "total_liab": ("Liabilities",),
    "total_hldr_eqy_exc_min_int": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ),
    "money_cap": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ),
    # **只放贸易应收语义等价的概念**。应收-营收增速差与 Beneish DSRI 比的是
    # "销售形成的应收"与营收；贷款/票据/其他应收与营业收入没有同一经济含义，
    # 拿它们兜底会生成看似完整实则无效的风险信号（PDD 实测只有 NotesAndLoans
    # 与 OtherReceivables 口径 → 该项如实留空，走"数据不足"）。
    "accounts_receiv": (
        "AccountsReceivableNetCurrent",
        "AccountsReceivableGrossCurrent",
    ),
    "inventories": ("InventoryNet",),
    "total_cur_assets": ("AssetsCurrent",),
    # 格雷厄姆准则层（graham_screen）新增：流动负债/长期债务/利息支出。
    # 短期借款概念在各公司间过于分裂，刻意不收——净债务口径按"仅长期"
    # 标注方向性偏差，胜过用错概念拼出貌似完整的合计。
    "total_cur_liab": ("LiabilitiesCurrent",),
    # 长期债务：链上首个有值者胜（逐期）。LongTermDebt 含一年内到期部分，排在专门的非流动
    # 概念之后；可转债/票据概念是发行可转债的中概股常用口径——拼多多 20-F 只报
    # ConvertibleDebtNoncurrent（2026-09 companyfacts 实查），不报 LongTermDebt*
    "lt_debt": (
        "LongTermDebtNoncurrent",
        "LongTermDebt",
        "ConvertibleNotesPayableNoncurrent",
        "ConvertibleDebtNoncurrent",
        "ConvertibleLongTermNotesPayable",
        "LongTermNotesPayable",
        "SeniorLongTermNotes",
        "LongTermDebtAndCapitalLeaseObligations",
    ),
    # 已付股东股息（现金流量表，量级由消费侧取绝对值）；FY 行有经营现金流而无该概念 → 0
    "div_paid_owners": (
        "PaymentsOfDividends",
        "PaymentsOfDividendsCommonStock",
        "PaymentsOfOrdinaryDividends",
    ),
    "int_exp": ("InterestExpense", "InterestExpenseDebt"),
    "fix_assets": ("PropertyPlantAndEquipmentNet",),
    "n_cashflow_act": ("NetCashProvidedByUsedInOperatingActivities",),
    "n_cashflow_inv_act": ("NetCashProvidedByUsedInInvestingActivities",),
    "n_cash_flows_fnc_act": ("NetCashProvidedByUsedInFinancingActivities",),
    "sga_exp": ("SellingGeneralAndAdministrativeExpense",),
    "depr_fa_coga_dpba": (
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
        "Depreciation",
    ),
    "basic_eps": ("EarningsPerShareBasic",),
    "diluted_eps": ("EarningsPerShareDiluted",),
}

# 拆分科目求和兜底：概念链整条落空时，把这些分项**相加**补上。
# 兜底链解决的是"同一概念不同 tag"，解决不了"一个概念被拆成两个 tag"——
# PDD/BABA 实测都不报 SellingGeneralAndAdministrativeExpense，只报
# SellingAndMarketingExpense + GeneralAndAdministrativeExpense 两条，
# 而 Beneish M-score 的 SGAI 因子要的是合计值。
EDGAR_CONCEPT_SUMS: Dict[str, tuple] = {
    "sga_exp": ("SellingAndMarketingExpense", "GeneralAndAdministrativeExpense"),
}


# duration facts 的期间长度容差（日历天）：财年 52/53 周与季度长度都有浮动
_ANNUAL_DAYS = (330, 400)
_QUARTER_DAYS = (60, 115)

# 分类保留额度：年度对齐十年覆盖深度（与 A股 报表 8 期同量级），季度只留近两年
EDGAR_ANNUAL_KEEP = 12
EDGAR_QUARTERLY_KEEP = 8


def _fact_duration_days(item: Dict[str, Any]) -> Optional[int]:
    """duration fact 的期间长度；instant fact（无 start）返回 None。"""
    start, end = str(item.get("start") or ""), str(item.get("end") or "")
    if not start or not end:
        return None
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except ValueError:
        return None


def _matches_period(item: Dict[str, Any], fp: str) -> bool:
    """该 fact 的期间长度是否符合 fp 声明的口径。

    (end, fp) **不能**唯一标识 companyfacts 的期间值：同一 (end, fp, filed)
    下会并存单季与年初至今累计值，只有 start/期间长度不同（实测 AAPL
    ('2018-09-29','FY') 同时含全年 265.6B 与 Q4 单季 62.9B）。不按期间长度
    选口径，年度营收/利润就会在全年与单季之间漂移。
    """
    days = _fact_duration_days(item)
    if days is None:
        return True  # instant fact（资产/负债等时点科目）无期间概念
    low, high = _ANNUAL_DAYS if fp == "FY" else _QUARTER_DAYS
    return low <= days <= high


def _apply_concept_sums(
    facts: Dict[str, Any],
    rows: Dict[tuple, Dict[str, Any]],
    marks: Dict[tuple, tuple],
    currency: str = "USD",
) -> None:
    """概念链落空的期间用分项求和补齐（只补空，不覆盖链上已有值）。

    逐期判断：同一家公司可能早年报合计、近年改拆分，整体判断会漏掉半段历史。

    **必须分项齐全才求和**。只披露营销费的年份若把营销费当成完整 SGA，
    SGAI/M-score 会得到一个数值正常但系统性偏低的结果——这比留空危险得多，
    因为下游无从知道它是不完整合计。缺分项时留空，走"数据不足"路径。
    """
    for field, components in EDGAR_CONCEPT_SUMS.items():
        per_component: Dict[str, Dict[tuple, float]] = {}
        for concept in components:
            concept_data = facts.get(concept)
            if not concept_data:
                continue
            units = _edgar_unit_rows(concept_data, currency)
            latest: Dict[tuple, tuple] = {}  # 期间键 → (filed, val)
            for item in units:
                end, fp, value = (
                    str(item.get("end") or ""),
                    str(item.get("fp") or ""),
                    item.get("val"),
                )
                if not end or value is None or not _matches_period(item, fp):
                    continue
                key = (end, fp)
                filed = str(item.get("filed") or "")
                if key not in latest or filed >= latest[key][0]:
                    latest[key] = (filed, value)
            per_component[concept] = {key: value for key, (_, value) in latest.items()}
        if len(per_component) < len(components):
            continue  # 有分项该公司整体就没报 → 无从求和
        complete_keys = set.intersection(*(set(v) for v in per_component.values()))
        for key in complete_keys:
            if (key, field) in marks or key not in rows:
                continue  # 链上已有值，或该期间没有任何其他科目（不凭空造行）
            rows[key][field] = sum(v[key] for v in per_component.values())


# 判定报告币种的核心科目：营收/归母净利/总资产（各自的整条兜底链）
EDGAR_CURRENCY_PROBE_FIELDS = ("total_revenue", "n_income_attr_p", "total_assets")
_ISO_CURRENCY_UNIT = re.compile(r"^[A-Z]{3}$")


def edgar_reporting_currency(facts: Dict[str, Any]) -> str:
    """发行人的报告币种：核心科目 FY 事实覆盖期数最多的 ISO 币种单位（并列时 USD 优先）。

    中概 20-F 发行人（PDD/BABA）以人民币编表，companyfacts 里 `USD` 单位只是最新一年的
    "为方便读者按 1 美元=7.xx 元折算"——每份 20-F 只折本年，多年序列逐年折算率不同，
    比较列与更早年份根本没有 USD 值（PDD 2016-2017、BABA 应收账款整条只有 CNY）。
    报告币种的全序列恒比便利折算长，按覆盖期数取即可确定地选中它；美国本土 10-K 发行人
    只有 USD 单位，结果不变。companyfacts 不含 dei 的报告币种事实（PDD 无 dei、BABA 的 dei
    只有股数与 ADS 比例），只能从单位推断。核心科目一条都没有时退回 USD（原口径）。
    """
    periods: Dict[str, set] = {}
    for field in EDGAR_CURRENCY_PROBE_FIELDS:
        for concept in EDGAR_CONCEPT_CHAINS[field]:
            units = (facts.get(concept) or {}).get("units") or {}
            for unit, items in units.items():
                if not _ISO_CURRENCY_UNIT.match(unit):
                    continue  # 每股（CNY/shares）、股数（shares）等非货币单位不参与
                bucket = periods.setdefault(unit, set())
                for item in items:
                    if (
                        item.get("fp") == "FY"
                        and item.get("end")
                        and item.get("val") is not None
                        and _matches_period(item, "FY")
                    ):
                        bucket.add((field, item["end"]))
    if not periods:
        return "USD"
    return max(sorted(periods), key=lambda unit: (len(periods[unit]), unit == "USD"))


def _edgar_unit_rows(concept_data: Dict[str, Any], currency: str) -> List[Dict[str, Any]]:
    """概念在报告币种下的事实：金额 `<CUR>`、每股 `<CUR>/shares`。

    **只取报告币种**：该币种缺某概念时留空，不回退到 USD——同一期间行里混着人民币科目与
    美元便利折算科目，下游的比率/估值都会静默算错（每行只有一个 currency 标签）。
    """
    units = concept_data.get("units") or {}
    return units.get(currency) or units.get(f"{currency}/shares") or []


def _mark_other_currency_gaps(
    facts: Dict[str, Any], rows: Dict[tuple, Dict[str, Any]], currency: str
) -> None:
    """报告币种下为空、但该期有别币种事实（或该概念整条没有报告币种单位）的科目记
    `edgar_missing_reasons[field] = other_currency_only`：不混币取数的代价是「不可知」，
    不能被下游当成「发行人没报这个概念」按 0 推断（股息记录/长期债务）。"""
    own_units = {currency, f"{currency}/shares"}
    for field, chain in EDGAR_CONCEPT_CHAINS.items():
        foreign_only = False
        foreign_periods: set = set()
        for concept in chain:
            units = (facts.get(concept) or {}).get("units") or {}
            foreign = [unit for unit in units if unit not in own_units]
            if not foreign:
                continue
            if not any(units.get(unit) for unit in own_units):
                foreign_only = True  # 该概念整条没有报告币种单位：任何期间的空值都不可知
            for unit in foreign:
                for item in units[unit]:
                    end, fp = str(item.get("end") or ""), str(item.get("fp") or "")
                    if end and item.get("val") is not None and _matches_period(item, fp):
                        foreign_periods.add((end, fp))
        if not foreign_only and not foreign_periods:
            continue
        for key, row in rows.items():
            if row.get(field) is None and (foreign_only or key in foreign_periods):
                row.setdefault("edgar_missing_reasons", {})[field] = EDGAR_MISSING_OTHER_CURRENCY


def fetch_edgar_companyfacts(symbol: str, market: str) -> List[Dict[str, Any]]:
    """EDGAR XBRL → 每 (end, fp) 一行。

    口径选择：duration fact 必须与 fp 的期间长度相符（FY=全年、Qx=单季），
    不符者整条丢弃而不是任其覆盖；同口径下多 filing 取 filed 最新（重述胜）。
    币种：全部科目按发行人的报告币种取（edgar_reporting_currency），行上 `currency` 如实标注。
    """
    from .report_fetchers import edgar_companyfacts, edgar_lookup

    lookup = edgar_lookup(symbol)
    if not lookup:
        raise ValueError(f"美股代码 {symbol} 未在 SEC 注册表中找到")
    facts = (edgar_companyfacts(lookup["cik"]).get("facts") or {}).get("us-gaap") or {}
    currency = edgar_reporting_currency(facts)
    if currency != "USD":
        logger.info("EDGAR %s 报告币种为 %s，按该币种取数（不取美元便利折算）", symbol, currency)

    rows: Dict[tuple, Dict[str, Any]] = {}
    # (期间键, 字段) → (概念优先级, filed)：优先级与重述判定都必须**逐期**做
    marks: Dict[tuple, tuple] = {}
    for field, chain in EDGAR_CONCEPT_CHAINS.items():
        # 整条链全扫，不在首个有值概念处 break：公司换 XBRL tag 后首选概念
        # 只覆盖近几年，旧年份的值只存在于备用概念里，一次性 break 会整段丢失
        for rank, concept in enumerate(chain):
            concept_data = facts.get(concept)
            if not concept_data:
                continue
            for item in _edgar_unit_rows(concept_data, currency):
                end = str(item.get("end") or "")
                fp = str(item.get("fp") or "")
                value = item.get("val")
                if not end or value is None:
                    continue
                if not _matches_period(item, fp):
                    continue  # 口径不符（累计值混入单季期、单季混入年报期）
                key = (end, fp)
                filed = str(item.get("filed") or "")
                previous = marks.get((key, field))
                if previous is not None:
                    previous_rank, previous_filed = previous
                    if rank > previous_rank:
                        continue  # 该期已有更高优先级概念的值，备用概念不覆盖
                    if rank == previous_rank and filed < previous_filed:
                        continue  # 同概念多 filing：filed 最新者胜（重述）
                row = rows.setdefault(
                    key,
                    {
                        "end_date": end.replace("-", ""),
                        "fp": fp,
                        "form": item.get("form"),
                        "currency": currency,
                        # 概念链版本：「缺概念 → 0」只对按当前链抓取的行成立
                        "edgar_chain_version": EDGAR_PIVOT_VERSION,
                    },
                )
                row[field] = value
                marks[(key, field)] = (rank, filed)

    _apply_concept_sums(facts, rows, marks, currency)
    _mark_other_currency_gaps(facts, rows, currency)

    # 分类封顶：年度与季度各留各的额度。统一按 end_date 取前 N 会被数量占优的
    # 季度行占满——实测 AAPL 158 个期间里 FY 仅 20 个，一刀切 40 行只剩 4 个
    # 年度，十余年年报史（利润质量/趋势分析的输入）被静默丢弃。
    ordered = sorted(rows.values(), key=lambda r: r["end_date"], reverse=True)
    annual = [row for row in ordered if row["fp"] == "FY"][:EDGAR_ANNUAL_KEEP]
    quarterly = [row for row in ordered if row["fp"] != "FY"][:EDGAR_QUARTERLY_KEEP]
    return sorted(annual + quarterly, key=lambda r: r["end_date"], reverse=True)
