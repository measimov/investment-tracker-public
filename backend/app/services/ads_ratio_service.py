"""美股 ADS 与普通股的换算比（1 ADS = N 股普通股）。

EDGAR 的每股盈利/隐含股数按**普通股**口径，行情价是 **ADS** 价——20-F 发行人不换算，PE/PB
会被放大或缩小 N 倍。换算比两个来源，按优先级：

1. **用户规则**（security_rules 的 `ADS_RATIO`，payload `{ratio}`）：用户域，只在请求带着
   用户时生效（详情页 profile / 观察清单 / 分析 job 的发起人）；用于解析失败或解析值有误的兜底。
2. **年报封面自动解析**：最新一份 20-F（v3 起也含 10-K，#352）主文档封面 Section 12(b)
   登记表里的「American depositary shares, each representing four Class A ordinary shares」。
   结果落 `security_profile_data`（dataset=`ads_ratio`，period_key=`current`，全局）。

解析层（`parse_ads_ratio`）是纯函数、金样测试对象（`tests/fixtures/ads/` 真实封面节选）：
封面优先；封面上出现两个不同比例 → None（不猜）；封面没有时才看正文的术语定义句
（「"ADSs" are to the American depositary shares, each of which represents …」），须全部一致。动词只认现在时（represents / representing / represent），
「prior to …, each ADS represented …」这类历史比例天然不匹配。

10-K 发行人封面没有登记 ADS（美国本土普通股）估值按 1:1；未解析出比例的 20-F 发行人，以及封面
登记了 ADS 却解析不出比例的 10-K 发行人，估值判 indeterminate。
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from .payload_versions import versions_current

logger = logging.getLogger("investment_tracker.ads_ratio")

MARKET = "美股"
DATASET = "ads_ratio"
PERIOD_KEY = "current"
# 改动解析规则（词表/正则/封面边界）就 bump：缓存按 (accession, 版本) 命中，
# 不 bump 的话修好的解析器被旧的 not_found 行挡住。v3（#352）：「each representing」前的
# 数量不参与换算（封面脚注上标）；10-K 申报人也解析封面
ADS_PARSER_VERSION = 3
# 同一份 20-F 下载失败的重试上限（每次都是整份主文档，BABA 近 12MB）
MAX_FETCH_ATTEMPTS = 3

# ---------------------------------------------------------------------------
# 解析（纯函数）
# ---------------------------------------------------------------------------

_UNITS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_TENS = ("twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
_ORDINALS = {
    "half": 2,
    "third": 3,
    "quarter": 4,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "twelfth": 12,
    "fifteenth": 15,
    "twentieth": 20,
    "fortieth": 40,
    "fiftieth": 50,
    "hundredth": 100,
}

_WORD = (
    r"(?:(?:" + "|".join(_TENS) + r")[- ](?:one|two|three|four|five|six|seven|eight|nine)"
    r"|" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")"
)
# 数字数量不限位数（「10000 ADSs represent one share」= 0.0001）；千分位写法单列
_DIGITS = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_CARDINAL = rf"(?:{_DIGITS}|{_WORD})"
_ORDINAL = "|".join(sorted(_ORDINALS, key=len, reverse=True))
# 分数：one-tenth of one / 1/10 of an / one half of one
_FRACTION = (
    rf"(?:(?P<fn>{_CARDINAL})[- ](?P<fd>{_ORDINAL})s?"
    rf"|(?P<sn>\d{{1,4}})/(?P<sd>\d{{1,4}}))"
    r" of (?:one|an|a|1)"
)
_SHARES_AMOUNT = rf"(?:{_FRACTION}|(?P<n>{_CARDINAL}))"
_SHARE_CLASS = (
    r"(?:\(?(?:class|series) [a-z0-9]{1,3}\)? )?(?:(?:ordinary|common|equity) )?shares?\b"
)
_ADS = r"(?:american depositary shares?|american depository shares?|adss?)"
_LEAD = rf"(?:(?:every|each) (?P<every>{_CARDINAL})|(?P<a>{_CARDINAL})|each|every)"
_VERB = (
    r"(?:representing|represents|represent|which represents?|that represents?"
    r"|each representing|each represents|representative of)"
)
_RATIO_RE = re.compile(
    rf"(?:\b{_LEAD} )?(?P<ads>{_ADS})\b"
    # 可选插入语：（"ADSs"）、逗号/括号、each (of which | ADS | American depositary share)
    r"(?: ?\(\s*[\"“”']?adss?[\"“”']?\s*\))?"
    r"\s?[,(]?\s?"
    rf"(?:(?:each|every one) (?:(?:of which|{_ADS}) )?)?"
    rf"{_VERB} {_SHARES_AMOUNT} {_SHARE_CLASS}"
)

_COVER_START_RE = re.compile(r"section 12\s*\(\s*b\s*\)")
_COVER_END_RE = re.compile(
    r"section 12\s*\(\s*g\s*\)|section 15\s*\(\s*d\s*\)|indicate the number of outstanding"
)
# 正文只认术语定义句（「"ADSs" are to the American depositary shares, each of which represents
# four Class A ordinary shares」）：正文里还有历史比例变更的叙述（「changed the ratio from 1 ADS
# representing 1 share to 10 ADSs representing 1 share」，BIDU/TCOM 真实原文），全文一致性
# 判不出哪个是现行比例
_DEFINITION_RE = re.compile(r"[\"“”']adss?[\"“”'],? (?:are to|refers? to|means?)\b")
DEFINITION_LOOKBACK = 80
# 封面最多看这么远（12(b) 登记表必在首页；找不到起点就当封面缺失）
COVER_SCAN_CHARS = 30_000
SOURCE_TEXT_CHARS = 240


def _normalize(text: str) -> str:
    # NBSP 与各种连字符/破折号（如 one-tenth 里的非断行连字符）统一成 ASCII
    text = re.sub("[\u2010\u2011\u2012\u2013\u2014]", "-", text.replace("\u00a0", " "))
    return re.sub(r"\s+", " ", text).lower()


def _cardinal_value(token: str) -> Optional[Decimal]:
    token = token.strip().replace(",", "")
    if not token:
        return None
    try:
        return Decimal(token)
    except InvalidOperation:
        pass
    parts = re.split(r"[- ]", token)
    total = 0
    for part in parts:
        if part not in _UNITS:
            return None
        total += _UNITS[part]
    return Decimal(total)


# 「ADSs, each representing …」「ADSs, each of which represents …」：「每一份」ADS 的表述，
# 比例与 ADS 前面的数量无关
_PER_EACH_RE = re.compile(r"\b(?:each|every one)\b")


def _per_each(match: re.Match) -> bool:
    """ADS 之后是「每一份」的表述：前导数量不参与换算（v3，#352）。

    封面登记表里前一格的脚注上标会被 html_to_text 转成孤立数字，与下一格的 ADS 连成
    「…hong kong limited 2 american depositary shares, each representing eight…」，
    按前导数量算就成了 2 ADS = 8 股、比例 4（应为 8）。「N ADSs, each representing M
    shares」在语法上本就是每份 M 股，不论 N 是多少。"""
    return bool(_PER_EACH_RE.search(match.string[match.end("ads") : match.end()]))


def _match_ratio(match: re.Match) -> Optional[Decimal]:
    """匹配 → 每 1 ADS 对应的普通股数。"""
    groups = match.groupdict()
    if groups.get("fd"):
        numerator = _cardinal_value(groups["fn"])
        denominator = Decimal(_ORDINALS[groups["fd"]])
        shares = numerator / denominator if numerator is not None else None
    elif groups.get("sd"):
        denominator = Decimal(groups["sd"])
        shares = Decimal(groups["sn"]) / denominator if denominator else None
    else:
        shares = _cardinal_value(groups.get("n") or "")
    lead = None if _per_each(match) else (groups.get("every") or groups.get("a"))
    ads = _cardinal_value(lead) if lead else Decimal(1)
    if shares is None or ads is None or shares <= 0 or ads <= 0:
        return None
    return shares / ads


# 数量词表（ADS 数量前缀是否被完整消费的判据）：数字、基数词、以及正则不支持的
# hundred/thousand/million 这类量级词
_QUANTITY_WORDS = frozenset(_UNITS) | {"hundred", "thousand", "million", "billion"}
_TOKEN_BEFORE_RE = re.compile(r"([a-z0-9][a-z0-9,.\-]*)\s+$")


def _is_quantity_token(token: str) -> bool:
    token = token.strip(",.")
    if not token:
        return False
    if re.fullmatch(r"[\d,.]+", token):
        return True
    return all(part in _QUANTITY_WORDS for part in token.split("-") if part)


def _truncated_lead(text: str, start: int) -> bool:
    """匹配起点之前紧挨着的词仍属于数量表达 → ADS 数量前缀没有被完整消费。

    `_LEAD` 可选：「One hundred ADSs represent one share」里 hundred 不在词表，finditer 会从
    ADSs 起重新匹配并按 1 个 ADS 处理（得出 1 而不是 0.01）；「One hundred and twenty ADSs」
    会只取 twenty（PR #234 评审 P2）。只看以空白紧接匹配起点的那个词（「(1) American
    depositary shares」这类脚注编号被括号隔开，不算）；「and」要再往前一个词也是数量词才算。"""
    prefix = text[max(0, start - 60) : start]
    match = _TOKEN_BEFORE_RE.search(prefix)
    if not match:
        return False
    token = match.group(1)
    if _is_quantity_token(token):
        return True
    if token == "and":
        before = _TOKEN_BEFORE_RE.search(prefix[: match.start()])
        return bool(before and _is_quantity_token(before.group(1)))
    return False


class _AmbiguousQuantity(Exception):
    """某个候选的 ADS 数量表达没有被完整识别：宁可整体判 None 走人工规则，也不按 1 个 ADS 猜。"""


def _candidates(text: str, *, definitions_only: bool = False) -> List[Tuple[Decimal, str]]:
    found: List[Tuple[Decimal, str]] = []
    for match in _RATIO_RE.finditer(text):
        if definitions_only and not _DEFINITION_RE.search(
            text[max(0, match.start() - DEFINITION_LOOKBACK) : match.start() + 1]
        ):
            continue
        if not _per_each(match) and _truncated_lead(text, match.start()):
            raise _AmbiguousQuantity(text[max(0, match.start() - 40) : match.end()])
        ratio = _match_ratio(match)
        if ratio is None:
            continue
        start = max(0, match.start() - 40)
        snippet = text[start : match.end()].strip()
        found.append((_canonical(ratio), snippet[-SOURCE_TEXT_CHARS:]))
    return found


def _canonical(value: Decimal) -> Decimal:
    """1/3 这类循环小数量化到 10 位，再去尾零（4.000 → 4）。"""
    return value.quantize(Decimal("1E-10")).normalize()


def _cover_region(text: str) -> Optional[str]:
    head = text[:COVER_SCAN_CHARS]
    start = _COVER_START_RE.search(head)
    if not start:
        return None
    end = _COVER_END_RE.search(head, start.end())
    return head[start.end() : end.start() if end else len(head)]


def _unanimous(candidates: List[Tuple[Decimal, str]]) -> Optional[Tuple[Decimal, str]]:
    values = {ratio for ratio, _ in candidates}
    if len(values) != 1:
        return None
    return candidates[0]


def parse_ads_ratio(
    text: str, *, form: Optional[str] = None, filing_date: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """20-F 文本 → {ratio, source_text, section, form, filing_date} 或 None。

    ratio = 每 1 ADS 对应的普通股数（Decimal；"ten ADSs representing one share" = 0.1）。
    封面（Section 12(b) 登记表）优先：封面上有候选就只看封面，不同比例并存 → None；
    封面没有候选才看正文的术语定义句（"ADSs" are to …），定义句须全部一致。"""
    normalized = _normalize(text or "")
    if not normalized:
        return None
    cover = _cover_region(normalized)
    chosen: Optional[Tuple[Decimal, str]] = None
    section = None
    try:
        if cover is not None:
            cover_candidates = _candidates(cover)
            if cover_candidates:
                chosen, section = _unanimous(cover_candidates), "cover"
                if chosen is None:
                    return None  # 封面自相矛盾：不猜
        if chosen is None:
            body_candidates = _candidates(normalized, definitions_only=True)
            chosen, section = _unanimous(body_candidates), "body"
    except _AmbiguousQuantity:
        # 数量前缀不支持（one hundred / one hundred and twenty …）：返回 None，由特例规则兜底，
        # 绝不静默按 1 个 ADS 落库参与估值
        return None
    if chosen is None:
        return None
    ratio, source_text = chosen
    return {
        "ratio": ratio,
        "source_text": source_text,
        "section": section,
        "form": form,
        "filing_date": filing_date,
    }


def parse_ads_ratio_html(
    html: str, *, form: Optional[str] = None, filing_date: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    from .report_sections import html_to_text

    return parse_ads_ratio(html_to_text(html or ""), form=form, filing_date=filing_date)


_ADS_MENTION_RE = re.compile(r"american deposit[ao]ry|\badss?\b")


def ads_listed_on_cover(text: str) -> Optional[bool]:
    """年报封面 Section 12(b) 登记表里是否登记了 ADS；找不到封面返回 None（不可知）。

    10-K 申报人多数是美国本土公司（普通股直接上市，1:1），但也有以 ADS 交易的（BeOne
    1 ADS = 13 股、再鼎医药 1 ADS = 10 股，2026-02 两份 10-K 封面实查）：登记了 ADS 却解析
    不出比例时估值不能按 1:1。"""
    cover = _cover_region(_normalize(text or ""))
    if cover is None:
        return None
    return bool(_ADS_MENTION_RE.search(cover))


def format_ratio(value: Any) -> str:
    """Decimal/字符串 → 展示用（4 → "4"，0.5 → "0.5"）。"""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    return format(number.normalize(), "f")


# ---------------------------------------------------------------------------
# 抓取与落库
# ---------------------------------------------------------------------------


def _load_row(db: Session, symbol: str):
    from ..models.security_profile import SecurityProfileData

    return (
        db.query(SecurityProfileData)
        .filter(
            SecurityProfileData.symbol == symbol,
            SecurityProfileData.market == MARKET,
            SecurityProfileData.dataset == DATASET,
            SecurityProfileData.period_key == PERIOD_KEY,
        )
        .first()
    )


def _save(db: Session, symbol: str, payload: Dict[str, Any]) -> None:
    from .profile_store import upsert_profile_row

    upsert_profile_row(db, symbol, MARKET, DATASET, PERIOD_KEY, payload)
    db.commit()


def _annual_kind(form: Any) -> Optional[str]:
    """最新年报的表单类别：20-F / 10-K；其余（40-F、没有年报）为 None。"""
    text = str(form or "")
    for kind in ("20-F", "10-K"):
        if text.startswith(kind):
            return kind
    return None


def ensure_ads_ratio(db: Session, symbol: str, *, force: bool = False) -> Dict[str, Any]:
    """确保库内有最新一份年报（20-F / 10-K）的封面换算比；返回 {status, ...}。

    status：
    - `not_registered`：SEC 注册表里没有这个代码
    - `not_20f`：最新年报既不是 20-F 也不是 10-K（或没有年报），不落库
    - `cached`：最新年报已按当前解析器版本处理过（ok / not_found / no_ads），零下载
    - `ok`：本次下载并解析出换算比
    - `not_found`：20-F 发行人，或封面登记了 ADS 的 10-K 发行人，却解析不出比例（估值 indeterminate）
    - `no_ads`：10-K 封面**明确**没有登记 ADS（美国本土普通股，估值按 1:1）
    - `cover_unknown`：10-K 找不到封面 Section 12(b) 登记表，无法判断是否以 ADS 交易——**不是**
      no_ads：不缓存、计入重试次数；上一份年报有明确结论（ok/no_ads）时沿用它，否则落
      cover_unknown 行，估值两项 indeterminate（见 ads_ratio_missing）
    - `failed`：下载失败（保留库内旧结果；同一份年报最多重试 MAX_FETCH_ATTEMPTS 次）
    - `capped`：同一份年报下载失败或封面无法识别已到上限，不再重试（新一份年报或解析器
      升版后重新计数；`force=True` 无视上限）
    网络异常（清单拉取）上抛，调用方按需吞掉。

    v3（#352）起 10-K 也下载封面：部分 10-K 申报人以 ADS 交易（BeOne 13 股、再鼎医药 10 股），
    此前一律按 1:1 估值，PE/PB 偏高 10 倍以上且没有任何提示。每份 10-K 只下载一次（按
    accession 缓存）。10-K 只认封面登记表：正文术语定义句不足以证明本土公司以 ADS 交易。
    """
    from .report_fetchers import edgar_download_filing, edgar_lookup, edgar_recent_annual_filings

    symbol = str(symbol or "").strip().upper()
    lookup = edgar_lookup(symbol)
    if not lookup:
        return {"symbol": symbol, "status": "not_registered"}
    filings = edgar_recent_annual_filings(lookup["cik"], limit=1)
    latest = filings[0] if filings else None
    kind = _annual_kind((latest or {}).get("form"))
    if latest is None or kind is None:
        return {"symbol": symbol, "status": "not_20f", "form": (latest or {}).get("form")}

    row = _load_row(db, symbol)
    previous: Dict[str, Any] = dict(row.payload or {}) if row is not None else {}
    same_filing = previous.get("accession") == latest["accession"]
    if (
        not force
        and same_filing
        and versions_current(previous, parser_version=ADS_PARSER_VERSION)
        and previous.get("status") in ("ok", "not_found", "no_ads")
    ):
        return {"symbol": symbol, "status": "cached", **_summary(previous)}
    # 重试计数按 (年报, 解析器版本) 记：同一份年报换了解析器理应重新尝试
    pending_attempts = (
        int(previous.get("fetch_attempts") or 0)
        if previous.get("pending_accession") == latest["accession"]
        and previous.get("pending_parser_version") == ADS_PARSER_VERSION
        else 0
    )
    if not force and pending_attempts >= MAX_FETCH_ATTEMPTS:
        return {"symbol": symbol, "status": "capped", "error": previous.get("last_error")}

    try:
        html = edgar_download_filing(lookup["cik"], latest["accession"], latest["primary_document"])
    except Exception as exc:  # 下载失败：保留旧结果，只记重试计数
        error = f"{type(exc).__name__}: {str(exc)[:160]}"
        logger.warning("%s 下载失败 %s %s: %s", kind, symbol, latest["accession"], error)
        _save(
            db,
            symbol,
            _pending(previous, latest["accession"], pending_attempts, error),
        )
        return {"symbol": symbol, "status": "failed", "error": error}

    from .report_sections import html_to_text

    text = html_to_text(html or "")
    parsed = parse_ads_ratio(text, form=str(latest["form"]), filing_date=str(latest["filing_date"]))
    ads_listed = ads_listed_on_cover(text)
    if kind == "10-K":
        if ads_listed is None:
            # 找不到 12(b) 登记表：不知道是不是 ADS，绝不当成普通股按 1:1（PR #358 评审 P2）。
            # 不缓存：计入重试次数（下载截断、SEC 返回错误页都会这样），到上限后 capped。
            error = "10-K 封面未识别出 Section 12(b) 证券登记表，无法判断是否以 ADS 交易"
            logger.warning("%s %s %s", error, symbol, latest["accession"])
            base = previous if previous.get("status") in ("ok", "no_ads") else {}
            if not base:
                base = {
                    "cik": lookup["cik"],
                    "accession": latest["accession"],
                    "primary_document": latest["primary_document"],
                    "form": str(latest["form"]),
                    "filing_date": str(latest["filing_date"]),
                    "report_date": latest.get("report_date"),
                    "parser_version": ADS_PARSER_VERSION,
                    "status": "cover_unknown",
                    "ads_listed": None,
                    "ratio": None,
                    "source_text": None,
                    "section": None,
                }
            _save(db, symbol, _pending(base, latest["accession"], pending_attempts, error))
            return {"symbol": symbol, "status": "cover_unknown", "error": error}
        if ads_listed is False:
            parsed = None  # 10-K 只认封面登记表（见 docstring）
        status = "ok" if parsed else ("not_found" if ads_listed else "no_ads")
    else:
        status = "ok" if parsed else "not_found"
    payload: Dict[str, Any] = {
        "cik": lookup["cik"],
        "accession": latest["accession"],
        "primary_document": latest["primary_document"],
        "form": str(latest["form"]),
        "filing_date": str(latest["filing_date"]),
        "report_date": latest.get("report_date"),
        "parser_version": ADS_PARSER_VERSION,
        "status": status,
        "ads_listed": ads_listed,
        "ratio": format_ratio(parsed["ratio"]) if parsed else None,
        "source_text": parsed["source_text"] if parsed else None,
        "section": parsed["section"] if parsed else None,
    }
    if previous.get("ratio") and parsed and payload["ratio"] != previous["ratio"]:
        payload["previous_ratio"] = previous["ratio"]
        payload["previous_filing_date"] = previous.get("filing_date")
        logger.warning(
            "ADS 换算比变化 %s：%s → %s（%s）",
            symbol,
            previous["ratio"],
            parsed["ratio"],
            latest["filing_date"],
        )
    _save(db, symbol, payload)
    if status == "not_found":
        logger.warning("%s 封面未解析出 ADS 换算比 %s %s", kind, symbol, latest["accession"])
    return {"symbol": symbol, "status": payload["status"], **_summary(payload)}


def _pending(base: Dict[str, Any], accession: str, attempts: int, error: str) -> Dict[str, Any]:
    """本次没得到结论：在 base（上一份的明确结论，或 cover_unknown 行）上记重试计数。"""
    return {
        **base,
        "pending_accession": accession,
        "pending_parser_version": ADS_PARSER_VERSION,
        "fetch_attempts": attempts + 1,
        "last_error": error,
    }


def _summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ratio": payload.get("ratio"),
        "filing_date": payload.get("filing_date"),
        "form": payload.get("form"),
    }


# ---------------------------------------------------------------------------
# 解析结果 + 用户规则 → 生效换算比
# ---------------------------------------------------------------------------


def _rule_ratios(db: Session, user_id: Optional[int], symbols: Iterable[str]) -> Dict[str, Decimal]:
    if user_id is None:
        return {}
    from .security_rule_service import get_ads_ratios

    return get_ads_ratios(db, user_id, symbols)


def _parsed_ratios(db: Session, symbols: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    from ..models.security_profile import SecurityProfileData

    wanted = sorted(set(symbols))
    if not wanted:
        return {}
    rows = (
        db.query(SecurityProfileData.symbol, SecurityProfileData.payload)
        .filter(
            SecurityProfileData.market == MARKET,
            SecurityProfileData.dataset == DATASET,
            SecurityProfileData.period_key == PERIOD_KEY,
            SecurityProfileData.symbol.in_(wanted),
        )
        .all()
    )
    return {symbol: payload or {} for symbol, payload in rows}


def resolved_from(
    rule_ratio: Optional[Decimal], parsed: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """纯函数：用户规则优先，其次年报（20-F / 10-K）封面解析值。"""
    if rule_ratio is not None:
        text = format_ratio(rule_ratio)
        return {
            "ratio": rule_ratio,
            "source": "rule",
            "filing_date": None,
            "note": f"1 ADS = {text} 股（用户规则）",
        }
    if parsed and parsed.get("ratio"):
        try:
            ratio = Decimal(str(parsed["ratio"]))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if ratio <= 0:
            return None
        filing_date = parsed.get("filing_date")
        form = _annual_kind(parsed.get("form")) or "20-F"
        where = f"{form} 封面" if parsed.get("section") != "body" else f"{form} 正文"
        return {
            "ratio": ratio,
            "source": form,
            "filing_date": filing_date,
            "note": f"1 ADS = {format_ratio(ratio)} 股（{where} {filing_date or ''}）".replace(
                " ）", "）"
            ),
        }
    return None


def resolve_ads_ratios(
    db: Session, symbols: Iterable[str], user_id: Optional[int] = None
) -> Dict[str, Dict[str, Any]]:
    """批量版（观察清单列表）：两条 IN 查询；未解析也无规则的标的不在结果里。"""
    symbols = list(symbols)
    rules = _rule_ratios(db, user_id, symbols)
    parsed = _parsed_ratios(db, symbols)
    result: Dict[str, Dict[str, Any]] = {}
    for symbol in symbols:
        resolved = resolved_from(rules.get(symbol), parsed.get(symbol))
        if resolved is not None:
            result[symbol] = resolved
        elif ads_ratio_missing(parsed.get(symbol)):
            entry = {
                "ratio": None,
                "missing": True,
                "form": _annual_kind(parsed[symbol].get("form")),
            }
            if parsed[symbol].get("ads_listed") is None:
                entry["unknown"] = True  # 封面无法识别：是否以 ADS 交易都不知道
            result[symbol] = entry
    return result


def ads_ratio_missing(parsed: Optional[Dict[str, Any]]) -> bool:
    """纯函数：10-K 发行人没有可用比例、又不能确认是普通股——估值不能按 1:1。

    两种情形（v3，#352）：封面登记了 ADS 却解析不出比例（ads_listed=True），或封面无法识别、
    不知道是否以 ADS 交易（ads_listed=None，PR #358 评审 P2）。**只有明确 ads_listed=False
    才按普通股 1:1**。20-F 发行人没有比例时估值由表单本身判 indeterminate
    （security_profile_service），这里只补 10-K。"""
    if not parsed or parsed.get("ratio"):
        return False
    return _annual_kind(parsed.get("form")) == "10-K" and parsed.get("ads_listed") is not False


def resolve_ads_ratio(
    db: Session, symbol: str, user_id: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """{ratio: Decimal, source: 'rule'|'20-F'|'10-K', note, filing_date} 或 None；
    10-K 封面登记了 ADS 却无比例、或封面无法识别（且无用户规则）时为
    {ratio: None, missing: True, form[, unknown: True]}。

    user_id 为 None（无用户上下文）时只用解析值——用户规则是用户域数据。"""
    return resolve_ads_ratios(db, [symbol], user_id).get(symbol)
