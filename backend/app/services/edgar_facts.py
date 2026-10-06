"""SEC EDGAR companyfacts（XBRL）→ 每 (期末, fp) 一行的透视（#281，由 security_profile_service 迁出）。

概念兜底链、分项求和兜底、按报告币种取数、期间长度校验与年度/季度分类封顶都在这里，
版本常量 `EDGAR_PIVOT_VERSION` 与它所版本化的透视代码同处一个模块——改透视口径就在这里
bump，不必再跨文件找（此前常量在 earnings_quality、代码在 security_profile_service）。
模块顶层只依赖标准库；网络请求在 `fetch_edgar_companyfacts` 内按需导入 report_fetchers。
"""

import re
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core.logging import get_app_logger

logger = get_app_logger(__name__)

# EDGAR 透视行的版本（行上 `edgar_chain_version`）。v2：长期债务扩展链与已付股息链；
# v3：按**报告币种**取数（中概 20-F 发行人为 CNY，此前只取 USD 单位——那只是最新一两年的
# 便利折算，多年序列残缺且逐年折算率不同）。v4（#351）：报告币种**逐期**判定（改报币种的发行人
# 最新年度不再整段消失）；只有时点事实的季度占位行不再生成；EPS 链补 EarningsPerShareBasicAndDiluted；
# 已付股息链 CommonStock 概念排前。v5：季度身份按期末日（后续季报里上一季度的比较数不再以本期
# fp 另起一行，#359）；拆股前申报的 EPS 按已证实的拆股因子折成最新股本口径（#289）。
# 部署后美股档案需重新同步一次才会换成新版本的行
EDGAR_PIVOT_VERSION = 5
# 「现金流量表在而无股息概念 → 0」「本期无长期债务概念而往年有 → 0」只对 v2+ 的行成立——
# 旧链抓的行缺这些概念只说明当时没抓，不说明公司没有（v3 只改取数币种，不影响这条前提）。
# 股息（本模块）与长期债务（graham_screen）共用这一个阈值与 edgar_zero_inference_allowed
EDGAR_ZERO_INFERENCE_MIN_VERSION = 2
# 行上 `edgar_missing_reasons[field]` 的取值：该科目在报告币种下为空、但发行人用**别的币种**
# 披露过这个概念。只有不带这个标记的空值才是「发行人没报这个概念」——股息/长期债务的
# 「缺概念 → 0」推断只对后者成立（本模块 _mark_other_currency_gaps 写入）
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
    # 已付股东股息（现金流量表，量级由消费侧取绝对值）；FY 行有经营现金流而无该概念 → 0。
    # CommonStock 排前（v4，#351）：us-gaap 定义里 PaymentsOfDividends 是「付给普通股股东、优先股
    # 股东与非控股股东」的合计（SEC companyfacts 的 description 原文，2026-09 核对），两个都打时
    # 取合计会把优先股/非控股股息算成向普通股东派息；与港股 div_paid_owners「不含非控股」同口径。
    # 只打合计概念的公司仍用它兜底
    "div_paid_owners": (
        "PaymentsOfDividendsCommonStock",
        "PaymentsOfOrdinaryDividends",
        "PaymentsOfDividends",
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
    # 基本与摊薄相同而只打一个合并标签的申报人（亏损年份常见）：EarningsPerShareBasicAndDiluted
    # 作两条链的最后兜底（v4，#351）
    "basic_eps": ("EarningsPerShareBasic", "EarningsPerShareBasicAndDiluted"),
    "diluted_eps": ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"),
}

# 拆分科目求和兜底：概念链整条落空时，把这些分项**相加**补上。
# 兜底链解决的是"同一概念不同 tag"，解决不了"一个概念被拆成两个 tag"——
# PDD/BABA 实测都不报 SellingGeneralAndAdministrativeExpense，只报
# SellingAndMarketingExpense + GeneralAndAdministrativeExpense 两条，
# 而 Beneish M-score 的 SGAI 因子要的是合计值。
EDGAR_CONCEPT_SUMS: Dict[str, tuple] = {
    "sga_exp": ("SellingAndMarketingExpense", "GeneralAndAdministrativeExpense"),
}


