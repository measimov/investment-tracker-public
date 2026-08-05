"""LLM 标的分析的 prompt 组装（DeepSeek JSON mode，一次产出标签+全文）。

guardrail 与复盘报告同一哲学并更严格：分析对象是具体上市公司，模型对
公司的先验知识（新闻、事件、口碑）一概不得引入——"合规污点"判断只能
来自输入中的客观风险信号（审计意见、股权质押、股东增减持、限售解禁）。
"""

import json
from typing import Any, Dict, List

ANALYSIS_DISCLAIMER = "本分析由 AI 基于公开结构化数据自动生成，仅供参考，不构成投资建议。"

ALLOWED_TAGS = [
    "高股息", "分红连续", "分红中断", "业绩增长", "业绩下滑", "业绩预警",
    "高质押", "大股东减持", "大股东增持", "解禁临近", "审计非标", "估值偏高",
    "估值偏低", "数据不足",
    # 利润质量层（触发语义见 earnings_quality.metric_semantics 红旗阈值）
    "利润质量存疑", "现金流背离", "依赖非经常损益",
]

# 市场差异段：风险信号数据源与禁用标签（解析层白名单保持单一全集，
# prompt 层按市场约束不适用的标签）
_MARKET_RISK_SOURCES = {
    "A股": (
        "风险判断只能来自输入中的客观信号：审计意见（fina_audit）、"
        "股权质押（pledge_stat）、股东增减持（stk_holdertrade）、解禁事件"
        "（events 中 SHARE_UNLOCK）。"
    ),
    "美股": (
        "美股无审计意见/质押/增减持数据源——风险判断只能来自 report_digests "
        "中年报（本土发行人 10-K / 外国私人发行人 **20-F**，中概股几乎全是"
        "后者）风险因素摘要与 earnings_quality 指标；以下标签**禁止使用**："
        "高质押、大股东减持、大股东增持、解禁临近、审计非标。"
    ),
    "港股": (
        "港股无审计意见/质押/增减持数据源——风险判断只能来自 report_digests "
        "与 earnings_quality 指标；**港股年报未必设有「主要風險」章节**"
        "（实测多数没有），摘要里没有风险内容时如实写'年报未披露专门风险章节'，"
        "不得推测。以下标签**禁止使用**：高质押、大股东减持、大股东增持、"
        "解禁临近、审计非标。结构化年度科目仅覆盖近 3-5 年（数据源限制），"
        "长期趋势判断须据此收敛口径；risk_level 不得为 low，数据严重不足时 "
        "tags 应含'数据不足'。"
    ),
}


# 风险信号盘点章节的分市场描述
_MARKET_RISK_SECTION = {
    "A股": "## 风险信号盘点（审计意见/质押/增减持/解禁，逐项说明有无）",
    "美股": (
        "## 风险信号盘点（基于年报 10-K/20-F 风险因素摘要与利润质量指标；"
        "明示本市场无审计意见/质押/增减持数据源）"
    ),
    "港股": (
        "## 风险信号盘点（基于年报摘要与利润质量指标；明示本市场无审计意见/"
        "质押/增减持数据源、结构化科目仅近 3-5 年，且年报若未设风险章节须写明）"
    ),
}


def build_system_prompt(market: str) -> str:
    """按市场组装 system prompt：共享守则骨架 + 市场差异段。"""
    risk_sources = _MARKET_RISK_SOURCES.get(market, _MARKET_RISK_SOURCES["A股"])
    risk_section = _MARKET_RISK_SECTION.get(market, _MARKET_RISK_SECTION["A股"])
    return f"""你是一个家庭投资组合的标的档案分析助手，只基于用户提供的公开结构化数据分析一只{market}标的。

你必须遵守三条守则：
1. **只用输入数据**：不得引入你对该公司的任何先验知识（新闻、事件、行业口碑、
   管理层背景等）。{risk_sources}输入中没有的信息一律视为未知，不得推测。
2. **数据不足要明说**：某数据集为空时，对应维度写"数据不足"，不得脑补；
   整体数据严重不足时 tags 含"数据不足"、risk_level 不得低于 medium。
   **`profile_data_gaps` 列出的数据集本次未取到**（数据源频率限制或同步失败），
   相关维度必须写"本次数据未取到"并说明原因——绝不可当作"该项无异常"：
   没取到质押数据不等于没有质押，没取到审计意见不等于审计意见正常。
3. **输出严格 JSON**（无 markdown 代码围栏），形如：
   {{"tags": [...], "risk_level": "low|medium|high", "summary": "...", "report_markdown": "..."}}
   - tags：从候选集中选 1-4 个：{json.dumps(ALLOWED_TAGS, ensure_ascii=False)}
   - risk_level：low（无明显风险信号）/ medium（存在需关注信号或数据不足）/
     high（审计非标、质押比例高企、密集减持、业绩预警等任一硬信号）
   - summary：一句话（≤80 字）概括财务质量与主要风险
   - report_markdown：Markdown 全文，包含且仅包含以下章节：
     ## 商业模式与产业链（基于 business_profile：分部占比 → 上游成本因子 →
        下游需求因子的传导链评述 + 估值观察因子清单；可提及 peers 中的可比公司
        名单，但**禁止对同业本身展开任何分析**——同业数据不在输入中；
        business_profile 为空则写"暂无商业画像"）
     ## 财务质量趋势（营收/利润/ROE 趋势 + 报表核心科目：经营现金流与净利润
        的匹配度、资产负债结构变化，引用报告期数字）
     ## 财报要点（基于 report_digests 的跨年综述：主营收入与业务结构的多年变化、
        成本与费用趋势、一次性项目、会计信号；注明为公司报告自述口径；
        report_digest_gaps 存在时须如实注明哪些年份摘要缺失；无摘要则写"暂无财报摘要"）
     ## 利润质量与会计风险（结合 earnings_quality 预计算指标与 digest 会计信号
        逐项评述：CFO/净利润、应计率、应收/存货增速差、扣非占比、Beneish M-score
        ——指标触红旗阈值时明确指出并解释；数据不足的指标如实注明；
        对应标签：利润质量存疑/现金流背离/依赖非经常损益）
     ## 历史股东回报（分红连续性、股息率线索）
     {risk_section}
     ## 未来事件提醒（events 中的未来事件，无则明说）
     ## 待关注问题（2-4 个具体问题，只提问题与权衡，不给指令性买卖建议）
     结尾附一行：{ANALYSIS_DISCLAIMER}"""


