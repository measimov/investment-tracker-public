"""LLM 标的分析的 prompt 组装（DeepSeek JSON mode，一次产出标签+全文）。

guardrail 与复盘报告同一哲学并更严格：分析对象是具体上市公司，模型对
公司的先验知识（新闻、事件、口碑）一概不得引入——"合规污点"判断只能
来自输入中的客观风险信号（审计意见、股权质押、股东增减持、限售解禁）。
"""

import json
import re
import unicodedata

from typing import Any, Dict, List

from .graham_screen import GRAHAM_CRITERIA_NAMES_ZH, GRAHAM_VERDICT_LABELS_ZH
from .prompt_guardrails import NO_PRIOR_KNOWLEDGE_CLAUSE

ANALYSIS_DISCLAIMER = "本分析由 AI 基于公开结构化数据自动生成，仅供参考，不构成投资建议。"

# report_markdown 的必需章节（名称，不含括号说明）：解析层按它校验完整性，
# build_system_prompt 里的章节标题与它逐一对应（test_security_profile 守护）。
# JSON mode 下正文里的英文双引号会被约束解码当成字符串结束、随即补 `}` 收尾——
# 输出仍是合法 JSON，但报告停在半截（离线评测 qwen3.8-flash 20 份截断 12 份），
# 只校验 JSON 结构拦不住（#287）。
REPORT_SECTIONS = (
    "商业模式与产业链",
    "财务质量趋势",
    "财报要点",
    "利润质量与会计风险",
    "格雷厄姆准则解读",
    "非对称性与脆弱性",
    "历史股东回报",
    "风险信号盘点",
    "未来事件提醒",
    "待关注问题",
)

# 标签的近义写法 → 白名单写法（只收实测见过的；离线评测 DeepSeek Flash 官方/方舟托管与
# Claude Haiku 各把「依赖非经常损益」写成过「依赖非经常性损益」，整份分析因此被拒）
TAG_SYNONYMS: Dict[str, str] = {
    "依赖非经常性损益": "依赖非经常损益",
}


class IncompleteReportError(ValueError):
    """报告缺必需章节（通常是约束解码在正文英文引号处提前收尾）：同一输入重试可能成功，
    调用方重试一次后按 error_kind=incomplete 记失败。"""


ALLOWED_TAGS = [
    "高股息",
    "分红连续",
    "分红中断",
    "业绩增长",
    "业绩下滑",
    "业绩预警",
    "高质押",
    "大股东减持",
    "大股东增持",
    "解禁临近",
    "审计非标",
    "估值偏高",
    "估值偏低",
    "数据不足",
    # 利润质量层（触发语义见 earnings_quality.metric_semantics 红旗阈值）
    "利润质量存疑",
    "现金流背离",
    "依赖非经常损益",
    # 格雷厄姆×塔勒布框架层（依据 graham_screen 预计算结果，不得自行心算；
    # 估值准则 indeterminate 时禁用安全边际两标签——无估值数据谈不上边际）
    "安全边际充足",
    "安全边际不足",
    "财务强度高",
    "高杠杆脆弱",
    "净现金充裕",
    "尾部风险暴露",
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
        "「安全边际充足」只在格雷厄姆四项（市盈率、市净率、流动比率、长期债务）"
        "全部达标且估值数据充足（价格不陈旧、"
        "每股盈利为最近一年内的 TTM/年报、无估算告警）时可用，服务端强制校验。"
    ),
    "港股": (
        "港股无审计意见/质押/增减持数据源——风险判断只能来自 report_digests "
        "与 earnings_quality 指标；逐期引用输入中实际提供的风险要点，注明报告期。"
        "风险字段缺失或写'原文未提及'时，只能说'所提供节选未包含具体风险内容'；"
        "不得推断年报没有风险章节，也不得把某一期的缺口推广到所有年份。"
        "以下标签**禁止使用**：高质押、大股东减持、大股东增持、"
        "解禁临近、审计非标。「安全边际充足」只在格雷厄姆四项全部达标且估值数据充足"
        "（价格不陈旧、每股盈利为最近一年内的 TTM/年报、无估算告警）时可用，服务端强制校验。"
        "结构化科目来自披露易年报/中报原文抽取（report_statements，可达十年，"
        "is_comparative=true 为比较期列）并以雅虎数据补缺；输入里年度行仍稀少时须"
        "据实收敛口径；risk_level 不得为 low，数据严重不足时 tags 应含'数据不足'。"
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
        "质押/增减持数据源、结构化科目以年报/中报原文抽取为准；风险摘要的缺口按报告期说明）"
    ),
}


