"""雪球观点摘要的 prompt 构建与输出解析强制。

与 security_analysis_prompts 同款双重强制：白名单/枚举既写进 system prompt
（请求），又在 parse_opinion_output 里逐条校验（强制）——JSON mode 只保证
语法合法，不保证遵守约定；非法输出一律 ValueError = 确定性失败不烧重试。

**接地校验用输入统计量而非 prompt 承诺**：`近期转多` 这类变化标签必须与
recent/baseline 分组的真实条数自洽（近期零发言谈不上转向、无基线谈不上
"转"、有基线谈不上"新增关注"）。作者名必须来自输入（防幻觉作者）。

无缓存版本机制：与 security_analysis 同款，24h 新鲜度自然过期，字节变化
零成本（见 prompt_guardrails 模块 docstring 的三管线对照）。
"""

import json
from typing import Any, Dict, List, Set

from .prompt_guardrails import no_prior_knowledge_guardrail

# 观点标签白名单（1-4 个）：整体立场 / 近期变化（重点） / 语境 三层
ALLOWED_OPINION_TAGS = [
    "一致看多", "偏多", "多空分歧", "偏空", "一致看空", "中性观望",
    "近期转多", "近期转空", "新增关注", "关注度上升", "关注度下降", "讨论沉寂",
    "事件驱动讨论", "观点数据不足",
]

STANCE_VALUES = frozenset({"看多", "看空", "中性", "不明"})
RECENT_CHANGE_VALUES = frozenset({"转多", "转空", "新增", "无"})

# 近期窗口零发言时不可能成立的标签
_RECENT_ACTIVITY_TAGS = frozenset({"近期转多", "近期转空", "新增关注", "关注度上升"})

OPINION_DISCLAIMER = (
    "本摘要由 AI 基于雪球用户公开发言自动生成，内容为第三方个人观点的转述，"
    "仅供参考，不构成投资建议。"
)


def build_system_prompt(recent_days: int) -> str:
    guardrail = no_prior_knowledge_guardrail(
        "雪球用户发言记录",
        "输入中未出现的作者或观点一律视为未知",
    )
    tags_json = json.dumps(ALLOWED_OPINION_TAGS, ensure_ascii=False)
    return (
        "你是一个家庭投资组合的雪球观点整理助手，任务是只基于用户提供的雪球发言记录，"
        "归纳几位被长期关注的作者对某一只标的的观点及其**近期变化**。\n\n"
        "四条守则：\n"
        "1. 观点是引述不是事实：所有输入都是雪球用户的个人观点，你只做归纳转述，"
        "不得把它们当作对公司基本面的事实判断，更不得据此给出任何买卖建议。\n"
        f"2. 只用输入数据：{guardrail}对该公司和这些作者的先验知识同样禁止引入。\n"
        f"3. 近期变化要有依据：输入按 recent（近 {recent_days} 天）与 baseline（更早）"
        "分组。author_stances 必须**恰好覆盖输入中出现的每位作者一次**（不重复、"
        "不遗漏、不虚构）；每位作者的 recent_change 只能由该作者自己的两段发言支撑"
        "——「转多/转空」要求其 baseline 与 recent 都有发言且立场反转，「新增」要求"
        "其 baseline 完全无发言。顶层「近期转多/近期转空/新增关注」只有在至少一位"
        "作者有对应变化时才能用；「新增关注」还要求 baseline 整体无人提及。发言太少"
        "判断时，如实使用「观点数据不足」标签并在正文说明，不得脑补。\n"
        "4. 输出严格 JSON（不要 markdown 代码围栏），形如：\n"
        '{"tags": [...], "summary": "...", "author_stances": [...], "report_markdown": "..."}\n'
        f"- tags：从候选集中选 1-4 个：{tags_json}\n"
        "- summary：不超过 80 字的一句话观点概括\n"
        "- author_stances：每位在输入中出现的作者一条对象："
        '{"author": 作者昵称（必须来自输入）, "stance": "看多|看空|中性|不明", '
        '"recent_change": "转多|转空|新增|无", "evidence": 支撑判断的原文短引述（≤60字）}\n'
        "- report_markdown：Markdown 全文，包含且仅包含以下章节：\n"
        "  ## 近期观点变化（重点：谁转多/转空/新增关注，引述关键原文并附日期；无变化则明说）\n"
        "  ## 各作者立场综述（逐作者：整体立场、依据、与更早发言的对比）\n"
        "  ## 讨论焦点（大家在争论什么：估值/业绩/事件/情绪；无明确焦点则明说）\n"
        "  ## 数据边界（发言条数、时间跨度、覆盖作者数；样本不足时明确提示）\n"
        f"  正文结尾单独一行写：{OPINION_DISCLAIMER}"
    )


def build_opinion_messages(input_payload: Dict[str, Any]) -> List[Dict[str, str]]:
    serialized = json.dumps(
        input_payload, ensure_ascii=False, separators=(",", ":"), default=str
    )
    recent_days = int((input_payload.get("meta") or {}).get("recent_days") or 30)
    user_content = (
        "请基于下方 JSON 的雪球发言记录生成该标的的观点摘要"
        "（严格按 system 约定输出 JSON）：\n\n"
        f"```json\n{serialized}\n```"
    )
    return [
        {"role": "system", "content": build_system_prompt(recent_days)},
        {"role": "user", "content": user_content},
    ]


