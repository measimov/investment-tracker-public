"""LLM 复盘报告与追问对话的 prompt 组装。"""

from typing import Any, Dict, List

from .llm_report_input import serialize_input

DISCLAIMER = "本报告由 AI 基于家庭账本数据自动生成，仅供复盘讨论参考，不构成投资建议。"

_GUARDRAILS = """你必须遵守两条守则：
1. 不得虚构任何输入数据中未包含的外部信息（实时行情、新闻、宏观判断、个股基本面等）。
   需要外部信息才能回答时，明确说明"账本数据中不包含该信息"。
   输入中附带的基准指数数据（analytics.benchmarks）可以引用并用于相对表现分析。
2. 尊重口径标注：账户级收益为"权益仓口径"（仅证券投入、口径内精确，闲置现金与
   外部出入金不计入），转述时必须说明该口径，不得当作全账户收益；凡带有
   experimental/实验 标记的指标（TTWR、风险指标、按笔胜率等），转述时必须保留
   "实验"字样，不得当作权威结果。meta.estimate_semantics 解释了各类标记的含义。"""

REPORT_SYSTEM_PROMPT = f"""你是一个家庭投资组合的复盘助手，只基于用户提供的账本结构化数据进行分析。
金额默认为人民币（CNY），输出使用 Markdown（简体中文）。

{_GUARDRAILS}"""

CHAT_SYSTEM_PROMPT = f"""你是一个家庭投资组合的复盘助手，正在与用户就一份已生成的复盘报告进行追问讨论。
只基于报告与附带的账本结构化数据回答，金额默认为人民币（CNY），使用简体中文。

{_GUARDRAILS}"""

_REPORT_INSTRUCTIONS = f"""请基于下方 JSON 账本数据生成一份投资复盘报告，Markdown 格式，包含且仅包含以下章节：

## 业绩概览
总收益构成（已实现/未实现/股息）、总市值与投入本金、年化（注明权益仓口径）。

## 区间归因
已实现盈亏的主要贡献标的、股息贡献、TTWR 区间表现摘要（引用 analytics 数据）；
如 analytics.benchmarks 有可用基准，给出相对基准的表现（超额收益，注明为
价格指数算术差、不含股息）。

## 持仓集中度与风险
前几大持仓权重、市场分布、风险指标（夏普/回撤等，注明实验口径）、按笔胜率。

## 数据质量与对账状态
必须点名：过时（stale）与缺失价格的标的、各账户对账状态（尤其 MISMATCHED/PENDING）、
data_quality 与 analytics 中的 warnings。数据没有问题时也要明确说明"本期无数据质量问题"。

## 待讨论的决策问题
提出 3–5 个具体、可讨论的问题（如集中度、长期亏损标的的去留、现金股息再投等），
供家庭成员共同决策。只提问题与权衡，不给指令性买卖建议。

报告末尾固定附一行免责声明：{DISCLAIMER}"""


def build_report_messages(input_payload: Dict[str, Any]) -> List[Dict[str, str]]:
    return [
        {"role": "system", "content": REPORT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"{_REPORT_INSTRUCTIONS}\n\n```json\n{serialize_input(input_payload)}\n```",
        },
    ]


def build_chat_messages(
    report_content: str,
    input_payload: Dict[str, Any],
    history: List[Dict[str, str]],
    question: str,
) -> List[Dict[str, str]]:
    """追问上下文 = 报告全文 + 生成时的账本数据 + 近若干轮历史 + 新问题。"""
    context = (
        "以下是已生成的复盘报告全文与生成时使用的账本数据，后续问题均基于它们讨论。\n\n"
        f"# 报告\n\n{report_content}\n\n# 账本数据\n\n```json\n{serialize_input(input_payload)}\n```"
    )
    messages = [
        {"role": "system", "content": CHAT_SYSTEM_PROMPT},
        {"role": "user", "content": context},
        {"role": "assistant", "content": "已阅读报告与账本数据，请提出你的问题。"},
    ]
    messages.extend({"role": m["role"], "content": m["content"]} for m in history)
    messages.append({"role": "user", "content": question})
    return messages
