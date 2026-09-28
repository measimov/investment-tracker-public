"""披露易「股票發行人現金股息公告」（EF001 表格）：清单 + 下载 + 纯函数解析 + 缓存。

港股分红公告的唯一结构化来源。港交所自 2022 年起要求发行人用标准表格
（t1code=10000 公告及通告 / t2Gcode=3 / t2code=13251「股息或分派（公告表格）」）
披露现金股息，2 页 PDF，pdfplumber 文本干净、字段为「标签 值」一行一项。
更早的股息只写在业绩公告正文里（13250），不解析——那一段历史没有结构化来源。

分层：
- `list_dividend_forms`：检索清单（复用 report_fetchers 的 stockId 映射、限速桶与
  Referer，限速桶与披露易年报下载共用）；
- `download_form_text`：PDF 下载（report_fetchers 的墙钟看门狗）→ 逐页文本；
- `parse_dividend_form` / `parse_dividend_form_with_reason`：**纯函数**，
  金样 `tests/fixtures/hkex_dividend/`。认不出的一律返回 None 并给原因，不猜；
- `resolve_current_dividends`：同一笔股息（报告期末 + 股息类型 + 股息性质）的
  多份公告按公告时间取最新一份（更新公告取代旧公告，撤回公告取消该笔股息）；
- `ensure_dividend_forms`：DB 缓存（`security_profile_data` 数据集
  `hkex_dividend_form`，period_key = 披露易文档号，全局）。文本随解析结果一起存，
  bump `HKEX_DIVIDEND_PARSER_VERSION` 只需重解析、零下载。
"""

import io
import re
from dataclasses import dataclass, field as dataclass_field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from ..core.logging import get_app_logger
from . import report_fetchers

logger = get_app_logger(__name__)

# 改标签词表/正则/字段口径就 bump：缓存里的 payload 带着原文，重解析零下载
HKEX_DIVIDEND_PARSER_VERSION = 1

DATASET = "hkex_dividend_form"
SOURCE = "hkexnews-dividend"

# 披露易文件类别（2026-09-28 tiertwo_c.json 实测）
FORM_T1_CODE = 10000  # 公告及通告
FORM_T2G_CODE = 3  # 股息或分派
FORM_T2_CODE = 13251  # 股息或分派（公告表格）

# 表格里的币种写法 → 本仓库币种代码
_CURRENCY_ALIASES = {"RMB": "CNY", "CNH": "CNY"}

# 需要取值的字段标签（繁体，表格原文）
FIELD_LABELS: Dict[str, str] = {
    "issuer_name": "發行人名稱",
    "stock_code": "股份代號",
    "title": "公告標題",
    "announcement_date": "公告日期",
    "status": "公告狀態",
    "update_reason": "更新/撤回理由",
    "dividend_type": "股息類型",
    "dividend_nature": "股息性質",
    "financial_year_end": "財政年末",
    "period_end": "宣派股息的報告期末",
    "declared": "宣派股息",
    "approval_date": "股東批准日期",
    "payment": "派息金額及公司預設派發貨幣",
    "exchange_rate": "匯率",
    "ex_date": "除淨日",
    "book_close": "暫停辦理股份過戶登記手續之日期",
    "record_date": "記錄日期",
    "pay_date": "股息派發日",
}

# 其余标签与分节标题：只用于判断「这一行不是别的字段的折行值」
_OTHER_LABELS = (
    "多櫃檯股份代號及貨幣",
    "相關股份代號及名稱",
    "為符合獲取股息分派而遞交股份過戶",
    "文件之最後時限",
    "股份過戶登記處及其地址",
    "股息所涉及的代扣所得稅",
    "發行人所發行上市權證/可轉換債券",
    "其他信息",
)
_SECTION_HEADERS = (
    "EF001",
    "免責聲明",
    "股票發行人現金股息公告",
    "股息信息",
    "香港過戶登記處相關信息",
    "代扣所得稅信息",
    "有關代預扣所得稅之更多補充",
    "發行人所發行上市權證/可轉換債券的相關信息",
    "其他信息",
    "發行人董事",
)
_WITHHOLDING_END_HEADERS = (
    "發行人所發行上市權證/可轉換債券的相關信息",
    "發行人所發行上市權證/可轉換債券",
    "其他信息",
    "發行人董事",
)

