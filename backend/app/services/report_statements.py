"""年报/中报 PDF 文本 → 三张合并报表的定位与行解析（纯函数，无 I/O、无 DB）。

输入是 pdfplumber `page.extract_text()` 的逐页文本（与 `report_sections` 同一约定），
输出是每张表的**结构化行**：行号 id、标签、附注号、各列数值、上下文行。数字的归一
（科目映射）交给 LLM，但 **LLM 只返回行 id**，数值一律由本模块解析的原文数字求得——
模型没有任何机会编造或改写一个数字（见 `report_statement_prompts`）。

定位策略（按行流而非按页——A 股年报是连续排版，报表可从页中开始）：
- 标题行必须**整行**是报表名（允许 `1、`/`（一）` 编号前缀与 `（續）` 后缀）：
  附註里的「43 綜合現金流量表附註」「(a) 於綜合財務狀況表確認的金額」都不是整行；
- 起始页页首是附註标题（綜合財務報表附註 / NOTES TO …）的候选剔除（附註节里
  常有与报表同名的小节标题，如 00700 2014 年报的「42 綜合現金流量表」）；
- 「財務概要」五年摘要页也会出现 `簡明綜合全面收益表` 整行标题：按**列数**剔除
  （年报正表两列，五年摘要五列；中报损益/现金流四列是合法的）；
- 块从标题行延伸到下一个终止标题（另一张表、母公司报表、权益变动表、附註）或页首
  换成别的报表为止；港股每页重复标题，同类相邻块合并（綜合收益表 + 綜合全面收益表）。

改动本文件的定位/解析逻辑必须 bump `STATEMENT_EXTRACTOR_VERSION`——缓存以它为键，
不 bump 就是修复被自己的缓存遮住（与 `SECTION_EXTRACTOR_VERSION` 同一约定）。
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Sequence, Tuple

# v4：币种+单位分开排版（「美元 千元」「RMB million」）算一个布局单元，且表头布局须与数据行吻合才采信
# v9：括号负数内侧空格、列期末日/重复年份、每股单位提示进 context
# v10：币种包裹的每股金额（「HK$4.889港元」「人民幣0.67元」「(0.1481)港元」）解析为数值、币种进
#      context；独占一行的「基本 Basic」「攤薄」附到其后数值行的 context；页边报告名排进数据行
#      （「二零二零年年報 基 本 …」）时剥掉报告名
# v11：被空格拆开的两位附注号（「現金及現金等價物 2 6 2,105,184 1,815,678」）在列数已确认时粘回
#      再剥（#263：货币资金曾被取成 2 千元，00728 2025 中报 EPS 取成附注号 20）
# v13：03900 2016 年报逗号前断字；已确认列数与附注不足以解释时粘回唯一的前导数字断字
STATEMENT_EXTRACTOR_VERSION = 13
STATEMENT_KINDS = ("income", "balance", "cashflow")

# 报表标题核心（繁/简；港股「綜合」= A股「合并」）。income 同时覆盖损益表与全面收益表
# 「綜合」= 港股 IFRS 口径，「合并」= A股，「合併」= 港股按美国准则编报（京东 09618 用
# 「合併經營狀況及綜合收益表」= statement of operations and comprehensive income）
#
# 第一轮生产实测补上的变体（每条都对应一家持仓公司的真实标题）：
# - 中报前缀「中期簡明綜合…」/「未經審核簡明綜合…」（02156/06049/01023/02313 全部中报）
# - 「合併損益及其他綜合收益表」（00883：「綜合收益」在此处= comprehensive income，不是合并）
# - 「合併綜合收益表」（00728：合併 + 綜合 双前缀）
# - 「合併利潤表」（01133：H 股按中国准则编报，利润表而非损益表）
# - 「合併經營狀況及綜合收益╱（損失）表」（09618 2023：亏损年份标题带 ╱（損失））
_CONSOL = r"(?:綜合|综合|合并|合併)(?:綜合|综合)?"
_COND = r"(?:中期)?(?:未經審核|未经审核)?(?:簡明|简明)?(?:中期)?"
_TITLE_CORE = {
    "income": (
        rf"{_COND}{_CONSOL}(?:中期)?"
        r"(?:經營狀況及綜合收益|经营状况及综合收益|經營狀況|经营状况"
        r"|損益及(?:其他)?(?:全面|綜合)(?:收益|收入)|损益及(?:其他)?(?:全面|综合)(?:收益|收入)"
        r"|全面收益|全面收入|損益|损益|收益|利潤|利润)"
        r"(?:\s*[╱/／]?\s*[（(]?(?:損失|虧損|亏损)[）)]?)?表"
    ),
    "balance": rf"{_COND}{_CONSOL}(?:中期)?(?:財務狀況|财务状况|資產負債|资产负债)表",
    "cashflow": rf"{_COND}{_CONSOL}(?:中期)?(?:現金流量|现金流量)表",
}
_PREFIX = r"(?:\d{1,3}\s*[、.．]?\s*|[（(]?[一二三四五六七八九十]{1,3}[）)]?\s*[、.．]?\s*)?"
# 行尾页码（01995：页眉「綜合全面收益表 82」把页码排在同一行）
_PAGE_NO = r"(?:\s+\d{1,3})?"
# 标题尾缀：（續）/（未經審核）/（未經審計）可叠加，括号允许错位（00883 中报排版出来的
# 「（未經審計（）續）」）；只认这几个词，「（已經審計）」是財務摘要页的表格标题不是正表
_SUFFIX = r"(?:[\s（）()]*(?:續|续|未經審核|未经审核|未經審計|未经审计))*[\s（）()]*"
TITLE_LINE_RE = {
    kind: re.compile(rf"^{_PREFIX}(?P<title>{core}){_SUFFIX}{_PAGE_NO}\s*$")
    for kind, core in _TITLE_CORE.items()
}
# 终止标题：母公司报表、权益变动表、附註起点（都不属于三张合并报表）
_TERMINATOR_RE = re.compile(
    r"^" + _PREFIX + r"(?:"
    r"母公司(?:資產負債|资产负债|利潤|利润|損益|损益|現金流量|现金流量)表"
    # 中国准则年报（01133）里紧随合并报表的母公司报表只叫「資產負債表」「利潤表」「現金流量表」
    # ——不带「合併/綜合」的裸标题整行出现即视为合并报表结束（裸标题本来就不会被认作合并报表）
    r"|(?:資產負債|资产负债|利潤|利润|損益|损益|現金流量|现金流量|財務狀況|财务状况)表"
    rf"|{_COND}{_CONSOL}(?:中期)?(?:權益變動|权益变动|所有者权益变动|股東權益變動|股东权益变动)表"
    r"|(?:綜合|综合|合并|合併)?(?:財務報表|财务报表|中期財務資料|中期财务资料)(?:附註|附注)"
    r"|NOTES TO THE (?:CONSOLIDATED )?FINANCIAL STATEMENTS"
    r")" + _SUFFIX + _PAGE_NO + r"\s*$",
    re.I,
)
_NOTES_HEADER_RE = re.compile(r"附註|附注|NOTES TO", re.I)
_SUMMARY_HEADER_RE = re.compile(r"概要|摘要|Summary|Highlights", re.I)
# 目录页：「28 簡明綜合損益表」这类带页码的目录行与编号标题同形，只能按页首「目錄」排除。
# 页眉可能先占两行（01023 2023 中报：「1 時代集團控股有限公司 … PB」「2023中期報告」之后第三行
# 才是「目錄」），所以看 _page_first_lines 取到的全部页首行（3 行），不只前两行
_TOC_HEADER_RE = re.compile(r"目錄|目录|CONTENTS", re.I)

# 字间空格的标题（00728：「合 併 綜 合 收 益 表」「合 併 權 益 變 動 表」）：连续 ≥3 个单字
# 以单个空格相隔时去掉空格。只压这种「单字链」，表头里「附註 人民幣千元 人民幣千元」的
# 词间空格是列单位的分隔符，必须保留
# 全角破折號「－」（U+FF0D）是空值记号不是文字（#339）：留在字符类里会被「單字鏈」压进科目名
_CJK_CHAR = r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uff0c\uff0e-\uffef]"
_SPACED_CJK_RE = re.compile(rf"{_CJK_CHAR}(?:[ \u3000]{_CJK_CHAR}){{2,}}")


def squash_spaced_cjk(text: str) -> str:
    return _SPACED_CJK_RE.sub(lambda m: m.group(0).replace(" ", "").replace("\u3000", ""), text)


# 空值记号：半角连字符、en/em dash，以及全角破折號「－」与「―」（#339：03900 2019 中报同一页
# 混用「–」与「－」，00799 2016 损益表用「－」——不认时上期值被当成本期、行尾的整行丢失）。
# U+2212「−」单独成 token 时同样是空值；紧贴数字时是负号，由 normalize_minus_sign 先改成「-」
NULL_MARKS = frozenset({"–", "—", "-", "－", "―", "−"})
_NUM = r"\(?-?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?|\(?-?\d+(?:\.\d+)?\)?|[–—\-－―−]"
_VALUES = rf"(?:(?:{_NUM})\s+)*(?:{_NUM})"
_ROW_RE = re.compile(rf"^(?P<label>\S.*?)\s+(?P<values>{_VALUES})\s*$")
_VALUES_ONLY_RE = re.compile(rf"^(?P<values>{_VALUES})\s*$")
_NOTE_TOKEN_RE = re.compile(
    r"^(?:\d{1,2}(?:\([a-z]\))?|\d{1,2}[a-z]|[一二三四五六七八九十]{1,3}[、.．]\d{1,2})$"
)
_NUM_TOKEN_RE = re.compile(rf"^(?:{_NUM})$")
_YEAR_ARABIC_RE = re.compile(r"(?<!\d)(20\d{2}|19\d{2})(?!\d)")
_YEAR_CJK_RE = re.compile(r"([一二三四五六七八九零〇]{4})\s*年")
_CJK_DIGITS = {
    "零": "0",
    "〇": "0",
    "一": "1",
    "二": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
}
_UNIT_PATTERNS = (
    (re.compile(r"百萬|百万|million", re.I), 1_000_000),
    (re.compile(r"千元|千港元|千美元|thousand|'000|’000", re.I), 1_000),
    (re.compile(r"萬元|万元"), 10_000),
)
_CURRENCY_PATTERNS = (
    (re.compile(r"人民幣|人民币|RMB|CNY"), "CNY"),
    (re.compile(r"港幣|港币|港元|HK\$|HKD"), "HKD"),
    (re.compile(r"美元|US\$|USD"), "USD"),
)
_INTERIM_COLUMNS_RE = re.compile(
    r"(三個月|三个月|three months).*(六個月|六个月|six months)", re.I | re.S
)

MIN_ROWS = 6
MAX_BLOCK_PAGES = 6
HEADER_LINES = 12
HEADER_MAX_LINES = 40


@dataclass
class StatementRow:
    row_id: str
    label: str
    note: str
    values: List[Optional[Decimal]]
    context: List[str] = field(default_factory=list)

    def to_payload(self) -> Dict[str, object]:
        return {
            "id": self.row_id,
            "label": self.label,
            "note": self.note,
            "values": [str(v) if v is not None else None for v in self.values],
            "context": list(self.context),
        }


@dataclass
class ParsedStatement:
    kind: str
    page_start: int  # 1-based
    page_end: int
    title: str
    header: List[str]
    unit_multiplier: int
    currency: Optional[str]
    years: List[int]  # 表头解析出的年份（按列顺序，可能为空）
    column_count: int  # 主导列数
    interim_four_columns: bool  # 三个月 + 六个月 四列布局
    rows: List[StatementRow]

    def to_payload(self) -> Dict[str, object]:
        return {
            "kind": self.kind,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "title": self.title,
            "header": list(self.header),
            "unit_multiplier": self.unit_multiplier,
            "currency": self.currency,
            "years": list(self.years),
            "column_count": self.column_count,
            "interim_four_columns": self.interim_four_columns,
            "rows": [row.to_payload() for row in self.rows],
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, object]) -> "ParsedStatement":
        rows = [
            StatementRow(
                row_id=str(item["id"]),
                label=str(item.get("label") or ""),
                note=str(item.get("note") or ""),
                values=[Decimal(v) if v is not None else None for v in item.get("values") or []],
                context=[str(c) for c in item.get("context") or []],
            )
            for item in payload.get("rows") or []
        ]
        return cls(
            kind=str(payload["kind"]),
            page_start=int(payload["page_start"]),
            page_end=int(payload["page_end"]),
            title=str(payload.get("title") or ""),
            header=[str(h) for h in payload.get("header") or []],
            unit_multiplier=int(payload.get("unit_multiplier") or 1),
            currency=payload.get("currency") or None,
            years=[int(y) for y in payload.get("years") or []],
            column_count=int(payload.get("column_count") or 0),
            interim_four_columns=bool(payload.get("interim_four_columns")),
            rows=rows,
        )


# ---------------------------------------------------------------------------- 行流


@dataclass(frozen=True)
class _Line:
    page: int  # 1-based
    index_in_page: int
    text: str


# 只含标题前缀的孤行：「簡明綜合」「中期簡明綜合」「合併」——下一行若能与之拼成报表/终止标题
# 就是被换行拆开的标题（03900 2019 中报把「簡明綜合」单独排一行）
_TITLE_PREFIX_ONLY_RE = re.compile(rf"^{_COND}{_CONSOL}$")


def _line_stream(pages: Sequence[str]) -> List[_Line]:
    stream: List[_Line] = []
    for page_no, page in enumerate(pages, start=1):
        texts = [squash_spaced_cjk(raw.strip()) for raw in (page or "").splitlines()]
        texts = [t for t in texts if t]
        merged: List[str] = []
        i = 0
        while i < len(texts):
            text = texts[i]
            if (
                i + 1 < len(texts)
                and _TITLE_PREFIX_ONLY_RE.match(text)
                and (match_title(text + texts[i + 1]) or _is_terminator(text + texts[i + 1]))
            ):
                merged.append(text + texts[i + 1])
                i += 2
                continue
            merged.append(text)
            i += 1
        for idx, text in enumerate(merged):
            stream.append(_Line(page_no, idx, text))
    return stream


def match_title(line: str) -> Optional[Tuple[str, str]]:
    """整行报表标题 → (kind, title)；否则 None。"""
    for kind, pattern in TITLE_LINE_RE.items():
        found = pattern.match(line)
        if found:
            return kind, found.group("title")
    return None


def _is_terminator(line: str) -> bool:
    return bool(_TERMINATOR_RE.match(line))


def parse_number(token: str) -> Optional[Decimal]:
    text = token.strip()
    if not text or text in NULL_MARKS:
        return None
    if text.count("(") != text.count(")"):
        # 括号不配对（「(1,034,206」）：负号是否成立说不清，宁可缺值也不静默丢负号
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(",", "")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return -value if negative else value


# 被空格拆开的数字（09926 2020：`854,84 3 416,97 5`；01133：`1,648,565,774.6 1`）：pdfplumber
# 把末位数字排到了独立的 word。两种形态的证据强度不同，处理时机也不同：
# - 「两位的千分组 + 空格 + 一位数字」：`854,84` 本身就不是合法数值（千分组必须三位），这是
#   行内的**明确断字证据**，解 token 时就粘回去；
# - 「带千分组的一位小数 + 空格 + 一位数字」：`1,234.5 6` 完全可能是合法的两列（千元/百万元
#   报表里一位小数常见），只有在**已知列数且 token 数多出来**时才粘（PR #207 评审 P1）——
#   `_parse_row_tokens` 不知道列数，所以这一步放在 parse_row 里按列数做
_SPLIT_THOUSANDS_RE = re.compile(r"(\d,\d{2}) (\d)(?![\d,.])")
_SPLIT_COMMA_RE = re.compile(r"(?<![\d,.])(\d{1,3}(?:,\d{3})*)\s+(,\d{3})(?!\d)")
_SPLIT_DECIMAL_RE = re.compile(r"(\d{1,3}(?:,\d{3})+\.\d) (\d)(?![\d,.])")
_SPLIT_LEADING_RE = re.compile(r"^\(?-?\d{1,2}$")
_GROUPED_TAIL_RE = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?$")


# 括号负数的内侧空格（01023 2024「銷售 成本 ( 1,034,206) ( 1,222,076)」）：`_NUM` 的括号不能带
# 空格，不规范化时三种变体都会错——「( 1,034,206)」本期数被吞进标签、上期顶到本期；
# 「(1,034,206 )」同样错列；「( 1,034,206 )」整行不是数字行被丢掉。只收紧紧贴数字的那一侧
_PAREN_OPEN_SPACE_RE = re.compile(r"\([ 　]+(?=-?\d)")
_PAREN_CLOSE_SPACE_RE = re.compile(r"(?<=\d)[ 　]+\)")


def normalize_paren_spaces(line: str) -> str:
    return _PAREN_CLOSE_SPACE_RE.sub(")", _PAREN_OPEN_SPACE_RE.sub("(", line))


# 数学减号 U+2212 紧贴数字（「−1,234」「(−56)」）是负号：换成 ASCII「-」交给 `_NUM`；
# 单独成 token 的「−」留作空值记号（NULL_MARKS）
_MINUS_SIGN_RE = re.compile(r"\u2212(?=\d)")


def normalize_minus_sign(line: str) -> str:
    return _MINUS_SIGN_RE.sub("-", line)


# 币种包裹的每股金额（v10）：港股損益表的每股盈利常把币种写进每个数值格——「HK$4.889港元」「HK$5.692
# 港元」（00148 全部年报）、「人民幣0.67元」（03900）、「人民幣2.02 人民幣1.62元」「RMB2.11 RMB1.68」（02313
# 双语年报，前后缀不齐）、「0.0537美元」「(0.1481)港元」（00799）、「US$0.1084」（00799 2020 中报）。
# `_NUM` 不认这些 token，整行被当成纯文本丢掉，基本/摊薄行消失，模型只能把上方「每股盈利 13」小标题
# 的附注号映射成 EPS。只在**行尾数值串里至少两个 token 带币种**时才解包（每股盈利两期都带），单个
# 「每股面值 HK$0.10」之类的正文不动；币种/单位不丢，写进该行 context（「仙」由构建层 ÷100）
# 「人民幣RMB1.67元」：02313 2020-2023 中报的双语前缀
_WRAP_PREFIX = r"(?:人民幣|人民币)RMB|HK\$|US\$|RMB|人民幣|人民币"
_WRAP_SUFFIX = r"港元|美元|元|港仙|仙"
_WRAP_NUM = r"\(?-?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?|\(?-?\d+(?:\.\d+)?\)?"
_WRAPPED_TOKEN_RE = re.compile(
    rf"^(?P<pre>{_WRAP_PREFIX})?(?P<num>{_WRAP_NUM})(?P<suf>{_WRAP_SUFFIX})?$"
)
# 与数字分开排版的前后缀（「HK$5.692 港元」「人民幣 0.67 元」）先贴回数字；前缀与标签粘连的
# （03900 2020 年报字间空格压缩后的「基本人民幣1.05元」）在前缀前断开
_WRAP_SUFFIX_SPACE_RE = re.compile(rf"(?<=[\d)])[ 　]+(?={_WRAP_SUFFIX}(?:[ 　]|$))")
_WRAP_PREFIX_SPACE_RE = re.compile(rf"(?:^|(?<=[ 　]))({_WRAP_PREFIX})[ 　]+(?=\(?-?\d)")
_WRAP_PREFIX_GLUED_RE = re.compile(
    rf"(?<=[^\s(（])(?<!人民幣)(?<!人民币)({_WRAP_PREFIX})(?=\(?-?\d)"
)
MIN_WRAPPED_TOKENS = 2


def unwrap_currency_amounts(line: str) -> Tuple[str, List[str]]:
    """行尾数值串里的币种包裹 token → 纯数字；返回 (新行, 币种/单位标记)。条件不满足原样返回。"""
    if not re.search(rf"{_WRAP_PREFIX}|{_WRAP_SUFFIX}", line):
        return line, []
    compact = _WRAP_PREFIX_SPACE_RE.sub(r"\1", _WRAP_SUFFIX_SPACE_RE.sub("", line))
    compact = _WRAP_PREFIX_GLUED_RE.sub(r" \1", compact)
    tokens = compact.split()
    plain: List[str] = []
    markers: List[str] = []
    wrapped = 0
    i = len(tokens)
    while i > 0:
        token = tokens[i - 1]
        found = _WRAPPED_TOKEN_RE.match(token)
        if found and (found.group("pre") or found.group("suf")):
            wrapped += 1
            marker = f"{found.group('pre') or ''}…{found.group('suf') or ''}"
            if marker not in markers:
                markers.insert(0, marker)
            plain.insert(0, found.group("num"))
        elif _NUM_TOKEN_RE.match(token):
            plain.insert(0, token)
        else:
            break
        i -= 1
    if wrapped < MIN_WRAPPED_TOKENS:
        return line, []
    return " ".join(tokens[:i] + plain), markers


def glue_split_digits(line: str) -> str:
    """只粘有明确断字证据的形态（畸形两位千分组、逗号前空格）。"""
    previous = None
    while previous != line:
        previous = line
        line = _SPLIT_THOUSANDS_RE.sub(r"\1\2", line)
        line = _SPLIT_COMMA_RE.sub(r"\1\2", line)
    return line


def glue_leading_digits(tokens: List[str], expected: int, *, has_note: bool = False) -> List[str]:
    """「1 4,879,912」只有合并后唯一吻合已确认列数（及附注列）时才粘。"""
    if not expected or len(tokens) <= expected:
        return tokens
    candidates = []
    joinable = 0
    for i in range(len(tokens) - 1):
        if not _SPLIT_LEADING_RE.match(tokens[i]) or not _GROUPED_TAIL_RE.match(tokens[i + 1]):
            continue
        joined = tokens[i] + tokens[i + 1]
        if not _NUM_TOKEN_RE.match(joined):
            continue
        joinable += 1
        glued = tokens[:i] + [joined] + tokens[i + 2 :]
        if len(glued) == expected or (
            not has_note and len(glued) == expected + 1 and _LEADING_NOTE_RE.match(glued[0])
        ):
            candidates.append(glued)
    return candidates[0] if joinable == len(candidates) == 1 else tokens


def glue_decimal_tail(tokens: List[str], expected: int) -> List[str]:
    """按列数粘小数尾数：token 数比已知列数多才尝试，且粘完必须恰好落到 列数 或 列数+1
    （首 token 是附注号）——否则原样返回。列数未知或已吻合的行一律不动；「附注号 + 恰好列数」
    的布局由 parse_row 先行识别，不会走到这里。"""
    if not expected or len(tokens) <= expected:
        return tokens
    glued = _SPLIT_DECIMAL_RE.sub(r"\1\2", " ".join(tokens)).split()
    if len(glued) >= len(tokens):
        return tokens
    if len(glued) == expected:
        return glued
    if len(glued) == expected + 1 and _LEADING_NOTE_RE.match(glued[0]):
        return glued  # 多出的那个是附注号，随后由 _split_leading_note 剥离
    return tokens


def _parse_row_tokens(line: str) -> Optional[Tuple[str, str, List[str]]]:
    """数字行 → (label, note, value_tokens)；非数字行返回 None。
    无标签的合计行（`6 751,766 660,257` / `229,801 196,467`）标签为空。"""
    line = normalize_paren_spaces(normalize_minus_sign(line))
    line = glue_split_digits(unwrap_currency_amounts(line)[0])
    only = _VALUES_ONLY_RE.match(line)
    if only:
        tokens = only.group("values").split()
        if len(tokens) == 1 and _NOTE_TOKEN_RE.match(tokens[0]):
            return None  # 单独一个附注号，不是数字行
        # 无标签合计行「6 751,766 660,257」的首 token 是附注号还是数值，单看这一行分不清
        # （「80 1,200」也是合法的两列数据）：交给 _split_leading_note 按已确认的列数判
        return "", "", tokens
    found = _ROW_RE.match(line)
    if not found:
        return None
    label = found.group("label").strip()
    tokens = found.group("values").split()
    note = ""
    # 标签尾部粘着附注号：「所得稅開支 12(a) (47,448) (45,018)」/「货币资金 七、1 …」
    parts = label.split()
    if len(parts) >= 2 and _NOTE_TOKEN_RE.match(parts[-1]) and not _NUM_TOKEN_RE.match(parts[-1]):
        note, label = parts[-1], " ".join(parts[:-1])
    elif _NOTE_TOKEN_RE.match(label) and not _NUM_TOKEN_RE.match(label):
        note, label = label, ""
    if not tokens:
        return None
    return label, note, tokens


def parse_row(
    line: str, *, expected_columns: int = 0
) -> Optional[Tuple[str, str, List[Optional[Decimal]]]]:
    """数字行 → (label, note, values)。紧贴数值列的纯数字附注号只有在已知列数（多出一个
    token）时才剥离；不知道列数就原样当数值——宁可多一列也不错列。"""
    parsed = _parse_row_tokens(line)
    if parsed is None:
        return None
    label, note, tokens = parsed
    note, tokens = resolve_row_tokens(note, tokens, expected_columns)
    return label, note, [parse_number(t) for t in tokens]


def resolve_row_tokens(note: str, tokens: List[str], expected: int) -> Tuple[str, List[str]]:
    """按已确认的列数把一行的数值 token 定型为 (附注号, 各列 token)。**`_parse_block` 与
    `parse_row` 共用这一个入口**（#341：`glue_decimal_tail` 曾只在 parse_row 里调用，而生产解析
    不经过 parse_row，01133 的尾数粘合在生产上从未生效——测试测的是另一条路径）。顺序：
    1. 标签里已带附注号：不再剥；
    2. 「附注号 + 恰好列数」：列布局已被完整解释，**不得再粘**——先粘会把两个合法金额粘成一个、
       附注号顶成本期金额，两期静默错列（PR #207 评审 P1）；
    3. 按列数粘小数尾数（`glue_decimal_tail`）；
    4. 两位附注号被拆成两个一位数时粘回（`_glue_split_note`）；
    5. 附注仍不能解释列布局时，按列数粘分组金额的前导数字（`glue_leading_digits`），再剥附注。
    """
    if note:
        return note, glue_leading_digits(tokens, expected, has_note=True)
    if expected and len(tokens) == expected + 1 and _LEADING_NOTE_RE.match(tokens[0]):
        return tokens[0], tokens[1:]
    tokens = glue_decimal_tail(tokens, expected)
    tokens = _glue_split_note(tokens, expected)
    if expected and len(tokens) == expected + 1 and _LEADING_NOTE_RE.match(tokens[0]):
        return tokens[0], tokens[1:]
    tokens = glue_leading_digits(tokens, expected)
    return _split_leading_note(tokens, expected)


def _years_in(text: str) -> List[int]:
    years = [int(y) for y in _YEAR_ARABIC_RE.findall(text)]
    for cjk in _YEAR_CJK_RE.findall(text):
        digits = "".join(_CJK_DIGITS.get(ch, "") for ch in cjk)
        if len(digits) == 4:
            years.append(int(digits))
    return years


def _detect_unit(header: Sequence[str]) -> int:
    text = " ".join(header)
    for pattern, multiplier in _UNIT_PATTERNS:
        if pattern.search(text):
            return multiplier
    return 1


def _detect_currency(header: Sequence[str]) -> Optional[str]:
    text = " ".join(header)
    for pattern, code in _CURRENCY_PATTERNS:
        if pattern.search(text):
            return code
    return None


def _column_year_line(header: Sequence[str]) -> Tuple[Optional[int], List[int]]:
    """表头里第一条含 ≥2 个年份的行 = 列年份行（「2025年 2024年」/「二零二五年 二零二四年」）。

    标题日期行不算：00148 全部年报的「For the year ended 31 December 2016 截至二零一六年十二月
    三十一日止年度」是同一个日期的中英双语，裸年份抽出来是 [2016, 2016]，此前把它当列年份，
    上期列（2015）找不到，比较列与比较期行全部缺失。行内的完整日期若都是同一天，就是标题
    日期：去掉后再看剩下的年份；两个不同的完整日期（「2023年6月30日 2023年1月1日」）才是列。"""
    for index, line in enumerate(header):
        text = line
        dates = _full_dates(line)
        if dates and len({value for _, _, value in dates}) == 1:
            for start, end, _ in reversed(dates):
                text = text[:start] + " " + text[end:]
        years = _years_in(text)
        if len(years) >= 2:
            return index, years
    return None, []


def _header_years(header: Sequence[str]) -> List[int]:
    return _column_year_line(header)[1]


def _is_header_like(line: str, years: List[int]) -> bool:
    """年份行/单位行/币种行等表头噪音，不当数字行。"""
    if _YEAR_CJK_RE.search(line):
        return True
    found = _years_in(line)
    if found and all(1990 <= y <= 2100 for y in found):
        stripped = re.sub(r"[\s年月日]", "", line)
        if re.fullmatch(r"(?:20\d{2}|19\d{2}){1,6}", stripped):
            return True
    return False


# ---------------------------------------------------------------------------- 定位


@dataclass
class _Block:
    kind: str
    title: str
    start: int  # 行流索引
    end: int  # 不含
    lines: List[_Line]


def _page_first_lines(stream: Sequence[_Line], idx: int, count: int = 3) -> List[str]:
    page = stream[idx].page
    out = []
    j = idx
    while j >= 0 and stream[j].page == page and stream[j].index_in_page > 0:
        j -= 1
    while j < len(stream) and stream[j].page == page and len(out) < count:
        out.append(stream[j].text)
        j += 1
    return out


def _collect_block(stream: Sequence[_Line], start: int, kind: str) -> _Block:
    title = stream[start].text
    start_page = stream[start].page
    lines: List[_Line] = [stream[start]]
    i = start + 1
    while i < len(stream):
        line = stream[i]
        if line.page - start_page >= MAX_BLOCK_PAGES:
            break
        if _is_terminator(line.text):
            break
        found = match_title(line.text)
        if found and found[0] != kind:
            break
        if found and line.page != start_page and line.index_in_page <= 2:
            # 新一页页首又出现同类标题：结束本块，让下一页作为独立候选再由 locate_statements
            # 按「相邻同类块」并入。否则 02156 中报里当页眉印在业绩公告首页的标题会把真正
            # 的报表页吞进一个没有表头年份的块里，整块被拒后报表页也一并跳过
            break
        if line.index_in_page == 0 and line.page != start_page:
            head = _page_first_lines(stream, i)
            if head and _NOTES_HEADER_RE.search(head[0]) and not match_title(head[0]):
                break
        lines.append(line)
        i += 1
    return _Block(kind=kind, title=title, start=start, end=i, lines=lines)


_SMALL_INT_RE = re.compile(r"^\d{1,2}$")


_FOOTER_LABEL_RE = re.compile(
    r"年度報告|年度报告|年報|年报|中期報告|中期报告|Annual Report|Interim Report", re.I
)


def _is_page_number_row(label: str, note: str, values: List[Optional[Decimal]]) -> bool:
    """页眉/页脚里的页码（「120」或「2025 39」= 年份 + 页码，或「2023年度報告 79」）被当成只有
    一两个值的行。只在页首/页尾那一行判定（调用方保证），正文里的单值行（如每股股息）不受影响。"""
    if label and not (_FOOTER_LABEL_RE.search(label) and len(values) == 1):
        return False
    if note or not values or len(values) > 2 or any(v is None for v in values):
        return False
    ints = [int(v) for v in values if v == v.to_integral_value()]
    if len(ints) != len(values):
        return False
    small = [v for v in ints if 0 < v < 1000]
    years = [v for v in ints if 1990 <= v <= 2100]
    return (len(ints) == 1 and len(small) == 1) or (
        len(ints) == 2 and len(small) == 1 and len(years) == 1
    )


def _is_year_only_row(values: List[Optional[Decimal]]) -> bool:
    """「FOR THE YEAR ENDED 31 DECEMBER 2025」这类双语表头行会被当成只有一个值 2025 的行。"""
    return (
        len(values) == 1
        and values[0] is not None
        and values[0] == values[0].to_integral_value()
        and 1990 <= int(values[0]) <= 2100
    )


_LEADING_NOTE_RE = re.compile(r"^\d{1,2}(?:\([a-z]\))?$")


_SINGLE_DIGIT_RE = re.compile(r"^\d$")


def _glue_split_note(tokens: List[str], expected: int) -> List[str]:
    """两位附注号被 PDF 字距拆成两个一位数（「現 金及現金等價物 2 6 2,105,184 1,815,678」，02313
    2016；00148 2020-2023、00728 2025 中报同形）：token 数比已确认列数**恰好多 2** 且前两个都是
    一位纯数字时粘成一个附注号，交给 `_split_leading_note` 剥掉。列数已确认时多出两个 token
    没有别的合法解释（列数未知不动）；生产全部 34,892 行抽取行里命中 22 行、全部是附注号，
    「列数 + 2」而前两个不是一位数的 43 行不受影响（#263）。"""
    if (
        expected
        and len(tokens) == expected + 2
        and _SINGLE_DIGIT_RE.match(tokens[0])
        and _SINGLE_DIGIT_RE.match(tokens[1])
    ):
        return [tokens[0] + tokens[1], *tokens[2:]]
    return tokens