# 时点（资产负债表）科目：其余科目都是期间（duration）事实
EDGAR_INSTANT_FIELDS = frozenset(
    {
        "total_assets",
        "total_liab",
        "total_hldr_eqy_exc_min_int",
        "money_cap",
        "accounts_receiv",
        "inventories",
        "total_cur_assets",
        "total_cur_liab",
        "lt_debt",
        "fix_assets",
    }
)


def is_quarter_placeholder(row: Dict[str, Any]) -> bool:
    """季度行却没有任何期间科目：10-Q 比较资产负债表的占位行（v4 起不再生成，#351-2）。
    同步时库里 v3 留下的这类行按它删除。"""
    if row.get("fp") == "FY":
        return False
    duration_fields = set(EDGAR_CONCEPT_CHAINS) - EDGAR_INSTANT_FIELDS
    return not any(row.get(field) is not None for field in duration_fields)


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


def _pivot_concepts() -> set:
    concepts = {concept for chain in EDGAR_CONCEPT_CHAINS.values() for concept in chain}
    concepts.update(c for parts in EDGAR_CONCEPT_SUMS.values() for c in parts)
    return concepts


def _quarter_number(end: str, fiscal_year_ends: List[str]) -> Optional[int]:
    """期末日相对财年末推算季度序号（1–3）：上一个财年末之后约 91 天一季，取不到上一个
    财年末时用下一个财年末倒推；推不出 1–3（缺财年末、或恰是四季度）返回 None。"""
    try:
        target = date.fromisoformat(end)
    except ValueError:
        return None
    earlier = [e for e in fiscal_year_ends if e < end]
    later = [e for e in fiscal_year_ends if e > end]
    number = None
    if earlier:
        number = round((target - date.fromisoformat(max(earlier))).days / 91.3)
    elif later:
        number = 4 - round((date.fromisoformat(min(later)) - target).days / 91.3)
    return number if number in (1, 2, 3) else None


def edgar_quarter_labels(facts: Dict[str, Any]) -> Dict[str, str]:
    """季度期末日 → 季度标签（v5，#359）。

    companyfacts 的 `fp` 是**申报**的财季，不是事实所属的期间：后续季报里上一季度的比较数
    （权益变动表的上季度净利润、上季末余额）照样挂着本期的 fp——SNDK 2025-10-03 结束的
    一季度，净利润在 Q1、Q2、Q3 三份 10-Q 里各出现一次，透视成 `20251003|Q1/Q2/Q3` 三行，
    挤占季度额度。季度的身份只看**期末日**：同一期末日只留一行。标签由期末日相对财年末推算
    （`_quarter_number`，财年末取自年度事实）；推不出时取最早报告这一季度单季数的申报的 fp
    （不能一律用它：分拆上市的 SNDK 首份 10-Q 是 FY2025 Q2，里面比较期 2024-09-27 那一季
    挂的是 Q2）。标签只用于展示与排序，TTM 连续性按期末日判断（graham_screen）。"""
    earliest: Dict[str, Tuple[str, str]] = {}  # 期末日 → (filed, fp)
    fiscal_year_ends: set = set()
    for concept in _pivot_concepts():
        units = (facts.get(concept) or {}).get("units") or {}
        for items in units.values():
            for item in items:
                fp = str(item.get("fp") or "")
                end = str(item.get("end") or "")
                days = _fact_duration_days(item)
                if not end or not fp or days is None:
                    continue
                if fp == "FY":
                    if _ANNUAL_DAYS[0] <= days <= _ANNUAL_DAYS[1]:
                        fiscal_year_ends.add(end)
                    continue
                if not _QUARTER_DAYS[0] <= days <= _QUARTER_DAYS[1]:
                    continue
                candidate = (str(item.get("filed") or ""), fp)
                if end not in earliest or candidate < earliest[end]:
                    earliest[end] = candidate
    ordered_ends = sorted(fiscal_year_ends)
    labels: Dict[str, str] = {}
    for end, (_, fp) in earliest.items():
        number = _quarter_number(end, ordered_ends)
        labels[end] = f"Q{number}" if number else fp
    return labels