_FORM_MARKER = "股票發行人現金股息公告"
_PAGE_FOOTER_RE = re.compile(r"^第\s*\d+\s*頁\s*共\s*\d+\s*頁")
_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_AMOUNT_RE = re.compile(
    r"每\s*(?P<per>\d+)?\s*股\s*(?P<pre>[A-Z]{3})?\s*(?P<amount>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<post>[A-Z]{3})?"
)
_RATE_RE = re.compile(
    r"(?P<base_amt>\d+(?:\.\d+)?)\s*(?P<base>[A-Z]{3})\s*[:：]\s*"
    r"(?P<rate>\d[\d,]*(?:\.\d+)?)\s*(?P<quote>[A-Z]{3})"
)
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_NOT_APPLICABLE = "不適用"
# 首份公告常只定了宣派金额：派发金额/汇率/除淨日写「有待公佈」，后续以更新公告补齐
_PENDING = "有待公佈"
_SCRIP_RE = re.compile(r"以股代息")
_CURRENCY_ELECTION_RE = re.compile(r"(選擇|選取)[^。\n]{0,20}(貨幣|幣種|幣值)|(貨幣|幣種)[^。\n]{0,10}選擇")

STATUS_KINDS = {"新公告": "new", "更新公告": "update", "撤回公告": "withdrawal"}


# ---------------------------------------------------------------------------
# 纯函数解析
# ---------------------------------------------------------------------------