def _split_leading_note(tokens: List[str], expected: int) -> Tuple[str, List[str]]:
    """附注号常紧贴数值列（「物業、設備及器材 17 149,905 80,185」）。**只认一条判据**：token 数
    比已确认的列数恰好多一个且首 token 是附注号形态（1-2 位整数，可带 (a)）。列数已吻合的行
    一律不猜——「利息收入 80 1,200」在百万元报表里是合法的两列数据，按"其余金额带千分位"
    去剥会把本期 80 当附注、上期 1,200 错位成本期（PR #201 评审 P1）。列数未知时不剥。"""
    if not expected or len(tokens) != expected + 1:
        return "", tokens
    if not _LEADING_NOTE_RE.match(tokens[0]):
        return "", tokens
    return tokens[0], tokens[1:]


# 表头"每列一个币种/单位"的布局单元。同一列的币种与单位可能粘在一起（「人民幣千元」「RMB’000」）
# 也可能分开排版（「美元 千元」「RMB million」）：**一个单位 token 就是一列**，紧邻的纯币种 token
# 是它的修饰，不另计；整行只有纯币种 token 时（京东「附註 人民幣 人民幣 美元」，单位在另一行
# 「（以百萬元計…）」）才按币种 token 计列
_CURRENCY_WORDS = r"人民幣|人民币|港幣|港币|港元|美元|RMB|HK\$|US\$|USD|HKD|CNY"
_UNIT_WORDS = r"千元|百萬元|百万元|萬元|万元|元|'000|’000|million|thousand|millions|thousands"
_CURRENCY_ONLY_RE = re.compile(rf"^(?:{_CURRENCY_WORDS})$", re.I)
# 「人民幣千元」「美元千元」也有「千美元」「百萬美元」（09618 2020 的美元折算列）：币种可前可后
_MAGNITUDE_WORDS = r"千|百萬|百万|萬|万|million|thousand"
_UNIT_BEARING_RE = re.compile(
    rf"^(?:{_CURRENCY_WORDS})?(?:{_UNIT_WORDS})$|^(?:{_MAGNITUDE_WORDS})\s*(?:{_CURRENCY_WORDS})$",
    re.I,
)


