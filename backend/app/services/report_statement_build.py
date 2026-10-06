"""港股报表构建层：抽取行（结构化行 + LLM 映射）→ 会计期行（#281 由 report_statement_service 迁出）。

`STATEMENT_BUILD_VERSION`（report_statement_prompts）覆盖的全部口径都在这里：EPS 单位（仙→元，
跨报告传播）、EPS 附注号守卫、夹层权益、资产小计修复、重列标记、`build_period_rows` 取数，
以及比较期合并与比较列证据。全部是纯函数（不碰 DB）；需要库里雅虎行的证据由服务层取来传入。
改这里的口径必须 bump `STATEMENT_BUILD_VERSION`，然后跑 `scripts/rebuild_report_statements.py`。
"""

from __future__ import annotations
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Tuple
from ..core.logging import get_app_logger
from .hk_report_catalog import hkex_sort_key
from .payload_versions import versions_current
from .report_statement_checks import (
    CROSS_CHECK_FIELDS,
    EPS_CENTS_RATIO_RANGE,
    EPS_CHECK_FIELDS,
    IDENTITY_REL_TOL,
    finalize_validation,
    hard_failures,
    header_restated,
)
from .report_statement_prompts import (
    DERIVED_SUM_FIELDS,
    EXPENSE_MAGNITUDE_FIELDS,
    FIELD_KIND,
    PER_SHARE_FIELDS,
    STATEMENT_BUILD_VERSION,
    STATEMENT_PROMPT_VERSION,
    statement_row_current,
)
from .report_statements import (
    STATEMENT_EXTRACTOR_VERSION,
    ParsedStatement,
    period_columns,
    resolve_value,
)

logger = get_app_logger(__name__)


def _decimal_to_number(value: Optional[Decimal]) -> Optional[float]:
    if value is None:
        return None
    return float(value)


# ---------------------------------------------------------------------------- 构建：EPS 单位（#223）

# 每股盈利以「仙」（1/100 元）列示的标记：「基本（港仙）」「每股港仙/人民幣仙」「人民幣分」「cents」
CENTS_RE = re.compile(r"仙|人民幣分|人民币分|\bcents?\b", re.I)
# 映射行之前的邻行只在像每股盈利小标题时才看（「每股盈利（港仙）」下面跟「基本」「攤薄」）；
# 股息行（「擬派末期股息每股 5 港仙」）不是 EPS 的单位
_EPS_CONTEXT_RE = re.compile(
    r"每股(?:盈利|收益|虧損|亏损)|per\s+share|基本|攤薄|摊薄|basic|diluted", re.I
)
_DIVIDEND_RE = re.compile(r"股息|dividend", re.I)
EPS_NEIGHBOR_ROWS = 3
# 隐含股数（归母净利 / 每股盈利）相差在 2 倍内视为同一单位：仙与元差 100 倍，不会混淆
EPS_SHARES_RATIO = 2.0


def per_share_divisor(statement: ParsedStatement, row_ids: Sequence[str]) -> int:
    """损益表里映射到 EPS 的行按「仙」列示时返回 100，否则 1。

    证据：映射行本身的标签/上下文（01023「基本（港仙）」）、紧邻其上的每股盈利小标题行
    （「每股盈利（港仙）」→「基本」「攤薄」），以及表头（「以每股港仙列示」）。"""
    index = {row.row_id: i for i, row in enumerate(statement.rows)}
    texts: List[str] = []
    for row_id in row_ids:
        i = index.get(row_id)
        if i is None:
            continue
        row = statement.rows[i]
        texts.append(row.label)
        texts.extend(row.context)
        for neighbor in statement.rows[max(0, i - EPS_NEIGHBOR_ROWS) : i]:
            if _EPS_CONTEXT_RE.search(neighbor.label) and not _DIVIDEND_RE.search(neighbor.label):
                texts.append(neighbor.label)
    texts.extend(line for line in statement.header if not _DIVIDEND_RE.search(line))
    return 100 if any(CENTS_RE.search(text or "") for text in texts) else 1


# ---------------------------------------------------------------------------- 构建：EPS 附注号守卫（build v2）

# 「每股盈利 13」「母公司普通股股權持有人應佔每股盈利 14」「OF THE COMPANY 10」这类小标题行的
# 唯一数值是**附注号**：抽取器 v9 之前币种包裹的「HK$4.889港元」不认，基本/摊薄行整行丢失，
# 模型只能把小标题映射成 EPS（00148/00799/02313/03900/09926 共 33 份报告，basic_eps = 13.0）。
# 判据全部确定性：映射行只有**一个 token**（多列报表里单 token 行不可能同时是本期与比较期）、
# 是 1-2 位不带小数点的整数（附注号形态）、标签不含基本/摊薄；行名或上下文是每股盈利小标题。
# 其后几行里有基本（摊薄）行则改指向它，否则丢弃该科目交给雅虎补缺——宁缺毋滥
EPS_HEADING_RE = re.compile(
    r"每股(?:盈利|收益|虧損|亏损|（虧損）|（亏损）)|per\s+share|\bEPS\b", re.I
)
_EPS_KIND_RE = {
    "basic_eps": re.compile(r"基本|basic", re.I),
    # 「攤簿」是 03900 2017 中报的原文错字
    "diluted_eps": re.compile(r"攤薄|摊薄|攤簿|diluted", re.I),
}
EPS_REDIRECT_ROWS = 4


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _is_eps_note_row(statement: ParsedStatement, row) -> bool:
    if statement.column_count < 2 or len(row.values) != 1:
        return False
    value = row.values[0]
    if value is None or value.as_tuple().exponent != 0 or not 1 <= value <= 99:
        return False
    if any(pattern.search(_squash(row.label)) for pattern in _EPS_KIND_RE.values()):
        return False
    return bool(EPS_HEADING_RE.search(" ".join([row.label, *row.context])))


