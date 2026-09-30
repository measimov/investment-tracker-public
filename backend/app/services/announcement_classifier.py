"""公告分类（纯函数，#306）：标题规则为主，官方分类（巨潮 announcementType 代码、披露易
LONG_TEXT 方括号类别、EDGAR form 与 8-K items）为辅。

规则来自 2026-09 对跟踪范围 46 只标的近 180 天官方公告的实测（巨潮 1477 条、披露易 755 条）：
港股标题为繁体，先经 zhconv 转简体再走同一套正则。规则按顺序匹配、先中先得，所以顺序即优先级
（例：「发行股份及支付现金购买资产」须先判重组再判融资；「回购股份的说明函件」是股东大会一般
授权的例行文件，须先于回购判治理）。

改规则 → bump ANNOUNCEMENT_CLASSIFIER_VERSION → `manage.py reclassify-announcements`（零外呼）。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

ANNOUNCEMENT_CLASSIFIER_VERSION = 1

# 归类 → 中文名（前端与推送文案共用；顺序即详情页筛选项顺序）
CATEGORIES = {
    "financing": "融资",
    "restructuring": "重组/收购",
    "earnings_alert": "业绩预告/快报",
    "suspension": "停复牌/风险警示",
    "legal": "诉讼/处罚/监管",
    "buyback": "回购",
    "shareholding": "增减持/权益变动",
    "incentive": "股权激励",
    "dividend": "分红",
    "periodic": "定期报告",
    "management": "董事/高管变动",
    "governance": "股东会/治理",
    "other": "其他",
}
IMPORTANCE_ORDER = {"major": 2, "normal": 1, "minor": 0}

try:  # 港股繁体标题 → 简体；zhconv 在 requirements 内，缺库时退化为原文（仅港股召回下降）
    import zhconv

    def _to_simplified(text: str) -> str:
        return zhconv.convert(text, "zh-cn")
except ImportError:  # pragma: no cover

    def _to_simplified(text: str) -> str:
        return text


@dataclass(frozen=True)
class Classification:
    category: str
    importance: str
    rule_id: str


@dataclass(frozen=True)
class _Rule:
    rule_id: str
    category: str
    importance: str
    title: Optional[re.Pattern] = None
    official: Optional[re.Pattern] = None
    exclude: Optional[re.Pattern] = None
    markets: Optional[Tuple[str, ...]] = None
    importance_fn: Optional[Callable[[str], str]] = None


def _re(pattern: str) -> re.Pattern:
    return re.compile(pattern)


# 融资里的例行文件（募集资金存放报告、中介核查意见等）不是「发行事件」
_ROUTINE_DOC = r"募集资金.{0,12}(存放|使用|管理)|专项报告|鉴证|核查意见|法律意见|保荐书|上市保荐|承销总结|律师|会计师事务所|合规性的报告|证券股份有限公司关于|证券有限责任公司关于"
_DEBT = r"公司债|企业债|中期票据|短期融资券|超短期融资券|债务证券|票据计划|绿色.{0,6}债券|科技创新.{0,4}债券|定向债务融资|资产支持"


def _buyback_importance(title: str) -> str:
    if re.search(r"方案|预案|计划|议案|建议|授权", title) and not re.search(
        r"进展|实施结果|结果|完成|注销", title
    ):
        return "major"
    return "minor"


def _shareholding_importance(title: str) -> str:
    if re.search(r"计划|预披露|拟", title) and re.search(r"减持|增持", title):
        return "major"
    if re.search(r"要约|控制权|控股股东.{0,6}变更|实际控制人.{0,6}变更", title):
        return "major"
    return "normal"


def _progress_is_normal(title: str) -> str:
    return "normal" if re.search(r"进展|补充|更正|延期", title) else "major"


def _procedural_is_normal(title: str) -> str:
    return (
        "normal"
        if re.search(r"延长|有效期|授权董事会|相关事宜|文件更新|申请文件", title)
        else "major"
    )


def _approval_is_major(title: str) -> str:
    return (
        "major"
        if re.search(r"批复|同意注册|注册生效|审核通过|获得.{0,10}(核准|注册)", title)
        else "normal"
    )


# 规则表：顺序即优先级
RULES: Tuple[_Rule, ...] = (
    # 例行治理文件先截走：股东大会一般授权的「回购股份的说明函件」、通函、代表委任表格
    # 港股例行申报：翌日披露报表（含购回的算回购进展）、证券变动月报表、公司通讯发布通知
    _Rule("hk.returns.buyback", "buyback", "minor", title=_re(r"翌日披露报表.{0,20}购回")),
    _Rule(
        "hk.returns",
        "governance",
        "minor",
        title=_re(r"翌日披露报表|证券变动月报表|月报表|通知信函|回条|刊发通知|公司通讯"),
    ),
    _Rule(
        "governance.general_mandate",
        "governance",
        "minor",
        title=_re(
            r"一般性?授权|说明函件|代表委任表格|通函|更改.{0,6}登记|翌日披露报表.{0,6}已发行股份变动$"
        ),
    ),
    _Rule(
        "suspension",
        "suspension",
        "major",
        title=_re(r"停牌|复牌|暂停买卖|恢复买卖|风险警示|\*ST|退市|终止上市|除牌|撤销.{0,4}警示"),
        official=_re(r"\[停牌\]|\[复牌\]|\[除牌\]"),
    ),
    _Rule(
        "legal",
        "legal",
        "major",
        title=_re(
            r"诉讼|仲裁|行政处罚|立案|监管函|警示函|问询函|关注函|纪律处分|监管措施|处罚决定|违规|调查通知|判决|裁决"
        ),
        exclude=_re(r"未被|不存在|审核问询函|问询函的回复|问询函回复|回复的提示性公告"),
    ),
    _Rule(
        "earnings_alert",
        "earnings_alert",
        "major",
        title=_re(
            r"业绩预告|业绩快报|盈利警告|盈利预警|盈利预喜|正面盈利|负面盈利|预增|预减|预亏|扭亏|业绩预计"
        ),
        official=_re(r"\[盈利警告\]|(^|\|\|)012111\b"),
    ),
    _Rule(
        "restructuring",
        "restructuring",
        "major",
        title=_re(
            r"重大资产重组|资产重组|购买资产|出售资产|吸收合并|合并|收购|须予披露的交易|主要交易|非常重大|反收购|分拆|资产置换|股权收购|收购.{0,10}股权"
        ),
        official=_re(r"\[须予披露的交易\]|\[主要交易\]|\[非常重大|\[收购"),
        exclude=_re(_ROUTINE_DOC + r"|审核报告|减值测试|业绩承诺.{0,6}实现"),
        importance_fn=_progress_is_normal,
    ),
    # 控股股东发行可交换债是股东层面的融资（以所持股份为标的），不是公司融资
    _Rule(
        "shareholding.exchangeable", "shareholding", "normal", title=_re(r"可交换公司债券|可交换债")
    ),
    # 发行过程文件（交易所问询、审核意见、注册批复）：批复/同意注册是节点，其余为过程
    _Rule(
        "financing.review",
        "financing",
        "normal",
        title=_re(
            r"(发行|可转换|可转债|债券|股票).{0,40}(问询函|审核中心|上市委员会|注册批复|同意注册|注册生效|予以注册|批复|受理)|(问询函|批复|同意注册).{0,20}(发行|可转换|可转债)"
        ),
        exclude=_re(r"激励|限制性|持股计划|注册资本"),
        importance_fn=_approval_is_major,
    ),
    _Rule(
        "financing.convertible",
        "financing",
        "major",
        title=_re(r"可转换公司债券|可转债|可换股|可转换债券|可转换为"),
        exclude=_re(_ROUTINE_DOC + r"|持有人会议规则|转股价格.{0,4}调整|付息|赎回|回售|转股结果"),
        importance_fn=_procedural_is_normal,
    ),
    _Rule(
        "financing.debt",
        "financing",
        "normal",
        title=_re(_DEBT),
        official=_re(r"\[发行债务证券\]|债务证券"),
    ),
    _Rule(
        "financing.equity",
        "financing",
        "major",
        title=_re(
            r"向特定对象发行|向不特定对象发行|非公开发行|定向增发|配股|配售|供股|发行股份|发行A股|发行H股|增发|募集说明书|发行结果|上市公告书"
        ),
        official=_re(r"\[配售\]|\[供股\]|\[根据一般性授权发行股份\]"),
        exclude=_re(_ROUTINE_DOC + r"|限制性股票|激励|持股计划|股份奖励|已发行股份变动"),
        importance_fn=_procedural_is_normal,
    ),
    _Rule(
        "buyback",
        "buyback",
        "minor",
        title=_re(r"回购|购回|股份购回"),
        official=_re(r"\[股份购回\]"),
        exclude=_re(r"回购价格|回购注销|限制性股票"),
        importance_fn=_buyback_importance,
    ),
    _Rule(
        "shareholding",
        "shareholding",
        "normal",
        title=_re(
            r"减持|增持|权益变动|持股5%|股份质押|质押|解除质押|冻结|(控股股东|实际控制人).{0,10}(增持|减持|质押|冻结|变更|转让|捐赠|一致行动)|要约收购|举牌|询价转让"
        ),
        importance_fn=_shareholding_importance,
    ),
    _Rule(
        "incentive",
        "incentive",
        "normal",
        title=_re(
            r"股权激励|限制性股票|股票期权|购股权|持股计划|股份奖励|股份计划|激励计划|解除限售|归属"
        ),
        official=_re(r"\[股份计划\]|\[根据特定授权发行股份\]"),
        exclude=_re(r"律师|法律意见|核查意见"),
    ),
    _Rule(
        "dividend",
        "dividend",
        "normal",
        title=_re(r"利润分配|分红|派息|股息|权益分派|分派|除权|除净|末期股息|中期股息|现金红利"),
        official=_re(r"\[股息或分派"),
        exclude=_re(r"律师|法律意见|核查意见"),
    ),
    _Rule(
        "periodic",
        "periodic",
        "normal",
        title=_re(
            r"年度报告|半年度报告|季度报告|年报|中期报告|季度业绩|中期业绩|末期业绩|全年业绩|业绩公布|业绩公告|营运数据|运营数据"
        ),
        official=_re(r"\[年报\]|\[中期/半年度报告\]|\[季度业绩\]|\[中期业绩\]|\[末期业绩\]"),
        exclude=_re(r"摘要|说明会|投资者|ESG|社会责任|环境、社会|可持续发展"),
    ),
    _Rule("periodic.summary", "periodic", "minor", title=_re(r"报告摘要|业绩说明会")),
    _Rule(
        "management",
        "management",
        "normal",
        title=_re(
            r"辞职|辞任|离任|聘任|委任|选举|换届|更换|董事长|总经理|总裁|首席执行官|财务总监|高级管理人员|董事会秘书|公司秘书"
        ),
        official=_re(r"\[更换董事|\[更换行政总裁|\[更换公司秘书"),
        exclude=_re(r"候选人声明|提名人声明|述职|薪酬|管理办法|管理制度|议事规则|工作细则|对照表"),
    ),
    # 经营进展（港股自愿公告、产品获批、重大合同）：不在上面各类里，但通常影响判断
    _Rule(
        "business.update",
        "other",
        "normal",
        title=_re(r"自愿公告|获批|获得.{0,16}批准|批准上市|中标|重大合同|战略合作"),
    ),
    _Rule(
        "governance",
        "governance",
        "minor",
        title=_re(
            r"股东会|股东大会|股东周年大会|股东特别大会|董事会|监事会|决议|章程|制度|规则|议事|法律意见|独立董事|述职|核查意见|鉴证|内部控制|审计报告|社会及管治|ESG|社会责任|可持续发展|说明会|投资者关系|接待|提名|候选人|会议资料|表决结果|董事名单|核数师|担保|理财|关联交易|合规性|发行保荐|关连交易|财务资助|授信|外汇|衍生品|会计政策|会计师事务所|资金占用|风险评估|套期保值|期货|票据池|责任险|管理办法|管理规定|薪酬|债权人|发展规划|质量回报双提升|行动方案|证券变动月报表|翌日披露报表|资金占用"
        ),
        official=_re(
            r"\[股东周年大会|\[股东特别大会|\[董事会召开日期\]|\[修订宪章文件\]|\[持续关连交易\]|\[关连交易\]"
        ),
    ),
)

_DEFAULT = Classification("other", "minor", "other")
# 披露易「內幕消息」未被标题规则认出时至少是 normal（通常涉及价格敏感事项）
_INSIDE_INFO = re.compile(r"\[内幕消息\]")


def normalize_text(text: str) -> str:
    """标题/分类原文 → 比对用文本：HTML 实体解码、繁转简、去空白。"""
    unescaped = html.unescape(text or "").replace("&#x2f;", "/")
    return re.sub(r"\s+", "", _to_simplified(unescaped))


# EDGAR：form 与 8-K items 是结构化的官方分类，比标题可靠，走单独映射（不经标题规则）。
# category_raw 形如 "8-K|2.02,9.01"。未列出的 form 在同步时就不入库（EDGAR_FORMS）。
EDGAR_ITEM_RULES: Tuple[Tuple[str, str, str], ...] = (
    # (item, category, importance)：按严重程度排序，一份 8-K 取第一个命中的
    ("1.03", "legal", "major"),  # 破产或接管
    ("3.01", "suspension", "major"),  # 退市通知/不符合上市标准
    ("4.02", "legal", "major"),  # 已发布财报不可依赖
    ("2.01", "restructuring", "major"),  # 完成收购或处置资产
    ("1.01", "restructuring", "normal"),  # 订立重大协议（常见为授信/合作，不一定是并购）
    ("2.02", "periodic", "normal"),  # 经营业绩与财务状况
    ("5.02", "management", "normal"),  # 董事/高管变动
    ("4.01", "governance", "normal"),  # 更换审计师
    ("2.03", "financing", "normal"),  # 新增重大直接债务
    ("3.02", "financing", "normal"),  # 未登记的股份发行
    ("5.07", "governance", "minor"),  # 股东投票结果
    ("5.03", "governance", "minor"),  # 章程修订
)
EDGAR_FORM_RULES: Tuple[Tuple[str, str, str], ...] = (
    (r"^(10-K|20-F|40-F|10-Q)(/A)?$", "periodic", "normal"),
    (r"^(S-1|F-1|S-3|F-3|S-8|F-3ASR|S-3ASR)(/A)?$|^424B\d*$", "financing", "normal"),
    (r"^SC 13D(/A)?$", "shareholding", "major"),
    (r"^SC 13G(/A)?$", "shareholding", "normal"),
    (r"^(4|144)(/A)?$", "shareholding", "minor"),
    (r"^(DEF 14A|DEFA14A)$", "governance", "minor"),
)


def classify_edgar(form: str, items: str) -> Classification:
    form = (form or "").strip().upper()
    if form.startswith(("8-K", "6-K")):
        item_set = {item.strip() for item in (items or "").split(",") if item.strip()}
        for item, category, importance in EDGAR_ITEM_RULES:
            if item in item_set:
                return Classification(category, importance, f"edgar.8k.{item}")
        return Classification("other", "minor", "edgar.8k.other")
    for pattern, category, importance in EDGAR_FORM_RULES:
        if re.match(pattern, form):
            return Classification(category, importance, f"edgar.{form}")
    return _DEFAULT


def classify(market: str, title: str, category_raw: str = "") -> Classification:
    """(market, 标题, 官方分类原文) → (category, importance, rule_id)。"""
    if market == "美股" and "|" in (category_raw or ""):
        form, _, items = category_raw.partition("|")
        return classify_edgar(form, items)
    text = normalize_text(title)
    official = _to_simplified(html.unescape(category_raw or ""))
    for rule in RULES:
        if rule.markets and market not in rule.markets:
            continue
        hit_title = bool(rule.title and rule.title.search(text))
        hit_official = bool(rule.official and rule.official.search(official))
        if not (hit_title or hit_official):
            continue
        if rule.exclude and rule.exclude.search(text):
            continue
        importance = rule.importance_fn(text) if rule.importance_fn else rule.importance
        return Classification(rule.category, importance, rule.rule_id)
    if _INSIDE_INFO.search(official):
        return Classification("other", "normal", "other.inside_information")
    return _DEFAULT


def group_key(symbol: str, market: str, ann_date: str, category: str) -> str:
    return f"{symbol}|{market}|{ann_date}|{category}"


# 同组代表标题（同级取最早发布：调用方按发布时间升序传入标题）。
# 代表标题的优先级（先中先得）。配套文件的标题也常带「方案」「预案」字样（「…方案的论证分析报告」
# 「…预案披露的提示性公告」），所以先按「以主文件名收尾」判，再按提示性公告，配套报告/规则垫底——
# 生产实测：只按「含预案/方案」判时，安琪 2026-09 可转债组的代表标题落在论证分析报告上。
_REPRESENTATIVE_TIERS: Sequence[re.Pattern] = (
    re.compile(r"(预案|方案|计划|报告书|公告书|募集说明书)(（[^（）]*）)?$"),
    re.compile(r"提示性公告"),
    re.compile(
        r"^(?!.*(论证|可行性|规则|摊薄|意见|核查)).*(预案|方案|计划|公告书|募集说明书|公告$)"
    ),
    re.compile(r"论证|可行性"),
)


def representative_index(titles: Iterable[str]) -> int:
    """一组公告里最能代表事件的那份（索引）。titles 须按发布时间**升序**传入：同级取第一个，
    即最早发布的那份（更正/补充公告晚于原公告）。"""
    items: List[str] = list(titles)
    best, best_rank = 0, len(_REPRESENTATIVE_TIERS) + 1
    for index, title in enumerate(items):
        text = normalize_text(title)
        rank = next(
            (tier for tier, pattern in enumerate(_REPRESENTATIVE_TIERS) if pattern.search(text)),
            len(_REPRESENTATIVE_TIERS),
        )
        if rank < best_rank:
            best, best_rank = index, rank
    return best
