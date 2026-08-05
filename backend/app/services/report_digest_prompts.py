"""财报章节摘要（digest）的 prompt 组装与输出校验。

digest 是 map-reduce 的 map 层：每份报告只摘要一次、永久缓存。schema 按
owner 深度诉求定制——不止财务变化，还要业务分部占比、上下游产业链、
成本构成、一次性项目与会计信号（利润质量线索）。

**分档**（`DIGEST_TIERS`）：最新年报与中报（A 档）值得最大输入预算与全字段；
第 2-5 期（B 档）维持现状；第 6-10 期（C 档）只摘要 mdna 的核心字段——因为
`serialize_digest_for_analysis` 本来就把五年以上的摘要压成那四个字段，旧年份
的另外五个字段是**生成了就丢**。
"""

import json
from typing import Any, Dict, List, Tuple

# digest prompt / 分档逻辑的版本号。bump 会使所有 report_digest 缓存失效并重跑，
# 但**不**触发重新下载与抽取（那是 SECTION_EXTRACTOR_VERSION 的职责）。
DIGEST_PROMPT_VERSION = 2

DIGEST_FIELDS = (
    "经营回顾", "业务分部占比", "上下游与产业链", "主营收入结构",
    "成本与费用", "一次性项目", "会计信号", "风险要点", "展望",
)

# C 档字段：与 serialize_digest_for_analysis 压缩后保留的集合严格一致
COMPACT_DIGEST_FIELDS = ("主营收入结构", "一次性项目", "会计信号")

DIGEST_TIERS: Dict[str, Dict[str, Any]] = {
    "A": {
        "budget": 40_000,
        "sections": ("business", "mdna", "risk_factors"),
        "fields": DIGEST_FIELDS,
    },
    "B": {
        "budget": 30_000,
        "sections": ("business", "mdna", "risk_factors"),
        "fields": DIGEST_FIELDS,
    },
    "C": {
        "budget": 12_000,
        "sections": ("mdna",),
        "fields": COMPACT_DIGEST_FIELDS,
    },
}
DEFAULT_TIER = "B"


def tier_spec(tier: str) -> Dict[str, Any]:
    return DIGEST_TIERS.get(tier, DIGEST_TIERS[DEFAULT_TIER])


def assign_digest_tiers(targets: List[Dict[str, Any]]) -> Dict[str, str]:
    """按报告期新旧给每份报告分档，返回 {period_key: tier}。

    最新年报与中报进 A 档；其后四期年报 B 档；更早的进 C 档。targets 需已按
    end_date 倒序（`plan_report_targets_detailed` 的约定），这里不重排以免与
    调用方的处理顺序脱节。
    """
    tiers: Dict[str, str] = {}
    annual_seen = 0
    for target in targets:
        if target.get("report_type") == "semi":
            tiers[target["period_key"]] = "A"  # 最新中报：最贴近当下的经营信息
            continue
        tiers[target["period_key"]] = (
            "A" if annual_seen == 0 else "B" if annual_seen <= 4 else "C"
        )
        annual_seen += 1
    return tiers

DIGEST_SYSTEM_PROMPT = """你是财报章节摘要助手。只依据用户提供的报告原文节选做摘要，禁止引入任何对该公司的先验知识；原文未提及的内容一律写"原文未提及"，不得推测补全。

原文可能被截断或混入目录/页眉噪声，只总结可确认的内容。

输出严格 JSON（无 markdown 围栏），键与要求：
{"经营回顾": "本期经营核心情况",
 "业务分部占比": "分产品/分行业收入与毛利占比及同比变化，引用主营构成表数字",
 "上下游与产业链": "上游主要原材料/采购依赖与供应商集中度（如前五供应商占比）；下游客户结构/需求驱动与客户集中度（如前五客户占比）",
 "主营收入结构": "收入构成变化及其驱动因素",
 "成本与费用": "成本构成、费用率变化、异常或大额支出",
 "一次性项目": "非经常性损益、资产处置、减值、政府补助等一次性影响",
 "会计信号": "会计政策/会计估计变更、收入确认方式变化、大额关联交易、利润质量线索；无则写'原文未见明显信号'",
 "风险要点": "报告披露的主要风险",
 "展望": "管理层对未来的展望与计划",
 "关键数字": ["3-8 条关键数字，每条须带原文数值与所属期间"]}

每个文本字段 ≤400 字。"""