def _unit_token_columns(header: Sequence[str]) -> int:
    """表头里币种/单位布局单元的数量（取单元最多的一行）；不足 2 返回 0。
    「附註 美元千元 美元千元」「附註 美元 千元 美元 千元」「Notes RMB million RMB million」都是
    2 列；「附註 人民幣 人民幣 美元」是 3 列（多一列美元折算）。"""
    best = 0
    for line in header:
        tokens = line.split()
        units = sum(1 for token in tokens if _UNIT_BEARING_RE.match(token))
        currencies = sum(1 for token in tokens if _CURRENCY_ONLY_RE.match(token))
        columns = units if units >= 2 else (currencies if units == 0 and currencies >= 2 else 0)
        best = max(best, columns)
    return best


def _expected_columns(
    header: Sequence[str],
    years: List[int],
    counts: Dict[int, int],
    first_tokens: Dict[int, List[str]],
) -> int:
    """已确认的列数，按可靠性依次：
    1. 表头的币种/单位 token 布局（每列一个）——同时覆盖"整表美元计价的两列表"与"人民币两列 +
       美元折算一列"（京东）；
    2. 表头年份数 vs 数字行 token 数的**最小常见值**（少于年份数的每股/单值行与孤例不参与）：
       最小常见值不超过年份数 → 年份数；恰好多 1 且这些行首 token 全是附注形态 → 年份数
       （全行带附注、只有孤零零一条无附注合计的表）；否则最小常见值（真有额外列）；
    3. 都没有 → 最小常见值 / 众数。"""
    floor = len(years) if len(years) >= 2 else 2
    common = [count for count, freq in counts.items() if freq >= 2 and count >= floor]
    if common:
        min_common = min(common)
    elif counts:
        min_common = max(counts, key=lambda k: (counts[k], k))
    else:
        min_common = 0
    unit_columns = _unit_token_columns(header)
    # 表头布局必须与数据行吻合（行 token 数 = 列数，或多一个附注号）才可信；否则分组没法确认，
    # 退回年份/数据行约束——原始 token 数绝不能直接覆盖已知的两列表头
    if unit_columns >= 2 and (not min_common or min_common in (unit_columns, unit_columns + 1)):
        return unit_columns
    if len(years) >= 2:
        if min_common <= len(years):
            return len(years)
        if min_common == len(years) + 1:
            firsts = first_tokens.get(min_common, [])
            if firsts and all(_LEADING_NOTE_RE.match(t) for t in firsts):
                return len(years)
        return min_common
    return min_common


