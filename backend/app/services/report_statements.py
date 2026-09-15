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

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Sequence, Tuple

# v4：币种+单位分开排版（「美元 千元」「RMB million」）算一个布局单元，且表头布局须与数据行吻合才采信
STATEMENT_EXTRACTOR_VERSION = 4
STATEMENT_KINDS = ("income", "balance", "cashflow")

# 报表标题核心（繁/简；港股「綜合」= A股「合并」）。income 同时覆盖损益表与全面收益表
# 「綜合」= 港股 IFRS 口径，「合并」= A股，「合併」= 港股按美国准则编报（京东 09618 用
# 「合併經營狀況及綜合收益表」= statement of operations and comprehensive income）
_CONSOL = r"(?:綜合|综合|合并|合併)"
_TITLE_CORE = {
    "income": (
        rf"(?:簡明|简明)?{_CONSOL}"
        r"(?:經營狀況及綜合收益|经营状况及综合收益|經營狀況|经营状况"
        r"|損益及(?:其他)?全面(?:收益|收入)|损益及(?:其他)?全面(?:收益|收入)"
        r"|全面收益|全面收入|損益|损益|收益|利润)表"
    ),
    "balance": rf"(?:簡明|简明)?{_CONSOL}(?:財務狀況|财务状况|資產負債|资产负债)表",
    "cashflow": rf"(?:簡明|简明)?{_CONSOL}(?:現金流量|现金流量)表",
}
_PREFIX = r"(?:\d{1,2}\s*[、.．]?\s*|[（(]?[一二三四五六七八九十]{1,3}[）)]?\s*[、.．]?\s*)?"
_SUFFIX = r"(?:\s*[（(]\s*(?:續|续)\s*[）)])?"
TITLE_LINE_RE = {
    kind: re.compile(rf"^{_PREFIX}(?P<title>{core}){_SUFFIX}\s*$")
    for kind, core in _TITLE_CORE.items()
}
# 终止标题：母公司报表、权益变动表、附註起点（都不属于三张合并报表）
_TERMINATOR_RE = re.compile(
    r"^" + _PREFIX + r"(?:"
    r"母公司(?:資產負債|资产负债|利润|損益|损益|現金流量|现金流量)表"
    r"|(?:簡明|简明)?(?:綜合|综合|合并|合併)(?:權益變動|权益变动|所有者权益变动|股東權益變動|股东权益变动)表"
    r"|(?:綜合|综合|合并|合併)?(?:財務報表|财务报表|中期財務資料|中期财务资料)(?:附註|附注)"
    r"|NOTES TO THE (?:CONSOLIDATED )?FINANCIAL STATEMENTS"
    r")" + _SUFFIX + r"\s*$",
    re.I,
)
_NOTES_HEADER_RE = re.compile(r"附註|附注|NOTES TO", re.I)
_SUMMARY_HEADER_RE = re.compile(r"概要|摘要|Summary|Highlights", re.I)