def normalize_quarter_periods(facts: Dict[str, Any]) -> Dict[str, Any]:
    """把季度事实的 fp 改写成期末日对应的季度标签（edgar_quarter_labels），原 fp 留在
    `fp_filed`。之后按 (期末日, fp) 透视时，同一季度的本期数与后续申报里的比较数落进同一行，
    filed 最新者胜（与年度行的重述规则一致）。年度（FY）事实不动；没有单季数的期末日（上年末的
    比较资产负债表）保持原 fp，由季度占位行规则丢弃。"""
    labels = edgar_quarter_labels(facts)
    if not labels:
        return facts
    concepts = _pivot_concepts()
    normalized: Dict[str, Any] = {}
    for concept, data in facts.items():
        if concept not in concepts:
            normalized[concept] = data
            continue
        units: Dict[str, List[Dict[str, Any]]] = {}
        for unit, items in ((data or {}).get("units") or {}).items():
            rewritten = []
            for item in items:
                fp = str(item.get("fp") or "")
                label = labels.get(str(item.get("end") or ""))
                if fp and fp != "FY" and label and label != fp:
                    item = {**item, "fp": label, "fp_filed": fp}
                rewritten.append(item)
            units[unit] = rewritten
        normalized[concept] = {**data, "units": units}
    return normalized


# 拆股口径（v5，#289）：EPS 按拆股因子统一到最新股本口径
EDGAR_SPLIT_RATIO_CONCEPTS = ("StockholdersEquityNoteStockSplitConversionRatio1",)
_EPS_CONCEPTS = (
    "EarningsPerShareBasic",
    "EarningsPerShareDiluted",
    "EarningsPerShareBasicAndDiluted",
)
EPS_FIELDS = ("basic_eps", "diluted_eps")
# 同一期 EPS 前后两次申报的比值与拆股因子的容差（申报值四舍五入到分：1.22 × 10 = 12.2 vs 12.25）
SPLIT_FACTOR_TOLERANCE = 0.02
# 比值落在这个区间内视为普通重述/更正，不是拆股
_RESTATEMENT_NOISE = (1 / 1.5, 1.5)
# 没有拆股比例事实时，重述证据须来自至少这么多个期间、且比值接近整数或整数的倒数
MIN_RESTATED_PERIODS_WITHOUT_RATIO = 2


def _clean_factor(ratio: float) -> Optional[float]:
    """比值接近整数（正拆）或整数的倒数（合股）时返回该因子，否则 None。"""
    for candidate in (round(ratio), 1 / round(1 / ratio) if ratio < 1 else None):
        if candidate and abs(ratio / candidate - 1) <= 0.01:
            return float(candidate)
    return None


def _eps_restatements(facts: Dict[str, Any], currency_for: Callable[[tuple], str]) -> List[dict]:
    """同一期 EPS 在前后两份申报里的值差出拆股量级的配对（原文证据）。"""
    pairs: List[dict] = []
    for concept in _EPS_CONCEPTS:
        data = facts.get(concept)
        if not data:
            continue
        by_key: Dict[tuple, Dict[str, float]] = {}
        for item in _period_items(data, currency_for):
            fp = str(item.get("fp") or "")
            value = item.get("val")
            if not value or not _matches_period(item, fp):
                continue
            key = (str(item["end"]), fp)
            by_key.setdefault(key, {})[str(item.get("filed") or "")] = float(value)
        for key, filings in by_key.items():
            ordered = sorted(filings.items())
            for (filed_a, before), (filed_b, after) in zip(ordered, ordered[1:]):
                if before * after <= 0:
                    continue
                ratio = before / after
                if _RESTATEMENT_NOISE[0] < ratio < _RESTATEMENT_NOISE[1]:
                    continue
                pairs.append(
                    {
                        "period": f"{key[0].replace('-', '')}|{key[1]}",
                        "concept": concept,
                        "before": {"filed": filed_a, "value": before},
                        "after": {"filed": filed_b, "value": after},
                        "ratio": ratio,
                    }
                )
    return pairs


