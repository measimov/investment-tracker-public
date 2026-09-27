"""LLM 标的分析的 prompt 组装（DeepSeek JSON mode，一次产出标签+全文）。

guardrail 与复盘报告同一哲学并更严格：分析对象是具体上市公司，模型对
公司的先验知识（新闻、事件、口碑）一概不得引入——"合规污点"判断只能
来自输入中的客观风险信号（审计意见、股权质押、股东增减持、限售解禁）。
"""

import json

from typing import Any, Dict, List

from .graham_screen import GRAHAM_CRITERIA_NAMES_ZH, GRAHAM_VERDICT_LABELS_ZH
from .prompt_guardrails import NO_PRIOR_KNOWLEDGE_CLAUSE

ANALYSIS_DISCLAIMER = "本分析由 AI 基于公开结构化数据自动生成，仅供参考，不构成投资建议。"

ALLOWED_TAGS = [
    "高股息", "分红连续", "分红中断", "业绩增长", "业绩下滑", "业绩预警",
    "高质押", "大股东减持", "大股东增持", "解禁临近", "审计非标", "估值偏高",
    "估值偏低", "数据不足",
    # 利润质量层（触发语义见 earnings_quality.metric_semantics 红旗阈值）
    "利润质量存疑", "现金流背离", "依赖非经常损益",
    # 格雷厄姆×塔勒布框架层（依据 graham_screen 预计算结果，不得自行心算；
    # 估值准则 indeterminate 时禁用安全边际两标签——无估值数据谈不上边际）
    "安全边际充足", "安全边际不足", "财务强度高", "高杠杆脆弱", "净现金充裕",
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
        "与 earnings_quality 指标；**港股年报未必设有「主要風險」章节**"
        "（实测多数没有），摘要里没有风险内容时如实写'年报未披露专门风险章节'，"
        "不得推测。以下标签**禁止使用**：高质押、大股东减持、大股东增持、"
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
        "质押/增减持数据源、结构化科目以年报/中报原文抽取为准，且年报若未设风险章节须写明）"
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


def build_system_prompt(market: str) -> str:
    """按市场组装 system prompt：共享守则骨架 + 市场差异段。"""
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
MARKET_MIN_RISK_LEVEL: Dict[str, str] = {"港股": "medium"}
MARKET_RISK_FLOOR_REASON: Dict[str, str] = {
    "港股": "港股数据边界：无审计意见/质押/增减持等客观风险信号源，「无明显风险信号」不成立",
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
    "pe", "pb_or_product", "current_ratio", "lt_debt_vs_net_current_assets",
)


# 估值方法按**市场**确定（不靠 basis 里有没有某个字段猜）：A股 = Tushare 快照；港股/美股 =
# 行情价 ÷ 报表推算的估计值。graham_screen 也在 basis 里写了 `valuation_method`，市场未知
# （调用方没传 market）时才用它。PR #232 评审：此前按「basis 有没有 price」区分，而 A股
# 快照的 basis 也带 price（快照收盘价），A股 的安全边际充足被全部误拒
VALUATION_METHOD_BY_MARKET: Dict[str, str] = {
    "A股": "snapshot", "港股": "estimated", "美股": "estimated",
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


def valuation_data_sufficient(
    criterion: Dict[str, Any] | None, market: str | None = None
) -> bool:
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
    items = {
        item.get("criterion"): item for item in graham_screen.get("criteria") or []
    }
    if not all((items.get(key) or {}).get("verdict") == "pass" for key in _MARGIN_OF_SAFETY_CRITERIA):
        return False
    return all(
        valuation_data_sufficient(items.get(key), market) for key in ("pe", "pb_or_product")
    )


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
    report_markdown = report.strip()
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
    }