_GROUPED_AMOUNT_RE = re.compile(r"\d{1,3}(?:,\d{3})+")
# 每股指标的单位提示（#223：EPS 以「仙」列示却按元入库）。表内的纯文本行「人民幣仙 人民幣仙」
# （02669 中报 EPS 小表的列单位）、「每股盈利（以每股人民幣列示）」此前只在无标签行的上下文里
# 保留，其后的「基本及攤薄 21.33 23.45」有标签，提示就丢了——现在附到其后各行的 context，
# 直到回到带千分位的金额行为止。标签不改；单位折算由 report_statement_service 读 context 做
_UNIT_HINT_RE = re.compile(r"仙|每股(?!面值)|cents?\b|per share", re.I)
# 整表的金额单位行（京东「（以百萬元計，股份及每股數據除外）」）不是每股单位提示
_TABLE_UNIT_RE = re.compile(r"百萬|百万|千元|千港元|千美元|million|thousand|['’]000", re.I)
UNIT_HINT_MAX_CHARS = 40


# 独占一行的每股盈利类别（00799 2016 年报、02313 双语年报：「基本 Basic」一行，下一行才是
# 「－年度利潤 – For profit for the year 人民幣2.11元 人民幣1.68元」——基本与摊薄两行标签完全相同，
# 只有这一行说明它是哪一类）。附到其后各行的 context，直到下一条类别行或回到千分位金额行
_EPS_KIND_LINE_RE = re.compile(
    r"^(?=.*(?:基本|攤薄|摊薄|basic|diluted))[\s\-–—－]*(?:基本|攤薄|摊薄|基本及攤薄|基本及摊薄)?\s*"
    r"(?:(?<![A-Za-z])(?:Basic|Diluted|Basic and diluted))?\s*[:：]?\s*$",
    re.I,
)
# 币种包裹 token 解包后留在 context 里的原文币种/单位（「HK$…港元」「…港仙」）
WRAPPED_UNIT_CONTEXT = "數值原文帶幣種單位："