COMPACT_DIGEST_SYSTEM_PROMPT = """你是财报章节摘要助手。只依据用户提供的报告原文节选做摘要，禁止引入任何对该公司的先验知识；原文未提及的内容一律写"原文未提及"，不得推测补全。

原文可能被截断或混入目录/页眉噪声，只总结可确认的内容。

这是**较早年份**的报告，只需产出用于长期趋势对比的核心字段。输出严格 JSON（无 markdown 围栏）：
{"主营收入结构": "收入构成变化及其驱动因素",
 "一次性项目": "非经常性损益、资产处置、减值、政府补助等一次性影响",
 "会计信号": "会计政策/会计估计变更、收入确认方式变化、大额关联交易、利润质量线索；无则写'原文未见明显信号'",
 "关键数字": ["3-8 条关键数字，每条须带原文数值与所属期间"]}

每个文本字段 ≤400 字。"""

_SECTION_LABELS: Tuple[Tuple[str, str], ...] = (
    ("business", "公司业务概要"),
    ("company_profile", "公司简介与主要财务指标（注意：这不是业务概要，只含登记信息与指标表）"),
    ("mdna", "管理层讨论与分析"),
    ("risk_factors", "风险因素"),
)


def build_digest_messages(
    symbol: str, market: str, report_type: str, end_date: str,
    sections: Dict[str, str], *, tier: str = DEFAULT_TIER,
) -> List[Dict[str, str]]:
    type_label = {
        "annual": "年度报告", "semi": "半年度报告", "10-K": "10-K 年度报告",
        "20-F": "20-F 年度报告（外国私人发行人）",
    }.get(report_type, report_type)
    parts = []
    for name, label in _SECTION_LABELS:
        body = sections.get(name)
        if body:
            parts.append(f"【{label}】\n{body}")
    user_content = (
        f"报告期 {end_date} {type_label}（{market} {symbol}）章节原文节选如下，"
        "请按 system 约定输出 JSON 摘要：\n\n" + "\n\n".join(parts)
    )
    return [
        {
            "role": "system",
            "content": (
                COMPACT_DIGEST_SYSTEM_PROMPT if tier == "C" else DIGEST_SYSTEM_PROMPT
            ),
        },
        {"role": "user", "content": user_content},
    ]


def parse_digest_output(content: str, *, tier: str = DEFAULT_TIER) -> Dict[str, Any]:
    """校验 digest JSON；非法输出抛 ValueError（确定性失败，不烧重试）。

    必填字段按档放宽：C 档只要求核心三字段 + 关键数字。若仍按全字段校验，C 档
    每一份都会判确定性失败并计 attempts，两次之后永久跳过——分档反而制造缺口。
    """
    try:
        data = json.loads(content)
    except ValueError as exc:
        raise ValueError(f"digest 输出不是合法 JSON: {content[:200]}") from exc
    if not isinstance(data, dict):
        raise ValueError("digest 输出必须是 JSON 对象")

    digest: Dict[str, Any] = {}
    for field in tier_spec(tier)["fields"]:
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"digest 缺少字段或为空: {field}")
        digest[field] = value.strip()[:600]

    key_numbers = data.get("关键数字")
    if not isinstance(key_numbers, list) or not all(
        isinstance(item, str) for item in key_numbers
    ):
        raise ValueError("关键数字必须是字符串数组")
    digest["关键数字"] = [item.strip() for item in key_numbers[:8] if item.strip()]
    return digest