def _graham_label_list() -> str:
    return "、".join(GRAHAM_CRITERIA_NAMES_ZH.values())


# 正文用语守则（全市场）：2026-09 生产扫描，最新一份分析里约三分之二把输入字段名与英文
# 判定词原样写进正文（`current_ratio 2.24 → pass`、`graham_screen.status=ok`、
# `passed 7 / failed 0`），且每只标的写法各异。输入侧给了 name_zh / verdict_zh，这里再
# 把「正文只用中文名」写成硬性要求。
REPORT_WORDING_RULE = (
    "**正文用语**：report_markdown 与 summary 面向普通投资者，引用输入数据一律用中文说法"
    "（如 财报摘要、商业画像、利润质量指标、格雷厄姆准则、脆弱性信号、依据、补充口径），"
    "**不得出现输入 JSON 的字段名、JSON 路径或字段取值**（如 graham_screen、criteria[].basis、"
    "supplement、status=ok、current_ratio、basic_eps、as_of_year、price_stale=false）。"
    "格雷厄姆各准则只用中文名（即 criteria[].name_zh："
    f"{_graham_label_list()}），判定只写"
    f"{'/'.join(GRAHAM_VERDICT_LABELS_ZH.values())}（即 criteria[].verdict_zh），"
    "计数写成「达标 N 项、不达标 N 项、不可判定 N 项」；"
    "**不得使用英文判定词**（pass、fail、indeterminate、passed、failed、verdict）。"
    "PE、PB、TTM、EPS、ROE 等通用财务缩写可以使用。"
)