def _is_unit_hint(text: str) -> bool:
    return (
        len(text) <= UNIT_HINT_MAX_CHARS
        and bool(_UNIT_HINT_RE.search(text))
        and not _TABLE_UNIT_RE.search(text)
    )


# 页边的报告名排进了数据行（03900 2020 年报：「二零二零年年報 基 本 人民幣1.05元 人民幣0.55元」，
# 字间空格压缩后是「二零二零年年報基本 …」）：
# 中文数字年份让 `_is_header_like` 把整行当表头噪音丢掉，基本 EPS 行就此消失。只在剥掉报告名后
# 剩下的是**带标签、至少两个数值**的数据行时才剥——页脚「二零一九年中期報告 033」不动
_RUNNING_TITLE_RE = re.compile(
    r"^[一二三四五六七八九零〇]{4}\s*年\s*(?:年報|年报|年度報告|年度报告|中期報告|中期报告)\s*"
)


def strip_running_title(line: str) -> str:
    found = _RUNNING_TITLE_RE.match(line)
    if not found:
        return line
    rest = line[found.end() :]
    parsed = _parse_row_tokens(rest)
    if parsed and parsed[0] and len(parsed[2]) >= 2:
        return rest
    return line


def _is_data_row(text: str) -> bool:
    """金额行：至少两个数值列，或含千分位金额；「ENDED 30 JUNE 2025」这类只有一个裸数字
    的表头行不算，年份行（「2025年 2024年」）也不算。"""
    parsed = _parse_row_tokens(text)
    if parsed is None or _is_header_like(text, []):
        return False
    tokens = parsed[2]
    return len(tokens) >= 2 or any(_GROUPED_AMOUNT_RE.search(token) for token in tokens)