_NUM = r"\(?-?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?|\(?-?\d+(?:\.\d+)?\)?|[–—-]"
_VALUES = rf"(?:(?:{_NUM})\s+)*(?:{_NUM})"
_ROW_RE = re.compile(rf"^(?P<label>\S.*?)\s+(?P<values>{_VALUES})\s*$")
_VALUES_ONLY_RE = re.compile(rf"^(?P<values>{_VALUES})\s*$")
_NOTE_TOKEN_RE = re.compile(
    r"^(?:\d{1,2}(?:\([a-z]\))?|\d{1,2}[a-z]|[一二三四五六七八九十]{1,3}[、.．]\d{1,2})$"
)
_NUM_TOKEN_RE = re.compile(rf"^(?:{_NUM})$")
_YEAR_ARABIC_RE = re.compile(r"(?<!\d)(20\d{2}|19\d{2})(?!\d)")
_YEAR_CJK_RE = re.compile(r"([一二三四五六七八九零〇]{4})\s*年")
_CJK_DIGITS = {"零": "0", "〇": "0", "一": "1", "二": "2", "三": "3", "四": "4",
               "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
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
_INTERIM_COLUMNS_RE = re.compile(r"(三個月|三个月|three months).*(六個月|六个月|six months)", re.I | re.S)

MIN_ROWS = 6
MAX_BLOCK_PAGES = 6
HEADER_LINES = 12


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
                row_id=str(item["id"]), label=str(item.get("label") or ""),
                note=str(item.get("note") or ""),
                values=[Decimal(v) if v is not None else None for v in item.get("values") or []],
                context=[str(c) for c in item.get("context") or []],
            )
            for item in payload.get("rows") or []
        ]
        return cls(
            kind=str(payload["kind"]), page_start=int(payload["page_start"]),
            page_end=int(payload["page_end"]), title=str(payload.get("title") or ""),
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


def _line_stream(pages: Sequence[str]) -> List[_Line]:
    stream: List[_Line] = []
    for page_no, page in enumerate(pages, start=1):
        idx = 0
        for raw in (page or "").splitlines():
            text = raw.strip()
            if not text:
                continue
            stream.append(_Line(page_no, idx, text))
            idx += 1
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
    if text in {"–", "—", "-", ""}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(",", "")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return -value if negative else value


def _parse_row_tokens(line: str) -> Optional[Tuple[str, str, List[str]]]:
    """数字行 → (label, note, value_tokens)；非数字行返回 None。
    无标签的合计行（`6 751,766 660,257` / `229,801 196,467`）标签为空。"""
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
    if not note:
        note, tokens = _split_leading_note(tokens, expected_columns)
    return label, note, [parse_number(t) for t in tokens]


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


def _header_years(header: Sequence[str]) -> List[int]:
    """表头里第一条含 ≥2 个年份的行决定列年份（「2025年 2024年」/「二零二五年 二零二四年」）。"""
    for line in header:
        years = _years_in(line)
        if len(years) >= 2:
            return years
    return []


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
        if line.index_in_page == 0 and line.page != start_page:
            head = _page_first_lines(stream, i)
            if head and _NOTES_HEADER_RE.search(head[0]) and not match_title(head[0]):
                break
        lines.append(line)
        i += 1
    return _Block(kind=kind, title=title, start=start, end=i, lines=lines)


_SMALL_INT_RE = re.compile(r"^\d{1,2}$")


def _is_year_only_row(values: List[Optional[Decimal]]) -> bool:
    """「FOR THE YEAR ENDED 31 DECEMBER 2025」这类双语表头行会被当成只有一个值 2025 的行。"""
    return (
        len(values) == 1
        and values[0] is not None
        and values[0] == values[0].to_integral_value()
        and 1990 <= int(values[0]) <= 2100
    )


_LEADING_NOTE_RE = re.compile(r"^\d{1,2}(?:\([a-z]\))?$")


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
_UNIT_BEARING_RE = re.compile(rf"^(?:{_CURRENCY_WORDS})?(?:{_UNIT_WORDS})$", re.I)


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
    header: Sequence[str], years: List[int], counts: Dict[int, int],
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


def _parse_block(block: _Block) -> ParsedStatement:
    texts = [line.text for line in block.lines]
    header = texts[:HEADER_LINES]
    years = _header_years(header)
    interim_four = bool(_INTERIM_COLUMNS_RE.search(" ".join(header)))
    # 第一遍：解析全部数字行，拿主导列数（附注号粘列的行会多一列，是少数）
    raw_rows: List[Tuple[str, str, List[str], List[str]]] = []
    context: List[str] = []
    counts: Dict[int, int] = {}
    first_tokens: Dict[int, List[str]] = {}
    for offset, text in enumerate(texts):
        if offset == 0:
            continue
        if _is_header_like(text, years):
            continue
        parsed = _parse_row_tokens(text)
        if parsed is None:
            context.append(text)
            context = context[-2:]
            continue
        label, note, tokens = parsed
        if not tokens:
            continue
        raw_rows.append((label, note, tokens, list(context) if not label else []))
        counts[len(tokens)] = counts.get(len(tokens), 0) + 1
        first_tokens.setdefault(len(tokens), []).append(tokens[0])
        if label:
            context = []
    expected = _expected_columns(header, years, counts, first_tokens)
    # 第二遍：剥附注号、剔年份行、定型
    rows: List[StatementRow] = []
    final_counts: Dict[int, int] = {}
    for label, note, tokens, ctx in raw_rows:
        if not note:
            note, tokens = _split_leading_note(tokens, expected)
        values = [parse_number(t) for t in tokens]
        if _is_year_only_row(values):
            continue
        rows.append(
            StatementRow(row_id=f"r{len(rows) + 1}", label=label, note=note, values=values, context=ctx)
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


def _acceptable(parsed: ParsedStatement, start_head: List[str], *, report_type: str) -> bool:
    if len(parsed.rows) < MIN_ROWS:
        return False
    if start_head and _SUMMARY_HEADER_RE.search(start_head[0]):
        return False
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


def locate_statements(
    pages: Sequence[str], *, report_type: str = "annual"
) -> Dict[str, Optional[ParsedStatement]]:
    """逐页文本 → {kind: ParsedStatement|None}。每类取**第一个**合格块，并吞并紧随
    其后（相邻页）的同类块（綜合收益表 + 綜合全面收益表 / 多页資產負債表）。"""
    stream = _line_stream(pages)
    found: Dict[str, Optional[ParsedStatement]] = {kind: None for kind in STATEMENT_KINDS}
    absorbed_until: Dict[str, int] = {}
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
            if _acceptable(parsed, head, report_type=report_type):
                found[kind] = parsed
                absorbed_until[kind] = parsed.page_end
        elif (
            parsed.page_start <= absorbed_until.get(kind, -10) + 1
            and len(parsed.rows) >= 3
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
    pick = (lambda seq, y: len(seq) - 1 - seq[::-1].index(y)) if parsed.interim_four_columns else (
        lambda seq, y: seq.index(y)
    )
    current = pick(parsed.years, year)
    prior = pick(parsed.years, year - 1) if (year - 1) in parsed.years else None
    return current, prior


def period_columns(
    parsed: ParsedStatement, *, report_type: str, end_date: str
) -> List[PeriodColumn]:
    """按报表类型与报告期决定各数值列对应的 (end_date, fp)。
    年报：本期 FY + 上期 FY；中报损益/现金流：本期 H1 + 上年同期 H1（四列取六个月两列）；
    中报资产负债表：期末 H1 + 上财年末 FY。列位置优先按表头年份，其次按 0/1。"""
    by_year = _year_columns(parsed, end_date=end_date)
    if by_year is not None:
        current, prior = by_year
    else:
        current, prior = 0, (1 if parsed.column_count >= 2 else None)
        if report_type == "interim" and parsed.kind != "balance" and parsed.interim_four_columns \
                and parsed.column_count >= 4:
            current, prior = 2, 3
    if report_type == "interim":
        if parsed.kind == "balance":
            cols = [PeriodColumn(current, end_date, "H1", True)]
            if prior is not None or parsed.column_count >= 2:
                prior_col = prior if prior is not None else 1
                cols.append(PeriodColumn(prior_col, _prior_fiscal_year_end(end_date), "FY", False))
            return cols
        cols = [PeriodColumn(current, end_date, "H1", True)]
        if prior is not None:
            cols.append(PeriodColumn(prior, _shift_year(end_date, -1), "H1", False))
        return cols
    cols = [PeriodColumn(current, end_date, "FY", True)]
    if prior is not None:
        cols.append(PeriodColumn(prior, _shift_year(end_date, -1), "FY", False))
    return cols


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
    for row_id in row_ids:
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


def statement_rows_for_prompt(parsed: ParsedStatement, *, columns: Sequence[int]) -> List[Dict[str, object]]:
    """给 LLM 看的行：id、标签、附注、上下文与所选列的原文数值（字符串，只读）。"""
    out = []
    for row in parsed.rows:
        values = []
        for col in columns:
            value = row.values[col] if col < len(row.values) else None
            values.append(str(value) if value is not None else "—")
        out.append({
            "id": row.row_id,
            "label": row.label or "(无标签)",
            **({"note": row.note} if row.note else {}),
            **({"context": row.context} if row.context else {}),
            "values": values,
        })
    return out