def supplementary_rules(market: str) -> List[str]:
    """补充约束（#288）：两轮离线评测（第二轮 10 只标的、第三轮 8 只留出标的，双评审）
    针对各模型共性失分点整理的通用条款；DeepSeek Flash 在留出集上 19.5 → 21.6 分，
    规则分 3.0 → 3.9。原文与评测记录见 #288。涉及计算的条目（股息对照、下半年推算、
    ROE、科目变动方向、低基数）改为引用服务端预计算的 signals（#265）。"""
    # 可选值由服务端下限派生（单一来源）：prompt 写了「不得为 low」而解析层没有下限时，
    # 模型不遵守就原样入库、遵守了口径又没记录（PR #308 评审 P3-2）
    floor = MARKET_MIN_RISK_LEVEL.get(market)
    risk_choices = (
        "、".join(level for level in _RISK_ORDER if _RISK_ORDER[level] >= _RISK_ORDER[floor])
        + f"（不得为 {'、'.join(level for level in _RISK_ORDER if _RISK_ORDER[level] < _RISK_ORDER[floor])}）"
        if floor
        else None
    )
    rules = [
        f"- risk_level 在本市场只能取 {risk_choices}。" if risk_choices else None,
        "- 时点：以 meta.as_of_date（正文称「数据日」）为当前日期。events 中 status=past 的是已发生事件，"
        "不得写进「未来事件提醒」；该章节只列 status=upcoming 的事件，没有就写「无」。",
        "- 章节标题：只写上文 10 个章节的名称本身（如「## 商业模式与产业链」），"
        "不得把括号里的写作说明、字段名或指令抄进标题或正文。",
        "- 取舍：输入数据较多，只使用与判断相关的数据，其余字段可以忽略，不必逐项复述；"
        "同一数字有多个来源时按 data_semantics 写明的优先级取一个，不要混用。",
        "- 预计算优先：signals 里已给出的量（同比、下半年推算、单季、股息总额与每股分红、股息支付率、"
        "自由现金流、股息占自由现金流比例、股息率、科目变动方向与幅度、占总资产比例、净现金、ROE）"
        "一律直接引用，不得自行重算或改写方向；signals 没有给出的才可计算，并写出算式与结果。",
        "- 自由现金流：只引用 signals 中 free_cashflow / fcf_yi 及对应 basis 的金额与期间；"
        "默认经营现金流减资本开支绝对值。数据缺失时注明无法计算，不得换用其他来源的定义。",
        "- 摘要数字核对：被隔离的字段不是公司未披露；用同期结构化报表，缺失时注明待核对。"
        "未能核对不等于通过；不得把调整后利润、分部收入等其他口径替代归母净利或合并营收。",
        "- 数字与方向：比较两期时先写出两期原值再写方向（上升/下降/持平），方向必须与原值一致。",
        "- 数字格式：金额换算为亿元（或百万元、亿港元、亿美元）保留 2 位小数，比率保留 1–2 位小数；"
        "不得照抄 4 位以上小数或以「元」为单位的长整数。",
        "- 预计算比率是舍入后的展示值；阈值结论沿用已有判定与方向，不得用展示值重新判定。",
        "- 口径与单位：每股金额与股息按每股口径写（输入为「每 10 股」时折算为每股），不得改写为「每 10 股」；"
        "注意币种与数量级，同一句内不要混用；港股/美股报表币种以行内 currency 为准。",
        "- 口径与时期：每个数字写明所属期间（如 2025 年度、2026 上半年）；格雷厄姆准则与脆弱性信号是年度口径"
        "（以其依据中的年份为准），不得写成中报期间。同一指标有多个口径时写明口径且不直接比较："
        "加权/摊薄 ROE、归母/扣非净利润（两者的同比增速是不同字段，不得互换）、营业收入/营业总收入；"
        "业绩快报与正式报告并存时以正式报告为准。",
        "- ROE：引用 signals.roe 并注明口径（字段名或 formula）；signals.roe 为空时才写「数据不足」。",
        "- 标签须与数据一致：「业绩增长」须最新财年或最新中报（季报）营收与归母净利均同比增长；「业绩下滑」"
        "须最新财年或最新中报（季报）归母净利同比下降，依据的是哪一期要在正文写明（见 signals.period_signals）；"
        "「高股息」须有输入中的股息率（signals.shareholder_returns.dividend_yield），不得自行推算股息率；「净现金充裕」须净现金为正且"
        "占总资产不低于 5%；「高杠杆脆弱」「尾部风险暴露」须有对应脆弱性信号触发。标签与正文结论冲突时，"
        "按正文依据修正标签。",
        "- 「利润质量存疑」须最新财年至少一项预计算指标触发且不是低基数失真（signals.balance_changes 里"
        "low_base=true 的科目，其增速差不算），或 M-score 触发；只有历史年份触发时只在正文提示，不贴该标签。「现金流背离」只在"
        "净利润为正且经营现金流/净利润低于阈值时成立；净利润 ≤ 0 的年份该比率与扣非占比不计（为空，ratio_unavailable 标注 ni_non_positive），不得自行相除后作红旗解读。"
        "两者都不得仅凭单个中报期推断。",
        "- 分红标签：signals.shareholder_returns.ever_paid=false（从未派息）时不得用「分红中断」"
        "（也不用「分红连续」）；「分红中断」只用于曾经派息后停止。",
        "- 安全边际：市盈率、市净率、流动比率、长期债务四项全部达标时不得使用「安全边际不足」"
        "（满足估值数据充足条件时可用「安全边际充足」，否则两者都不用），其余准则不达标只在正文说明。",
        "- 风险等级 high 只在出现硬信号时使用（审计非标、业绩预警、密集减持、质押比例高企）；"
        "「高质押」须质押比例处于高位（如 30% 以上）或快速上升。",
        "- 股东回报：引用 signals.shareholder_returns 的逐年股息总额（已含中期）、股息支付率与股息占自由"
        "现金流比例，并写明自由现金流口径（fcf_basis）；有 flags 时必须在正文指出；股息率引用 "
        "dividend_yield，estimated=true 时写明是估算，value_pct 为空时不得自行推算股息率。",
        "- 有数据就要写到的信号：① 最新中报与上年同期对比、下半年推算（signals.period_signals）；"
        "② 最近单季的变化（latest_single_quarter，有时）；③ 股息占自由现金流的比例；④ 净利润明显高于或"
        "低于营业利润时指出非经营项目的影响（不推测具体原因）；⑤ 资本开支与自由现金流的变化；"
        "⑥ 货币资金与有息负债各自占总资产的比例（signals.balance_changes，两者同时处于高位即存贷双高，"
        "写出比例，不要只下结论）。",
        "- 先验知识：公司对自身行业地位的描述（龙头、领先等）只能以「公司自述」引用；输入没有提供的业务名称、"
        "竞争格局、监管事件、市场观点一律不写；待关注问题只能基于输入中的数据提出，不得引入输入没有的业务"
        "假设（如备货、监管、客户名称）。",
        "- 引号：正文引号一律用中文引号「」，不得使用英文双引号。",
        "- 篇幅：report_markdown 控制在 2500–5000 个汉字，宁可精炼，不堆砌数字。",
    ]
    return [rule for rule in rules if rule]