def _parse_block(block: _Block) -> ParsedStatement:
    texts = [line.text for line in block.lines]
    # 表头 = 第一条数字行之前的全部行（至少 HEADER_LINES、最多 HEADER_MAX_LINES）：02156 中报把
    # 报表印在业绩公告首页，董事会声明段落把年份/单位行推到第 13 行之后，固定取 12 行看不到
    first_row = next(
        (i for i, text in enumerate(texts[1:HEADER_MAX_LINES], start=1) if _is_data_row(text)),
        HEADER_LINES,
    )
    # 每页的头两行与末两行（页眉/页脚所在：09926 的页脚是「120」+ 公司名两行），用于剔除页码行
    page_edges = set()
    by_page: Dict[int, List[int]] = {}
    for i, line in enumerate(block.lines):
        by_page.setdefault(line.page, []).append(i)
    for offsets in by_page.values():
        page_edges.update(offsets[:2])
        page_edges.update(offsets[-2:])
    header = texts[: max(HEADER_LINES, first_row)]
    years = _header_years(header)
    interim_four = bool(_INTERIM_COLUMNS_RE.search(" ".join(header)))
    # 第一遍：解析全部数字行，拿主导列数（附注号粘列的行会多一列，是少数）
    raw_rows: List[Tuple[str, str, List[str], List[str], int]] = []
    context: List[str] = []
    unit_hints: List[
        str
    ] = []  # 每股单位提示（「人民幣仙 人民幣仙」「每股盈利（以每股港仙列示）」）
    eps_kind: Optional[str] = (
        None  # 独占一行的「基本 Basic」「攤薄」：其后的数值行才是那一类每股盈利
    )
    counts: Dict[int, int] = {}
    first_tokens: Dict[int, List[str]] = {}
    for offset, text in enumerate(texts):
        if offset == 0:
            continue
        text = strip_running_title(text)
        if _is_header_like(text, years):
            continue
        parsed = _parse_row_tokens(text)
        if parsed is None:
            context.append(text)
            context = context[-2:]
            if _is_unit_hint(text) and text not in unit_hints:
                unit_hints.append(text)
            if _EPS_KIND_LINE_RE.match(text):
                eps_kind = text
            continue
        label, note, tokens = parsed
        if not tokens:
            continue
        if any(_GROUPED_AMOUNT_RE.search(token) for token in tokens):
            unit_hints = []  # 回到金额区：每股单位提示到此为止，不附到金额行上
            eps_kind = None
        row_context = list(context) if not label else []
        hints = list(unit_hints)
        if eps_kind and eps_kind not in hints:
            hints.append(eps_kind)
        markers = unwrap_currency_amounts(normalize_paren_spaces(normalize_minus_sign(text)))[1]
        if markers:
            hints.append(WRAPPED_UNIT_CONTEXT + "、".join(markers))
        row_context = hints + [c for c in row_context if c not in hints]
        raw_rows.append((label, note, tokens, row_context, offset))
        counts[len(tokens)] = counts.get(len(tokens), 0) + 1
        first_tokens.setdefault(len(tokens), []).append(tokens[0])
        if label and len(tokens) == 1 and _is_unit_hint(label) and label not in unit_hints:
            # 「本公司普通股權持有人應佔每股盈利（港仙） 11」：小标题带附注号，其后的「基本及攤薄」才是数值行
            unit_hints.append(label)
        if label:
            context = []
    expected = _expected_columns(header, years, counts, first_tokens)
    # 第二遍：剥附注号、剔年份行、定型
    rows: List[StatementRow] = []
    final_counts: Dict[int, int] = {}
    for label, note, tokens, ctx, offset in raw_rows:
        note, tokens = resolve_row_tokens(note, tokens, expected)
        values = [parse_number(t) for t in tokens]
        if _is_year_only_row(values):
            continue
        if offset in page_edges and _is_page_number_row(label, note, values):
            continue
        rows.append(
            StatementRow(
                row_id=f"r{len(rows) + 1}", label=label, note=note, values=values, context=ctx
            )
        )
        final_counts[len(values)] = final_counts.get(len(values), 0) + 1
    column_count = max(final_counts, key=lambda k: (final_counts[k], k)) if final_counts else 0
    return ParsedStatement(
        kind=block.kind,
        page_start=block.lines[0].page,
        page_end=block.lines[-1].page,
        title=block.title,
        header=header,
        unit_multiplier=_detect_unit(header),
        currency=_detect_currency(header),
        years=years,
        column_count=column_count,
        interim_four_columns=interim_four,
        rows=rows,
    )


def _structurally_ok(parsed: ParsedStatement, start_head: List[str], *, report_type: str) -> bool:
    """排除性判定：財務摘要/概要页、目录页、五年概要、列数超限、附註页。不看行数与表头证据，
    续页（页首重复标题、不重复表头）也按这一条判。"""
    if start_head and _SUMMARY_HEADER_RE.search(start_head[0]):
        return False
    if any(_TOC_HEADER_RE.search(line) for line in start_head):
        return False  # 目录页
    distinct_years = len(set(parsed.years))
    if distinct_years >= 5:
        return False  # 五年财务概要
    # 年报正表两列；美国准则口径的港股（京东）损益表三年 + 美元折算列 = 4 列；
    # 中报损益/现金流 三个月+六个月 = 4 列
    max_cols = 4 if report_type == "interim" else max(3, distinct_years + 1)
    if parsed.column_count > max_cols:
        return False
    if start_head and _NOTES_HEADER_RE.search(start_head[0]) and not match_title(start_head[0]):
        return False
    return True


# 中国准则（H 股按 CAS 编报，01133）的列标题不写年份：「本期金額 上期金額」「期末餘額 期初餘額」；
# 01133 2017-2021 年报的利潤表/現金流量表写作「本期發生額 上期發生額」（也有「本年發生額 上年發生額」）
_CAS_PERIOD_CAPTION_RE = re.compile(
    r"(?:本期|上期|本年|上年)(?:金額|金额|發生額|发生额)|期末餘額|期初餘額|期末余额|期初余额"
)


def _header_evidence(parsed: ParsedStatement) -> bool:
    """表头是不是一张正表的表头：整个表头里至少有一个年份（「截至2025年12月31日止年度」
    「2025年\n2024年」分两行也算），且有列年份行或单位/币种之一。

    「列年份」（`parsed.years`，同一行 ≥2 个年份，用于把数值列对到会计期）与「表头有年份
    证据」是两回事：两个年份被抽成两行的合法报表列年份为空，仍是正表，列对应退回按位置。
    02156 中报把报表标题当页眉印在业绩公告首页，那一页有「截至二零二五年六月三十日」却
    既无列年份行也无单位/币种，正文是董事会声明——这是要排除的。"""
    if any(_CAS_PERIOD_CAPTION_RE.search(line) for line in parsed.header):
        return True  # 本期/上期列标题 = 期间证据（列对应按位置：本期在前）
    if not any(_years_in(line) for line in parsed.header):
        return False
    return bool(parsed.years) or parsed.unit_multiplier != 1 or bool(parsed.currency)


def _acceptable(
    parsed: ParsedStatement, start_head: List[str], *, report_type: str, min_rows: int = MIN_ROWS
) -> bool:
    if len(parsed.rows) < min_rows:
        return False
    if not _structurally_ok(parsed, start_head, report_type=report_type):
        return False
    return _header_evidence(parsed)


def _combine(head: ParsedStatement, tail: ParsedStatement) -> ParsedStatement:
    """短首块 + 续页块 → 一张表：表头/年份/单位/币种取首块，行顺延编号，列数按合并后的众数。"""
    rows: List[StatementRow] = []
    counts: Dict[int, int] = {}
    for row in list(head.rows) + list(tail.rows):
        rows.append(
            StatementRow(
                row_id=f"r{len(rows) + 1}",
                label=row.label,
                note=row.note,
                values=list(row.values),
                context=list(row.context),
            )
        )
        counts[len(row.values)] = counts.get(len(row.values), 0) + 1
    return ParsedStatement(
        kind=head.kind,
        page_start=head.page_start,
        page_end=tail.page_end,
        title=head.title,
        header=list(head.header),
        unit_multiplier=head.unit_multiplier,
        currency=head.currency,
        years=list(head.years),
        column_count=max(counts, key=lambda k: (counts[k], k)),
        interim_four_columns=head.interim_four_columns,
        rows=rows,
    )


