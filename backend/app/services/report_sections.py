"""财报章节抽取（纯函数，无网络/DB——金样测试对象）。

A股年报按证监会准则有标准章节结构：
- 第二节 公司简介和主要财务指标 / **公司业务概要**（主要业务、经营模式、行业情况）
- 第三节 **管理层讨论与分析**（2021 格式修订前为"经营情况讨论与分析"，
  更早为"董事会报告"；含主营构成表、成本分析表、前五客户/供应商占比、风险因素）

抽取采用三级回退并返回 locator 标记（供金样断言与线上观测）：
① 节标题正则；② 目录页码法；③ 关键词窗口保底。
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# 抽取逻辑版本号。**改了定位/评分/边界逻辑就要 bump**——缓存键里只有源指纹，
# 它反映的是"报告本身变没变"，对抽取逻辑的变化完全无感：不 bump 的话修好之后
# 库里那批抽错章节的节选与摘要会永久留着，把修复遮得干干净净。
# v2 = 分档优先级 + 内容置信度守门员 + 真边界（业务概要抽成登记信息页的修复）
# v3 = 中文风险小节抽取（只认独占一行的小节标题，无关键词盲窗兜底）
# v5 = 港股二次上市（20-F 中文翻译版）章节变体：有關本公司的資料 / 營運與
#      財務回顧及前景（实测京东 09618 四期年报全部定位失败的根因）
# v4 = 美股侧：20-F Item 映射、Item 标题行首锚定与空格容忍（交叉引用曾被当成
#      章节起点，iXBRL 又把标题拆成 `ITE M`）、边界优先取真实找到的候选、
#      iXBRL <ix:header> 整块剔除（实测泄漏 18k 字符 XBRL 上下文）、英文特征词
#      评分。**必须与 v3 分开**：v3 只改了中文侧，若共用版本号，#118 合入后
#      已按 v3 缓存的 10-K 节选会直接命中旧缓存，这些美股修复对存量完全无效。
SECTION_EXTRACTOR_VERSION = 5

# 存储上限：纯防御（防解析失控写爆 JSONB），正常章节远不会触及。
# **不是**预算控制点——预算只在 digest 期做一次，见 budget_section。
# 此前 50k 的抽取期硬切造成"双重截断"：digest 取的"尾部"其实是 50k 切点前的
# 内容而非章节真尾，而 风险要点/展望 两个字段恰恰依赖真尾部。
SECTION_STORE_MAX_CHARS = 200_000
TRUNCATION_MARK = "……[已截断]"

# 章节内容置信度阈值：低于此值即推进下一级回退。
# 原判据是 `len < 200`——一个**长度**检查而非**内容**检查，于是几万字符的
# 「公司简介和主要财务指标」（注册地址、股票简称、多年财务指标表）稳稳通过，
# 20/20 份报告的"业务概要"抽的都是这份 boilerplate。
SECTION_MIN_CONFIDENCE = 0.35

# 关键词窗口保底的取样长度（不知道章节边界时的盲窗）
KEYWORD_WINDOW_CHARS = 50_000

# 中文数字节序（一~二十足够覆盖年报节数）
_CN_NUM = "一二三四五六七八九十"

# 目标章节 → 标题候选，**按优先级分档**（档内才比长度）。
# 简繁并列：港股年报用繁体且有变体（及/與分析、業務回顧）。
_CN_SECTION_TITLES: Dict[str, List[List[str]]] = {
    "business": [
        ["公司业务概要", "公司業務概要"],
        [
            "业务概要", "業務概要", "业务回顾", "業務回顧", "主营业务", "主營業務",
            # 港股二次上市的中概股年报是 20-F 的中文翻译版（实测京东 09618）：
            # 业务章节叫「有關本公司的資料」（Item 4 的直译）
            "有關本公司的資料", "有關公司的資料",
        ],
        # 港股多数无独立"业务概要"节，业务与战略写在主席报告（致股东信）里
        ["主席報告", "主席报告"],
    ],
    "mdna": [
        [
            "管理层讨论与分析", "管理層討論及分析", "管理層討論與分析", "管理层讨论及分析",
            # 20-F 翻译版的 MD&A（Item 5 的直译，"與/及""前景/展望"均有变体）
            "營運與財務回顧及前景", "營運及財務回顧及前景",
            "經營及財務回顧及展望", "經營及財務回顧與展望",
        ],
        ["经营情况讨论与分析", "經營情況討論與分析", "管理层讨论", "管理層討論"],
        ["董事会报告", "董事會報告"],
    ],
    # 中文年报没有 10-K 那样的独立风险章节：A股写在 MD&A 里的「可能面对的风险」，
    # 港股是公司条例要求的「主要風險和不確定因素」（董事会报告/MD&A 之下的小节）。
    # 抽不到就返回 None 走正常缺口路径——不能假装这是一个必然存在的章节。
    "risk_factors": [
        ["主要風險和不確定因素", "主要風險及不確定因素", "主要风险和不确定因素"],
        ["可能面对的风险", "可能面對的風險", "主要風險", "主要风险", "風險因素", "风险因素"],
    ],
}

# 「公司简介和主要财务指标」是登记信息页（注册地址/股票简称/多年指标表），
# **不是**业务概要。独立成节：只在 business 缺失时进 digest，且标签必须写明
# 它不是业务概要——贴错标签比缺这一节更糟，LLM 被告知"这是业务概要"就只能
# 从注册地址里硬编商业模式。
_CN_PROFILE_TITLES: List[List[str]] = [
    ["公司简介和主要财务指标", "公司簡介和主要財務指標"],
    ["公司简介", "公司簡介"],
]

# 章节特征词：正向命中提分、反向命中降分并打质量标记
_SECTION_SIGNALS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "business": {
        "positive": (
            "主营业务", "主營業務", "经营模式", "經營模式", "行业情况", "行業情況",
            "核心竞争力", "核心競爭力", "业务回顾", "業務回顧", "产品", "產品",
            "商业模式", "商業模式", "行业格局", "行業格局",
        ),
        "negative": (
            "注册地址", "註冊地址", "办公地址", "辦公地址", "股票简称", "股票簡稱",
            "信息披露媒体", "資訊披露", "联系人和联系方式", "聯繫人",
            "会计师事务所办公地址", "公司网址", "電子信箱",
        ),
    },
    "mdna": {
        "positive": (
            "营业收入", "營業收入", "毛利率", "主营业务分析", "主營業務分析",
            "主营构成", "主營構成", "前五名客户", "前五大客戶", "经营情况",
            "經營情況", "同比", "报告期内", "報告期內", "收益", "分部",
        ),
        "negative": ("本节所述内容详见", "本節所述內容詳見"),
    },
    "company_profile": {"positive": ("股票简称", "股票簡稱", "注册地址"), "negative": ()},
    "risk_factors": {
        "positive": (
            "风险", "風險", "不确定", "不確定", "可能导致", "可能導致",
            "监管", "監管", "竞争", "競爭", "汇率", "匯率",
        ),
        "negative": ("风险管理架构", "風險管理架構", "内部监控", "內部監控"),
    },
}

# 英文特征词（10-K/20-F）。单独一张表而不是并进上面：并进去会把分母撑大，
# 中文报告的命中率被英文词稀释一半，置信度门槛就形同虚设。评分取两套的较高者
# ——中文报告几乎不会命中英文词，反之亦然，无需语言检测。
_SECTION_SIGNALS_EN: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "business": {
        "positive": (
            "our business", "segment", "products", "customers", "competition",
            "operations", "revenue", "market",
        ),
        "negative": ("table of contents", "incorporated by reference"),
    },
    "mdna": {
        "positive": (
            "results of operations", "compared to", "revenue", "gross margin",
            "operating expenses", "cash flow", "liquidity", "fiscal",
        ),
        "negative": ("incorporated by reference",),
    },
    "risk_factors": {
        "positive": (
            "we may", "could adversely", "risks relating", "our business",
            "regulatory", "competition", "uncertain",
        ),
        "negative": ("table of contents",),
    },
}

# "节/章"两种节级命名并存（招行等用"第X章"，实测 2026-08-03）
_SECTION_HEAD_RE = re.compile(
    rf"^\s*第\s*[{_CN_NUM}]+\s*[节章]\s+(?P<title>\S[^\n]*)$", re.MULTILINE
)


@dataclass
class SectionResult:
    text: str
    locator: str  # section_title | toc_pages | keyword_window
    truncated: bool
    confidence: float = 1.0
    quality_flags: Tuple[str, ...] = ()

    @property
    def chars(self) -> int:
        return len(self.text)


def score_section(name: str, text: str) -> Tuple[float, List[str]]:
    """章节内容置信度 0-1 + 质量标记（纯函数，金样测试的一等对象）。

    回退链的推进判据。`boilerplate_profile` 标记专门锁住"业务概要抽成公司
    登记信息页"这个缺陷：正向命中率极低且负向特征密集时判定抽错了。
    """
    variants = [
        table[name] for table in (_SECTION_SIGNALS, _SECTION_SIGNALS_EN) if name in table
    ]
    if not variants or not text:
        return 1.0, []
    # 中英各评一次取高者：中文报告几乎不命中英文词，反之亦然
    scored = [_score_with(signals, text) for signals in variants]
    return max(scored, key=lambda item: item[0])


def _score_with(
    signals: Dict[str, Tuple[str, ...]], text: str
) -> Tuple[float, List[str]]:
    sample = text[:20_000].lower()  # 只看头部：章节主题在开头就该显现
    positive = sum(1 for word in signals["positive"] if word.lower() in sample)
    negative = sum(1 for word in signals["negative"] if word.lower() in sample)
    total_positive = len(signals["positive"]) or 1

    flags: List[str] = []
    hit_ratio = positive / total_positive
    score = min(1.0, hit_ratio * 3)  # 命中 1/3 特征词即满分
    if negative:
        score = max(0.0, score - negative * 0.15)
    if hit_ratio < 0.12 and negative >= 3:
        flags.append("boilerplate_profile")
        score = 0.0
    if positive == 0:
        flags.append("no_positive_signal")
    return round(score, 3), flags


def _truncate(text: str) -> Tuple[str, bool]:
    """仅防御性存储上限；正常章节原样返回（预算控制在 digest 期做）。"""
    text = text.strip()
    if len(text) <= SECTION_STORE_MAX_CHARS:
        return text, False
    return text[:SECTION_STORE_MAX_CHARS] + TRUNCATION_MARK, True


def _find_section_by_headers(text: str, title_tiers: List[List[str]]) -> Optional[str]:
    """① 节标题正则：定位目标节标题至下一个'第X节'标题。

    **按优先级分档，档内才比长度**。原实现把所有候选拍平后 `max(key=len)`，
    长度覆盖了优先级：旧格式年报里「第二节 公司简介和主要财务指标」（含多年
    财务指标表、极长）会压过「第三节 公司业务概要」，于是业务概要永远抽不到。

    目录行（同行带页码点线 `……12`）在候选阶段就滤掉。
    """
    matches = list(_SECTION_HEAD_RE.finditer(text))
    if not matches:
        return None
    for titles in title_tiers:
        candidates = []
        for index, match in enumerate(matches):
            title_line = match.group("title").strip()
            if not any(title in title_line for title in titles):
                continue
            if re.search(r"[.…]{2,}\s*\d+\s*$", title_line):
                continue  # 目录行
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            candidates.append(text[start:end])
        if candidates:
            # 档内取最长者（滤掉目录残段）；**不下探低优先级档**
            return max(candidates, key=len)
    return None


# 年报顶层章节标题全集：用于"独立标题行"定位时判定**下一节从哪开始**。
# 港股年报无「第X节」编号，标题直接独占一行；A股 的节标题行也常被页码或
# 页眉公司名污染（实测招行：`18 第三章 管理层讨论与分析`）。
_BOUNDARY_TITLES: Tuple[str, ...] = (
    # A股
    "公司简介和主要财务指标", "公司业务概要", "管理层讨论与分析", "经营情况讨论与分析",
    "重要事项", "股份变动及股东情况", "优先股相关情况", "董事、监事、高级管理人员",
    "公司治理", "环境和社会责任", "财务报告", "备查文件目录", "董事会报告", "监事会报告",
    # 港股（繁体）
    "公司資料", "財務概要", "主席報告", "管理層討論及分析", "管理層討論與分析",
    # 港股二次上市（20-F 中文翻译版）的顶层章节。**标题必须取自真实固件的
    # 独立行**（_heading_occurrences 是整行精确匹配）：此前按 20-F 英文目录
    # 猜成「董事及高級管理人員」「主要股東」，一个都没命中，MD&A 越过下一章
    # 一路吃到「財務資料」，把治理/薪酬/持股 20k 字符当成 MD&A 缓存
    "有關本公司的資料", "營運與財務回顧及前景", "風險因素概要",
    "董事、高級管理人員和員工", "主要股東及關聯交易", "財務資料",
    "業務回顧及展望", "業務回顧", "董事及高級管理層", "董事會報告", "企業管治報告",
    "環境、社會及管治報告", "獨立核數師報告", "綜合收益表", "綜合全面收益表",
    "綜合財務狀況表", "綜合權益變動表", "綜合現金流量表", "綜合財務報表附註",
    "五年財務概要", "釋義",
)
_BOUNDARY_TITLE_SET = frozenset(_BOUNDARY_TITLES)


# 双语年报把中英标题排在同一行（`管理層討論與分析 MANAGEMENT DISCUSSION AND ANALYSIS`）
_TRAILING_ASCII_RE = re.compile(r"[\sA-Za-z&,'’.\-–—/()]+$")


def _normalize_heading_line(raw: str) -> str:
    """剥掉页码前后缀与并排的英文译名，得到候选标题。"""
    text = re.sub(r"^\d{1,4}\s+", "", raw.strip())
    text = re.sub(r"\s+\d{1,4}$", "", text).strip()
    text = _TRAILING_ASCII_RE.sub("", text).strip()
    return text


def _heading_occurrences(line_normalized: str) -> List[Tuple[int, int, str]]:
    """所有"独立标题行"出现位置 [(start, end, title)]，按位置升序。

    独立标题行 = 整行（剥掉页码与并排英文译名后）**恰好等于**某个顶层章节名。
    用 == 而非 endswith：`主要業務及業務回顧` 这种小节标题不是顶层边界，
    endswith 会把它误判成新章节从而把正文切碎。

    港股把章节名作为**页眉**在该节每一页重复，因此同名连续出现是正常的，
    判定下一节边界时必须找**不同**标题。
    """
    occurrences: List[Tuple[int, int, str]] = []
    for match in re.finditer(r"^[ \t]*(?P<line>\S[^\n]*)$", line_normalized, re.MULTILINE):
        stripped = _normalize_heading_line(match.group("line"))
        if not stripped or len(stripped) > 24:
            continue
        if stripped in _BOUNDARY_TITLE_SET:
            occurrences.append((match.start("line"), match.end("line"), stripped))
    return occurrences


def _toc_zone_end(occurrences: List[Tuple[int, int, str]], total_chars: int) -> int:
    """目录区末尾偏移。

    目录里每个章节名同样独占一行，直接按标题行定位会把整个目录当成正文，
    切出来的"章节"其实是目录残段（实测 02156 只切到 2335 字符）。
    判据是**标题不重复**而非间隔：目录里每个章节名只列一次且顺序排列；正文页眉
    则是同名反复出现。只按间隔判定会让港股的每页页眉把簇一路延伸到正文中段
    （实测 00700 一路吞到 26043 字符，正好盖掉了 MD&A 的真实起点）。
    """
    head_limit = max(total_chars // 7, 4_000)
    seen: set = set()
    toc_end = 0
    for start, end, title in occurrences:
        if start > head_limit:
            break
        if title in seen:
            break  # 出现重复 → 目录结束，进入正文（页眉重复）
        if seen and start - toc_end > 1_500:
            break  # 间隔过大 → 不是连续的目录清单
        seen.add(title)
        toc_end = end
    return toc_end if len(seen) >= 5 else 0


def _find_section_by_bare_heading(
    line_normalized: str, titles: List[str], *, body_start: int
) -> Optional[str]:
    """①.5 独立标题行：无「第X节」编号时的确定性定位。

    起点取正文区首个命中标题；终点取其后**首个不同**顶层标题（跳过同名
    页眉重复）。相比关键词窗口的 50k 盲窗，这里边界是真实的。
    """
    occurrences = _heading_occurrences(line_normalized)
    if not occurrences:
        return None
    # 目录区里每个章节名同样独占一行，必须先跳过，否则切出的是目录残段
    toc_end = _toc_zone_end(occurrences, len(line_normalized))
    body_start = max(body_start, toc_end)
    for index, (start, end, title) in enumerate(occurrences):
        if start < body_start:
            continue  # 目录区
        if not any(target in title for target in titles):
            continue
        # 命中的可能是所属章节里的一个小节标题（港股 `業務回顧及展望` 在
        # `主席報告` 章内），此时重复的页眉写的是章节名而非它自己
        skip = _running_header_titles(occurrences, start, toc_end)
        section_end = len(line_normalized)
        for next_start, _, next_title in occurrences[index + 1:]:
            if next_title not in skip:
                section_end = next_start
                break
        body = line_normalized[end:section_end]
        if len(body.strip()) >= 200:
            return body
    return None


RISK_SECTION_MAX_CHARS = 20_000

# 小节标题行允许的尾随内容：空白、页码、序号、中英文标点——**不含汉字**
_HEADING_TRAILER = r"[ \t\d.．、,，:：;；()（）\[\]【】\-—–_]{0,8}"


def _find_section_by_subsection_heading(
    line_normalized: str, titles: List[str], *, body_start: int
) -> Optional[str]:
    """小节标题行定位（供风险章节用）：标题必须**独占一行**。

    风险章节不是顶层章节，只能靠小节标题找。但绝不能退化成关键词盲窗：
    「主要风险」作为子串会命中财务报表附注里的收入确认政策——实测招行与海信
    家电都抽出「主要风险和报酬转移给客户」开头的 50k 盲窗，一段会计政策被贴上
    【风险因素】喂给 LLM。宁可返回 None 走缺口路径。

    尾随只允许**页码/编号/标点**（`_HEADING_TRAILER`），不允许任何汉字：
    「任意 0-4 个字符」这种放宽会把独立成行的「主要风险和报酬」重新放进来
    （「和报酬」正好三个字），会计附注又绕回来了。
    """
    occurrences = _heading_occurrences(line_normalized)
    toc_end = _toc_zone_end(occurrences, len(line_normalized))
    start_at = max(body_start, toc_end)
    for title in titles:
        pattern = rf"^[ \t]*{re.escape(title)}{_HEADING_TRAILER}$"
        for match in re.finditer(pattern, line_normalized, re.MULTILINE):
            if match.start() < start_at:
                continue
            end = min(len(line_normalized), match.end() + RISK_SECTION_MAX_CHARS)
            # 与关键词窗口同法跳过本章页眉：风险小节在董事会报告/MD&A 之下，
            # 该章页眉每页重复，见标题即截断只会切出一页残段（实测 02156 只剩 262 字）
            skip = _running_header_titles(occurrences, match.start(), toc_end)
            for next_start, _, next_title in occurrences:
                if next_start <= match.end() or next_title in skip:
                    continue
                end = min(end, next_start)
                break
            body = line_normalized[match.start():end]
            if len(body.strip()) >= 200:
                return body
    return None


def _find_section_by_toc(text: str, titles: List[str]) -> Optional[str]:
    """② 目录页码法：从目录行取起始页码，再取下一目录条目页码为终点，
    按页边界（\x0c 分页符或 pdfplumber 页拼接约定）切片。

    调用方传入的 text 以 \x0c 作为页分隔（pages_to_text 约定）。
    """
    pages = text.split("\x0c")
    if len(pages) < 3:
        return None
    toc_zone = "\x0c".join(pages[:8])  # 目录通常在前几页
    entries = []  # (page_no, title)
    for line_match in re.finditer(
        r"^\s*(?:第\s*[{}]+\s*节\s+)?(\S[^\n.…]*?)[.…\s]{{2,}}(\d{{1,4}})\s*$".format(_CN_NUM),
        toc_zone,
        re.MULTILINE,
    ):
        entries.append((int(line_match.group(2)), line_match.group(1).strip()))
    if not entries:
        return None
    entries.sort()
    start_page = end_page = None
    for position, (page_no, title_text) in enumerate(entries):
        if any(title in title_text for title in titles):
            start_page = page_no
            for next_page, _ in entries[position + 1:]:
                if next_page > page_no:
                    end_page = next_page
                    break
            break
    if start_page is None:
        return None
    # 年报页码≈PDF 页序（封面偏移 1-3 页，向前多取 1 页容错）
    lo = max(0, start_page - 2)
    hi = min(len(pages), (end_page + 1) if end_page else len(pages))
    if lo >= hi:
        return None
    return "\x0c".join(pages[lo:hi])


# 页眉回溯窗口：页眉按定义就在起点近处
_HEADER_LOOKBACK = 5_000


def _running_header_titles(
    occurrences: List[Tuple[int, int, str]], position: int, toc_end: int
) -> frozenset:
    """`position` 所在章节里"不能当作终点"的标题集合。

    两类：① 起点自己所在的那一行标题；② 该章的**页眉** —— 起点之前最近、且在
    起点之后仍会重复出现的标题。港股的小节标题（`業務回顧及展望`）常常独占一行
    而页眉写的是所属章节名（`主席報告`），只跳过①会在下一页页眉处就截断。

    ②只在起点前 `_HEADER_LOOKBACK` 字符内找：页眉按定义就在近处，无限回溯会
    把很久以前的某个标题误当页眉，从而把真正的下一章边界一并跳过。
    """
    lower = max(toc_end, position - _HEADER_LOOKBACK)
    before = [title for start, _, title in occurrences if lower <= start <= position]
    if not before:
        return frozenset()
    after = {title for start, _, title in occurrences if start > position}
    skip = {before[-1]}
    for title in reversed(before):
        if title in after:
            skip.add(title)
            break
    return frozenset(skip)


def _find_section_by_keyword(text: str, keywords: List[str]) -> Optional[str]:
    """③ 关键词窗口保底：正文中首次出现处起取窗口。

    终点优先取其后**首个与关键词所在章节不同**的顶层标题（银行与港股常把业务
    描述写成小节而非独立章节，只有关键词能定位起点，但终点仍可由下一个顶层
    标题确定）；找不到才退回定长盲窗。

    "不同"是关键：港股把章节名作为页眉在该节每一页重复，见标题即截断会把整节
    切成一页残段（实测 00700 的 `業務回顧及展望` 起点后 297 字就又撞上所属章节
    `主席報告` 的页眉，只剩年报第 5 页）。这里与 `_find_section_by_bare_heading`
    同法，先认出关键词落在哪个顶层章节，再跳过它自己的重复页眉。
    """
    normalized = text.replace("\x0c", "\n")  # 等长替换，偏移与 text 一致
    occurrences = _heading_occurrences(normalized)
    toc_end = _toc_zone_end(occurrences, len(normalized))
    for keyword in keywords:
        # 跳过前 3 页（封面/目录），避免命中目录行
        body_start = toc_end
        pages = text.split("\x0c")
        if len(pages) > 3:
            body_start = max(body_start, len("\x0c".join(pages[:3])))
        position = text.find(keyword, body_start)
        if position < 0:
            continue
        skip = _running_header_titles(occurrences, position, toc_end)
        for start, _, title in occurrences:
            if start <= position or title in skip:
                continue  # 未到起点，或本章页眉的重复出现
            return text[position:min(start, position + KEYWORD_WINDOW_CHARS)]
        # 保底窗口仍限长：这是"不知道章节边界在哪"的情况，取无限长
        # 只会把后续所有章节都吞进来
        return text[position:position + KEYWORD_WINDOW_CHARS]
    return None


_CJK_RE = re.compile(r"[一-鿿]")


def cjk_ratio(text: str) -> float:
    """CJK 字符占非空白字符的比例。"""
    compact = re.sub(r"\s", "", text)
    if not compact:
        return 0.0
    return len(_CJK_RE.findall(compact)) / len(compact)


def strip_english_lines(text: str, *, min_cjk_ratio: float = 0.15) -> Tuple[str, bool]:
    """双语年报剔除纯英文行；返回 (文本, 是否判定为双语)。

    港股年报中英同册（实测 02156 中英混排 75 万字符，是单语的两倍）。不剔除
    的话：① token 白白翻倍；② 中文特征词密度被稀释一半，置信度评分与预算装箱
    全部失真。整册几乎无中文（汇丰等纯英文报告）时原样返回，由英文词表处理。

    只做**行级**过滤：pdfplumber 把双栏排版合并成同一行时，该行中英混杂但
    含中文，予以保留——宁可留噪声也不切碎正文。
    """
    if cjk_ratio(text) < 0.05:
        return text, False  # 纯英文报告
    lines = text.split("\n")
    kept = [line for line in lines if not line.strip() or cjk_ratio(line) >= min_cjk_ratio]
    dropped = len(lines) - len(kept)
    # 丢弃行数占比过低说明本来就不是双语册，不做改动（避免误删表格数字行）
    if dropped < len(lines) * 0.15:
        return text, False
    return "\n".join(kept), True


def _flatten(title_tiers: List[List[str]]) -> List[str]:
    return [title for tier in title_tiers for title in tier]


def _body_start_offset(line_normalized: str, *, skip_pages: int = 3) -> int:
    """正文起点（跳过封面/目录若干页）的字符偏移。"""
    pages = line_normalized.split("\n\n")  # 归一后分页符已成 \n，用页数近似
    del pages
    parts = line_normalized.split("\x0c")
    if len(parts) > skip_pages:
        return len("\x0c".join(parts[:skip_pages]))
    # 归一后已无 \x0c：按行数近似跳过前 5% 或 200 行
    lines = line_normalized.split("\n")
    if len(lines) <= 50:
        return 0
    head = min(len(lines) // 20, 200)
    return len("\n".join(lines[:head]))


def _extract_one(
    name: str, line_normalized: str, text: str, title_tiers: List[List[str]]
) -> Optional[SectionResult]:
    """三级回退 + 内容置信度守门员；全部不合格时返回最后一次尝试的低置信结果。"""
    body_start = _body_start_offset(line_normalized)
    if name == "risk_factors":
        # 只认独占一行的小节标题，**不设关键词盲窗兜底**：抽不到就是没披露
        attempts = [(
            "subsection_heading",
            lambda: _find_section_by_subsection_heading(
                line_normalized, _flatten(title_tiers), body_start=body_start
            ),
        )]
    else:
        attempts = [
            ("section_title", lambda: _find_section_by_headers(line_normalized, title_tiers)),
            ("bare_heading", lambda: _find_section_by_bare_heading(
                line_normalized, _flatten(title_tiers), body_start=body_start
            )),
            ("toc_pages", lambda: _find_section_by_toc(text, _flatten(title_tiers))),
            # 关键词窗口依赖原文 \x0c 跳过目录页
            ("keyword_window",
             lambda: _find_section_by_keyword(text, _flatten(title_tiers))),
        ]
    # 风险章节没有"低置信也先用着"这一说：它只有一条定位路径，达不到阈值
    # 就是没定位到。返回低置信结果的话 _ensure_section 照样落库并送去摘要。
    allow_low_confidence = name != "risk_factors"
    fallback: Optional[SectionResult] = None
    for locator, run in attempts:
        extracted = run()
        if not extracted or len(extracted.strip()) < 200:
            continue
        body, truncated = _truncate(extracted)
        confidence, flags = score_section(name, body)
        result = SectionResult(
            text=body, locator=locator, truncated=truncated,
            confidence=confidence, quality_flags=tuple(flags),
        )
        if confidence >= SECTION_MIN_CONFIDENCE:
            return result
        # 不合格：记下最好的一个，继续下探
        if fallback is None or confidence > fallback.confidence:
            fallback = result
    if fallback is not None and allow_low_confidence:
        # 三级全部低置信：如实返回并标记，由调用方决定是否使用
        return SectionResult(
            text=fallback.text, locator=fallback.locator, truncated=fallback.truncated,
            confidence=fallback.confidence,
            quality_flags=tuple([*fallback.quality_flags, "low_confidence"]),
        )
    return None


def extract_cn_sections(text: str) -> Dict[str, Optional[SectionResult]]:
    """A股/港股年报抽取 business + mdna（+ company_profile 兜底）。

    单节失败返回 None 不影响另一节。页首节标题前是分页符 \x0c 而非 \n，
    re.MULTILINE 的 ^ 不认——用等长替换归一（两者均单字符，偏移不变）；
    目录页码法与关键词窗口仍用原文分页。
    """
    # 双语册先剔英文行：否则中文特征词密度减半，置信度评分与预算装箱全失真
    text, bilingual = strip_english_lines(text)
    line_normalized = text.replace("\x0c", "\n")
    results: Dict[str, Optional[SectionResult]] = {}
    for section_name, title_tiers in _CN_SECTION_TITLES.items():
        results[section_name] = _extract_one(
            section_name, line_normalized, text, title_tiers
        )
    # 公司登记信息页：只在业务概要缺失时才有价值，且标签必须如实
    if results.get("business") is None:
        profile = _extract_one(
            "company_profile", line_normalized, text, _CN_PROFILE_TITLES
        )
        if profile is not None:
            results["company_profile"] = profile
    if bilingual:
        for result in results.values():
            if result is not None:
                result.quality_flags = tuple([*result.quality_flags, "bilingual_source"])
    return results


def pages_to_text(page_texts: List[str]) -> str:
    """pdfplumber 逐页文本 → 以 \x0c 分页符拼接（目录页码法依赖此约定）。"""
    return "\x0c".join(page or "" for page in page_texts)


# ---------------------------------------------------------------------------
# 美股 10-K（HTML → 文本 → Item 定位）
# ---------------------------------------------------------------------------

_HTML_BLOCK_TAGS_RE = re.compile(
    r"</?(?:p|div|tr|table|br|h[1-6]|li)[^>]*>", re.IGNORECASE
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
# 整块剔除（**连同内容**）：这些块里的文本不是正文，只剥标签会让它们的
# 文本节点混进来。iXBRL filing 的 <ix:header> 尤其毒——实测 PDD 20-F 剥完
# 标签仍残留 18,245 字符的 XBRL 上下文（`0001737806 2025 FY false 0 0
# http://fasb.org/us-gaap/...`），而且就堆在文档最前面，正好落进 Item 候选段。
_HTML_DROP_BLOCKS_RE = re.compile(
    r"<(script|style|ix:header|ix:hidden)\b[^>]*>.*?</\1>|<!--.*?-->",
    re.IGNORECASE | re.DOTALL,
)
_HTML_ENTITY = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&#160;": " ", "&#8217;": "'", "&#8220;": '"', "&#8221;": '"',
    "&#8211;": "-", "&#8212;": "-",
}

# 目标 Item：起始标题 → 正文标题关键词 → 终止标题候选。
# 20-F（外国私人发行人，中概股几乎全是它）与 10-K 的 Item 编号完全不同：
# 业务在 Item 4 而非 Item 1，MD&A 在 Item 5 而非 Item 7，风险因素是
# Item 3 之下的 D 小节而非独立 Item。套 10-K 的编号只会抽到别的章节。
def _spaced(word: str) -> str:
    """把词拆成"字符间可含空白"的模式。

    iXBRL 把标题拆进多个内联标签，剥标签后就成了 `ITE M 6.` / `IT EM` /
    `OPERATING AND FINAN CIAL`（实测 BABA 20-F 三种变体并存）。写死 `item`
    的正则完全匹配不到下一章标题，MD&A 于是一路吞到文末再被存储上限截断。
    """
    return r"\s*".join(re.escape(ch) for ch in word)


def _spaced_phrase(phrase: str) -> re.Pattern:
    return re.compile(r"\s*".join(_spaced(word) for word in phrase.split()), re.I)


_ITEM = _spaced("item")
_RISK_FACTORS = r"\s*".join((_spaced("risk"), _spaced("factors")))
_US_FORM_ITEMS: Dict[str, Dict[str, Tuple[str, Tuple[str, ...], Tuple[str, ...]]]] = {
    "10-K": {
        "business": (
            rf"^[ \t]*{_ITEM}\s*1\s*[\.:—-]", ("business",),
            (rf"^[ \t]*{_ITEM}\s*1a\s*[\.:—-]", rf"^[ \t]*{_ITEM}\s*2\s*[\.:—-]"),
        ),
        "risk_factors": (
            rf"^[ \t]*{_ITEM}\s*1a\s*[\.:—-]", ("risk factors",),
            (rf"^[ \t]*{_ITEM}\s*1b\s*[\.:—-]", rf"^[ \t]*{_ITEM}\s*2\s*[\.:—-]"),
        ),
        "mdna": (
            rf"^[ \t]*{_ITEM}\s*7\s*[\.:—-]", ("management's discussion", "management s discussion"),
            (rf"^[ \t]*{_ITEM}\s*7a\s*[\.:—-]", rf"^[ \t]*{_ITEM}\s*8\s*[\.:—-]"),
        ),
    },
    "20-F": {
        "business": (
            rf"^[ \t]*{_ITEM}\s*4\s*[\.:—-]", ("information on the company",),
            (rf"^[ \t]*{_ITEM}\s*4a\s*[\.:—-]", rf"^[ \t]*{_ITEM}\s*5\s*[\.:—-]"),
        ),
        # 风险因素在 Item 3.D 之下；先认 "D. Risk Factors" 小节标题，
        # 认不到才退回整个 Item 3（含选录财务数据，噪声但不致命）
        "risk_factors": (
            rf"^[ \t]*[a-e]\s*[\.:]\s*{_RISK_FACTORS}", ("risk factors",),
            (rf"^[ \t]*{_ITEM}\s*4\s*[\.:—-]",),
        ),
        "mdna": (
            rf"^[ \t]*{_ITEM}\s*5\s*[\.:—-]", ("operating and financial review",),
            (rf"^[ \t]*{_ITEM}\s*6\s*[\.:—-]",),
        ),
    },
}
# 20-F 的别名（EDGAR 也会出现 20-F/A 修订版）
_US_FORM_ALIASES = {"20-F/A": "20-F", "10-K/A": "10-K", "10-K405": "10-K"}

US_ITEM_MIN_CHARS = 500



def html_to_text(html: str) -> str:
    """粗剥 HTML（不引新依赖）：先整块剔除非正文块，再块级标签转换行、
    其余剥除、常见实体解码。"""
    text = _HTML_DROP_BLOCKS_RE.sub(" ", html)
    text = _HTML_BLOCK_TAGS_RE.sub("\n", text)
    text = _HTML_TAG_RE.sub(" ", text)
    for entity, replacement in _HTML_ENTITY.items():
        text = text.replace(entity, replacement)
    text = re.sub(r"&#\d+;|&[a-zA-Z]+;", " ", text)
    return re.sub(r"[ \t]{2,}", " ", text)


def _us_item_candidates(
    text: str, lower: str, start_pattern: str, end_patterns: Tuple[str, ...]
) -> List[Tuple[str, bool]]:
    """[(候选正文, 终止边界是否真的找到)]。"""
    candidates: List[Tuple[str, bool]] = []
    for start_match in re.finditer(start_pattern, lower, re.MULTILINE):
        end, bounded = len(text), False
        for end_pattern in end_patterns:
            end_match = re.search(end_pattern, lower[start_match.end():], re.MULTILINE)
            if end_match:
                end = min(end, start_match.end() + end_match.start())
                bounded = True
                break
        candidates.append((text[start_match.start():end], bounded))
    return candidates


def extract_us_items(
    html: str, *, form_type: str = "10-K"
) -> Dict[str, Optional[SectionResult]]:
    """10-K/20-F 抽取 business / risk_factors / mdna。

    Item 标题在目录与正文各出现一次。**不能简单取最长者**——终止标题因排版
    变体匹配不到时 `end` 落到文末，该候选一路吃到文尾必然最长，于是整份
    filing 被当成 Item 1 静默返回（与中文侧那个 `max(candidates, key=len)`
    是同一个病）。这里改为：先只看**边界真的找到**的候选，再在其中按正文
    标题关键词与内容置信度择优。
    """
    form = _US_FORM_ALIASES.get(form_type, form_type)
    specs = _US_FORM_ITEMS.get(form) or _US_FORM_ITEMS["10-K"]
    text = html_to_text(html)
    lower = text.lower()
    results: Dict[str, Optional[SectionResult]] = {}
    for name, (start_pattern, title_hints, end_patterns) in specs.items():
        candidates = [
            (body, bounded)
            for body, bounded in _us_item_candidates(
                text, lower, start_pattern, end_patterns
            )
            if len(body.strip()) >= US_ITEM_MIN_CHARS
        ]
        bounded_only = [body for body, bounded in candidates if bounded]
        # 有真实边界的候选优先；一个都没有才退回无边界候选（并标记）
        pool = bounded_only or [body for body, _ in candidates]
        if not pool:
            results[name] = None
            continue
        # 正文段的开头应紧跟 Item 标题文字，目录残段与交叉引用则不然。
        # 标题文字同样可能被 iXBRL 拆开（`OPERATING AND FINAN CIAL`），
        # 用空格容忍模式匹配而不是子串比对
        patterns = [_spaced_phrase(hint) for hint in title_hints]
        titled = [
            body for body in pool
            if any(pattern.search(body[:300]) for pattern in patterns)
        ]
        body = max(titled or pool, key=len)
        body, truncated = _truncate(body)
        confidence, flags = score_section(name, body)
        if not bounded_only:
            flags = [*flags, "unbounded_item"]
        results[name] = SectionResult(
            text=body, locator="item_heading", truncated=truncated,
            confidence=confidence, quality_flags=tuple(flags),
        )
    return results


# ---------------------------------------------------------------------------
# digest 输入预算：结构感知装箱（唯一的截断点）
# ---------------------------------------------------------------------------

_CN_SUBSECTION_RE = re.compile(
    rf"^\s*([{_CN_NUM}]+)、\s*(?P<title>\S[^\n]{{0,40}})\s*$", re.MULTILINE
)

# 小节权重：0 = 对 digest 九字段零贡献，可整节丢弃
_SUBSECTION_WEIGHTS: Tuple[Tuple[str, int], ...] = (
    ("主营业务分析", 3), ("主營業務分析", 3), ("主营构成", 3), ("主營構成", 3),
    ("经营情况讨论与分析", 3), ("經營情況討論與分析", 3), ("市場回顧", 3),
    ("公司未来发展的展望", 3), ("未來展望", 3), ("展望", 3),
    ("风险因素", 3), ("風險因素", 3), ("可能面对的风险", 3), ("主要風險", 3),
    ("核心竞争力", 2), ("核心競爭力", 2), ("行业格局和趋势", 2), ("行業格局", 2),
    ("业务回顾", 2), ("業務回顧", 2), ("财务回顾", 2), ("財務回顧", 2),
    ("资产及负债状况", 1), ("投资状况", 1), ("非标准审计意见", 1),
    ("募集资金", 0), ("主要控股参股公司", 0), ("主要子公司", 0),
)

OMISSION_TEMPLATE = "……[已省略小节：{names}]……"
INNER_OMISSION_TEMPLATE = "……[「{name}」节选中段省略]……"

# 块内切片下限：更小的切片没有阅读价值，宁可整块丢弃并如实写进省略清单
_MIN_BLOCK_SLICE = 600
# 达到该权重的小节允许切片（低权重块只能整取或丢弃）
_SLICEABLE_WEIGHT = 3
# 真尾兜底额度：结构化路径也必须落在章节真尾（风险要点/展望依赖它）
_TAIL_FLOOR_MIN = 800
_TAIL_FLOOR_RATIO = 10


@dataclass
class BudgetMeta:
    original_chars: int
    kept_chars: int
    strategy: str  # full | structured | head_tail
    omitted_chars: int
    dropped_subsections: Tuple[str, ...] = ()
    sliced_subsections: Tuple[str, ...] = ()


def _subsection_weight(title: str) -> int:
    for keyword, weight in _SUBSECTION_WEIGHTS:
        if keyword in title:
            return weight
    return 1  # 未知小节默认保留


# 章节间的预算权重：digest 九个字段里六个来自 mdna
SECTION_BUDGET_WEIGHTS: Dict[str, int] = {
    "mdna": 3, "business": 2, "risk_factors": 2, "company_profile": 1,
}
SECTION_MIN_BUDGET = 1_200


def share_budget(sizes: Dict[str, int], total: int) -> Dict[str, int]:
    """把**整份报告**的预算按章节权重分给各章节，用不完的回流。

    档位预算是一份报告的上限，不是每节各发一份：三节各发 40k 就是 120k，
    与声明的档位口径差三倍，分档省下的 token 也就无从谈起。

    分配与小节装箱同法：先各保底 `SECTION_MIN_BUDGET`（保底总额超预算时按权重
    末位淘汰），余量按权重注水，装得下的把富余让给还装不下的。
    """
    if not sizes:
        return {}
    ordered = sorted(sizes, key=lambda name: -SECTION_BUDGET_WEIGHTS.get(name, 1))
    while ordered and len(ordered) * SECTION_MIN_BUDGET > total:
        ordered.pop()
    if not ordered:
        return {}
    quota = {name: min(sizes[name], SECTION_MIN_BUDGET) for name in ordered}
    pool = total - sum(quota.values())
    pending = [name for name in ordered if sizes[name] > quota[name]]
    while pending and pool > 0:
        weight_sum = sum(SECTION_BUDGET_WEIGHTS.get(name, 1) for name in pending)
        snapshot = pool
        satisfied = set()
        for name in pending:
            share = snapshot * SECTION_BUDGET_WEIGHTS.get(name, 1) / weight_sum
            need = sizes[name] - quota[name]
            if need <= share:
                quota[name] += need
                pool -= need
                satisfied.add(name)
        if not satisfied:
            for name in pending:
                quota[name] += int(
                    snapshot * SECTION_BUDGET_WEIGHTS.get(name, 1) / weight_sum
                )
            break
        pending = [name for name in pending if name not in satisfied]
    return quota


def _omission_marker(titles: List[str], total: int) -> str:
    names = "、".join(titles[:6])
    if total > 6:
        names = f"{names}等 {total} 项"
    return f"\n{OMISSION_TEMPLATE.format(names=names)}\n"


def _marker_reserve(blocks: List[dict]) -> int:
    """省略标记长度的上界（按最长的 6 个小节名 + 全量计数估算）。

    必须在**装箱之前**预留：标记若是等块占满预算后再追加，
    `budget_section(..., budget=N)` 的结果就会静默超出 N，
    而下游正是拿这个值控制 LLM 输入总量的。
    """
    titles = sorted((block["title"] for block in blocks), key=len, reverse=True)
    return len(_omission_marker(titles, len(blocks) + 1))


def _allocate_quota(candidates: List[dict], total: int) -> Dict[int, int]:
    """给可切片小节分额度：先各保底 `_MIN_BLOCK_SLICE`，余量按权重注水分配，
    用不完的部分回流给仍装不下的块。

    不能贪心（按权重排序、谁在前谁装满）：一份年报里主营业务分析、风险因素、
    经营情况讨论与分析同为权重 3 且往往都超预算，贪心会让排在最前的那个独占
    全部余量，后两个照样整节消失——正是配额要消除的那条静默删除路径。
    """
    quota = {c["order"]: min(len(c["body"]), _MIN_BLOCK_SLICE) for c in candidates}
    pool = total - sum(quota.values())
    pending = [c for c in candidates if len(c["body"]) > quota[c["order"]]]
    while pending and pool > 0:
        weight_sum = sum(c["weight"] for c in pending)
        snapshot = pool
        satisfied = set()
        for candidate in pending:
            need = len(candidate["body"]) - quota[candidate["order"]]
            if need <= snapshot * candidate["weight"] / weight_sum:
                quota[candidate["order"]] += need
                pool -= need
                satisfied.add(candidate["order"])
        if not satisfied:  # 谁都装不满：按权重切分，注水结束
            for candidate in pending:
                quota[candidate["order"]] += int(
                    snapshot * candidate["weight"] / weight_sum
                )
            break
        pending = [c for c in pending if c["order"] not in satisfied]
    return quota


def _fit_block(body: str, limit: int, title: str, *, keep_head: bool) -> str:
    """把单个小节压进 limit（返回长度 <= limit）。

    keep_head=True 保留块内头尾，False 只保留尾部。**整块丢弃是最坏选择**：
    风险因素/展望常常单节就超过整章预算，丢掉它等于丢掉 digest 最依赖的两段。
    """
    if len(body) <= limit:
        return body
    marker = INNER_OMISSION_TEMPLATE.format(name=title)
    if limit <= len(marker) + 200:
        return body[-limit:]  # 额度小到放不下标记时保尾不保头
    room = limit - len(marker)
    if not keep_head:
        return marker + body[-room:]
    head = int(room * 0.6)
    return body[:head] + marker + body[-(room - head):]


def budget_section(text: str, *, budget: int) -> Tuple[str, BudgetMeta]:
    """把章节压到预算内：按小节权重装箱，保持原文顺序。

    两点与旧的"头 24k + 尾 6k"不同：
    1. **省略处写明小节名**。旧的 `……[中段省略]……` 不说省了什么，而 digest
       prompt 要求"原文未提及一律写'原文未提及'"——于是截断会伪装成"公司没
       披露"，静默污染摘要。
    2. head_tail 退化路径的尾部取的是**章节真尾**（此前抽取期先被 50k 硬切，
       digest 再取"尾部"，拿到的其实是切点前的内容，而风险要点/展望恰恰依赖真尾）。
    """
    text = text.strip()
    original = len(text)
    if original <= budget:
        return text, BudgetMeta(original, original, "full", 0)

    matches = list(_CN_SUBSECTION_RE.finditer(text))
    if len(matches) >= 3:
        blocks = []
        for index, match in enumerate(matches):
            start = match.start()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            title = match.group("title").strip()
            blocks.append({
                "order": index, "title": title, "body": text[start:end],
                "weight": _subsection_weight(title),
            })
        preamble = text[: matches[0].start()]
        reserve = _marker_reserve(blocks)
        # 末块单独留额度：只按权重装箱时，一个超预算的高权重块会把余量吃光，
        # 章节真尾（展望/风险结论常在此）连一个字都留不下
        tail_block = blocks[-1]
        tail_floor = min(
            len(tail_block["body"]),
            max(budget // _TAIL_FLOOR_RATIO, _TAIL_FLOOR_MIN)
            if tail_block["weight"] > 0
            else 400,
        )
        remaining = budget - len(preamble) - reserve - tail_floor
        if remaining >= _MIN_BLOCK_SLICE:
            chosen: Dict[int, str] = {}
            dropped: List[dict] = []
            sliced: List[str] = []
            ordinary = sorted(blocks[:-1], key=lambda b: (-b["weight"], b["order"]))
            dropped.extend(b for b in ordinary if b["weight"] <= 0)
            # 可切片（高权重）与只能整取的普通块分开：贪心地"排到谁装满谁"会让
            # 第一个超长高权重块吃光余量，后面同为高权重的风险因素/经营分析照样
            # 整节消失——所以先给每个可切片块留住最小份额，再谈余量怎么分
            sliceable = [b for b in ordinary if b["weight"] >= _SLICEABLE_WEIGHT]
            modest = [b for b in ordinary if 0 < b["weight"] < _SLICEABLE_WEIGHT]
            while sliceable and sum(
                min(len(b["body"]), _MIN_BLOCK_SLICE) for b in sliceable
            ) > remaining:
                dropped.append(sliceable.pop())  # 预算连保底都不够，按优先级末位淘汰
            floors = sum(min(len(b["body"]), _MIN_BLOCK_SLICE) for b in sliceable)
            # 普通块只能整取，用保底之外的余量竞争（整节留下优于切片）
            pool = remaining - floors
            for block in modest:
                if len(block["body"]) <= pool:
                    chosen[block["order"]] = block["body"]
                    pool -= len(block["body"])
                else:
                    dropped.append(block)
            quota = _allocate_quota(sliceable, floors + pool)
            for block in sliceable:
                kept = _fit_block(
                    block["body"], quota[block["order"]], block["title"], keep_head=True
                )
                chosen[block["order"]] = kept
                if len(kept) < len(block["body"]):
                    sliced.append(block["title"])
            remaining = floors + pool - sum(
                len(chosen[b["order"]]) for b in sliceable
            )
            # 末块：有价值时吃掉预留额度 + 前面没用完的余量；零权重（子公司名录
            # 之类）只保住真尾那一小段，余量宁可不用也不喂垃圾进 LLM
            tail_limit = (
                remaining + tail_floor if tail_block["weight"] > 0 else tail_floor
            )
            tail_kept = _fit_block(
                tail_block["body"], tail_limit, tail_block["title"],
                keep_head=tail_block["weight"] > 0,
            )
            if len(tail_kept) < len(tail_block["body"]):
                sliced.append(tail_block["title"])
            chosen[tail_block["order"]] = tail_kept

            parts = [preamble]
            if dropped:
                # 清单放开头而不是结尾——放结尾正文就不再以章节真尾结束
                parts.append(_omission_marker([b["title"] for b in dropped], len(dropped)))
            parts.extend(chosen[order] for order in sorted(chosen))
            body = "".join(parts)
            return body, BudgetMeta(
                original, len(body), "structured", original - len(body),
                tuple(b["title"] for b in dropped), tuple(sliced),
            )

    # 退化：头 + **真尾**（标记同样先占额度，总长不得超预算）
    marker = f"\n{OMISSION_TEMPLATE.format(names='中段')}\n"
    if budget <= len(marker) + 200:
        body = text[-budget:]
        return body, BudgetMeta(original, len(body), "head_tail", original - len(body))
    room = budget - len(marker)
    head = int(room * 0.8)
    body = text[:head] + marker + text[-(room - head):]
    return body, BudgetMeta(original, len(body), "head_tail", original - len(body))