def build_system_prompt(market: str) -> str:
    """按市场组装 system prompt：共享守则骨架 + 市场差异段 + 补充约束。"""
    rules = "\n".join(supplementary_rules(market))
    return _base_system_prompt(market) + "\n\n补充约束（优先于上文的一般表述）：\n" + rules


def _base_system_prompt(market: str) -> str:
    """守则骨架 + 市场差异段（#288 前的原文，章节标题与 REPORT_SECTIONS 对应）。"""
    risk_sources = _MARKET_RISK_SOURCES.get(market, _MARKET_RISK_SOURCES["A股"])
    risk_section = _MARKET_RISK_SECTION.get(market, _MARKET_RISK_SECTION["A股"])
    return f"""你是一个家庭投资组合的标的档案分析助手，只基于用户提供的公开结构化数据分析一只{market}标的。

你必须遵守三条守则：
1. **只用输入数据**：{NO_PRIOR_KNOWLEDGE_CLAUSE}（新闻、事件、行业口碑、
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
   - {REPORT_WORDING_RULE}
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
     ## 格雷厄姆准则解读（graham_screen.criteria 已给出逐项判定与依据，
        **禁止自行心算任何比率**——只做解读，准则名与判定词按上方「正文用语」：
        达标项说明该防御性来源、不达标项说明缺口大小与含义、不可判定项如实说明
        数据边界（如港股 PDF 抽取覆盖不足十年、雅虎补缺仅近 3-5 年、披露历史不足
        十年），绝不把"不可判定"说成"达标"或"不达标"；估值两项的判定口径是 TTM
        （港股/美股 = 行情收盘价 ÷ 报表推算的 TTM 每股盈利、MRQ 每股净资产为隐含
        股数估算，构成与价格日期见 criteria[].basis（正文称「依据」），价格陈旧时
        须提示）；criteria[].supplement（正文称「补充口径」）里的年报静态 PE 与原著
        三年平均 PE 仅作参考对照，不得据此改写判定；按 counts_zh 的达标/不达标/
        不可判定计数给出"防御型标准下的安全边际"总体评述；
        **仅当市盈率、市净率、流动比率、长期债务四项均达标** 才可用"安全边际充足"
        标签（服务端按 graham_screen 实际结果强制校验，不满足会被拒绝）；估值或
        财务强度不达标用"安全边际不足"）
     ## 非对称性与脆弱性（塔勒布视角，只依据输入数据定性评估：
        下行保护——净现金/硬资产/股息底，引用脆弱性信号（graham_screen.fragility）的数值；
        上行开放性——业务中的期权性来源（新业务、产能、渠道扩张等，
        只从 business_profile 与 report_digests 中找依据）；
        脆弱结构——高杠杆+薄利息覆盖、单一客户/供应商/产品依赖、
        对补贴或利率等稳定环境的隐性依赖；脆弱信号触发时对应标签
        高杠杆脆弱/尾部风险暴露，净现金显著为正可用净现金充裕）
     ## 历史股东回报（分红连续性、股息率线索）
     {risk_section}
     ## 未来事件提醒（events 中的未来事件，无则明说）
     ## 待关注问题（2-4 个具体问题，只提问题与权衡，不给指令性买卖建议）
     结尾附一行：{ANALYSIS_DISCLAIMER}"""