def locate_statements(
    pages: Sequence[str], *, report_type: str = "annual"
) -> Dict[str, Optional[ParsedStatement]]:
    """逐页文本 → {kind: ParsedStatement|None}。每类取**第一个**合格块，并吞并紧随
    其后（相邻页）的同类块（綜合收益表 + 綜合全面收益表 / 多页資產負債表）。

    页首重复标题会把一张表切成逐页的块（见 `_collect_block`），所以总行数门槛
    （MIN_ROWS）要在**组装完连续块之后**再判：首页只有五个金额行、其余在「（續）」页的
    报表，首块单独看不够行数，先作为「短首块」挂起，等相邻下一页的同类块来了合并后再判；
    结构性排除（无表头年份的公告页、目录页、附註页、五年概要）仍按单块判、不挂起。"""
    stream = _line_stream(pages)
    found: Dict[str, Optional[ParsedStatement]] = {kind: None for kind in STATEMENT_KINDS}
    absorbed_until: Dict[str, int] = {}
    pending: Dict[str, ParsedStatement] = {}  # 结构合格但行数不足的短首块
    i = 0
    while i < len(stream):
        matched = match_title(stream[i].text)
        if not matched:
            i += 1
            continue
        kind, _title = matched
        block = _collect_block(stream, i, kind)
        parsed = _parse_block(block)
        head = _page_first_lines(stream, i)
        current = found[kind]
        if current is None:
            short_head = pending.get(kind)
            continues_short_head = (
                short_head is not None
                and parsed.page_start == short_head.page_end + 1
                and block.lines[0].index_in_page <= 2
            )
            if continues_short_head and _structurally_ok(parsed, head, report_type=report_type):
                # 短首块的续页：只做排除性判定，不要求续页自带年份/单位（「綜合收益表（續）」
                # 下面往往直接就是金额行），年份/单位/币种从首块继承
                pending.pop(kind)
                parsed = _combine(short_head, parsed)
            elif _acceptable(parsed, head, report_type=report_type, min_rows=1):
                pending.pop(kind, None)
            else:
                pending.pop(kind, None)
                i = max(block.end, i + 1)
                continue
            if len(parsed.rows) >= MIN_ROWS:
                found[kind] = parsed
                absorbed_until[kind] = parsed.page_end
            else:
                pending[kind] = parsed
        elif (
            parsed.page_start <= absorbed_until.get(kind, -10) + 1
            # 页首重复标题的续页哪怕只剩一两行（00700 2014 綜合收益表第二页只有每股盈利）
            # 也要并入，否则它后面的綜合全面收益表也因不再相邻而丢失；非页首的同类标题
            # 仍要求 ≥3 行，挡住附註里「42 綜合現金流量表」这类小节
            and len(parsed.rows) >= (1 if block.lines[0].index_in_page <= 2 else 3)
            and parsed.column_count <= current.column_count
            and not (head and _NOTES_HEADER_RE.search(head[0]) and not match_title(head[0]))
        ):
            # 同类相邻块：并入（行 id 顺延，上下文保留）
            base = len(current.rows)
            for row in parsed.rows:
                row.row_id = f"r{base + 1}"
                base += 1
                current.rows.append(row)
            current.page_end = parsed.page_end
            absorbed_until[kind] = parsed.page_end
        i = max(block.end, i + 1)
    return found


# ---------------------------------------------------------------------------- 列选择


@dataclass(frozen=True)
class PeriodColumn:
    column: int  # rows[].values 的下标
    end_date: str  # YYYYMMDD
    fp: str  # FY / H1
    is_primary: bool  # 本报告自身的报告期（否则为比较期）


def _shift_year(end_date: str, years: int) -> str:
    return f"{int(end_date[:4]) + years}{end_date[4:]}"


def _prior_fiscal_year_end(interim_end: str) -> str:
    """中报资产负债表的比较列是上一财年末：6 月中报 → 上年 12/31；9 月中报（3 月财年）→ 当年 3/31。"""
    year, month = int(interim_end[:4]), int(interim_end[4:6])
    fy_month = ((month + 6 - 1) % 12) + 1
    fy_year = year if fy_month < month else year - 1
    day = {3: "31", 6: "30", 9: "30", 12: "31"}.get(fy_month, "31")
    return f"{fy_year}{fy_month:02d}{day}"


def _year_columns(parsed: ParsedStatement, *, end_date: str) -> Optional[Tuple[int, Optional[int]]]:
    """按表头年份定位本期列与上期列（美国准则口径的港股年份**升序**排列且多一列美元
    折算：不能假设列 0 是本期）。四列中报（三个月+六个月）取六个月那组 = 最后一次出现。
    表头无年份或找不到报告期年份 → None（退回按位置）。"""
    if not parsed.years:
        return None
    year = int(end_date[:4])
    if year not in parsed.years:
        return None
    pick = (
        (lambda seq, y: len(seq) - 1 - seq[::-1].index(y))
        if parsed.interim_four_columns
        else (lambda seq, y: seq.index(y))
    )
    current = pick(parsed.years, year)
    prior = pick(parsed.years, year - 1) if (year - 1) in parsed.years else None
    return current, prior


def _date_columns(
    parsed: ParsedStatement, *, report_type: str, end_date: str
) -> Optional[Tuple[int, Optional[int]]]:
    """按列期末日定位本期列与上期列（`column_dates`）；列日期不全或本期日期对不上 → None。
    上期列只认日期**恰好**是预期比较期的那一列：年报/中报损益 = 上年同日，中报资产负债表 =
    上财年末。「2020年1月1日」这类期初（新准则调整后的重列余额）不冒充上年末。"""
    if parsed.interim_four_columns:
        return None
    dates = column_dates(parsed)
    if not dates or any(value is None for value in dates) or end_date not in dates:
        return None
    current = dates.index(end_date)
    if report_type == "interim" and parsed.kind == "balance":
        expected = _prior_fiscal_year_end(end_date)
    else:
        expected = _shift_year(end_date, -1)
    prior = dates.index(expected) if expected in dates else None
    return current, prior


def period_columns(
    parsed: ParsedStatement, *, report_type: str, end_date: str
) -> List[PeriodColumn]:
    """按报表类型与报告期决定各数值列对应的 (end_date, fp)。
    年报：本期 FY + 上期 FY；中报损益/现金流：本期 H1 + 上年同期 H1（四列取六个月两列）；
    中报资产负债表：期末 H1 + 上财年末 FY。列位置依次按表头的列期末日、列年份、位置 0/1。

    列年份重复且读不出列日期时（年报 [2016, 2016] 这类），年报按位置取本期 = 第 0 列、
    上期 = 第 1 列——重复的裸年份不能决定列。"""
    by_date = _date_columns(parsed, report_type=report_type, end_date=end_date)
    by_year = by_date if by_date is not None else _year_columns(parsed, end_date=end_date)
    if by_year is not None:
        current, prior = by_year
        duplicated = len(set(parsed.years)) < len(parsed.years)
        if (
            by_date is None
            and prior is None
            and duplicated
            and report_type == "annual"
            and parsed.column_count >= 2
        ):
            current, prior = 0, 1
    else:
        current, prior = 0, (1 if parsed.column_count >= 2 else None)
        if (
            report_type == "interim"
            and parsed.kind != "balance"
            and parsed.interim_four_columns
            and parsed.column_count >= 4
        ):
            current, prior = 2, 3
    if report_type == "interim":
        if parsed.kind == "balance":
            cols = [PeriodColumn(current, end_date, "H1", True)]
            if prior is None and parsed.column_count >= 2:
                # 读不出上财年末那一列：沿用「另一列即上财年末」（中报资产负债表只有两列）
                prior = next((c for c in range(parsed.column_count) if c != current), None)
            if prior is not None:
                cols.append(PeriodColumn(prior, _prior_fiscal_year_end(end_date), "FY", False))
            return cols
        cols = [PeriodColumn(current, end_date, "H1", True)]
        if prior is not None:
            cols.append(PeriodColumn(prior, _shift_year(end_date, -1), "H1", False))
        return cols
    cols = [PeriodColumn(current, end_date, "FY", True)]
    if prior is not None:
        cols.append(PeriodColumn(prior, _shift_year(end_date, -1), "FY", False))
    return cols


_CJK_SMALL = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_DATE_NUM = r"\d{1,4}|[〇零一二三四五六七八九十]{1,4}"
_DATE_CJK = rf"(?P<y>{_DATE_NUM})\s*年\s*(?P<m>{_DATE_NUM})\s*月\s*(?P<d>{_DATE_NUM})\s*日"
# 「截至…日止年度」「於…日」，或整行只有一个日期（00883：「二零二五年十二月三十一日」）
_PERIOD_END_RE = re.compile(
    rf"(?:(?:截至|於|于)\s*|^(?=(?:{_DATE_NUM})\s*年\s*(?:{_DATE_NUM})\s*月\s*(?:{_DATE_NUM})\s*日"
    rf"\s*(?:止(?:年度|六個月|六个月))?\s*$)){_DATE_CJK}"
)
_MONTHS_EN = {
    "JANUARY": 1,
    "FEBRUARY": 2,
    "MARCH": 3,
    "APRIL": 4,
    "MAY": 5,
    "JUNE": 6,
    "JULY": 7,
    "AUGUST": 8,
    "SEPTEMBER": 9,
    "OCTOBER": 10,
    "NOVEMBER": 11,
    "DECEMBER": 12,
}
_PERIOD_END_EN_RE = re.compile(
    r"(?:ENDED|AS AT|AS OF)\s+(?P<d>\d{1,2})\s+(?P<mon>[A-Z]+)\s+(?P<y>\d{4})", re.I
)