def edgar_split_events(
    facts: Dict[str, Any], currency_for: Callable[[tuple], str]
) -> Tuple[List[dict], List[dict]]:
    """拆股事件 → (已证实, 未证实)。

    证据优先级（#289）：
    1. 公司披露的拆股比例事实（`StockholdersEquityNoteStockSplitConversionRatio1`，期末日即
       生效日）**且**有同一期 EPS 在生效日前后两份申报里的值、比值与比例吻合（后续年报按新
       股本重述比较期）→ 已证实，因子 = 披露的比例；
    2. 没有比例事实时，只有至少两个期间给出同一个「干净」比值（接近整数或整数倒数）才证实，
       生效日取不到，用「最晚的旧口径申报日 < 生效 ≤ 最早的新口径申报日」这一区间；
    3. 有比例事实而找不到重述证据 → 未证实，只在行上标注口径，不猜因子。
    `pre_filed_max` = 证据里最晚的旧口径申报日（只有重述证据时据它判旧口径）。"""
    pairs = _eps_restatements(facts, currency_for)
    dated: Dict[str, float] = {}
    for concept in EDGAR_SPLIT_RATIO_CONCEPTS:
        for items in ((facts.get(concept) or {}).get("units") or {}).values():
            for item in items:
                end, value = str(item.get("end") or ""), item.get("val")
                if end and value and float(value) > 0:
                    dated[end] = float(value)
    # 同一次拆股常在几份申报里以不同日期出现（公告日、登记日、生效日：NFLX 2015 年 7:1 有
    # 06-23 / 06-30 / 07-14 三个）：同一比例、相隔不到 400 天的归为一次，取最早日期
    ratio_facts: List[Tuple[str, float]] = []
    for day, ratio in sorted(dated.items()):
        if ratio_facts and ratio_facts[-1][1] == ratio:
            gap = (date.fromisoformat(day) - date.fromisoformat(ratio_facts[-1][0])).days
            if gap < 400:
                continue
        ratio_facts.append((day, ratio))

    def evidence_of(pair: dict, *, spans: Optional[List[str]] = None) -> dict:
        evidence = {k: pair[k] for k in ("period", "concept", "before", "after")}
        if spans:
            evidence["spans_confirmed_splits"] = spans  # 这笔重述同时跨了这些已证实的拆股
        return evidence

    def ratio_facts_within(pair: dict) -> List[Tuple[str, float]]:
        return [
            (day, ratio)
            for day, ratio in ratio_facts
            if pair["before"]["filed"] < day <= pair["after"]["filed"]
        ]

    # 1. 有比例事实的拆股：**逐次**证实（PR #381 评审）。一笔重述的前后申报之间若夹着多次
    #    拆股，它的比值是这几次的乘积——只有其余几次都已被各自的证据证实时，才能用它证实
    #    剩下这一次；反复迭代到不再有新证实
    confirmed_by_date: Dict[str, dict] = {}
    support: Dict[str, List[str]] = {}  # 证实该事件的全部证据里最晚的旧口径申报日
    changed = True
    while changed:
        changed = False
        for effective, ratio in ratio_facts:
            if effective in confirmed_by_date:
                continue
            for pair in pairs:
                window = ratio_facts_within(pair)
                if (effective, ratio) not in window:
                    continue
                others = [day for day, _ in window if day != effective]
                if any(day not in confirmed_by_date for day in others):
                    continue  # 同一窗口里还有没证实的拆股：这笔比值说明不了哪一次
                expected = ratio
                for day in others:
                    expected *= confirmed_by_date[day]["factor"]
                if abs(pair["ratio"] / expected - 1) > SPLIT_FACTOR_TOLERANCE:
                    continue
                if effective not in confirmed_by_date:
                    confirmed_by_date[effective] = {
                        "effective_date": effective,
                        "factor": ratio,
                        "basis": "ratio_fact+restated",
                        "evidence": evidence_of(pair, spans=others),
                    }
                    changed = True
                support.setdefault(effective, []).append(pair["before"]["filed"])
    for effective, event in confirmed_by_date.items():
        event["pre_filed_max"] = max(support[effective])
    confirmed: List[dict] = [
        confirmed_by_date[day] for day, _ in ratio_facts if day in confirmed_by_date
    ]
    unconfirmed: List[dict] = [
        {"effective_date": day, "ratio": ratio}
        for day, ratio in ratio_facts
        if day not in confirmed_by_date
    ]

    # 2. 只有重述证据的拆股：先用窗口内**已证实**事件的乘积解释每笔重述，只拿**剩下的**比值
    #    找新事件——跨两次拆股的累计比值（2×3=6）不能再被当成第三次独立拆股（PR #381 评审：
    #    FY2022 的 EPS 曾被折了 2×3×6=36 倍）；窗口里有未证实的比例事实时这笔重述说不清，
    #    不参与。新事件同样至少要两个期间给出同一个干净的剩余比值；接受一批后重算剩余比值
    def within_restated(pair: dict, event: dict) -> bool:
        return (
            pair["before"]["filed"] <= event["pre_filed_max"]
            and event["post_filed_min"] <= pair["after"]["filed"]
        )

    restated_events: List[dict] = []
    for _ in range(len(pairs) + 1):  # 每轮至少解释掉一组重述，轮数有上限
        by_factor: Dict[float, List[dict]] = {}
        for pair in pairs:
            window = ratio_facts_within(pair)
            if any(day not in confirmed_by_date for day, _ in window):
                continue
            explained = 1.0
            for day, _ in window:
                explained *= confirmed_by_date[day]["factor"]
            for event in restated_events:
                if within_restated(pair, event):
                    explained *= event["factor"]
            residual = pair["ratio"] / explained
            if _RESTATEMENT_NOISE[0] < residual < _RESTATEMENT_NOISE[1]:
                continue  # 已被已知拆股完全解释（或只是普通更正）
            factor = _clean_factor(residual)
            if factor:
                by_factor.setdefault(factor, []).append(pair)
        accepted = None
        for factor, group in sorted(by_factor.items()):
            pre_max = max(pair["before"]["filed"] for pair in group)
            post_min = min(pair["after"]["filed"] for pair in group)
            periods = {pair["period"] for pair in group}
            if len(periods) < MIN_RESTATED_PERIODS_WITHOUT_RATIO or pre_max >= post_min:
                continue  # 证据不足或新旧口径申报交错：不当拆股
            accepted = {
                "effective_date": None,
                "factor": factor,
                "basis": "restated",
                "pre_filed_max": pre_max,
                "post_filed_min": post_min,
                "evidence": evidence_of(group[0]),
            }
            break
        if accepted is None:
            break
        restated_events.append(accepted)
    return confirmed + restated_events, unconfirmed