def graham_for_llm(graham: Dict[str, Any] | None) -> Dict[str, Any] | None:
    """分析输入里的格雷厄姆结果：原结构不变（服务端校验仍按 criterion/verdict 读），每条
    准则附中文名 name_zh 与中文判定 verdict_zh，顶层附 counts_zh——模型照着中文写，
    不必把 current_ratio / pass 这类取值翻译或原样照抄。"""
    if not isinstance(graham, dict):
        return graham
    annotated = dict(graham)
    criteria = []
    for item in graham.get("criteria") or []:
        if not isinstance(item, dict):
            criteria.append(item)
            continue
        key = str(item.get("criterion") or "")
        verdict = str(item.get("verdict") or "")
        criteria.append(
            {
                **item,
                "name_zh": GRAHAM_CRITERIA_NAMES_ZH.get(key, key),
                "verdict_zh": GRAHAM_VERDICT_LABELS_ZH.get(verdict, verdict),
            }
        )
    if "criteria" in graham:
        annotated["criteria"] = criteria
    if graham.get("status") == "ok":
        annotated["counts_zh"] = (
            f"达标 {graham.get('passed', 0)} 项、不达标 {graham.get('failed', 0)} 项、"
            f"不可判定 {graham.get('indeterminate', 0)} 项"
        )
    semantics = graham.get("criteria_semantics")
    if isinstance(semantics, dict):
        annotated["criteria_semantics"] = {
            key: f"{GRAHAM_CRITERIA_NAMES_ZH.get(key, key)}：{text}"
            for key, text in semantics.items()
        }
    return annotated


_RATIO_FIELDS = frozenset(
    {
        # 只舍入已知的预计算展示指标，不按后缀猜测（share_ratio 是 ADS 换算事实）。
        "display_ratio",
        "cfo_ni_ratio",
        "cfo_ni_ratio_5y",
        "accruals_ratio",
        "receivable_vs_revenue_gap_pp",
        "inventory_vs_revenue_gap_pp",
        "recurring_profit_share",
        "gross_margin",
        "net_margin",
        "value_pct",
        "yoy_pct",
        "revenue_h2_yoy_pct",
        "net_income_h2_yoy_pct",
        "revenue_yoy_pct",
        "net_income_yoy_pct",
        "payout_ratio_pct",
        "dividends_to_fcf_pct",
        "share_of_assets_pct",
        "change_pct",
        "roe_pct",
        "debt_to_assets",
        "net_debt_to_assets",
        "interest_coverage",
        "net_cash_to_market_cap",
        "score",
        "DSRI",
        "GMI",
        "AQI",
        "SGI",
        "DEPI",
        "SGAI",
        "LVGI",
        "TATA",
    }
)
_RATIO_CRITERIA = frozenset({"pe", "pb_or_product", "current_ratio", "earnings_growth"})