def _cjk_int(text: str) -> Optional[int]:
    """「二零二五」→ 2025（逐位）；「十二」「三十一」→ 12 / 31（十进制读法）。"""
    if text.isdigit():
        return int(text)
    if "十" not in text:
        digits = "".join(_CJK_DIGITS.get(ch, "") for ch in text)
        return int(digits) if digits and len(digits) == len(text) else None
    tens, _, ones = text.partition("十")
    value = (_CJK_SMALL.get(tens, 1) if tens else 1) * 10
    if ones:
        if ones not in _CJK_SMALL:
            return None
        value += _CJK_SMALL[ones]
    return value


_MONTH_NAMES = "|".join(_MONTHS_EN)
_FULL_DATE_CJK_RE = re.compile(_DATE_CJK)
_FULL_DATE_EN_RE = re.compile(
    rf"(?<!\d)(?P<d>\d{{1,2}})\s+(?P<mon>{_MONTH_NAMES})\s*,?\s*(?P<y>\d{{4}})(?!\d)", re.I
)
_FULL_DATE_EN_US_RE = re.compile(
    rf"(?P<mon>{_MONTH_NAMES})\s+(?P<d>\d{{1,2}}),?\s+(?P<y>\d{{4}})(?!\d)", re.I
)
# 拆成两行的列日期：年份行「二零一七年 二零一六年」/「於二零一九年 於二零一九年」+ 月日行「六月三十日
# 十二月三十一日」/「十二月 六月」（01023 只写月份）/「30 June 31 December」
_CJK_MD = r"[〇零一二三四五六七八九十]{1,3}|\d{1,2}"
_MONTH_DAY_CJK_RE = re.compile(rf"(?P<m>{_CJK_MD})\s*月(?:\s*(?P<d>{_CJK_MD})\s*日)?")
_MONTH_DAY_EN_RE = re.compile(
    rf"(?<!\d)(?P<d>\d{{1,2}})\s+(?P<mon>{_MONTH_NAMES})\b|\b(?P<mon2>{_MONTH_NAMES})\s+(?P<d2>\d{{1,2}})(?!\d)",
    re.I,
)
_PERIOD_CAPTION_RE = re.compile(r"止|ended|ending", re.I)


def _compose_date(year: Optional[int], month: Optional[int], day: Optional[int]) -> Optional[str]:
    if not year or not month or not (1990 <= year <= 2100 and 1 <= month <= 12):
        return None
    last = calendar.monthrange(year, month)[1]
    day = last if day is None else day
    if not 1 <= day <= last:
        return None
    return f"{year}{month:02d}{day:02d}"


def _full_dates(line: str) -> List[Tuple[int, int, str]]:
    """行内的完整日期（年月日）→ [(start, end, YYYYMMDD)]，按位置排序、去重叠。"""
    found: List[Tuple[int, int, str]] = []
    for match in _FULL_DATE_CJK_RE.finditer(line):
        value = _compose_date(*(_cjk_int(match.group(k)) for k in ("y", "m", "d")))
        if value:
            found.append((match.start(), match.end(), value))
    for pattern in (_FULL_DATE_EN_RE, _FULL_DATE_EN_US_RE):
        for match in pattern.finditer(line):
            month = _MONTHS_EN.get(match.group("mon").upper())
            value = _compose_date(int(match.group("y")), month, int(match.group("d")))
            if value:
                found.append((match.start(), match.end(), value))
    found.sort()
    out: List[Tuple[int, int, str]] = []
    for item in found:
        if not out or item[0] >= out[-1][1]:
            out.append(item)
    return out


def _month_days(line: str) -> List[Tuple[int, Optional[int]]]:
    """只有月（日）、没有年份的列日期行 → [(month, day|None)]；标题（「截至六月三十日止六個月」）
    与带年份的行返回空。"""
    if _years_in(line) or _PERIOD_CAPTION_RE.search(line):
        return []
    out: List[Tuple[int, Optional[int]]] = []
    for match in _MONTH_DAY_CJK_RE.finditer(line):
        month = _cjk_int(match.group("m"))
        day = _cjk_int(match.group("d")) if match.group("d") else None
        if month and 1 <= month <= 12:
            out.append((month, day))
    if out:
        return out
    for match in _MONTH_DAY_EN_RE.finditer(line):
        month = _MONTHS_EN.get((match.group("mon") or match.group("mon2")).upper())
        day = int(match.group("d") or match.group("d2"))
        if month:
            out.append((month, day))
    return out


def column_dates(parsed: ParsedStatement) -> List[Optional[str]]:
    """各列期末日（与 `parsed.years` 等长、同序）；读不出返回空列表。

    列年份可以合法地重复——非日历财年中报的资产负债表（01023：「於二零二一年 於二零二一年」
    +「十二月 六月」= 2021-12-31 与 2021-06-30），中国准则的期初列（「2020年12月31日 2020年1月1日」）
    ——这时只有列日期能区分本期列与比较列。来源两种：列年份行自带完整日期，或紧邻的月日行。"""
    index, years = _column_year_line(parsed.header)
    if index is None:
        return []
    dates = [value for _, _, value in _full_dates(parsed.header[index])]
    if len(dates) == len(years) and len(set(dates)) >= 2:
        return list(dates)
    for offset in (1, 2, -1, -2):
        j = index + offset
        if not 0 <= j < len(parsed.header):
            continue
        month_days = _month_days(parsed.header[j])
        if month_days and len(month_days) == len(years):
            return [
                _compose_date(year, month, day) for year, (month, day) in zip(years, month_days)
            ]
    return []


def detect_period_end(parsed: ParsedStatement) -> Optional[str]:
    """从表头读报表自己声明的期末日（「截至二零二五年十二月三十一日止六個月」/「於2025年
    6月30日」/「ENDED 31 MARCH 2026」）→ YYYYMMDD；读不到返 None。

    披露易清单只给标题与公告日，期末日是按标题年份 + 刊发窗口猜的；6 月财年的公司
    （01023）"2026 中期報告" 的期末其实是 2025-12-31。表头是唯一的权威来源。"""
    for line in parsed.header:
        found = _PERIOD_END_RE.search(line)
        if found:
            groups = found.groupdict()
            year, month, day = (_cjk_int(groups[k]) for k in ("y", "m", "d"))
            if (
                year
                and month
                and day
                and 1990 <= year <= 2100
                and 1 <= month <= 12
                and 1 <= day <= 31
            ):
                return f"{year}{month:02d}{day:02d}"
        found = _PERIOD_END_EN_RE.search(line)
        if found and found.group("mon").upper() in _MONTHS_EN:
            year, day = int(found.group("y")), int(found.group("d"))
            month = _MONTHS_EN[found.group("mon").upper()]
            if 1990 <= year <= 2100 and 1 <= day <= 31:
                return f"{year}{month:02d}{day:02d}"
    return None


def years_consistent(parsed: ParsedStatement, *, end_date: str) -> bool:
    """表头年份与报告期年份是否对得上（对不上说明定位到了别的年份/别的表）。"""
    if not parsed.years:
        return True
    return int(end_date[:4]) in parsed.years


def resolve_value(
    parsed: ParsedStatement, row_ids: Sequence[str], column: int, *, scale: bool
) -> Optional[Decimal]:
    """若干行同一列求和（LLM 只给行 id）；全部缺失返回 None；按单位放大（每股指标除外）。"""
    by_id = {row.row_id: row for row in parsed.rows}
    total: Optional[Decimal] = None
    # 同一行 id 只计一次（#342-2：存量映射里可能有重复 id，逐个相加会让科目翻倍）
    for row_id in dict.fromkeys(row_ids):
        row = by_id.get(row_id)
        if row is None or column >= len(row.values):
            continue
        value = row.values[column]
        if value is None:
            continue
        total = value if total is None else total + value
    if total is None:
        return None
    return total * parsed.unit_multiplier if scale else total


def statement_rows_for_prompt(
    parsed: ParsedStatement, *, columns: Sequence[int]
) -> List[Dict[str, object]]:
    """给 LLM 看的行：id、标签、附注、上下文与所选列的原文数值（字符串，只读）。"""
    out = []
    for row in parsed.rows:
        values = []
        for col in columns:
            value = row.values[col] if col < len(row.values) else None
            values.append(str(value) if value is not None else "—")
        out.append(
            {
                "id": row.row_id,
                "label": row.label or "(无标签)",
                **({"note": row.note} if row.note else {}),
                **({"context": row.context} if row.context else {}),
                "values": values,
            }
        )
    return out