def _filed_before_split(event: dict, filed: str) -> bool:
    """该申报日的 EPS 是否为拆股前口径：有生效日按生效日（之后的申报已追溯调整），
    只有重述证据时按「最晚的旧口径申报日」。"""
    if event["effective_date"]:
        return filed < event["effective_date"]
    return filed <= event["pre_filed_max"]


def apply_split_adjustments(
    rows: Dict[tuple, Dict[str, Any]],
    marks: Dict[tuple, tuple],
    confirmed: List[dict],
    unconfirmed: List[dict],
) -> None:
    """按已证实的拆股事件把旧口径 EPS 折成最新股本口径，行上记 `eps_split_adjustment`
    （原值、来源申报日、因子与证据），格雷厄姆的盈利增长与 TTM 可追溯。

    判定只看**该 EPS 值来自哪份申报**（`_filed_before_split`）：生效日之前的申报即旧口径；
    后续申报已按新股本重述的值（filed 最新者胜，透视时已取到）不再调整。未证实的比例
    事实只在受影响的行上记 `eps_basis_unverified`，数值不动。"""
    for key, row in rows.items():
        adjustment: Dict[str, Any] = {}
        for field in EPS_FIELDS:
            value = row.get(field)
            mark = marks.get((key, field))
            if value is None or mark is None:
                continue
            source_filed = mark[1]
            factor = 1.0
            applied = []
            ambiguous = []
            for event in confirmed:
                if _filed_before_split(event, source_filed):
                    factor *= event["factor"]
                    applied.append(event)
                elif event["effective_date"] is None and source_filed < event["post_filed_min"]:
                    ambiguous.append(event)  # 新旧口径申报之间：分不清，不调整
            if ambiguous:
                row.setdefault("eps_basis_unverified", {})[field] = {
                    "source_filed": source_filed,
                    "restated_splits": ambiguous,
                    "note": "该值的申报日介于拆股前后两种口径之间，每股盈利保持原申报口径",
                }
            if applied:
                # 申报值精确到分（或厘）：折算后保留 6 位小数，去掉浮点噪声
                row[field] = round(value / factor, 6)
                adjustment.setdefault("original", {})[field] = value
                adjustment.setdefault("source_filed", {})[field] = source_filed
                adjustment["factor"] = factor
                adjustment["events"] = applied
            pending = [
                event
                for event in unconfirmed
                if source_filed < event["effective_date"] and key[0] < event["effective_date"]
            ]
            if pending:
                row.setdefault("eps_basis_unverified", {})[field] = {
                    "source_filed": source_filed,
                    "split_ratio_facts": pending,
                    "note": "拆股比例未获重述数据证实，每股盈利保持原申报口径",
                }
        if adjustment:
            row["eps_split_adjustment"] = adjustment


