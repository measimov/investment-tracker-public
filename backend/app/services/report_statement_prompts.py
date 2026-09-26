"""三张报表科目映射的 prompt 与输出契约。

LLM **只做映射，不碰数字**：输入是 `report_statements` 解析出的结构化行（id / 标签 /
附注 / 上下文 / 所选列的原文数值），输出是每个目标科目对应的**行 id 数组**（多行=求和）
或 null。数值由代码按 id 从原文行取出并按单位放大——模型没有任何机会编造或改写数字，
解析层再把不存在的 id 剔除并记 unresolved。

改 prompt / 字段定义 / 解析约束必须 bump `STATEMENT_PROMPT_VERSION`（缓存按它判新）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .report_statements import STATEMENT_EXTRACTOR_VERSION

STATEMENT_PROMPT_VERSION = 4

# 目标科目：与 report_fetchers.YAHOO_HK_FIELD_MAP / earnings_quality.pivot_rows_to_statements
# 对齐（同名 = 同口径），下游利润质量/格雷厄姆/分析输入零改动即可消费
STATEMENT_FIELDS: Dict[str, Dict[str, str]] = {
    "income": {
        "total_revenue": "收入/营业收入总额（合并口径的合计行，不是分部行）",
        "cost_of_revenue": "销售成本/收入成本/营业成本（按报表符号，通常为负或括号）",
        "gross_profit": "毛利",
        "operating_income": "经营盈利/经营溢利/营业利润（美国准则口径为 经营利润/income from operations）",
        "n_income_attr_p": "本公司权益持有人应占盈利（归属母公司股东净利润）",
        "total_profit": "除税前盈利/税前利润/利润总额",
        "income_tax": "所得税开支（按报表符号）",
        "ebitda": "EBITDA——仅当报表直接列出该行才映射，否则 null",
        "sga_exp": "销售及市场推广开支 + 一般及行政开支（多行求和；美国准则口径的 销售及市场费用+一般及行政费用）",
        "int_exp": "财务成本/利息开支/利息费用",
        "basic_eps": "每股基本盈利（每股金额，不按单位放大）",
        "diluted_eps": "每股摊薄盈利（每股金额，不按单位放大）",
    },
    "balance": {
        "total_assets": "资产总额/总资产/资产总值（港股净资产格式的报表常常没有这一行，"
        "此时留 null，由系统用 total_nca + total_cur_assets 推导，不要拿「總資產減流動負債」冒充）",
        "total_nca": "非流动资产总额/非流动资产总值/非流动资产合计",
        "total_cur_assets": "流动资产总额/流动资产总值/流动资产合计",
        "total_cur_liab": "流动负债总额/流动负债总值/流动负债合计",
        "total_ncl": "非流动负债总额/非流动负债总值/非流动负债合计",
        "accounts_receiv": "应收账款/贸易应收款项（若与其他应收合并列示则取合并行）",
        "inventories": "存货",
        "fix_assets": "物业、设备及器材/物业、厂房及设备/固定资产（不含使用权资产）",
        "money_cap": "现金及现金等价物（不含受限制现金/定期存款）",
        "total_liab": "负债总额/总负债（没有合计行则留 null，由系统用 total_cur_liab + total_ncl 推导）",
        "total_hldr_eqy_exc_min_int": "本公司权益持有人应占权益/归属母公司股东权益（不含非控制性权益）",
        "total_equity": "权益总额/权益合计/总权益（含非控制性权益；权益部分的合计行。没有则留 null，"
        "由系统用 total_hldr_eqy_exc_min_int + minority_int 推导）",
        "minority_int": "非控股权益/非控制性权益/少数股东权益——**权益部分**的那一行（可能为负）；"
        "不是负债内的少数股东款项",
        "total_debt": "借款合计：短期借款+长期借款+应付票据/债券（流动与非流动都算，多行求和；不含租赁负债与经营性应付）",
    },
    "cashflow": {
        "n_cashflow_act": "经营活动所得/（所用）现金流量净额",
        "capex": "购买物业、设备及器材（含在建工程/投资物业）的付款（按报表符号，通常为负）",
        "depr_fa_coga_dpba": "折旧及摊销（若现金流量表以间接法列出；多行求和；没有则 null）",
    },
}
# 每股指标不按单位放大
PER_SHARE_FIELDS = frozenset({"basic_eps", "diluted_eps"})
# 费用类科目报表里多为括号负数，落库统一取**正的绝对值**——与 Yahoo（cost_of_revenue 为正）
# 和 A 股 Tushare（sell_exp/admin_exp 为正）同口径，否则毛利率 = (收入−成本)/收入 会算成
# 超过 100%、Beneish 的 SGAI 因子符号反转。capex 保持报表符号（Yahoo 亦为负）。
EXPENSE_MAGNITUDE_FIELDS = frozenset(
    {"cost_of_revenue", "sga_exp", "int_exp", "income_tax", "depr_fa_coga_dpba"}
)
# 至少要解析出的科目：缺了说明定位到了别的表或映射失败，整份判确定性失败。每张表列出
# 若干「备选组」，任一组齐全即可——港股净资产格式的財務狀況表没有「資產總值」行（09926/
# 03900/06049 年报全是「非流動資產總值 / 流動資產總值 / 總資產減流動負債」），总资产由两个
# 分项合计推导（见 DERIVED_SUM_FIELDS）
REQUIRED_FIELDS: Dict[str, Tuple[Tuple[str, ...], ...]] = {
    "income": (("total_revenue",),),
    "balance": (("total_assets",), ("total_nca", "total_cur_assets")),
    "cashflow": (("n_cashflow_act",),),
}
# 未有收入的公司（18A 生物科技，09926 2020 中报）：損益表第一行就是「其他收入及收益淨額」，
# 根本没有收入行。收入缺失时，若表里**没有任何收入行**且映射出了净利/税前利润，接受并记
# unresolved——判据看行标签，不看模型说了什么（有收入行却没映射仍是确定性失败）
REVENUE_LABEL_RE = re.compile(
    r"^(?!其他|other)(?:[\d\s、.．()（）一二三四五六七八九十]*)?"
    r"(?:營業總收入|营业总收入|營業收入|营业收入|收入|收益|營業額|营业额|revenue|turnover)",
    re.I,
)
PRE_REVENUE_FALLBACK_FIELDS = ("n_income_attr_p", "total_profit")
# 软必需：缺了不整份判失败，而是**丢掉这张表**并记 unresolved——01133 式的现金流量表块
# 越界到乱码表时，损益/資產負債表本身是好的；没有经营现金流的 capex 单独也没用
SOFT_REQUIRED_KINDS = frozenset({"cashflow"})
# 报表没有直接列出时按分项求和推导的合计科目：目标 → 加数（缺任一加数则不推导）
DERIVED_SUM_FIELDS: Dict[str, Tuple[str, ...]] = {
    "total_assets": ("total_nca", "total_cur_assets"),
    "total_liab": ("total_cur_liab", "total_ncl"),
    "total_equity": ("total_hldr_eqy_exc_min_int", "minority_int"),
}
# 科目 → 所属报表（比较期按表合并、覆盖粒度都以此为准）；free_cashflow 由现金流量表推导
FIELD_KIND: Dict[str, str] = {
    field: kind for kind, fields in STATEMENT_FIELDS.items() for field in fields
}
FIELD_KIND["free_cashflow"] = "cashflow"


def statement_row_current(payload: Dict[str, Any]) -> bool:
    """report_statements 行是否由当前版本的抽取器与 prompt 生成（缺字段 = 版本 1 的历史行）。

    **所有读取路径都必须过它**（档案加载 / 格雷厄姆 / 分析输入 / 进度 / Yahoo 合并）：写入
    侧按双版本触发重算，但每轮受 max_new 限制只重算少量报告，未重算或重算失败的旧金额若
    仍被当成官方优先数据展示、送给分析并屏蔽 Yahoo，修复就被自己的缓存遮住（与摘要管线
    `digest_versions_current` 同一约定）。"""
    return (
        int(payload.get("extractor_version") or 1) == STATEMENT_EXTRACTOR_VERSION
        and int(payload.get("prompt_version") or 1) == STATEMENT_PROMPT_VERSION
    )

_SYSTEM_PROMPT = """你是财务报表科目映射器。用户给出一家上市公司一份年报/中报里三张合并报表的\
结构化行（每行有 id、原文标签、附注号、上下文与所选会计期的原文数值），以及一组目标科目及其\
定义。你的任务只有一件：为每个目标科目指出对应的**行 id**。