def _eps_redirect_target(statement: ParsedStatement, index: int, field: str) -> Optional[str]:
    """小标题之后（同一块内，最多 EPS_REDIRECT_ROWS 行）第一条标签或上下文写明该类别、且至少两个
    数值的行。「基本及攤薄」同时满足两类。上下文里的类别只认独占一行的「基本 Basic」提示，
    所以这里看的是 _EPS_KIND_RE 能否在 标签 或 context 中命中。"""
    pattern = _EPS_KIND_RE[field]
    for candidate in statement.rows[index + 1 : index + 1 + EPS_REDIRECT_ROWS]:
        if sum(1 for v in candidate.values if v is not None) < 2:
            continue
        texts = [_squash(candidate.label), *(_squash(c) for c in candidate.context)]
        if any(pattern.search(text) for text in texts):
            return candidate.row_id
    return None


def repair_eps_note_mapping(
    income: Optional[ParsedStatement], income_mapping: Dict[str, List[str]]
) -> Tuple[Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
    """basic/diluted_eps 映射到附注号小标题 → 改指向其后的基本（摊薄）行或丢弃。
    返回 (修复后的损益表映射, {field: 修复记录})；不需要修复原样返回、记录为空。"""
    if income is None or not income_mapping:
        return income_mapping, {}
    index = {row.row_id: i for i, row in enumerate(income.rows)}
    fixed = dict(income_mapping)
    repairs: Dict[str, Dict[str, Any]] = {}
    for field in ("basic_eps", "diluted_eps"):
        ids = list(income_mapping.get(field) or [])
        if len(ids) != 1 or ids[0] not in index:
            continue
        i = index[ids[0]]
        row = income.rows[i]
        if not _is_eps_note_row(income, row):
            continue
        target = _eps_redirect_target(income, i, field)
        if target:
            fixed[field] = [target]
        else:
            fixed.pop(field, None)
        repairs[field] = {
            "reason": "eps_note_number",
            "from_row": ids[0],
            "to_row": target,
            "note_number": str(row.values[0]),
        }
    return fixed, repairs


# 中国会计准则利润表（财政部报表格式）：「財務費用」是净额（利息费用 − 利息收入 ± 汇兑损益），
# 其下固定列「其中：利息費用 / 利息收入」。国际准则港股的「財務費用/財務成本」本身就是利息开支，
# 其下一行是汇兑、减值等别的科目——只在紧邻下一行是「其中：利息…」时才认定为中国准则净额口径
_NET_FINANCE_COST_RE = re.compile(r"^(財務費用|财务费用)")
_INTEREST_SUB_LINE_RE = re.compile(r"^其中[:：]?利息(費用|费用|支出)")


def repair_int_exp_mapping(
    income: Optional[ParsedStatement], income_mapping: Dict[str, List[str]]
) -> Tuple[Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
    """int_exp 映射到中国准则的净额「財務費用」、且紧接下一行是「其中：利息費用/支出」→ 改指该
    明细行（#264：利息覆盖倍数的分母应是利息费用，净额会被利息收入冲减、被汇兑损益扭曲）。
    必须锚定「其中：」且紧邻：同表营业总成本里还有金融子公司的「△利息支出」「利息支出」。
    2026-09 生产 103 份含「財務費用」行的报告里，其下紧跟「其中：利息…」的全部是中国准则报表。"""
    if income is None or not income_mapping:
        return income_mapping, {}
    ids = list(income_mapping.get("int_exp") or [])
    index = {row.row_id: i for i, row in enumerate(income.rows)}
    if len(ids) != 1 or ids[0] not in index:
        return income_mapping, {}
    i = index[ids[0]]
    if not _NET_FINANCE_COST_RE.match(_squash(income.rows[i].label)) or i + 1 >= len(income.rows):
        return income_mapping, {}
    detail = income.rows[i + 1]
    if not _INTEREST_SUB_LINE_RE.match(_squash(detail.label)):
        return income_mapping, {}
    if not any(value is not None for value in detail.values):
        return income_mapping, {}
    return {**income_mapping, "int_exp": [detail.row_id]}, {
        "int_exp": {"reason": "net_finance_cost", "from_row": ids[0], "to_row": detail.row_id}
    }


def effective_mapping(
    located: Dict[str, ParsedStatement], mapping: Dict[str, Dict[str, List[str]]]
) -> Tuple[Dict[str, Dict[str, List[str]]], Dict[str, Dict[str, Any]]]:
    """存储的 LLM 映射 → 构建时实际使用的映射（确定性修复在这里统一生效：EPS 单位证据与
    会计期行取数看到的是同一份映射）。修复都在损益表上，返回 (映射, {科目: 修复记录})。"""
    income = located.get("income")
    income_mapping, repairs = repair_eps_note_mapping(income, mapping.get("income") or {})
    income_mapping, interest_repairs = repair_int_exp_mapping(income, income_mapping)
    repairs = {**repairs, **interest_repairs}
    if not repairs:
        return mapping, {}
    return {**mapping, "income": income_mapping}, repairs


def _eps_row_ids(mapping: Dict[str, Dict[str, List[str]]]) -> List[str]:
    income = mapping.get("income") or {}
    return list(income.get("basic_eps") or income.get("diluted_eps") or [])


def eps_unit_evidence(
    located: Dict[str, ParsedStatement],
    mapping: Dict[str, Dict[str, List[str]]],
    target: Dict[str, Any],
    *,
    yahoo_eps: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """一份报告的 EPS 单位证据：{period_key, labelled, yahoo_cents, values{会计期: EPS 原文},
    implied_shares}。

    `values` 按会计期（末日|FY/H1）记原文 EPS——相邻报告的本期/比较列是同一个数（2020 年报
    比较列 16.36 = 2019 年报本期 16.36），据此把「仙」沿报告链传播；`implied_shares` = 归母净利
    （放大后）/ 原文 EPS，同一公司各期的量级相同，仙与元差 100 倍。`yahoo_cents`：某个年度列的
    原文 EPS 恰是雅虎同财年 EPS 的约 100 倍（币种一致）——01579 的「以人民幣分列示」是表内纯文本
    行，解析层（v8）没收进上下文，行名只写「基本」，雅虎是唯一的单位证据。"""
    income = located.get("income")
    mapping = effective_mapping(located, mapping)[0]
    row_ids = _eps_row_ids(mapping)
    if income is None or not row_ids:
        return None
    columns = period_columns(income, report_type=target["report_type"], end_date=target["end_date"])
    values: Dict[str, str] = {}
    implied: Optional[float] = None
    yahoo_cents = False
    low, high = EPS_CENTS_RATIO_RANGE
    for col in columns:
        eps = resolve_value(income, row_ids, col.column, scale=False)
        if eps is None or eps == 0:
            continue
        values[f"{col.end_date}|{col.fp}"] = str(eps)
        yahoo = (yahoo_eps or {}).get(col.end_date) if col.fp == "FY" else None
        if (
            yahoo
            and income.currency
            and yahoo.get("currency") == income.currency
            and yahoo.get("basic_eps")
        ):
            yahoo_cents = yahoo_cents or low <= float(eps) / float(yahoo["basic_eps"]) <= high
        if col.is_primary:
            profit = resolve_value(
                income,
                (mapping.get("income") or {}).get("n_income_attr_p") or [],
                col.column,
                scale=True,
            )
            if profit is not None and profit != 0:
                implied = abs(float(profit) / float(eps))
    return {
        "period_key": target["period_key"],
        "labelled": per_share_divisor(income, row_ids) == 100,
        "yahoo_cents": yahoo_cents,
        "values": values,
        "implied_shares": implied,
    }


def _same_decimal(lhs: str, rhs: str) -> bool:
    try:
        return Decimal(lhs) == Decimal(rhs)
    except (InvalidOperation, TypeError):
        return False


def _period_date(period_key: str) -> int:
    try:
        return int(str(period_key)[:8])
    except ValueError:
        return 0


def propagate_eps_units(evidence: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """同一标的各报告的 EPS 单位 → {period_key: {"divisor": 1|100, "basis": ...}}。

    1. 行名/表头写明「仙」→ 100（basis=label）；原文 EPS 是雅虎同财年的约 100 倍 → 100
       （basis=yahoo，01579 这类单位只写在被解析层丢弃的纯文本行里的公司）；
    2. **报告链**：未标注的报告若某会计期的 EPS 原文与已判为仙的报告同一会计期的 EPS 相等（本期
       对比较列，或反之），继承仙（basis=chain）——02669 只有 2020 年起的年报写了「每股港仙」，
       2016-2019 年报靠比较列首尾相接补齐；
    3. **隐含股数**：链接不上的（02669 的中报与年报没有同一会计期）按「归母净利 / EPS」与最近
       一份仙报告的隐含股数比较，在 2 倍内即同一单位（basis=shares）。
    迭代到不再变化；没有任何仙证据的公司全部为 1（不猜）。"""
    items = [e for e in evidence if e and e.get("period_key")]
    units: Dict[str, Dict[str, Any]] = {}
    for e in items:
        if e.get("labelled"):
            units[e["period_key"]] = {"divisor": 100, "basis": "label"}
        elif e.get("yahoo_cents"):
            units[e["period_key"]] = {"divisor": 100, "basis": "yahoo"}
    ordered = sorted(items, key=lambda e: e["period_key"], reverse=True)
    changed = True
    while changed:
        changed = False
        cents = [e for e in ordered if e["period_key"] in units]
        for item in ordered:
            key = item["period_key"]
            if key in units:
                continue
            linked = any(
                period in other["values"] and _same_decimal(value, other["values"][period])
                for other in cents
                for period, value in item["values"].items()
            )
            if linked:
                units[key] = {"divisor": 100, "basis": "chain"}
                changed = True
        if changed:
            continue
        cents = [e for e in ordered if e["period_key"] in units and e.get("implied_shares")]
        for item in ordered:
            key = item["period_key"]
            if key in units or not item.get("implied_shares") or not cents:
                continue
            nearest = min(
                cents,
                key=lambda other: (
                    abs(_period_date(other["period_key"]) - _period_date(key)),
                    other["period_key"],
                ),
            )
            ratio = item["implied_shares"] / nearest["implied_shares"]
            if 1 / EPS_SHARES_RATIO <= ratio <= EPS_SHARES_RATIO:
                units[key] = {"divisor": 100, "basis": "shares"}
                changed = True
    return {
        e["period_key"]: units.get(e["period_key"], {"divisor": 1, "basis": None}) for e in items
    }


# ---------------------------------------------------------------------------- 构建：夹层权益与资产小计

# 美国准则口径的夹层权益（09618 京东：「可轉換可贖回非控制性權益」「夾層權益」）；合计行
# （「負債、夾層權益及權益總額」）不是它
MEZZANINE_RE = re.compile(
    r"夾層|夹层|mezzanine|可贖回非控(?:股|制)|可赎回非控(?:股|制)|redeemable\s+non-?controlling",
    re.I,
)
_TOTAL_LABEL_RE = re.compile(r"總額|总额|總計|总计|合計|合计|總值|总值|\btotal\b", re.I)
# 修复采用门槛：替换后资产负债恒等式须在 0.1% 内闭合（原恒等式容差 1% 不通过才触发）
REPAIR_REL_TOL = 0.001


def mezzanine_row_ids(balance: ParsedStatement) -> List[str]:
    """夹层权益行（按行名确定性识别，不走 LLM）：第一条命中且不是合计行的行。"""
    for row in balance.rows:
        text = " ".join([row.label, *row.context])
        if MEZZANINE_RE.search(text) and not _TOTAL_LABEL_RE.search(row.label):
            return [row.row_id]
    return []


def _rel(lhs: float, rhs: float) -> float:
    denominator = max(abs(lhs), abs(rhs))
    return abs(lhs - rhs) / denominator if denominator else 0.0


def _row_value(row: Dict[str, Any], field: str) -> Optional[float]:
    value = row.get(field)
    return float(value) if isinstance(value, (int, float)) else None


def repair_balance_subtotals(
    row: Dict[str, Any],
    balance: ParsedStatement,
    column: int,
    balance_mapping: Dict[str, List[str]],
) -> Optional[Dict[str, Any]]:
    """资产负债恒等式不成立时，在同一张表的**无标签数值行**里找唯一能让它闭合的资产小计。

    映射把无标签小计行认错是这一类的共同根因：00148 2017 取了不含「分類為待售資產」的流动
    资产小计 r21，正确的是加上待售资产后的 r23；01995 2025H1 把无标签的流动资产小计 r18
    当成了总资产。候选只有两种替换：
    - `total_cur_assets` := 候选值，总资产改为 非流动 + 候选（总资产须是推导值或映射到无标签行）；
    - `total_assets` := 候选值（非流动/流动已知时须与其合计一致）。
    替换后 资产 = 负债 + 夹层权益 + 权益总额 须在 0.1% 内成立；按数值去重后候选**唯一**才采用，
    否则不修、仍判存疑。就地修改 row 并返回修复记录（`repaired_fields` 的条目），不修返回 None。"""
    liab, equity = _row_value(row, "total_liab"), _row_value(row, "total_equity")
    if liab is None or equity is None:
        return None
    target = liab + equity + (_row_value(row, "mezzanine_equity") or 0.0)
    assets = _row_value(row, "total_assets")
    if assets is not None and _rel(assets, target) <= IDENTITY_REL_TOL:
        return None
    by_id = {r.row_id: r for r in balance.rows}
    nca, cur = _row_value(row, "total_nca"), _row_value(row, "total_cur_assets")
    assets_ids = list(balance_mapping.get("total_assets") or [])
    assets_on_labelled_row = any((by_id.get(i) and by_id[i].label.strip()) for i in assets_ids)
    candidates: Dict[Tuple[str, float], str] = {}
    for candidate in balance.rows:
        if candidate.label.strip() or column >= len(candidate.values):
            continue
        raw = candidate.values[column]
        if raw is None:
            continue
        value = float(raw * balance.unit_multiplier)
        if _rel(value, target) <= REPAIR_REL_TOL and (assets is None or value != assets):
            if nca is None or cur is None or _rel(nca + cur, value) <= IDENTITY_REL_TOL:
                candidates.setdefault(("total_assets", value), candidate.row_id)
        if (
            nca is not None
            and not assets_on_labelled_row
            and (cur is None or value != cur)
            and _rel(nca + value, target) <= REPAIR_REL_TOL
        ):
            candidates.setdefault(("total_cur_assets", value), candidate.row_id)
    if len(candidates) != 1:
        return None
    (field, value), to_row = next(iter(candidates.items()))
    repaired: Dict[str, Any] = {}
    derived = row.setdefault("derived_fields", {})
    if field == "total_cur_assets":
        repaired["total_cur_assets"] = {
            "from_row": ",".join(balance_mapping.get("total_cur_assets") or []) or None,
            "to_row": to_row,
            "from_value": cur,
            "to_value": value,
        }
        row["total_cur_assets"] = value
        new_assets = nca + value
        repaired["total_assets"] = {
            "from_row": ",".join(assets_ids) or None,
            "to_row": None,
            "from_value": assets,
            "to_value": new_assets,
            "derived_from": list(DERIVED_SUM_FIELDS["total_assets"]),
        }
        row["total_assets"] = new_assets
        derived["total_assets"] = list(DERIVED_SUM_FIELDS["total_assets"])
    else:
        repaired["total_assets"] = {
            "from_row": ",".join(assets_ids) or None,
            "to_row": to_row,
            "from_value": assets,
            "to_value": value,
        }
        row["total_assets"] = value
        derived.pop("total_assets", None)
    row.setdefault("repaired_fields", {}).update(repaired)
    return repaired


# ---------------------------------------------------------------------------- 构建：所得税符号（#342-1）

# 中国准则利潤表把费用写成正数、以「減：」起头（「減：所得稅費用 1,234」），开支为正；
# 国际准则港股报表把开支写成括号负数、抵免为正（「所得稅（開支）╱抵免 (10,576) 7,112」）
_EXPENSE_POSITIVE_LABEL_RE = re.compile(r"^\s*(?:[減减][:：]|less[:\s])", re.I)
# 同表其他费用科目：用它们的原文符号判断本表的列示惯例（费用写负数还是正数）
_EXPENSE_CONVENTION_FIELDS = ("cost_of_revenue", "sga_exp", "int_exp")
# 「税前利润 ± 所得税 = 税后利润」的相对容差（原文数字相加，只有四舍五入误差）
TAX_IDENTITY_REL_TOL = 0.005
# 合并税后利润行：行名是利润/亏损，且**不是**归属行、税前行、全面收益行（PR #371 复审 P2：合并净利
# 行没抽到时，所得税的下一行可能直接是「本公司權益持有人應佔溢利」，拿归母净利做恒等式会判反）
_PROFIT_WORD_RE = re.compile(r"溢利|利潤|利润|盈利|虧損|亏损|profit|loss|net income", re.I)
_NOT_CONSOLIDATED_PROFIT_RE = re.compile(
    r"應佔|应占|歸屬|归属|歸於|归于|attributable|owners|holders|非控股|非控制|少數|少数|權益持有人"
    r"|权益持有人|股東|股东|除稅前|除税前|稅前|税前|beforetax|每股|pershare",
    re.I,
)
_COMPREHENSIVE_RE = re.compile(r"其他全面|綜合收益|综合收益|全面收益|comprehensive", re.I)
# 「年內溢利及全面收益總額」：没有其他全面收益的公司把两行合成一行，仍是合并税后利润
_PROFIT_AND_COMPREHENSIVE_RE = re.compile(r"(?:溢利|利潤|利润|profit)(?:及|和|and)", re.I)


def is_consolidated_profit_label(label: str) -> bool:
    """行名是否是合并税后利润（「年內溢利」「期內溢利╱（虧損）」「四、淨利潤」「Profit for the year」
    「年內溢利及全面收益總額」）。先去空白（字间空格的「本期利 潤」）。"""
    text = re.sub(r"\s+", "", label or "")
    if not _PROFIT_WORD_RE.search(text) or _NOT_CONSOLIDATED_PROFIT_RE.search(text):
        return False
    return not _COMPREHENSIVE_RE.search(text) or bool(_PROFIT_AND_COMPREHENSIVE_RE.search(text))


def _column_value(parsed: ParsedStatement, row_id: str, column: int) -> Optional[Decimal]:
    for row in parsed.rows:
        if row.row_id == row_id:
            return row.values[column] if column < len(row.values) else None
    return None


def _close(lhs: Decimal, rhs: Decimal) -> bool:
    scale = max(abs(lhs), abs(rhs))
    return abs(lhs - rhs) <= scale * Decimal(str(TAX_IDENTITY_REL_TOL)) if scale else True


def tax_sign_convention(
    income: Optional[ParsedStatement], mapping: Dict[str, List[str]], column: int
) -> int:
    """损益表某一列里所得税原文的符号惯例：+1 = 开支为正（按原样），-1 = 开支为负（取反）。

    依据按可信度依次（#371 评审 P2：不能拿「税前 − 所得税 ≈ 归母净利」裁决——少数股东损益
    不保证小于税额，正负都可能把符号判反）：
    1. **报表自己的合并税后利润**：所得税行紧接着的下一行（「年內溢利」「淨利潤」）等于税前利润
       加上还是减去原文税额——这是原文里的恒等式，与少数股东无关。只有行名确认是合并税后利润、
       且没有被映射成归母净利的行才用（下一行可能直接是归母或非控股损益，PR #371 复审 P2）；
    2. 行名以「減：」起头（中国准则）→ 开支为正；
    3. 同表其他费用科目（成本、销售及行政开支、财务成本）原文多为负 → 开支为负；多为正 → 为正；
    4. 都判不出按国际准则惯例（开支为负）。"""
    tax_ids = mapping.get("income_tax") or []
    if income is None or not tax_ids:
        return -1
    ids = [row.row_id for row in income.rows]
    tax_raw = sum(
        (v for v in (_column_value(income, rid, column) for rid in tax_ids) if v is not None),
        Decimal(0),
    )
    profit_ids = mapping.get("total_profit") or []
    profit = None
    if profit_ids:
        values = [_column_value(income, rid, column) for rid in profit_ids]
        if any(v is not None for v in values):
            profit = sum((v for v in values if v is not None), Decimal(0))
    last_tax = max((ids.index(rid) for rid in tax_ids if rid in ids), default=None)
    if profit is not None and tax_raw and last_tax is not None and last_tax + 1 < len(ids):
        after = income.rows[last_tax + 1]
        net = after.values[column] if column < len(after.values) else None
        mapped_elsewhere = set(profit_ids) | set(mapping.get("n_income_attr_p") or [])
        if (
            net is not None
            and after.row_id not in mapped_elsewhere
            and is_consolidated_profit_label(after.label)
        ):
            added, subtracted = _close(net, profit + tax_raw), _close(net, profit - tax_raw)
            if added and not subtracted:
                return -1
            if subtracted and not added:
                return 1
    labels = {row.row_id: row.label for row in income.rows}
    if any(_EXPENSE_POSITIVE_LABEL_RE.match(labels.get(rid) or "") for rid in tax_ids):
        return 1
    negatives = positives = 0
    for field in _EXPENSE_CONVENTION_FIELDS:
        for rid in mapping.get(field) or []:
            value = _column_value(income, rid, column)
            if value is not None and value < 0:
                negatives += 1
            elif value is not None and value > 0:
                positives += 1
    if positives > negatives:
        return 1
    return -1


def build_period_rows(
    located: Dict[str, ParsedStatement],
    mapping: Dict[str, Dict[str, List[str]]],
    target: Dict[str, Any],
    *,
    fingerprint: str,
    eps_unit: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """映射 + 解析行 → 各会计期的科目行（本期 is_comparative=False，比较期 True）。

    `eps_unit`：`propagate_eps_units` 给这份报告的 EPS 单位；缺省时只看本报告自己的标注。
    构建步骤（`STATEMENT_BUILD_VERSION` 覆盖的全部口径）：EPS 附注号守卫（映射修复）→ 取数 →
    每股指标按单位折元 → 夹层权益按行名取值 → 分项合计推导 → 资产小计修复 → FCF → 硬失败 → 校验。"""
    mapping, income_repairs = effective_mapping(located, mapping)
    if eps_unit is None:
        income = located.get("income")
        row_ids = _eps_row_ids(mapping)
        divisor = per_share_divisor(income, row_ids) if income is not None and row_ids else 1
        eps_unit = {"divisor": divisor, "basis": "label" if divisor != 1 else None}
    eps_divisor = int(eps_unit.get("divisor") or 1)
    rows_by_period: Dict[str, Dict[str, Any]] = {}
    income_column: Dict[str, int] = {}
    balance_column: Dict[str, int] = {}
    for kind, parsed in located.items():
        if not mapping.get(kind):
            continue  # 软必需科目缺失时整张表已被丢弃：不留空来源占位
        columns = period_columns(
            parsed, report_type=target["report_type"], end_date=target["end_date"]
        )
        restated = header_restated(parsed.header)
        for col in columns:
            key = f"{col.end_date}|{col.fp}"
            row = rows_by_period.setdefault(
                key,
                {
                    "end_date": col.end_date,
                    "fp": col.fp,
                    "currency": None,
                    "is_comparative": not col.is_primary,
                    "source_period_key": target["period_key"],
                    "source_report_type": target["report_type"],
                    "source_end_date": target["end_date"],
                    "source_url": target["url"],
                    "source_fingerprint": fingerprint,
                    "source_pages": {},
                    # 每张表的来源报告（比较期按表合并时判新旧）
                    "source_by_kind": {},
                    # 每张表各自识别出的币种/单位：三张表不一致是硬失败，不猜
                    "currency_by_kind": {},
                    "unit_by_kind": {},
                    "extractor_version": STATEMENT_EXTRACTOR_VERSION,
                    "prompt_version": STATEMENT_PROMPT_VERSION,
                    "build_version": STATEMENT_BUILD_VERSION,
                },
            )
            row["currency_by_kind"][kind] = parsed.currency
            row["unit_by_kind"][kind] = parsed.unit_multiplier
            row["source_pages"][kind] = [parsed.page_start, parsed.page_end]
            row["source_by_kind"][kind] = {
                "period_key": target["period_key"],
                "end_date": target["end_date"],
                "report_type": target["report_type"],
                "ann_date": target.get("ann_date"),
            }
            if not col.is_primary and restated:
                # 这份报告的比较列是重列数：作为证据核对更早报告的本期行时按重列处理（v4）
                row.setdefault("restated_by_kind", {})[kind] = True
            for field, row_ids in mapping.get(kind, {}).items():
                value = resolve_value(
                    parsed, row_ids, col.column, scale=field not in PER_SHARE_FIELDS
                )
                if value is None:
                    continue
                if field in EXPENSE_MAGNITUDE_FIELDS:
                    value = abs(value)
                if field in PER_SHARE_FIELDS and eps_divisor != 1:
                    value = value / eps_divisor
                    row["eps_unit"] = {
                        "source_unit": "cents",
                        "divisor": eps_divisor,
                        "basis": eps_unit.get("basis"),
                    }
                row[field] = _decimal_to_number(value)
            if kind == "income" and income_repairs:
                # 映射修复（EPS 附注号守卫、中国准则利息费用）对本表每一列都生效，各会计期行都记一笔
                # （比较期按表合并时随损益表走）
                row.setdefault("repaired_fields", {}).update(
                    {
                        field: {**repair, "to_value": row.get(field)}
                        for field, repair in income_repairs.items()
                    }
                )
            if kind == "income":
                income_column[key] = col.column
            if kind == "balance":
                balance_column[key] = col.column
                mezz = resolve_value(parsed, mezzanine_row_ids(parsed), col.column, scale=True)
                if mezz is not None:
                    row["mezzanine_equity"] = _decimal_to_number(mezz)
    rows: List[Dict[str, Any]] = []
    for key, row in rows_by_period.items():
        known = sorted({c for c in row["currency_by_kind"].values() if c})
        row["currency"] = known[0] if len(known) == 1 else None
        if row.get("income_tax") is not None and key in income_column:
            # 所得税落库为「开支为正、抵免为负」（与雅虎 TaxProvision 同号），按本表本列的原文惯例换号
            row["income_tax"] = row["income_tax"] * tax_sign_convention(
                located.get("income"), mapping.get("income") or {}, income_column[key]
            )
        # 记下**实际**由代码推导的科目及其输入：清洗输入时派生值一并失效（评审 P1）
        row["derived_fields"] = {}
        for derived, addends in DERIVED_SUM_FIELDS.items():
            if row.get(derived) is None and all(row.get(f) is not None for f in addends):
                row[derived] = sum(row[f] for f in addends)
                row["derived_fields"][derived] = list(addends)
        if key in balance_column and "balance" in located:
            repaired = repair_balance_subtotals(
                row, located["balance"], balance_column[key], mapping.get("balance") or {}
            )
            if repaired:
                logger.info(
                    "资产小计修复 %s %s: %s",
                    target.get("period_key"),
                    key,
                    {f: (r.get("from_row"), r.get("to_row")) for f, r in repaired.items()},
                )
        cfo, capex = row.get("n_cashflow_act"), row.get("capex")
        if cfo is not None and capex is not None:
            # capex 按报表符号通常为负；个别报表写正数，按绝对值扣才不会把 FCF 算大
            row["free_cashflow"] = cfo - abs(capex)
            row["derived_fields"]["free_cashflow"] = ["n_cashflow_act", "capex"]
        failures = hard_failures(row)
        if failures:
            if not row["is_comparative"]:
                raise ValueError("报表校验失败: " + "；".join(failures))
            # 比较期行不可信就丢掉：相邻年份的报告会再写一次，垃圾比较行比没有更糟
            logger.warning(
                "丢弃比较期行 %s（%s）: %s", row["end_date"], target.get("period_key"), failures
            )
            continue
        finalize_validation(row)
        rows.append(row)
    return rows


# 行上按表归属的构建元数据：比较期按表合并时随该表的来源一起取舍。`repaired_fields` 的条目
# 按科目所属报表逐条取舍（资产小计修复属资产负债表、EPS 附注号守卫属损益表）
_KIND_META = {"eps_unit": "income"}


def _merge_repaired_fields(merged: Dict[str, Any], incoming: Dict[str, Any], kind: str) -> None:
    repaired = {
        field: item
        for field, item in (merged.get("repaired_fields") or {}).items()
        if FIELD_KIND.get(field) != kind
    }
    repaired.update(
        {
            field: item
            for field, item in (incoming.get("repaired_fields") or {}).items()
            if FIELD_KIND.get(field) == kind
        }
    )
    if repaired:
        merged["repaired_fields"] = repaired
    else:
        merged.pop("repaired_fields", None)


def _source_rank(source: Optional[Dict[str, Any]]) -> tuple:
    """来源报告的新旧：报告期越新越优；同期年报优于中报；再同则公告日晚者（修订版）优。"""
    if not source:
        return ("", 0, "")
    return (
        str(source.get("end_date") or ""),
        1 if source.get("report_type") == "annual" else 0,
        hkex_sort_key(source.get("ann_date") or ""),
    )


def _merge_derived_fields(
    existing: Dict[str, Any],
    incoming: Dict[str, Any],
    incoming_kinds: set,
    filled_from_incoming: set,
) -> Dict[str, List[str]]:
    """合并后「哪些科目是推导出来的」必须与值的来源一致（#343-2）：表由来料胜出时取来料的
    推导记录，否则保留已有的；已有行缺、由来料补上的派生科目也随来料。整行合并曾原样保留旧的
    derived_fields——CFO 被判存疑置空后，由旧 CFO 推导的 FCF 不会随之失效。"""
    merged: Dict[str, List[str]] = {}
    for field, inputs in (existing.get("derived_fields") or {}).items():
        if FIELD_KIND.get(field) not in incoming_kinds and field not in filled_from_incoming:
            merged[field] = list(inputs)
    for field, inputs in (incoming.get("derived_fields") or {}).items():
        if FIELD_KIND.get(field) in incoming_kinds or field in filled_from_incoming:
            merged[field] = list(inputs)
    return merged


def merge_comparative_row(
    existing: Optional[Dict[str, Any]], incoming: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """比较期行的写入结果：None = 不写。

    - 目标期已有**本期权威行**（某份报告自己的报告期）→ 不写；
    - 目标期没有行 → 原样写；
    - 目标期已有比较期行（含旧版本行）→ **按表合并**：每张表取来源报告更新的那份，另一份只
      填该表缺失的科目。目标按报告期倒序处理，2025 年报先为 2024 写下三张表的比较期，随后
      2025 中报的资产负债表比较列也指向 2024——整行替换会把营收/现金流丢掉，而 2024 年报
      缺失/失败时它们永远回不来（PR #201 评审 P1）。"""
    if not existing:
        return incoming
    if not statement_row_current(existing):
        # 旧版本行（无论本期还是比较期）在重算成功前不算有效数据：不能挡住新版本的比较期，
        # 否则版本 bump 后该期在年报重算前一直无数据可读（每轮只重算 max_new 份）
        return incoming
    if not existing.get("is_comparative"):
        return None
    # 币种未知或不一致时**不得逐科目补数**（20 USD 会被贴成 20 CNY，元数据还指向另一份报告）：
    # 只能整行取单一来源——来源更新者胜，否则原样保留（PR #201 评审 P1）
    existing_currency, incoming_currency = existing.get("currency"), incoming.get("currency")
    if not existing_currency or not incoming_currency or existing_currency != incoming_currency:
        existing_rank = max(
            (_source_rank(src) for src in (existing.get("source_by_kind") or {}).values()),
            default=_source_rank(None),
        )
        incoming_rank = max(
            (_source_rank(src) for src in (incoming.get("source_by_kind") or {}).values()),
            default=_source_rank(None),
        )
        return incoming if incoming_rank > existing_rank else None
    merged = dict(existing)
    merged_sources = dict(existing.get("source_by_kind") or {})
    merged_pages = dict(existing.get("source_pages") or {})
    merged_restated = dict(existing.get("restated_by_kind") or {})
    incoming_sources = incoming.get("source_by_kind") or {}
    # 每张表由哪一侧胜出、哪些科目由来料补缺：派生科目记录（derived_fields）随值的来源走
    incoming_kinds: set = set()
    filled_from_incoming: set = set()
    for kind, source in incoming_sources.items():
        newer = _source_rank(source) >= _source_rank(merged_sources.get(kind))
        same_source = (merged_sources.get(kind) or {}).get("period_key") == source.get("period_key")
        if same_source:
            # 同一份报告重抽（修订版或重映射）：该表按来料整体替换——新映射不再映射的科目
            # 不能残留旧值（#343-5）
            for field in list(merged):
                if FIELD_KIND.get(field) == kind and incoming.get(field) is None:
                    merged[field] = None
        for field, value in incoming.items():
            if FIELD_KIND.get(field) != kind or value is None:
                continue
            if newer or merged.get(field) is None:
                if not newer:
                    filled_from_incoming.add(field)
                merged[field] = value
        if newer:
            incoming_kinds.add(kind)
            merged_sources[kind] = source
            merged_pages[kind] = (incoming.get("source_pages") or {}).get(kind)
            # 按表的构建元数据随该表的来源走：重列标记、EPS 单位（损益表）、小计修复（资产负债表）
            if (incoming.get("restated_by_kind") or {}).get(kind):
                merged_restated[kind] = True
            else:
                merged_restated.pop(kind, None)
            for meta, meta_kind in _KIND_META.items():
                if meta_kind != kind:
                    continue
                if incoming.get(meta):
                    merged[meta] = incoming[meta]
                else:
                    merged.pop(meta, None)
            _merge_repaired_fields(merged, incoming, kind)
    merged["source_by_kind"] = merged_sources
    merged["source_pages"] = merged_pages
    merged["derived_fields"] = _merge_derived_fields(
        existing, incoming, incoming_kinds, filled_from_incoming
    )
    if merged_restated:
        merged["restated_by_kind"] = merged_restated
    else:
        merged.pop("restated_by_kind", None)
    merged["currency"] = merged.get("currency") or incoming.get("currency")
    # 行级元数据取最新来源；版本号取当前（合并结果是当前代码写出的）
    newest_kind = max(merged_sources, key=lambda k: _source_rank(merged_sources[k]), default=None)
    if newest_kind:
        top = merged_sources[newest_kind]
        merged["source_period_key"] = top["period_key"]
        merged["source_report_type"] = top["report_type"]
        merged["source_end_date"] = top["end_date"]
    merged["is_comparative"] = True
    merged["extractor_version"] = STATEMENT_EXTRACTOR_VERSION
    merged["prompt_version"] = STATEMENT_PROMPT_VERSION
    merged["build_version"] = STATEMENT_BUILD_VERSION
    # 合并结果的科目来自两份报告，原 validation 已失效
    finalize_validation(merged)
    return merged


def comparative_evidence(comparative: Dict[str, Any]) -> Dict[str, Any]:
    """另一份报告比较列里可重跑的证据切片：来源与币种 + 交叉核对科目。存在主行的
    `comparative_evidence` 上——比较列本身通常被主行覆盖、没有第二条行可回读，规则升版重校验
    时若不保存这份证据，先前由它判出的存疑会被静默清除（PR #207 评审 P2）。"""
    evidence = {
        "source_period_key": comparative.get("source_period_key"),
        "source_report_type": comparative.get("source_report_type"),
        "source_end_date": comparative.get("source_end_date"),
        "currency": comparative.get("currency"),
        # 证据由哪一版代码产出（#343-4）：旧版本的比较列可能正是被修掉的那个错，重放时只认当前版本
        "extractor_version": comparative.get("extractor_version"),
        "prompt_version": comparative.get("prompt_version"),
        "build_version": comparative.get("build_version"),
    }
    # 每股盈利也存下（v5）：雅虎 EPS 大差异要靠更晚报告的比较列判断是口径不同还是映射错误
    for field in (*CROSS_CHECK_FIELDS, *EPS_CHECK_FIELDS):
        if comparative.get(field) is not None:
            evidence[field] = comparative[field]
    restated = {
        kind: True for kind, flag in (comparative.get("restated_by_kind") or {}).items() if flag
    }
    if restated:
        # 该报告表头标注了重列：v4 据此把差异判为 info（revalidate 重放时同样可用）
        evidence["restated_by_kind"] = restated
    return evidence


def evidence_current(evidence: Optional[Dict[str, Any]]) -> bool:
    """比较列证据是否由当前版本的抽取器/prompt/构建逻辑产出（缺版本字段 = 旧证据）。"""
    return bool(evidence) and versions_current(
        evidence,
        extractor_version=STATEMENT_EXTRACTOR_VERSION,
        prompt_version=STATEMENT_PROMPT_VERSION,
        build_version=STATEMENT_BUILD_VERSION,
    )


def _evidence_rank(evidence: Optional[Dict[str, Any]]) -> tuple:
    if not evidence:
        return _source_rank(None)
    return _source_rank(
        {
            "end_date": evidence.get("source_end_date"),
            "report_type": evidence.get("source_report_type"),
        }
    )


def attach_comparative_evidence(row: Dict[str, Any], comparative: Optional[Dict[str, Any]]) -> None:
    """把比较列证据挂到主行上；已有证据时只被来源更新（或同源）的替换。"""
    if row.get("comparative_evidence") and not evidence_current(row["comparative_evidence"]):
        row.pop("comparative_evidence")
    if not comparative or not statement_row_current(comparative):
        return
    incoming = comparative_evidence(comparative)
    if _evidence_rank(incoming) >= _evidence_rank(row.get("comparative_evidence")):
        row["comparative_evidence"] = incoming