def parse_opinion_output(
    content: str, *, author_stats: Dict[str, Dict[str, int]]
) -> Dict[str, Any]:
    """解析并强制校验 LLM 输出；任何违规抛 ValueError（确定性失败）。

    author_stats = {作者: {"recent": 条数, "baseline": 条数}}，来自
    build_opinion_input 的**收缩前**真实统计。接地校验的核心是逐作者：
    「转多/转空/新增」必须与**该作者自己**的时间序列自洽——全局计数证明
    不了任何一位作者的变化（评审 P1：作者 A 只在 baseline、作者 B 只在
    recent 时，给 B 标「转多」靠全局 baseline>0 也能混过）。author_stances
    必须恰好覆盖输入作者各一次；顶层近期变化标签必须有通过校验的逐作者
    变化背书。
    """
    recent_count = sum(stat.get("recent", 0) for stat in author_stats.values())
    baseline_count = sum(stat.get("baseline", 0) for stat in author_stats.values())

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"输出不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("输出必须是 JSON 对象")

    tags = data.get("tags")
    summary = data.get("summary")
    stances = data.get("author_stances")
    markdown = data.get("report_markdown")
    if not isinstance(tags, list) or not (1 <= len(tags) <= 4):
        raise ValueError("tags 必须是 1-4 个元素的数组")
    invalid = [tag for tag in tags if tag not in ALLOWED_OPINION_TAGS]
    if invalid:
        raise ValueError(f"tags 含白名单外标签: {invalid}")
    if len(set(tags)) != len(tags):
        raise ValueError("tags 不得重复")
    if "近期转多" in tags and "近期转空" in tags:
        raise ValueError("近期转多与近期转空互斥")

    # 全局接地：变化标签必须与输入的 recent/baseline 总量自洽
    if recent_count == 0:
        offending = [tag for tag in tags if tag in _RECENT_ACTIVITY_TAGS]
        if offending:
            raise ValueError(f"近期窗口零发言，不得使用标签: {offending}")
    if baseline_count > 0 and "新增关注" in tags:
        raise ValueError("baseline 已有发言，不得使用「新增关注」")
    if baseline_count == 0 and recent_count > 0 and "讨论沉寂" in tags:
        raise ValueError("仅近期有发言，不得使用「讨论沉寂」")

    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary 不能为空")
    if not isinstance(markdown, str) or not markdown.strip():
        raise ValueError("report_markdown 不能为空")

    if not isinstance(stances, list):
        raise ValueError("author_stances 必须是数组")
    parsed_stances: List[Dict[str, str]] = []
    seen_authors: Set[str] = set()
    validated_changes: Set[str] = set()
    for item in stances:
        if not isinstance(item, dict):
            raise ValueError("author_stances 元素必须是对象")
        author = item.get("author")
        stance = item.get("stance")
        change = item.get("recent_change")
        evidence = item.get("evidence")
        if author not in author_stats:
            raise ValueError(f"author_stances 出现输入之外的作者: {author!r}")
        if author in seen_authors:
            raise ValueError(f"author_stances 中作者重复: {author!r}")
        seen_authors.add(author)
        if stance not in STANCE_VALUES:
            raise ValueError(f"stance 非法: {stance!r}")
        if change not in RECENT_CHANGE_VALUES:
            raise ValueError(f"recent_change 非法: {change!r}")
        stat = author_stats[author]
        author_recent = int(stat.get("recent", 0))
        author_baseline = int(stat.get("baseline", 0))
        # 逐作者接地：变化只能由该作者自己的两段时间序列支撑
        if change in ("转多", "转空"):
            if author_baseline == 0:
                raise ValueError(f"作者 {author} 无 baseline 发言，不可能「{change}」")
            if author_recent == 0:
                raise ValueError(f"作者 {author} 近期窗口零发言，不可能「{change}」")
        if change == "新增":
            if author_baseline > 0:
                raise ValueError(f"作者 {author} baseline 已有发言，不得标「新增」")
            if author_recent == 0:
                raise ValueError(f"作者 {author} 近期窗口零发言，不得标「新增」")
        if not isinstance(evidence, str):
            raise ValueError("evidence 必须是字符串")
        validated_changes.add(change)
        parsed_stances.append({
            "author": author, "stance": stance,
            "recent_change": change, "evidence": evidence.strip()[:120],
        })

    missing = set(author_stats) - seen_authors
    if missing:
        raise ValueError(f"author_stances 缺少输入作者: {sorted(missing)}")

    # 顶层近期变化标签必须有逐作者变化背书（无背书 = 无据结论）
    top_level_requires = {"近期转多": "转多", "近期转空": "转空", "新增关注": "新增"}
    for tag, required_change in top_level_requires.items():
        if tag in tags and required_change not in validated_changes:
            raise ValueError(
                f"「{tag}」缺少逐作者依据：无任何作者通过「{required_change}」校验"
            )

    return {
        "tags": tags,
        "summary": summary.strip()[:300],
        "author_stances": parsed_stances,
        "report_markdown": markdown.strip(),
    }
