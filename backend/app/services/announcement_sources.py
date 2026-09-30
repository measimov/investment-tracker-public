"""官方公告三源：抓取（薄，复用 report_fetchers 的限速与请求头）+ 解析（纯函数，金样测试）。

统一记录形状 AnnouncementRecord（dict）：
    symbol, market, source, source_id, published_at(aware UTC), title, url, category_raw, payload
ann_date 与分类在同步层按业务时区 / 分类器补上。

- 巨潮（A/B 股）：hisAnnouncement 不限类别；B 股的 orgId 即对应 A 股公司（实测 900926 →
  gssh0600845），检索须用 orgId 里的 A 股代码，按 B 股代码查恒为 0 条。ETF 无 orgId → unsupported。
- 披露易（港股）：全部类别；公司一年公告可达数百条（02333 半年 186 条），按 60 天窗口分段。
- EDGAR（美股）：submissions 的 filings.recent 列式数组；只收 EDGAR_FORMS 列出的表单。
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from . import report_fetchers
from .market_sessions import market_timezone

AnnouncementRecord = Dict[str, Any]

SOURCE_CNINFO = "cninfo"
SOURCE_HKEX = "hkexnews"
SOURCE_EDGAR = "edgar"

_CNINFO_STATIC = "https://static.cninfo.com.cn"
_HKEX_BASE = "https://www1.hkexnews.hk"
_HK_TZ = market_timezone("港股")
_ORG_A_CODE = re.compile(r"^gss[hz]0(\d{6})$")
HKEX_WINDOW_DAYS = 60
CNINFO_MAX_PAGES = 40  # 30 条/页；一年上限约 1200 条，远超实测最多的标的


class UnsupportedSymbol(Exception):
    """该标的没有可用的官方公告源（ETF 等无 orgId / 港股无 stockId / 美股无 CIK）。"""


# ---------------------------------------------------------------------------
# 巨潮
# ---------------------------------------------------------------------------


def cninfo_stock_param(symbol: str) -> str:
    """巨潮检索用的 `代码,orgId`。B 股用 orgId 对应的 A 股代码；映射表确认无此码才抛
    UnsupportedSymbol，映射表加载失败原样抛出（暂时性失败不得记成「无官方源」）。"""
    # strict：映射表加载失败抛出（按失败处理、水位不推进），只有确认查无此码才是 unsupported
    org_id = report_fetchers.cninfo_org_id(symbol, strict=True)
    if not org_id:
        raise UnsupportedSymbol(f"巨潮无 {symbol} 的 orgId（ETF/基金或未收录）")
    match = _ORG_A_CODE.match(org_id)
    code = match.group(1) if match else symbol
    return f"{code},{org_id}"


def parse_cninfo_rows(
    rows: Iterable[Dict[str, Any]], symbol: str, market: str
) -> List[AnnouncementRecord]:
    records = []
    for row in rows:
        source_id = str(row.get("announcementId") or "").strip()
        ts = row.get("announcementTime")
        title = _clean_title(row.get("announcementTitle"))
        if not source_id or not isinstance(ts, (int, float)) or not title:
            continue
        adjunct = str(row.get("adjunctUrl") or "")
        records.append(
            {
                "symbol": symbol,
                "market": market,
                "source": SOURCE_CNINFO,
                "source_id": source_id,
                "published_at": datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
                "title": title,
                "url": f"{_CNINFO_STATIC}/{adjunct}" if adjunct else "",
                "category_raw": str(row.get("announcementType") or ""),
                "payload": {
                    "sec_code": row.get("secCode"),
                    "sec_name": row.get("secName"),
                    "column_id": row.get("columnId"),
                    "adjunct_type": row.get("adjunctType"),
                    "adjunct_size_kb": row.get("adjunctSize"),
                },
            }
        )
    return records


def fetch_cninfo(symbol: str, market: str, start: date, end: date) -> List[AnnouncementRecord]:
    stock = cninfo_stock_param(symbol)
    se_date = f"{start.isoformat()}~{end.isoformat()}"
    records: List[AnnouncementRecord] = []
    for page in range(1, CNINFO_MAX_PAGES + 1):
        body = report_fetchers.cninfo_announcement_page(stock, se_date=se_date, page=page)
        rows = body.get("announcements") or []
        records.extend(parse_cninfo_rows(rows, symbol, market))
        if not body.get("hasMore") or not rows:
            break
    return records


# ---------------------------------------------------------------------------
# 披露易
# ---------------------------------------------------------------------------


def parse_hkex_rows(
    rows: Iterable[Dict[str, Any]], symbol: str, market: str = "港股"
) -> List[AnnouncementRecord]:
    records = []
    for row in rows:
        source_id = str(row.get("NEWS_ID") or "").strip()
        title = _clean_title(row.get("TITLE"))
        published = _parse_hk_datetime(row.get("DATE_TIME"))
        if not source_id or not title or published is None:
            continue
        link = str(row.get("FILE_LINK") or "")
        records.append(
            {
                "symbol": symbol,
                "market": market,
                "source": SOURCE_HKEX,
                "source_id": source_id,
                "published_at": published,
                "title": title,
                "url": f"{_HKEX_BASE}{link}" if link else "",
                "category_raw": html.unescape(str(row.get("LONG_TEXT") or "")).strip(),
                "payload": {
                    "file_type": row.get("FILE_TYPE"),
                    "file_info": row.get("FILE_INFO"),
                    "stock_name": row.get("STOCK_NAME"),
                },
            }
        )
    return records


def fetch_hkex(symbol: str, start: date, end: date) -> List[AnnouncementRecord]:
    records: List[AnnouncementRecord] = []
    window_end = end
    while window_end >= start:
        window_start = max(start, window_end - timedelta(days=HKEX_WINDOW_DAYS - 1))
        rows = report_fetchers.hkex_announcements_raw(
            symbol, from_date=window_start.strftime("%Y%m%d"), to_date=window_end.strftime("%Y%m%d")
        )
        if rows is None:
            raise UnsupportedSymbol(f"披露易无 {symbol} 的 stockId")
        records.extend(parse_hkex_rows(rows, symbol))
        window_end = window_start - timedelta(days=1)
    return records


def _parse_hk_datetime(value: Any) -> Optional[datetime]:
    try:
        local = datetime.strptime(str(value or "").strip(), "%d/%m/%Y %H:%M")
    except ValueError:
        return None
    return local.replace(tzinfo=_HK_TZ).astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# EDGAR
# ---------------------------------------------------------------------------

# 入库的表单（其余如 SD、11-K、CORRESP 等对持仓判断无意义，不入库）
EDGAR_FORMS = re.compile(
    r"^(8-K|6-K|10-K|10-Q|20-F|40-F)(/A)?$|^(S-1|F-1|S-3|F-3|S-8|F-3ASR|S-3ASR)(/A)?$|^424B\d*$"
    r"|^SC 13[DG](/A)?$|^(4|144)(/A)?$|^(DEF 14A|DEFA14A)$"
)
EDGAR_FORM_LABELS = {
    "8-K": "8-K 重大事项报告",
    "6-K": "6-K 外国发行人报告",
    "10-K": "10-K 年度报告",
    "10-Q": "10-Q 季度报告",
    "20-F": "20-F 年度报告",
    "40-F": "40-F 年度报告",
    "4": "Form 4 内部人交易",
    "144": "Form 144 拟出售股份通知",
    "SC 13D": "13D 大股东持股（主动）",
    "SC 13G": "13G 大股东持股（被动）",
    "DEF 14A": "股东大会委托书",
    "DEFA14A": "股东大会补充委托书",
}
EDGAR_ITEM_LABELS = {
    "1.01": "订立重大协议",
    "1.02": "终止重大协议",
    "1.03": "破产或接管",
    "2.01": "完成收购或处置资产",
    "2.02": "经营业绩",
    "2.03": "新增重大债务",
    "2.04": "债务加速到期",
    "2.05": "退出或处置成本",
    "2.06": "重大减值",
    "3.01": "退市通知",
    "3.02": "未登记股份发行",
    "3.03": "股东权利变更",
    "4.01": "更换审计师",
    "4.02": "已发布财报不可依赖",
    "5.01": "控制权变更",
    "5.02": "董事/高管变动",
    "5.03": "章程修订",
    "5.07": "股东投票结果",
    "7.01": "公平披露",
    "8.01": "其他事项",
    "9.01": "财务报表及附件",
}


def edgar_title(form: str, items: str, description: str) -> str:
    base_form = form[:-2] if form.endswith("/A") else form
    label = EDGAR_FORM_LABELS.get(base_form) or (
        f"{form} 发行文件" if re.match(r"^(S-|F-|424B)", form) else form
    )
    if form.endswith("/A"):
        label += "（修订）"
    item_labels = [
        f"{EDGAR_ITEM_LABELS.get(item.strip(), item.strip())}（{item.strip()}）"
        for item in (items or "").split(",")
        if item.strip()
    ]
    title = f"{label}：{'、'.join(item_labels)}" if item_labels else label
    desc = (description or "").strip()
    redundant = {form.upper(), base_form.upper(), f"FORM {base_form}".upper(), "OWNERSHIP DOCUMENT"}
    if desc and desc.upper() not in redundant and not desc.upper().startswith("SCHEDULE 13"):
        title = f"{title} — {desc}"
    return title


def parse_edgar_recent(
    submissions: Dict[str, Any], symbol: str, *, since: date, market: str = "美股"
) -> List[AnnouncementRecord]:
    recent = ((submissions or {}).get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    cik = str((submissions or {}).get("cik") or "").lstrip("0")
    records = []
    for i, form in enumerate(forms):
        form = str(form or "").strip()
        if not EDGAR_FORMS.match(form):
            continue
        accession = _col(recent, "accessionNumber", i)
        filed = _col(recent, "filingDate", i)
        if not accession or not filed:
            continue
        try:
            filed_day = date.fromisoformat(filed[:10])
        except ValueError:
            continue
        if filed_day < since:
            continue
        accepted = _col(recent, "acceptanceDateTime", i)
        published = _parse_edgar_time(accepted) or datetime(
            filed_day.year, filed_day.month, filed_day.day, tzinfo=timezone.utc
        )
        items = _col(recent, "items", i)
        primary = _col(recent, "primaryDocument", i)
        folder = accession.replace("-", "")
        records.append(
            {
                "symbol": symbol,
                "market": market,
                "source": SOURCE_EDGAR,
                "source_id": accession,
                "published_at": published,
                "title": edgar_title(form, items, _col(recent, "primaryDocDescription", i)),
                "url": (
                    f"https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{primary}"
                    if cik and primary
                    else ""
                ),
                "category_raw": f"{form}|{items}",
                "payload": {"form": form, "items": items, "filing_date": filed, "cik": cik},
            }
        )
    return records


def fetch_edgar(symbol: str, start: date) -> List[AnnouncementRecord]:
    found = report_fetchers.edgar_lookup(symbol)
    if not found:
        raise UnsupportedSymbol(f"EDGAR 无 {symbol} 的 CIK")
    submissions = report_fetchers.edgar_submissions(int(found["cik"]))
    return parse_edgar_recent(submissions, symbol, since=start)


def _col(recent: Dict[str, Any], key: str, index: int) -> str:
    values = recent.get(key) or []
    return str(values[index] or "").strip() if index < len(values) else ""


def _parse_edgar_time(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------


def fetch_announcements(
    symbol: str, market: str, start: date, end: date
) -> List[AnnouncementRecord]:
    """按市场分派到对应官方源；无源的标的抛 UnsupportedSymbol。"""
    if market in ("A股", "B股"):
        return fetch_cninfo(symbol, market, start, end)
    if market == "港股":
        return fetch_hkex(symbol, start, end)
    if market == "美股":
        return [r for r in fetch_edgar(symbol, start) if r["published_at"].date() <= end]
    raise UnsupportedSymbol(f"{market} 无官方公告源")


def _clean_title(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"</?em>", "", text)
    return re.sub(r"\s+", " ", text).strip()