铁律：
1. 只输出行 id，绝不输出数字。数值由系统按 id 从原文取。
2. 一个科目对应多行时输出 id 数组（系统按同一列求和），例如销售及行政开支两行、长短期借款多行。
3. 报表里没有该科目，或没有把握，输出 null。宁缺毋滥：错误映射比缺失更糟。
4. 只能使用输入里出现过的 id；不得引入任何公司先验知识。
5. 合计科目优先取合并合计行（如「收入」的合计行而不是各分部行；无标签的合计行以上下文/附注判断）。
6. 符号按报表原样，不要为了正负去挑别的行。
6a. 財務狀況表若没有「資產總值/總資產」合计行（港股净资产格式），total_assets 留 null，\
改为映射 total_nca（非流動資產總值）与 total_cur_assets（流動資產總值），系统会相加；\
「總資產減流動負債」「流動資產淨值」都不是总资产。
6b. total_equity 取**权益部分**的合计行（權益總額/總權益/權益合計，含非控股權益），\
minority_int 取权益部分的「非控股權益/非控制性權益/少數股東權益」行；负债内的少数股东款项\
不算。没有权益总额行时 total_equity 留 null，系统用归母权益 + 非控股权益推导。
7. 输出严格 JSON：{"income": {科目: [id...] | null, ...}, "balance": {...}, "cashflow": {...}}，\
只包含输入中存在的报表，不要任何解释文字。"""


def build_statement_messages(
    *,
    symbol: str,
    market: str,
    report_type: str,
    end_date: str,
    statements: Dict[str, Dict[str, Any]],
) -> List[Dict[str, str]]:
    """statements: {kind: {"unit_multiplier", "currency", "columns": [说明...], "rows": [...]}}"""
    payload = {
        "security": {"symbol": symbol, "market": market},
        "report": {"type": report_type, "period_end": end_date},
        "targets": {kind: STATEMENT_FIELDS[kind] for kind in statements},
        "statements": statements,
        "output_schema": {
            kind: {field: "[row_id, ...] | null" for field in STATEMENT_FIELDS[kind]}
            for kind in statements
        },
    }
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "以下是报表结构化行与目标科目，请输出映射 JSON：\n```json\n"
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
                + "\n```"
            ),
        },
    ]


def parse_statement_mapping(
    content: str,
    statements: Dict[str, Sequence[str]],
    labels: Optional[Dict[str, Sequence[str]]] = None,
) -> Tuple[Dict[str, Dict[str, List[str]]], List[str]]:
    """校验映射 JSON → ({kind: {field: [row_id...]}}, unresolved)。

    statements: {kind: 可用行 id 列表}。不存在的 id 剔除并记入 unresolved（"kind.field:id"）；
    必需科目缺失抛 ValueError（确定性失败，不烧重试额度）。
    """
    try:
        data = json.loads(content)
    except ValueError as exc:
        raise ValueError(f"科目映射输出不是合法 JSON: {content[:200]}") from exc
    if not isinstance(data, dict):
        raise ValueError("科目映射输出必须是 JSON 对象")

    mapping: Dict[str, Dict[str, List[str]]] = {}
    unresolved: List[str] = []
    for kind, row_ids in statements.items():
        valid = set(row_ids)
        section = data.get(kind)
        if section is None:
            mapping[kind] = {}
            continue
        if not isinstance(section, dict):
            raise ValueError(f"{kind} 映射必须是对象")
        resolved: Dict[str, List[str]] = {}
        for field in STATEMENT_FIELDS[kind]:
            value = section.get(field)
            if value is None:
                continue
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                unresolved.append(f"{kind}.{field}:type")
                continue
            ids = [item.strip() for item in value if item.strip()]
            good = [item for item in ids if item in valid]
            unresolved.extend(f"{kind}.{field}:{item}" for item in ids if item not in valid)
            if good:
                resolved[field] = good
        mapping[kind] = resolved

    for kind, groups in REQUIRED_FIELDS.items():
        if kind not in statements:
            continue
        resolved_fields = mapping.get(kind, {})
        if not any(all(field in resolved_fields for field in group) for group in groups):
            if (
                kind == "income"
                and labels is not None
                and not any(REVENUE_LABEL_RE.match((label or "").strip()) for label in labels.get(kind, ()))
                and any(field in resolved_fields for field in PRE_REVENUE_FALLBACK_FIELDS)
            ):
                unresolved.append("income.total_revenue:no_revenue_line")
                continue
            wanted = " 或 ".join("+".join(group) for group in groups)
            if kind in SOFT_REQUIRED_KINDS:
                mapping[kind] = {}
                unresolved.append(f"{kind}.{wanted}:required")
                continue
            raise ValueError(f"科目映射缺少必需科目 {kind}.{wanted}")
    return mapping, unresolved
