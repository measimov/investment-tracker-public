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
]

ANALYSIS_SYSTEM_PROMPT = f"""你是一个家庭投资组合的标的档案分析助手，只基于用户提供的公开结构化数据分析一只 A 股标的。

你必须遵守三条守则：
1. **只用输入数据**：不得引入你对该公司的任何先验知识（新闻、事件、行业口碑、
   管理层背景等）。风险判断只能来自输入中的客观信号：审计意见（fina_audit）、
   股权质押（pledge_stat）、股东增减持（stk_holdertrade）、解禁事件（events 中
   SHARE_UNLOCK）。输入中没有的信息一律视为未知，不得推测。
2. **数据不足要明说**：某数据集为空时，对应维度写"数据不足"，不得脑补；
   整体数据严重不足时 tags 含"数据不足"、risk_level 不得低于 medium。
3. **输出严格 JSON**（无 markdown 代码围栏），形如：
   {{"tags": [...], "risk_level": "low|medium|high", "summary": "...", "report_markdown": "..."}}
   - tags：从候选集中选 1-4 个：{json.dumps(ALLOWED_TAGS, ensure_ascii=False)}
   - risk_level：low（无明显风险信号）/ medium（存在需关注信号或数据不足）/
     high（审计非标、质押比例高企、密集减持、业绩预警等任一硬信号）
   - summary：一句话（≤80 字）概括财务质量与主要风险
   - report_markdown：Markdown 全文，包含且仅包含以下章节：
     ## 财务质量趋势（营收/利润/ROE 等趋势，引用报告期数字）
     ## 历史股东回报（分红连续性、股息率线索）
     ## 风险信号盘点（审计意见/质押/增减持/解禁，逐项说明有无）
     ## 未来事件提醒（events 中的未来事件，无则明说）
     ## 待关注问题（2-4 个具体问题，只提问题与权衡，不给指令性买卖建议）
     结尾附一行：{ANALYSIS_DISCLAIMER}"""


def build_analysis_messages(input_payload: Dict[str, Any]) -> List[Dict[str, str]]:
    serialized = json.dumps(
        input_payload, ensure_ascii=False, separators=(",", ":"), default=str
    )
    user_content = (
        "请基于下方 JSON 数据生成该标的的档案分析（严格按 system 约定输出 JSON）：\n\n"
        f"```json\n{serialized}\n```"
    )
    return [
        {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def parse_analysis_output(content: str) -> Dict[str, Any]:
    """解析 JSON mode 输出并做结构校验；非法输出抛 ValueError（确定性失败）。"""
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
    if risk_level not in ("low", "medium", "high"):
        raise ValueError(f"risk_level 非法: {risk_level!r}")
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