def normalize_currency(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    text = code.strip().upper()
    return _CURRENCY_ALIASES.get(text, text)


def _parse_date(text: Optional[str]) -> Optional[date]:
    if not text:
        return None
    match = _DATE_RE.search(text)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _parse_decimal(text: str) -> Optional[Decimal]:
    try:
        return Decimal(text.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None


def parse_per_share_amount(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """「每 股 5.3HKD」「每 股 0.0908RMB」「每 10 股 3HKD」→ {amount, currency}（每 1 股）。

    币种前后置都认；恰好一个币种才算数（两个不同币种写在一格里不猜）。
    """
    if not text:
        return None
    match = _AMOUNT_RE.search(text)
    if not match:
        return None
    pre, post = match.group("pre"), match.group("post")
    if pre and post and pre != post:
        return None
    currency = normalize_currency(pre or post)
    amount = _parse_decimal(match.group("amount"))
    if currency is None or amount is None:
        return None
    per = int(match.group("per") or 1)
    if per <= 0:
        return None
    return {"amount": amount / per, "currency": currency}


def parse_exchange_rate(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """「1 RMB : 1.144387HKD」→ {from: CNY, to: HKD, rate: 1.144387}（每 1 单位 from）。"""
    if not text:
        return None
    match = _RATE_RE.search(text)
    if not match:
        return None
    base_amount = _parse_decimal(match.group("base_amt"))
    rate = _parse_decimal(match.group("rate"))
    if not base_amount or rate is None:
        return None
    return {
        "from": normalize_currency(match.group("base")),
        "to": normalize_currency(match.group("quote")),
        "rate": rate / base_amount,
    }


def _clean_lines(text: str) -> List[str]:
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or _PAGE_FOOTER_RE.match(line) or line == "EF001":
            continue
        lines.append(line)
    return lines


def _label_value(line: str, label: str) -> Optional[str]:
    """行首是完整标签（其后是空白或行尾）→ 返回标签后的值（可能为空串）。"""
    if not line.startswith(label):
        return None
    rest = line[len(label):]
    if rest and not rest[0].isspace():
        return None  # 「宣派股息的報告期末」不是「宣派股息」
    return rest.strip()


def _is_structural(line: str) -> bool:
    if line in _SECTION_HEADERS:
        return True
    for label in (*FIELD_LABELS.values(), *_OTHER_LABELS):
        if _label_value(line, label) is not None:
            return True
    return False


def extract_fields(text: str) -> Dict[str, str]:
    """「标签 值」逐行取值。标签独占一行（值折行、标签垂直居中）时取上下相邻的
    非结构行拼接——00799 的「其他 / 股息類型 / 特別股息」。"""
    lines = _clean_lines(text)
    fields: Dict[str, str] = {}
    for index, line in enumerate(lines):
        for key, label in FIELD_LABELS.items():
            if key in fields:
                continue
            value = _label_value(line, label)
            if value is None:
                continue
            if not value:
                parts = []
                if index > 0 and not _is_structural(lines[index - 1]):
                    parts.append(lines[index - 1])
                if index + 1 < len(lines) and not _is_structural(lines[index + 1]):
                    parts.append(lines[index + 1])
                value = " ".join(parts)
            fields[key] = value
            break
    return fields


def _first_percent_after(text: str, anchor: str) -> Optional[Decimal]:
    position = text.find(anchor)
    if position < 0:
        return None
    match = _PERCENT_RE.search(text, position)
    return _parse_decimal(match.group(1)) if match else None


def parse_withholding(text: str) -> Dict[str, Any]:
    """代扣所得稅信息一节 → {applicable, rates_percent, non_resident_enterprise_percent,
    southbound_individual_percent, note}。

    applicable：值恰为「不適用」→ False；有任何税率或说明 → True；整节缺失 → None。
    税率只从「有關代預扣所得稅之更多補充」表里取（没有表时取整节）；按持有人类型
    定位只是展示用的提示，入账的税额永远由用户按券商到账填写。
    """
    lines = _clean_lines(text)
    start = next((i for i, line in enumerate(lines) if line == "代扣所得稅信息"), None)
    if start is None:
        return {"applicable": None, "rates_percent": [], "note": None,
                "non_resident_enterprise_percent": None, "southbound_individual_percent": None}
    block: List[str] = []
    for line in lines[start + 1:]:
        if any(line.startswith(header) for header in _WITHHOLDING_END_HEADERS):
            break
        block.append(line)
    label = "股息所涉及的代扣所得稅"
    body = " ".join(
        line.replace(label, "").strip() for line in block
    ).strip()
    table_start = body.find("有關代預扣所得稅之更多補充")
    table = body[table_start:] if table_start >= 0 else body
    rates = sorted({_parse_decimal(m) for m in _PERCENT_RE.findall(table)} - {None})
    if body == _NOT_APPLICABLE:
        applicable: Optional[bool] = False
    elif rates or body:
        applicable = True
    else:
        applicable = None
    return {
        "applicable": applicable,
        "rates_percent": rates,
        "non_resident_enterprise_percent": _first_percent_after(table, "非居民企業"),
        "southbound_individual_percent": _first_percent_after(table, "港股通"),
        "note": body[:600] or None,
    }


def parse_dividend_form_with_reason(text: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """EF001 现金股息公告文本 → (字段 dict, None) 或 (None, 原因)。

    必需：股份代號、公告日期、公告狀態（新/更新/撤回）；非撤回公告另需股息類型、
    宣派股息的報告期末、股息性質、除淨日与每股派息金额（预设派发货币；缺失时仅当宣派币种
    即派发币种才用宣派金额）。任何一项认不出都返回 None——绝不猜。
    """
    if _FORM_MARKER not in (text or ""):
        return None, "不是「股票發行人現金股息公告」表格"
    fields = extract_fields(text)

    stock_code = (fields.get("stock_code") or "").split()
    if not stock_code or not stock_code[0].isdigit():
        return None, "缺少股份代號"
    announcement_date = _parse_date(fields.get("announcement_date"))
    if announcement_date is None:
        return None, "缺少公告日期"
    status = (fields.get("status") or "").strip()
    status_kind = STATUS_KINDS.get(status)
    if status_kind is None:
        return None, f"無法識別的公告狀態: {status or '（空）'}"

    declared = parse_per_share_amount(fields.get("declared"))
    payment = parse_per_share_amount(fields.get("payment"))
    exchange_rate = parse_exchange_rate(fields.get("exchange_rate"))
    book_close_text = fields.get("book_close") or ""
    book_dates = [_parse_date(m.group(0)) for m in _DATE_RE.finditer(book_close_text)]
    form: Dict[str, Any] = {
        "issuer_name": fields.get("issuer_name") or None,
        "stock_code": stock_code[0].zfill(5),
        "title": fields.get("title") or None,
        "announcement_date": announcement_date,
        "status": status,
        "status_kind": status_kind,
        "update_reason": fields.get("update_reason") or None,
        "dividend_type": (fields.get("dividend_type") or "").strip() or None,
        "dividend_nature": (fields.get("dividend_nature") or "").strip() or None,
        "financial_year_end": _parse_date(fields.get("financial_year_end")),
        "period_end": _parse_date(fields.get("period_end")),
        "declared": declared,
        "payment": payment,
        "exchange_rate": exchange_rate,
        "approval_date": _parse_date(fields.get("approval_date")),
        "ex_date": _parse_date(fields.get("ex_date")),
        "book_close": (
            {"start": book_dates[0], "end": book_dates[-1]} if len(book_dates) >= 2 else None
        ),
        "record_date": _parse_date(fields.get("record_date")),
        "pay_date": _parse_date(fields.get("pay_date")),
        "withholding": parse_withholding(text),
        "scrip_option": bool(_SCRIP_RE.search(text)),
        "currency_election": bool(_CURRENCY_ELECTION_RE.search(text)),
    }
    form["pending"] = False
    if status_kind == "withdrawal":
        return form, None

    if not form["dividend_type"]:
        return None, "缺少股息類型"
    if form["period_end"] is None:
        return None, "缺少宣派股息的報告期末"
    if not form["dividend_nature"]:
        # 性質是取代关系身份三元组的一员（末期普通 vs 末期特別是两笔）：缺了就不知道这份
        # 公告更新的是哪一笔，只能按身份不完整处理（PR #249 评审 P2）
        return None, "缺少股息性質"
    payment_text = (fields.get("payment") or "").strip()
    ex_date_text = (fields.get("ex_date") or "").strip()
    if (form["ex_date"] is None and _PENDING in ex_date_text) or (
        payment is None and _PENDING in payment_text
    ):
        # 合法的「待定」公告：身份字段齐全、参与取代关系，但不产生建议/事件/复权
        form["pending"] = True
        return form, None
    if form["ex_date"] is None:
        return None, "缺少除淨日"
    if payment is None:
        if payment_text and payment_text != _NOT_APPLICABLE:
            return None, f"無法識別派息金額: {payment_text[:40]}"
        if declared is None:
            return None, "缺少每股派息金額"
        if exchange_rate and exchange_rate["from"] != exchange_rate["to"]:
            return None, "缺少預設派發貨幣金額（宣派與派發幣種不同）"
        form["payment"] = dict(declared)
    if form["payment"]["amount"] <= 0:
        return None, "每股派息金額不為正"
    return form, None


def parse_dividend_form(text: str) -> Optional[Dict[str, Any]]:
    return parse_dividend_form_with_reason(text)[0]


def partial_identity(text: str) -> Optional[Tuple[Any, ...]]:
    """解析失败的表格尽量认出「是哪一笔股息」：报告期末 + 股息类型 + 股息性质。

    金额/日期认不出，但身份认得出时，只需挂起这一笔（它可能修订了金额或撤回）；
    身份三项（含股息性質）缺任一项都算认不出，无从判断影响范围，由调用方挂起整个标的
    （PR #249 评审 P2）。
    """
    if _FORM_MARKER not in (text or ""):
        return None
    fields = extract_fields(text)
    identity = (
        _parse_date(fields.get("period_end")),
        (fields.get("dividend_type") or "").strip() or None,
        (fields.get("dividend_nature") or "").strip() or None,
    )
    # 三项缺一即认不出：只认前两项会生成一个与现行股息对不上的新身份，旧公告照样现行
    return identity if identity_complete(identity) else None


def identity_complete(identity: Tuple[Any, ...]) -> bool:
    return all(part not in (None, "") for part in identity)


# ---------------------------------------------------------------------------
# 取代关系（纯函数）
# ---------------------------------------------------------------------------


def dividend_identity(form: Dict[str, Any]) -> Tuple[Any, ...]:
    """同一笔股息的身份：报告期末 + 股息类型 + 股息性质（末期普通 vs 末期特別是两笔）。"""
    return (form.get("period_end"), form.get("dividend_type"), form.get("dividend_nature"))


@dataclass
class DividendResolution:
    """取代关系解析结果。

    - current：现行股息（每笔取最新一份公告，撤回与待定已剔除）；
    - pending：最新一份是「有待公佈」的待定公告；
    - blocked：最新一份**解析失败但身份认得出**——它可能修订金额或撤回旧公告，更早的
      公告不得顶替成现行值；`ex_dates` 是该笔旧公告出现过的除净日，调用方据此保留
      （不改写也不删除）已有的建议与事件；
    - unscoped：解析失败且身份认不出的表格，以及身份字段缺失的撤回公告——无从判断影响
      哪一笔，调用方须挂起整个标的的写入。
    """

    current: List[Dict[str, Any]] = dataclass_field(default_factory=list)
    pending: List[Dict[str, Any]] = dataclass_field(default_factory=list)
    blocked: List[Dict[str, Any]] = dataclass_field(default_factory=list)
    unscoped: List[Dict[str, Any]] = dataclass_field(default_factory=list)

    @property
    def protected_ex_dates(self) -> Set[date]:
        return {day for item in self.blocked for day in item["ex_dates"]}


def resolve_dividend_resolution(entries: Iterable[Dict[str, Any]]) -> DividendResolution:
    """[{form | unresolved, sort_key, ...}] → DividendResolution（每笔股息取最新一份）。

    `sort_key` 由调用方给出（清单时间 + 文档号），越大越新。解析失败的表格以
    `{"unresolved": True, "identity": (…) | None}` 参与：它比已解析的旧公告新时，
    这笔股息挂起，而不是让旧公告继续当现行值。
    """
    resolution = DividendResolution()
    latest: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    seen_ex_dates: Dict[Tuple[Any, ...], Set[date]] = {}
    for entry in entries:
        if entry.get("unresolved"):
            key = entry.get("identity")
            if key is None:
                resolution.unscoped.append(entry)
                continue
        else:
            form = entry["form"]
            key = dividend_identity(form)
            if not identity_complete(key):
                # 撤回（或身份字段不全的公告）对应哪一笔认不出：按其中一两项去匹配会生成
                # 一个对不上的新身份，被撤回/被更新的股息照样现行——整标的挂起
                resolution.unscoped.append(entry)
                continue
            if form.get("ex_date"):
                seen_ex_dates.setdefault(key, set()).add(form["ex_date"])
        current = latest.get(key)
        if current is None or entry["sort_key"] > current["sort_key"]:
            latest[key] = entry

    for key, entry in latest.items():
        if entry.get("unresolved"):
            resolution.blocked.append({
                "identity": key,
                "entry": entry,
                "ex_dates": sorted(seen_ex_dates.get(key, set())),
            })
            continue
        form = entry["form"]
        if form["status_kind"] == "withdrawal":
            continue
        (resolution.pending if form.get("pending") else resolution.current).append(entry)
    resolution.current.sort(key=lambda entry: (entry["form"]["ex_date"], entry["sort_key"]))
    resolution.pending.sort(key=lambda entry: entry["sort_key"])
    resolution.blocked.sort(key=lambda item: item["entry"]["sort_key"])
    return resolution


def resolve_dividend_states(
    entries: Iterable[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(现行股息, 待定股息)；挂起的股息两边都不出现。写库路径用 resolve_dividend_resolution。"""
    resolution = resolve_dividend_resolution(entries)
    return resolution.current, resolution.pending


def resolve_current_dividends(entries: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """现行股息（取代关系已解析、撤回/待定/挂起已剔除），按除净日排序。"""
    return resolve_dividend_resolution(entries).current


# ---------------------------------------------------------------------------
# JSON 往返（缓存 payload）
# ---------------------------------------------------------------------------

_DATE_FIELDS = (
    "announcement_date", "financial_year_end", "period_end", "approval_date",
    "ex_date", "record_date", "pay_date",
)


def form_to_json(form: Dict[str, Any]) -> Dict[str, Any]:
    def convert(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, dict):
            return {k: convert(v) for k, v in value.items()}
        if isinstance(value, list):
            return [convert(v) for v in value]
        return value

    return convert(form)


def form_from_json(payload: Dict[str, Any]) -> Dict[str, Any]:
    form = dict(payload)
    for field in _DATE_FIELDS:
        if form.get(field):
            form[field] = date.fromisoformat(form[field])
    if form.get("book_close"):
        form["book_close"] = {
            k: date.fromisoformat(v) if v else None for k, v in form["book_close"].items()
        }
    for field in ("declared", "payment"):
        if form.get(field):
            form[field] = {**form[field], "amount": Decimal(form[field]["amount"])}
    if form.get("exchange_rate"):
        form["exchange_rate"] = {**form["exchange_rate"],
                                 "rate": Decimal(form["exchange_rate"]["rate"])}
    withholding = form.get("withholding") or {}
    if withholding:
        form["withholding"] = {
            **withholding,
            "rates_percent": [Decimal(v) for v in withholding.get("rates_percent") or []],
            **{
                key: Decimal(withholding[key]) if withholding.get(key) is not None else None
                for key in ("non_resident_enterprise_percent", "southbound_individual_percent")
            },
        }
    return form


# ---------------------------------------------------------------------------
# HTTP（清单与下载，可整体 monkeypatch）
# ---------------------------------------------------------------------------


def document_id(url: str) -> str:
    """披露易 PDF 链接 → 文档号（`.../2026051901240_c.pdf` → 2026051901240）。"""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    return name.split(".", 1)[0].split("_", 1)[0][:40]


def _list_datetime(text: str) -> Optional[datetime]:
    try:
        return datetime.strptime(text.strip(), "%d/%m/%Y %H:%M")
    except (ValueError, AttributeError):
        return None


def list_dividend_forms(symbol: str, from_date: date, to_date: date) -> List[Dict[str, Any]]:
    """披露易现金股息公告表格清单 → [{doc_id, title, listed_at, url}]（公告时间倒序）。"""
    documents = report_fetchers.hkex_title_search(
        symbol,
        t1code=FORM_T1_CODE,
        t2g_code=FORM_T2G_CODE,
        t2code=FORM_T2_CODE,
        from_date=from_date.strftime("%Y%m%d"),
        to_date=to_date.strftime("%Y%m%d"),
    )
    forms = []
    for document in documents:
        listed_at = _list_datetime(document.get("ann_date") or "")
        forms.append({
            "doc_id": document_id(document["url"]),
            "title": document.get("title") or "",
            "listed_at": listed_at.isoformat() if listed_at else None,
            "url": document["url"],
        })
    return forms


def extract_pdf_text(data: bytes) -> str:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)


def download_form_text(url: str) -> str:
    return extract_pdf_text(report_fetchers.download_report_pdf(url, source="hkexnews"))


# ---------------------------------------------------------------------------
# DB 缓存
# ---------------------------------------------------------------------------


def _load_cached(db: Session, symbol: str, market: str) -> Dict[str, Dict[str, Any]]:
    from ..models.security_profile import SecurityProfileData

    rows = db.query(SecurityProfileData).filter(
        SecurityProfileData.symbol == symbol,
        SecurityProfileData.market == market,
        SecurityProfileData.dataset == DATASET,
    ).all()
    return {row.period_key: dict(row.payload or {}) for row in rows}


def _store(db: Session, symbol: str, market: str, doc_id: str, payload: Dict[str, Any]) -> None:
    from .security_profile_service import upsert_profile_row

    upsert_profile_row(db, symbol, market, DATASET, doc_id, payload)


def _build_payload(listing: Dict[str, Any], text: str) -> Dict[str, Any]:
    form, reason = parse_dividend_form_with_reason(text)
    return {
        "parser_version": HKEX_DIVIDEND_PARSER_VERSION,
        "doc_id": listing["doc_id"],
        "url": listing["url"],
        "title": listing.get("title"),
        "listed_at": listing.get("listed_at"),
        "status": "ok" if form else "unparsed",
        "reason": reason,
        "form": form_to_json(form) if form else None,
        "text": text,
    }


def ensure_dividend_forms(
    db: Session,
    symbol: str,
    market: str,
    *,
    from_date: date,
    to_date: date,
    on_download: Optional[Callable[[], None]] = None,
) -> Dict[str, Any]:
    """检索清单 → 只下载缓存里没有的表格 → 解析落库；返回本标的全部缓存表格。

    返回 {"entries": [...], "downloaded": n, "unparsed": [{doc_id, url, reason}]}；
    entries 是 resolve_current_dividends 的输入（含缓存里早于 from_date 的历史表格，
    复权因子需要全部历史）。清单或任一下载失败直接抛出——漏掉一份更新公告会让
    旧金额冒充现行值，宁可整标的失败重来。`on_download` 每下载一份回调一次（续租）。
    """
    listing = list_dividend_forms(symbol, from_date, to_date)
    cached = _load_cached(db, symbol, market)
    downloaded = 0
    for item in listing:
        existing = cached.get(item["doc_id"])
        if existing and existing.get("text") is not None:
            if existing.get("parser_version") != HKEX_DIVIDEND_PARSER_VERSION:
                payload = _build_payload({**existing, **item}, existing["text"])
                _store(db, symbol, market, item["doc_id"], payload)
                cached[item["doc_id"]] = payload
            continue
        text = download_form_text(item["url"])
        downloaded += 1
        payload = _build_payload(item, text)
        _store(db, symbol, market, item["doc_id"], payload)
        cached[item["doc_id"]] = payload
        if on_download:
            on_download()
    # 清单外的缓存行（更早窗口）若解析器升版也顺带重解析
    for doc_id, payload in list(cached.items()):
        stale = payload.get("parser_version") != HKEX_DIVIDEND_PARSER_VERSION
        if stale and payload.get("text") is not None:
            refreshed = _build_payload(payload, payload["text"])
            _store(db, symbol, market, doc_id, refreshed)
            cached[doc_id] = refreshed
    return {
        "entries": cached_entries(cached.values()),
        "downloaded": downloaded,
        "unparsed": [
            {"doc_id": p.get("doc_id"), "url": p.get("url"), "reason": p.get("reason"),
             "listed_at": p.get("listed_at")}
            for p in cached.values()
            if p.get("status") != "ok"
        ],
    }


def cached_entries(payloads: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """缓存 payload → resolve_dividend_resolution 的输入。

    解析失败的表格**也要参与**（`unresolved`，身份从原文尽量认出）：此前在这里直接丢弃，
    更新公告认不出时旧公告会静默重新成为现行值（PR #249 评审 P2）。
    """
    entries = []
    for payload in payloads:
        if payload.get("status") != "ok" or not payload.get("form"):
            text = payload.get("text") or ""
            identity = partial_identity(text)
            announced = _parse_date(extract_fields(text).get("announcement_date")) if text else None
            entries.append({
                "unresolved": True,
                "identity": identity,
                "reason": payload.get("reason"),
                "doc_id": payload.get("doc_id"),
                "url": payload.get("url"),
                # 与已解析表格同一排序口径；清单时间缺失时用原文里的公告日期
                "sort_key": (
                    payload.get("listed_at") or (announced.isoformat() if announced else ""),
                    payload.get("doc_id") or "",
                ),
            })
            continue
        form = form_from_json(payload["form"])
        entries.append({
            "form": form,
            "doc_id": payload.get("doc_id"),
            "url": payload.get("url"),
            # 清单时间（精确到分）优先，其次公告日期；同一分钟内按文档号
            "sort_key": (
                payload.get("listed_at") or form["announcement_date"].isoformat(),
                payload.get("doc_id") or "",
            ),
        })
    return entries


def load_cached_entries(db: Session, symbol: str, market: str) -> List[Dict[str, Any]]:
    """只读缓存（复权因子重算用，零网络）。"""
    return cached_entries(_load_cached(db, symbol, market).values())