def _ratios_for_llm(value: Any, field: str = "") -> Any:
    """仅复制预计算区的比率展示值；判定、数据库和金额/每股值仍保留原精度。"""
    if isinstance(value, dict):
        return {
            key: _ratios_for_llm(
                item,
                "display_ratio"
                if key == "value" and value.get("criterion") in _RATIO_CRITERIA
                else key,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_ratios_for_llm(item) for item in value]
    if isinstance(value, float) and field in _RATIO_FIELDS:
        rounded = round(value, 2)
        # 极小的非零值不能伪装成零；用明确区间保留符号，不输出冗长小数。
        if rounded == 0 and value != 0:
            return "大于 0 且小于 0.01" if value > 0 else "大于 -0.01 且小于 0"
        return rounded + 0.0
    return value


def build_analysis_messages(input_payload: Dict[str, Any]) -> List[Dict[str, str]]:
    display_payload = dict(input_payload)
    for section in ("earnings_quality", "signals", "graham_screen"):
        if section in display_payload:
            display_payload[section] = _ratios_for_llm(display_payload[section])
    serialized = json.dumps(display_payload, ensure_ascii=False, separators=(",", ":"), default=str)
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
_A_SHARE_ONLY_TAGS = frozenset({"高质押", "大股东减持", "大股东增持", "解禁临近", "审计非标"})
# 美股/港股没有审计意见/质押/增减持/解禁数据源，这些标签在解析层确定性拒绝。
# 「安全边际充足」不再整体禁用（PR #232，用户确认「数据充足即可放开」）：估值两项已由
# 行情价 ÷ 报表推算可判定，是否放行交给 margin_of_safety_allowed——四项 pass 之外，
# 估算型估值还要求数据充足（valuation_data_sufficient）。
MARKET_BANNED_TAGS: Dict[str, frozenset] = {
    "美股": _A_SHARE_ONLY_TAGS,
    "港股": _A_SHARE_ONLY_TAGS,
}
# 估算型估值（港股/美股）的「数据充足」：最新年报期末距价格日不超过此天数
VALUATION_MAX_FY_AGE_DAYS = 460

# 风险等级下限：数据边界决定"无明显风险信号"这个判断本身不成立的市场。
# 港股结构化科目已可由披露易年报/中报 PDF 抽取覆盖至十年（雅虎只补缺、仅近 3-5 年），
# 但覆盖仍不均（实测 02313 0 行、02156 3 行、09618 6 行），且本市场没有审计意见/质押/
# 增减持这类客观风险信号源——"无明显风险信号"这个判断本身仍不成立，low 属无依据的
# 乐观，下限保留。
#
# 低于下限时**上调而不是拒绝**（2026-09-27 生产：02313 连续两次因 low 被整份丢弃，重试
# 结果相同）：下限表达的是数据边界而不是模型看错了数据，其余内容仍然有效。上调记录落
# `risk_level_adjusted` 并在全文末尾加注，前端风险标签旁提示——不静默改写模型的判断。
MARKET_MIN_RISK_LEVEL: Dict[str, str] = {"港股": "medium", "美股": "medium"}
MARKET_RISK_FLOOR_REASON: Dict[str, str] = {
    "港股": "港股数据边界：无审计意见/质押/增减持等客观风险信号源，「无明显风险信号」不成立",
    "美股": "美股数据边界：无审计意见/质押/增减持等客观风险信号源，「无明显风险信号」不成立",
}
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
_RISK_LABELS = {"low": "低", "medium": "中", "high": "高"}


# 「安全边际充足」的服务端判定条件（评审 P1 二轮）：prompt 里写的"估值两项
# pass 且财务强度 pass 才可用"只是请求；LLM 返回白名单内的这个标签一样会被
# 持久化并与页面预计算表直接矛盾。条件在这里定义一次，parse 按 graham_screen
# 实际 verdict 强制。
MARGIN_OF_SAFETY_TAG = "安全边际充足"
# 估值两项 + 财务强度两项（流动比率、长期债务≤净流动资产）全部 pass；
# 任一 fail/indeterminate 均不放行（评审 P1 三轮：漏掉债务准则时，负债超出
# 净流动资产的标的仍会被贴上安全边际充足）
_MARGIN_OF_SAFETY_CRITERIA = (
    "pe",
    "pb_or_product",
    "current_ratio",
    "lt_debt_vs_net_current_assets",
)


# 估值方法按**市场**确定（不靠 basis 里有没有某个字段猜）：A股 = Tushare 快照；港股/美股 =
# 行情价 ÷ 报表推算的估计值。graham_screen 也在 basis 里写了 `valuation_method`，市场未知
# （调用方没传 market）时才用它。PR #232 评审：此前按「basis 有没有 price」区分，而 A股
# 快照的 basis 也带 price（快照收盘价），A股 的安全边际充足被全部误拒
VALUATION_METHOD_BY_MARKET: Dict[str, str] = {
    "A股": "snapshot",
    "港股": "estimated",
    "美股": "estimated",
}


def valuation_method(criterion: Dict[str, Any] | None, market: str | None = None) -> str:
    """snapshot（数据源直接给出 PE/PB）或 estimated（行情价 ÷ 报表推算）。"""
    by_market = VALUATION_METHOD_BY_MARKET.get(str(market or ""))
    if by_market:
        return by_market
    basis = (criterion or {}).get("basis") or {}
    method = basis.get("valuation_method")
    if method in ("snapshot", "estimated"):
        return method
    return "snapshot"  # 无 basis 的旧结果只可能来自 A股 快照（港股/美股此前恒为 indeterminate）


def valuation_data_sufficient(criterion: Dict[str, Any] | None, market: str | None = None) -> bool:
    """估值准则的输入是否足以支撑「安全边际充足」这个结论。

    A股 的 pe/pb 来自 Tushare 估值快照，视为充足（原准入逻辑）。港股/美股是行情价 ÷
    报表推算的估计值，要求：价格不陈旧；basis 无估算告警（note：未滚动/期间不衔接/隐含
    股数偏离等）；每股盈利所用最新年报期末距价格日不超过 VALUATION_MAX_FY_AGE_DAYS。
    """
    if not criterion:
        return False
    if valuation_method(criterion, market) == "snapshot":
        return True
    basis = criterion.get("basis") or {}
    if basis.get("price") is None:
        return False
    if basis.get("price_stale") or basis.get("note"):
        return False
    price_date = str(basis.get("price_date") or "")[:10]
    components = basis.get("components") or []
    fy_period = str((components[0] or {}).get("period") or "") if components else ""
    fy_end = fy_period.split("|")[0]
    if criterion.get("criterion") == "pe":
        if not (price_date and len(fy_end) == 8):
            return False
        from datetime import date as _date

        try:
            price_day = _date.fromisoformat(price_date)
            fy_day = _date(int(fy_end[:4]), int(fy_end[4:6]), int(fy_end[6:]))
        except ValueError:
            return False
        return (price_day - fy_day).days <= VALUATION_MAX_FY_AGE_DAYS
    return True


def margin_of_safety_allowed(
    graham_screen: Dict[str, Any] | None, market: str | None = None
) -> bool:
    """估值两项 + 财务强度两项全部 pass，且估值数据充足，才允许"安全边际充足"；
    无 graham 结果视为不允许。market 决定估值方法（A股 快照 / 港股美股 估算）。"""
    if not graham_screen or graham_screen.get("status") != "ok":
        return False
    items = {item.get("criterion"): item for item in graham_screen.get("criteria") or []}
    if not all(
        (items.get(key) or {}).get("verdict") == "pass" for key in _MARGIN_OF_SAFETY_CRITERIA
    ):
        return False
    return all(valuation_data_sufficient(items.get(key), market) for key in ("pe", "pb_or_product"))


def parse_analysis_output(
    content: str,
    market: str | None = None,
    graham_screen: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """解析 JSON mode 输出并做结构校验；非法输出抛 ValueError（确定性失败）。

    market 非空时额外执行该市场的硬约束（禁用标签拒绝；风险等级低于下限时上调并记录在
    `risk_level_adjusted`，报告末尾加注）；graham_screen 传入时按实际准则 verdict 硬校验
    "安全边际充足"。
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
    # 白名单契约在解析层强制执行：JSON mode 只保证语法不保证遵守 prompt，模型自造标签会
    # 污染持仓页的结构化标签列。越界标签**丢弃并记录**而非整份拒绝（#287）：此前一个近义
    # 写法或第 5 个标签就让整份分析作废、白烧一次 token，而正文本身无问题
    tags, adjustments = _normalize_tags(tags, market)
    if MARGIN_OF_SAFETY_TAG in tags and not margin_of_safety_allowed(graham_screen, market):
        raise ValueError(
            f"'{MARGIN_OF_SAFETY_TAG}' 要求 graham_screen 的 pe/pb_or_product/"
            "current_ratio/lt_debt_vs_net_current_assets 全部 pass，与预计算结果矛盾"
        )
    if risk_level not in ("low", "medium", "high"):
        raise ValueError(f"risk_level 非法: {risk_level!r}")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary 缺失")
    if not isinstance(report, str) or not report.strip():
        raise ValueError("report_markdown 缺失")
    report_markdown, section_adjustments = _check_report_completeness(report.strip())
    adjustments.extend(section_adjustments)
    adjusted = None
    floor = MARKET_MIN_RISK_LEVEL.get(str(market or ""))
    if floor and _RISK_ORDER[risk_level] < _RISK_ORDER[floor]:
        adjusted = {
            "from": risk_level,
            "to": floor,
            "reason": MARKET_RISK_FLOOR_REASON.get(
                str(market), f"{market} 数据边界有限，风险等级下限为 {floor}"
            ),
        }
        risk_level = floor
        report_markdown += (
            f"\n\n---\n\n> 注：模型给出的风险等级为「{_RISK_LABELS[adjusted['from']]}」，"
            f"按{market}风险等级下限上调为「{_RISK_LABELS[floor]}」（{adjusted['reason']}）。"
        )
    if "数据不足" in tags and risk_level == "low":
        raise ValueError('标注"数据不足"时 risk_level 不得为 low')
    return {
        "tags": tags,
        "risk_level": risk_level,
        "risk_level_adjusted": adjusted,
        "summary": summary.strip()[:300],
        "report_markdown": report_markdown,
        "adjustments": adjustments,
    }


MAX_TAGS = 4


def _normalize_tags(tags: List[str], market: str | None) -> tuple[List[str], List[Dict[str, Any]]]:
    """近义归一 → 丢弃白名单外/本市场禁用 → 截到 4 个；返回 (标签, 调整记录)。
    丢完一个不剩才算确定性失败。"""
    adjustments: List[Dict[str, Any]] = []
    banned = MARKET_BANNED_TAGS.get(str(market or ""), frozenset())
    kept: List[str] = []
    for raw in tags:
        tag = unicodedata.normalize("NFKC", raw).strip().replace(" ", "")
        tag = TAG_SYNONYMS.get(tag, tag)
        if tag != raw:
            adjustments.append({"type": "tag_normalized", "from": raw, "to": tag})
        if tag not in ALLOWED_TAGS:
            adjustments.append({"type": "tag_dropped", "tag": raw, "reason": "not_allowed"})
            continue
        if tag in banned:
            adjustments.append({"type": "tag_dropped", "tag": tag, "reason": "market_banned"})
            continue
        if tag not in kept:
            kept.append(tag)
    if len(kept) > MAX_TAGS:
        adjustments.append({"type": "tags_truncated", "dropped": kept[MAX_TAGS:]})
        kept = kept[:MAX_TAGS]
    if not kept:
        raise ValueError(f"tags 无可用标签（原始：{tags}）")
    return kept, adjustments


# 章节级标题（# 或 ##；### 是章节内的小标题，不参与判定）
_HEADING_RE = re.compile(r"^#{1,2}(?!#)\s*(.+?)\s*$", re.M)
_HEADING_NUMBER_RE = re.compile(r"^(?:[0-9一二三四五六七八九十]+[、.．)）]\s*)")
# 句末标点：缺免责声明时，末尾不是完整句子就视为截断（半截报告不能靠补一行声明冒充完整）
_SENTENCE_END = tuple("。！？!?）)」”』…%")


def _heading_name(text: str) -> str:
    name = _HEADING_NUMBER_RE.sub("", text.strip())
    return re.split(r"[（(]", name, maxsplit=1)[0].strip()


def _check_report_completeness(report_markdown: str) -> tuple[str, List[Dict[str, Any]]]:
    """必需章节齐全；缺免责声明时补上（末尾须是完整句子）。缺章节抛 IncompleteReportError。"""
    headings = [_heading_name(match) for match in _HEADING_RE.findall(report_markdown)]
    missing = [
        section
        for section in REPORT_SECTIONS
        if not any(section in heading for heading in headings)
    ]
    if missing:
        raise IncompleteReportError(f"报告不完整，缺少章节：{'、'.join(missing)}")
    adjustments: List[Dict[str, Any]] = []
    extra = [
        heading
        for heading in headings
        if heading and not any(section in heading for section in REPORT_SECTIONS)
    ]
    if extra:
        adjustments.append({"type": "extra_sections", "sections": extra[:5]})
    if "不构成投资建议" not in report_markdown[-200:]:
        if not report_markdown.rstrip("*_ \n").endswith(_SENTENCE_END):
            raise IncompleteReportError("报告在末尾章节中途结束（缺免责声明且末句不完整）")
        report_markdown = f"{report_markdown}\n\n{ANALYSIS_DISCLAIMER}"
        adjustments.append({"type": "disclaimer_appended"})
    return report_markdown, adjustments