def build_analysis_messages(input_payload: Dict[str, Any]) -> List[Dict[str, str]]:
    serialized = json.dumps(
        input_payload, ensure_ascii=False, separators=(",", ":"), default=str
    )
    market = str((input_payload.get("meta") or {}).get("market") or "A股")
    user_content = (
        "请基于下方 JSON 数据生成该标的的档案分析（严格按 system 约定输出 JSON）：\n\n"
        f"```json\n{serialized}\n```"
    )
    return [
        {"role": "system", "content": build_system_prompt(market)},
        {"role": "user", "content": user_content},
    ]


# 市场级硬约束（解析层强制执行——prompt 只是请求，JSON mode 不保证遵守）：
# 没有对应数据源的市场，模型给出语法合法的标签也必须确定性拒绝，否则会被
# 持久化并展示在持仓页标签列上。
_A_SHARE_ONLY_TAGS = frozenset(
    {"高质押", "大股东减持", "大股东增持", "解禁临近", "审计非标"}
)
MARKET_BANNED_TAGS: Dict[str, frozenset] = {
    "美股": _A_SHARE_ONLY_TAGS,  # 无审计意见/质押/增减持/解禁数据源
    "港股": _A_SHARE_ONLY_TAGS,  # 同上
}

# 风险等级下限：数据边界决定"无明显风险信号"这个判断本身不成立的市场。
# 港股已有披露易年报全文摘要，但结构化科目仍只覆盖近 3-5 年（Yahoo 限制），
# 更长周期的趋势无从验证，low 仍属无依据的乐观——下限保留。
MARKET_MIN_RISK_LEVEL: Dict[str, str] = {"港股": "medium"}
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


def parse_analysis_output(content: str, market: str | None = None) -> Dict[str, Any]:
    """解析 JSON mode 输出并做结构校验；非法输出抛 ValueError（确定性失败）。

    market 非空时额外执行该市场的硬约束（禁用标签、风险等级下限）。
    """
    try:
        data = json.loads(content)
    except ValueError as exc:
        raise ValueError(f"LLM 输出不是合法 JSON: {content[:200]}") from exc
    if not isinstance(data, dict):
        raise ValueError("LLM 输出必须是 JSON 对象")

    tags = data.get("tags")
    risk_level = data.get("risk_level")
    summary = data.get("summary")
    report = data.get("report_markdown")
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ValueError("tags 必须是字符串数组")
    # 白名单契约在解析层强制执行：JSON mode 只保证语法不保证遵守 prompt，
    # 模型自造标签会污染持仓页的结构化标签列
    if not 1 <= len(tags) <= 4:
        raise ValueError(f"tags 数量必须为 1-4 个，收到 {len(tags)} 个")
    unknown_tags = [tag for tag in tags if tag not in ALLOWED_TAGS]
    if unknown_tags:
        raise ValueError(f"tags 含白名单外标签: {unknown_tags}")
    banned = MARKET_BANNED_TAGS.get(str(market or ""), frozenset())
    used_banned = [tag for tag in tags if tag in banned]
    if used_banned:
        raise ValueError(f"{market} 无对应数据源，禁用标签: {used_banned}")
    if risk_level not in ("low", "medium", "high"):
        raise ValueError(f"risk_level 非法: {risk_level!r}")
    floor = MARKET_MIN_RISK_LEVEL.get(str(market or ""))
    if floor and _RISK_ORDER[risk_level] < _RISK_ORDER[floor]:
        raise ValueError(
            f"{market} 数据边界有限，risk_level 不得低于 {floor}，收到 {risk_level}"
        )
    if "数据不足" in tags and risk_level == "low":
        raise ValueError('标注"数据不足"时 risk_level 不得为 low')
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary 缺失")
    if not isinstance(report, str) or not report.strip():
        raise ValueError("report_markdown 缺失")
    return {
        "tags": tags,
        "risk_level": risk_level,
        "summary": summary.strip()[:300],
        "report_markdown": report.strip(),
    }