def _unit_currency(unit: str) -> Optional[str]:
    """货币单位 → 币种：`CNY` / `CNY/shares` → CNY；股数、纯数等非货币单位 → None。"""
    base = unit.split("/", 1)[0]
    if not _ISO_CURRENCY_UNIT.match(base) or unit not in (base, f"{base}/shares"):
        return None
    return base


def _period_items(concept_data: Dict[str, Any], currency_for: Callable[[tuple], str]):
    """概念在**各期自己的报告币种**下的事实：金额 `<CUR>`、每股 `<CUR>/shares`。

    只取该期币种：该币种缺某概念时留空，不回退到别的币种——同一期间行里混着人民币科目与
    美元便利折算科目，下游的比率/估值都会静默算错（每行只有一个 currency 标签）。"""
    for unit, items in (concept_data.get("units") or {}).items():
        currency = _unit_currency(unit)
        if currency is None:
            continue
        for item in items:
            end, fp = str(item.get("end") or ""), str(item.get("fp") or "")
            if end and currency_for((end, fp)) == currency:
                yield item


def _apply_concept_sums(
    facts: Dict[str, Any],
    rows: Dict[tuple, Dict[str, Any]],
    marks: Dict[tuple, tuple],
    currency_for: Callable[[tuple], str],
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
            latest: Dict[tuple, tuple] = {}  # 期间键 → (filed, val)
            for item in _period_items(concept_data, currency_for):
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
    """发行人的全史报告币种：核心科目 FY 事实覆盖期数最多的 ISO 币种单位（并列时 USD 优先）。

    v4 起它只是**逐期判定的兜底与并列裁决**（见 edgar_period_currencies）：核心科目覆盖不到的
    期间键用它；某期最新申报里两种币种覆盖面相同时也用它。

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


def edgar_period_currencies(facts: Dict[str, Any]) -> Tuple[str, Dict[tuple, str]]:
    """逐期判定报告币种（v4，#351）→ (全史币种, {(end, fp): 该期币种})。

    全史只取一个币种（v3）在发行人**改报币种**时出错：2014–2022 报 CNY、FY2023 起改报 USD，
    CNY 期数多而胜出，FY2023 以后没有 CNY 事实，最新年度整行不生成，估值用的是三年前的年报
    且没有任何换币提示（反向情形下旧币种历史被整段删掉）。改为逐期：

    - 某期由**覆盖它的最新一份申报**决定（重述胜；改币后的比较数随新币种）；
    - 最新申报里同一期出现两种币种时，取该申报里覆盖期间**更多**的币种——报告币种有比较期，
      20-F 的美元便利折算只折本年（PDD FY2025：CNY 覆盖 2023–2025 三年，USD 只有 2025）；
    - 仍并列才用全史币种裁决，再并列取 USD。

    只看核心科目（营收/归母净利/总资产），覆盖不到的期间键由调用方退回全史币种。格雷厄姆与
    利润质量本就按行上 currency 处理换币（恒定币种折算、`currency_change`），逐行换币是安全的。
    """
    default = edgar_reporting_currency(facts)
    records: Dict[tuple, List[tuple]] = {}  # (end, fp) → [(filed, 申报, 币种)]
    for field in EDGAR_CURRENCY_PROBE_FIELDS:
        for concept in EDGAR_CONCEPT_CHAINS[field]:
            units = (facts.get(concept) or {}).get("units") or {}
            for unit, items in units.items():
                if not _ISO_CURRENCY_UNIT.match(unit):
                    continue
                for item in items:
                    end, fp = str(item.get("end") or ""), str(item.get("fp") or "")
                    if not end or item.get("val") is None or not _matches_period(item, fp):
                        continue
                    filed = str(item.get("filed") or "")
                    # 申报身份：accn（同日多份申报也分得开）；裁剪过的固件没有 accn 时退回 filed
                    filing = str(item.get("accn") or filed)
                    records.setdefault((end, fp), []).append((filed, filing, unit))
    coverage: Dict[str, Dict[str, set]] = {}  # 申报 → 币种 → 该申报以该币种覆盖的期间键
    for key, entries in records.items():
        for _, filing, unit in entries:
            coverage.setdefault(filing, {}).setdefault(unit, set()).add(key)

    currencies: Dict[tuple, str] = {}
    for key, entries in records.items():
        latest = max(filed for filed, _, _ in entries)
        filings = {filing for filed, filing, _ in entries if filed == latest}
        candidates = sorted({unit for _, filing, unit in entries if filing in filings})
        if len(candidates) == 1:
            currencies[key] = candidates[0]
            continue

        def breadth(unit: str) -> int:
            covered: set = set()
            for filing in filings:
                covered |= coverage[filing].get(unit, set())
            return len(covered)

        currencies[key] = max(
            candidates, key=lambda unit: (breadth(unit), unit == default, unit == "USD")
        )
    return default, currencies


def _mark_other_currency_gaps(facts: Dict[str, Any], rows: Dict[tuple, Dict[str, Any]]) -> None:
    """报告币种下为空、但该期有别币种事实（或该概念整条没有报告币种单位）的科目记
    `edgar_missing_reasons[field] = other_currency_only`：不混币取数的代价是「不可知」，
    不能被下游当成「发行人没报这个概念」按 0 推断（股息记录/长期债务）。

    报告币种按行（v4 逐期判定）：同币种的行一组，组内判定与单一币种时逐字节相同。"""
    groups: Dict[str, Dict[tuple, Dict[str, Any]]] = {}
    for key, row in rows.items():
        groups.setdefault(row["currency"], {})[key] = row
    for currency, group in groups.items():
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
            for key, row in group.items():
                if row.get(field) is None and (foreign_only or key in foreign_periods):
                    row.setdefault("edgar_missing_reasons", {})[field] = (
                        EDGAR_MISSING_OTHER_CURRENCY
                    )


def fetch_edgar_companyfacts(symbol: str, market: str) -> List[Dict[str, Any]]:
    """EDGAR XBRL → 每 (end, fp) 一行。

    口径选择：duration fact 必须与 fp 的期间长度相符（FY=全年、Qx=单季），
    不符者整条丢弃而不是任其覆盖；同口径下多 filing 取 filed 最新（重述胜）。
    币种：逐期按报告币种取（edgar_period_currencies），行上 `currency` 如实标注。
    """
    from .report_fetchers import edgar_companyfacts, edgar_lookup

    lookup = edgar_lookup(symbol)
    if not lookup:
        raise ValueError(f"美股代码 {symbol} 未在 SEC 注册表中找到")
    facts = (edgar_companyfacts(lookup["cik"]).get("facts") or {}).get("us-gaap") or {}
    # v5（#359）：季度身份按期末日，同一季度的比较数与本期数落进同一行
    facts = normalize_quarter_periods(facts)
    default_currency, period_currencies = edgar_period_currencies(facts)

    def currency_for(key: tuple) -> str:
        return period_currencies.get(key, default_currency)

    rows: Dict[tuple, Dict[str, Any]] = {}
    # (期间键, 字段) → (概念优先级, filed)：优先级与重述判定都必须**逐期**做
    marks: Dict[tuple, tuple] = {}
    # 有期间（duration）事实的期间键：季度行只有时点事实时是 10-Q 比较资产负债表的占位行
    duration_keys: set = set()
    for field, chain in EDGAR_CONCEPT_CHAINS.items():
        # 整条链全扫，不在首个有值概念处 break：公司换 XBRL tag 后首选概念
        # 只覆盖近几年，旧年份的值只存在于备用概念里，一次性 break 会整段丢失
        for rank, concept in enumerate(chain):
            concept_data = facts.get(concept)
            if not concept_data:
                continue
            for item in _period_items(concept_data, currency_for):
                end = str(item.get("end") or "")
                fp = str(item.get("fp") or "")
                value = item.get("val")
                if not end or value is None:
                    continue
                if not _matches_period(item, fp):
                    continue  # 口径不符（累计值混入单季期、单季混入年报期）
                key = (end, fp)
                if _fact_duration_days(item) is not None:
                    duration_keys.add(key)
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
                        "currency": currency_for(key),
                        # 概念链版本：「缺概念 → 0」只对按当前链抓取的行成立
                        "edgar_chain_version": EDGAR_PIVOT_VERSION,
                    },
                )
                row[field] = value
                marks[(key, field)] = (rank, filed)

    # 季度占位行（v4，#351）：10-Q 的比较资产负债表是上年末的时点事实，fp 却是本季度
    # （`20251231|Q1/Q2/Q3`）。这种行只有资产负债科目，却占着季度额度——Q3 10-Q 之后三行
    # 占位把上年同期 Q1 挤出 8 行，TTM 退回年报。只含时点事实的季度行不生成、不计额度；
    # 年度行不受影响（10-K 的比较期同时有损益，且年报行本来就要保留）
    for key in [key for key in rows if key[1] != "FY" and key not in duration_keys]:
        del rows[key]

    # v5（#289）：拆股前申报的 EPS 折成最新股本口径（只按已证实的拆股事件，行上记依据）
    confirmed_splits, unconfirmed_splits = edgar_split_events(facts, currency_for)
    apply_split_adjustments(rows, marks, confirmed_splits, unconfirmed_splits)

    _apply_concept_sums(facts, rows, marks, currency_for)
    _mark_other_currency_gaps(facts, rows)

    row_currencies = sorted({row["currency"] for row in rows.values()})
    if row_currencies != ["USD"]:
        logger.info("EDGAR %s 按报告币种取数（逐期）：%s", symbol, "、".join(row_currencies))

    # 分类封顶：年度与季度各留各的额度。统一按 end_date 取前 N 会被数量占优的
    # 季度行占满——实测 AAPL 158 个期间里 FY 仅 20 个，一刀切 40 行只剩 4 个
    # 年度，十余年年报史（利润质量/趋势分析的输入）被静默丢弃。
    ordered = sorted(rows.values(), key=lambda r: r["end_date"], reverse=True)
    annual = [row for row in ordered if row["fp"] == "FY"][:EDGAR_ANNUAL_KEEP]
    quarterly = [row for row in ordered if row["fp"] != "FY"][:EDGAR_QUARTERLY_KEEP]
    return sorted(annual + quarterly, key=lambda r: r["end_date"], reverse=True)
