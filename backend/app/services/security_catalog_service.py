"""标的全集（security catalog）：参考数据集的同步、检索与按需解析。

定位：目录只提供名称 / 拼音 / 类型 / 上市状态等**参考信息**；账本（交易/持仓/
自选）仍是用户自己那几行的权威——`search_securities` 把账本候选排在前面、目录
只补账本缺的字段，所以不会成为第二个漂移源。

来源（`LOADERS`，全部免费档 / 官方公开）：
- A股：Tushare `stock_basic`（含退市，`cnspell` 拼音）+ `fund_basic(market=E)` 场内基金
- 港股：港交所官方證券名單 xlsx（繁体名/分類/次分類，只收 股本 + 交易所買賣產品 +
  REIT，剔除窝轮/牛熊证/债券）+ Tushare `hk_basic`（简体名/拼音/英文名；不含 ETF）
- 美股：Tushare `us_basic`（中文名/英文名/classify）
- B股 / 漏网标的：`resolve_security` 按需调腾讯行情取名并沉淀（无免费全量名单）
- 新加坡股 / 加密货币：无来源，前端允许自由文本

每个来源是独立 loader：列级 COALESCE upsert 让 HKEX（繁体名/类型/板块）与
hk_basic（简体名/拼音）合成同一行；一源失败只在 `security_catalog_syncs` 记 failed、
旧行留存可搜（显式降级）。**永不删行**，退市只翻 `list_status`。

拼音兜底 `pypinyin`、繁简转换 `zhconv` 都是可选依赖：缺库时对应字段留 NULL，
`catalog_health().capabilities` 报出，不冒充成功。
"""

from __future__ import annotations

import io
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

import requests
from sqlalchemy import case, func, or_, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..config import settings
from ..core.logging import get_app_logger
from ..database import SessionLocal
from ..models.holding import Holding
from ..models.security_catalog import SecurityCatalogEntry, SecurityCatalogSync
from ..models.transaction import Transaction
from ..models.watchlist_item import WatchlistItem
from .stock_price_service import (
    classify_tushare_error,
    fetch_tencent_quote_name,
    get_exchange_type,
    tushare_query,
)
from .symbol_normalization import normalize_manual_symbol

logger = get_app_logger(__name__)

try:  # 可选依赖：拼音首字母兜底（fund_basic / us_basic / HKEX 独有行没有拼音字段）
    from pypinyin import Style as _PinyinStyle
    from pypinyin import lazy_pinyin as _lazy_pinyin

    PINYIN_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于安装环境
    PINYIN_AVAILABLE = False

try:  # 可选依赖：繁→简（HKEX 独有的 ETF/REIT/人民币柜台只有繁体名）
    from zhconv import convert as _zh_convert

    ZHCONV_AVAILABLE = True
except ImportError:  # pragma: no cover
    ZHCONV_AVAILABLE = False

A_MARKET, B_MARKET, HK_MARKET, US_MARKET = "A股", "B股", "港股", "美股"
CATALOG_MARKETS = (A_MARKET, B_MARKET, HK_MARKET, US_MARKET)
TENCENT_RESOLVE_MARKETS = CATALOG_MARKETS

HKEX_LIST_URL_ZH = (
    "https://www.hkex.com.hk/chi/services/trading/securities/securitieslists/ListOfSecurities_c.xlsx"
)
HKEX_LIST_URL_EN = (
    "https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx"
)
# 只收股本 / 交易所買賣產品 / REIT：窝轮 7250 + 牛熊证 6049 + 债券 1318 会淹没下拉检索
HKEX_KEEP_CATEGORIES = {"股本", "交易所買賣產品", "房地產投資信託基金"}
_HKEX_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}
_HKEX_TIMEOUT = (10, 90)

PERIODIC_INTERVAL_SECONDS = 6 * 3600  # tick；真实新鲜度按 last_success_at 判（重启不重拉）
UPSERT_CHUNK = 1000
SOURCE_TENCENT = "tencent-quote"
SOURCE_HKEX_DAYQUOT = "hkex-dayquot"

COVERAGE_NOTES = [
    "A股：Tushare stock_basic（含已退市）+ 场内 ETF/LOF（fund_basic）",
    "港股：港交所證券名單（股本 / ETF / REIT，不含窝轮牛熊证债券）+ Tushare hk_basic 补简体名与拼音",
    "美股：Tushare us_basic（普通股 / ADR / 优先股 / GDR）",
    "B股：无免费全量名单，输入代码时按需向腾讯行情解析名称并沉淀",
    "新加坡股 / 加密货币：无来源，请手工填写名称",
]


class CatalogFormatError(ValueError):
    """来源格式与预期不符（表头缺失 / 零行）——绝不静默入库。"""


@dataclass(frozen=True)
class CatalogRow:
    """loader 输出。None = 该来源不知道该列（upsert 时 COALESCE 保留库内值）。"""

    symbol: str
    market: str
    name: Optional[str] = None
    name_en: Optional[str] = None
    name_trad: Optional[str] = None
    pinyin: Optional[str] = None
    currency: Optional[str] = None
    currency_source: Optional[str] = None
    security_type: Optional[str] = None  # None → 写入占位 'unknown'，冲突时保留库内值
    board: Optional[str] = None
    exchange: Optional[str] = None
    list_status: Optional[str] = None  # None → 'unknown'，冲突时保留库内值
    list_date: Optional[date] = None
    delist_date: Optional[date] = None
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HkexListRow:
    code: int
    name: str
    category: str
    subcategory: Optional[str]
    lot_size: Optional[str]
    isin: Optional[str]


# ---------------------------------------------------------------- 纯函数


def pinyin_abbreviation(text: Optional[str], *, provided: Optional[str] = None) -> Optional[str]:
    """拼音首字母缩写（大写）：来源给了就用来源的；否则 pypinyin 兜底；无库返回 None。
    非汉字字符只保留字母数字（全角先 NFKC 归一：`宝信Ｂ` → BXB，`腾讯控股-R` → TXKGR）。"""
    if provided and str(provided).strip():
        return str(provided).strip().upper()[:50]
    if not text or not PINYIN_AVAILABLE:
        return None
    normalized = unicodedata.normalize("NFKC", str(text))
    parts = _lazy_pinyin(
        normalized,
        style=_PinyinStyle.FIRST_LETTER,
        errors=lambda chars: [ch for ch in chars if ch.isalnum()],
    )
    letters = "".join(part[:1] if len(part) > 1 else part for part in parts)
    letters = "".join(ch for ch in letters if ch.isalnum()).upper()
    return letters[:50] or None


def to_simplified(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    if not ZHCONV_AVAILABLE:
        return None
    return _zh_convert(str(text), "zh-cn")


def infer_hk_currency(symbol: str) -> Tuple[Optional[str], Optional[str]]:
    """港股人民币柜台（80000–89999）→ CNY；其余不推断（港交所日报的官方 CUR 会回填）。"""
    text = str(symbol or "").strip()
    if text.isdigit() and 80000 <= int(text) <= 89999:
        return "CNY", "inferred"
    return None, None


def infer_b_share_currency(symbol: str) -> Optional[str]:
    """沪 B（900xxx）以美元计价，深 B（200xxx）以港元计价。"""
    text = str(symbol or "").strip()
    if text.startswith("900"):
        return "USD"
    if text.startswith("200"):
        return "HKD"
    return None


def _parse_ymd(value: Any) -> Optional[date]:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none"}:
        return None
    text = text.replace("-", "")
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def _clean(value: Any) -> Optional[str]:
    """去空白 + NFKC 归一（全角字母/连字符 → 半角：`騰訊控股－Ｒ` → `騰訊控股-R`），
    否则用户敲 ASCII 的 `SW`/`-R` 搜不到名单里的全角写法。"""
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if not text or text.lower() in {"nan", "none"}:
        return None
    return text


_A_EXCHANGE_BY_SUFFIX = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}


def _split_ts_code(ts_code: str) -> Tuple[str, Optional[str]]:
    text = str(ts_code or "").strip().upper()
    if "." in text:
        symbol, suffix = text.rsplit(".", 1)
        return symbol, suffix
    return text, None


def _records(df) -> List[Dict[str, Any]]:
    if df is None or getattr(df, "empty", True):
        return []
    return df.to_dict("records")


def rows_from_stock_basic(df, *, list_status_code: str) -> List[CatalogRow]:
    """Tushare stock_basic → A股股票行。L/P → listed（P 记 detail），D → delisted。"""
    status = "delisted" if list_status_code == "D" else "listed"
    rows: List[CatalogRow] = []
    for record in _records(df):
        symbol, suffix = _split_ts_code(record.get("ts_code"))
        if not symbol:
            continue
        name = _clean(record.get("name"))
        detail: Dict[str, Any] = {}
        if list_status_code == "P":
            detail["list_status_code"] = "P"
        rows.append(
            CatalogRow(
                symbol=symbol,
                market=A_MARKET,
                name=name,
                pinyin=pinyin_abbreviation(name, provided=_clean(record.get("cnspell"))),
                currency="CNY",
                currency_source="tushare",
                security_type="stock",
                board=_clean(record.get("market")),
                exchange=_A_EXCHANGE_BY_SUFFIX.get(suffix or "", None),
                list_status=status,
                list_date=_parse_ymd(record.get("list_date")),
                delist_date=_parse_ymd(record.get("delist_date")),
                detail=detail,
            )
        )
    return rows


def rows_from_fund_basic(df) -> List[CatalogRow]:
    """Tushare fund_basic(market=E) → 场内基金行：REITs → reit；名含 ETF → etf；否则 fund。"""
    rows: List[CatalogRow] = []
    for record in _records(df):
        symbol, suffix = _split_ts_code(record.get("ts_code"))
        if not symbol:
            continue
        name = _clean(record.get("name")) or ""
        fund_type = _clean(record.get("fund_type"))
        if fund_type and "REIT" in fund_type.upper():
            security_type = "reit"
        elif "ETF" in name.upper():
            security_type = "etf"
        else:
            security_type = "fund"
        rows.append(
            CatalogRow(
                symbol=symbol,
                market=A_MARKET,
                name=name or None,
                pinyin=pinyin_abbreviation(name),
                currency="CNY",
                currency_source="tushare",
                security_type=security_type,
                exchange=_A_EXCHANGE_BY_SUFFIX.get(suffix or "", None),
                list_status="delisted" if _clean(record.get("status")) == "D" else "listed",
                list_date=_parse_ymd(record.get("list_date")),
                delist_date=_parse_ymd(record.get("delist_date")),
                detail={"fund_type": fund_type} if fund_type else {},
            )
        )
    return rows


_HKEX_HEADER_ALIASES = {
    "code": ("股份代號", "stock code"),
    "name": ("股份名稱", "name of securities"),
    "category": ("分類", "category"),
    "subcategory": ("次分類", "sub-category", "sub category"),
    "lot_size": ("買賣單位", "board lot"),
    "isin": ("國際證券號碌", "國際證券號碼", "isin"),
}


def _match_header(cell: Any) -> Optional[str]:
    text = str(cell or "").strip().lower()
    if not text:
        return None
    for key, aliases in _HKEX_HEADER_ALIASES.items():
        if any(text.startswith(alias.lower()) for alias in aliases):
            return key
    return None


def parse_hkex_list(xlsx_bytes: bytes) -> List[HkexListRow]:
    """港交所證券名單 xlsx → 行。前 10 行扫表头（含 股份代號 / Stock Code），按表头名取列。

    read_only 模式必须 `reset_dimensions()`：文件声明的维度只有 8 行，不重算只能读到 5 只。
    """
    import openpyxl

    workbook = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    sheet = workbook.worksheets[0]
    sheet.reset_dimensions()
    header_map: Dict[str, int] = {}
    data_start = None
    for index, row in enumerate(sheet.iter_rows(min_row=1, max_row=10, values_only=True), start=1):
        found = {key: col for col, cell in enumerate(row) if (key := _match_header(cell))}
        if "code" in found and "name" in found and "category" in found:
            header_map = found
            data_start = index + 1
            break
    if data_start is None:
        raise CatalogFormatError("港交所證券名單未找到表头（股份代號/股份名稱/分類）")

    rows: List[HkexListRow] = []
    for row in sheet.iter_rows(min_row=data_start, values_only=True):
        if not row or row[header_map["code"]] in (None, ""):
            continue
        code_text = str(row[header_map["code"]]).strip()
        if not code_text.isdigit():
            continue

        def cell(key: str) -> Optional[str]:
            col = header_map.get(key)
            return _clean(row[col]) if col is not None and col < len(row) else None

        rows.append(
            HkexListRow(
                code=int(code_text),
                name=cell("name") or "",
                category=cell("category") or "",
                subcategory=cell("subcategory"),
                lot_size=cell("lot_size"),
                isin=cell("isin"),
            )
        )
    if not rows:
        raise CatalogFormatError("港交所證券名單未解析出任何行")
    return rows


def _hkex_security_type(row: HkexListRow) -> str:
    sub = row.subcategory or ""
    if row.category == "股本":
        return "adr" if "預託證券" in sub else "stock"
    if row.category == "交易所買賣產品":
        return "etf"
    if row.category == "房地產投資信託基金":
        return "reit"
    return "unknown"


def _hkex_board(row: HkexListRow) -> Optional[str]:
    sub = row.subcategory or ""
    if "創業板" in sub:
        return "创业板"
    if "主板" in sub:
        return "主板"
    return None


def rows_from_hkex_lists(
    zh_rows: Iterable[HkexListRow], en_rows_by_code: Optional[Dict[int, HkexListRow]] = None
) -> List[CatalogRow]:
    en_rows_by_code = en_rows_by_code or {}
    rows: List[CatalogRow] = []
    for row in zh_rows:
        if row.category not in HKEX_KEEP_CATEGORIES:
            continue
        symbol = str(row.code).zfill(5)
        name_trad = _clean(row.name)
        name = to_simplified(name_trad)
        currency, currency_source = infer_hk_currency(symbol)
        en_row = en_rows_by_code.get(row.code)
        detail: Dict[str, Any] = {"category": row.category}
        if row.subcategory:
            detail["subcategory"] = row.subcategory
        if row.isin:
            detail["isin"] = row.isin
        if row.lot_size:
            detail["lot_size"] = row.lot_size
        rows.append(
            CatalogRow(
                symbol=symbol,
                market=HK_MARKET,
                name=name,
                name_en=_clean(en_row.name) if en_row else None,
                name_trad=name_trad,
                pinyin=pinyin_abbreviation(name or name_trad),
                currency=currency,
                currency_source=currency_source,
                security_type=_hkex_security_type(row),
                board=_hkex_board(row),
                exchange="HKEX",
                list_status="listed",
                detail=detail,
            )
        )
    return rows


def rows_from_hk_basic(df) -> List[CatalogRow]:
    """Tushare hk_basic → 港股简体名/拼音/英文名。security_type 留 None（港交所名單权威）。
    curr_type 对人民币柜台是错的（80700 报 HKD），8xxxx 一律按推断 CNY。"""
    rows: List[CatalogRow] = []
    for record in _records(df):
        symbol, _suffix = _split_ts_code(record.get("ts_code"))
        if not symbol:
            continue
        symbol = symbol.zfill(5) if symbol.isdigit() else symbol
        name = _clean(record.get("name"))
        inferred, inferred_source = infer_hk_currency(symbol)
        curr_type = _clean(record.get("curr_type"))
        currency, currency_source = (
            (inferred, inferred_source) if inferred else (curr_type, "tushare" if curr_type else None)
        )
        rows.append(
            CatalogRow(
                symbol=symbol,
                market=HK_MARKET,
                name=name,
                name_en=_clean(record.get("enname")),
                pinyin=pinyin_abbreviation(name, provided=_clean(record.get("cn_spell"))),
                currency=currency,
                currency_source=currency_source,
                board=_clean(record.get("market")),
                exchange="HKEX",
                list_status="delisted" if _clean(record.get("list_status")) == "D" else "listed",
                list_date=_parse_ymd(record.get("list_date")),
                delist_date=_parse_ymd(record.get("delist_date")),
            )
        )
    return rows


_US_TYPE_BY_CLASSIFY = {"EQ": "stock", "ADR": "adr", "PF": "pref", "GDR": "gdr"}


def rows_from_us_basic(df) -> List[CatalogRow]:
    rows: List[CatalogRow] = []
    for record in _records(df):
        symbol = _clean(record.get("ts_code"))
        if not symbol:
            continue
        symbol = symbol.upper()
        name = _clean(record.get("name"))
        delist = _parse_ymd(record.get("delist_date"))
        classify = _clean(record.get("classify"))
        rows.append(
            CatalogRow(
                symbol=symbol,
                market=US_MARKET,
                name=name,
                name_en=_clean(record.get("enname")),
                pinyin=pinyin_abbreviation(name),
                currency="USD",
                currency_source="tushare",
                security_type=_US_TYPE_BY_CLASSIFY.get(classify or "", "unknown"),
                exchange="US",
                list_status="delisted" if delist else "listed",
                list_date=_parse_ymd(record.get("list_date")),
                delist_date=delist,
                detail={"classify": classify} if classify else {},
            )
        )
    return rows


# ---------------------------------------------------------------- 网络（测试打桩）


def fetch_hkex_list(url: str) -> bytes:
    response = requests.get(url, headers=_HKEX_HEADERS, timeout=_HKEX_TIMEOUT)
    response.raise_for_status()
    return response.content


def tushare_available() -> bool:
    import os

    return bool((os.environ.get("TUSHARE_TOKEN") or settings.tushare_token or "").strip())


def _tushare_frame(api_name: str, *, required: bool, **kwargs):
    """tushare_query 对空返回抛 ValueError（已重试）。**只有附加查询允许为空**（退市腿、
    翻页尾页）；主查询（stock_basic L / fund_basic / hk_basic L / us_basic 第一页）返回空表
    只能是上游故障、权限或契约漂移——必须抛 CatalogFormatError 让该源记 failed，
    否则 0 行会被标成 ok、last_success_at 前移，接下来一周都不再重试且 health 显示正常。"""
    try:
        frame = tushare_query(api_name, **kwargs)
    except ValueError as exc:
        if "返回空数据" not in str(exc):
            raise
        frame = None
    if frame is None or getattr(frame, "empty", True):
        if required:
            params = ", ".join(f"{key}={value}" for key, value in kwargs.items() if key != "fields")
            raise CatalogFormatError(f"tushare {api_name} 主查询返回空表（{params or '无参数'}）")
        return None
    return frame


# ---------------------------------------------------------------- loaders


def load_tushare_stock_basic() -> List[CatalogRow]:
    fields = "ts_code,symbol,name,market,exchange,list_date,delist_date,list_status,cnspell"
    rows = rows_from_stock_basic(
        _tushare_frame("stock_basic", required=True, list_status="L", fields=fields),
        list_status_code="L",
    )
    rows += rows_from_stock_basic(
        _tushare_frame("stock_basic", required=False, list_status="D", fields=fields),
        list_status_code="D",
    )
    return rows


def load_tushare_fund_basic() -> List[CatalogRow]:
    return rows_from_fund_basic(
        _tushare_frame(
            "fund_basic",
            required=True,
            market="E",
            fields="ts_code,name,fund_type,list_date,delist_date,status",
        )
    )


def load_hkex_list() -> List[CatalogRow]:
    """中文名單必需；英文名單失败只丢 name_en（loader 仍成功）。"""
    zh_rows = parse_hkex_list(fetch_hkex_list(HKEX_LIST_URL_ZH))
    en_by_code: Dict[int, HkexListRow] = {}
    try:
        en_by_code = {row.code: row for row in parse_hkex_list(fetch_hkex_list(HKEX_LIST_URL_EN))}
    except Exception as exc:  # noqa: BLE001 - 英文名是锦上添花
        logger.warning("港交所英文證券名單获取失败，name_en 留空: %s", str(exc)[:160])
    return rows_from_hkex_lists(zh_rows, en_by_code)


def load_tushare_hk_basic() -> List[CatalogRow]:
    fields = "ts_code,name,fullname,enname,cn_spell,curr_type,market,list_status,list_date,delist_date"
    rows = rows_from_hk_basic(
        _tushare_frame("hk_basic", required=True, list_status="L", fields=fields)
    )
    rows += rows_from_hk_basic(
        _tushare_frame("hk_basic", required=False, list_status="D", fields=fields)
    )
    return rows


US_BASIC_PAGE_SIZE = 6000  # Tushare us_basic 单次上限；全表 2.4 万+ 行必须翻页（PDD 在第三页）
US_BASIC_MAX_PAGES = 12


def _tushare_pages(api_name: str, *, page_size: int, max_pages: int, **kwargs):
    """按 offset 翻页直到某页不足 page_size；超过 max_pages 抛错（防止上游分页语义变化时无限拉）。
    第一页是主查询（空即失败），后续页允许为空（尾页）。"""
    frames = []
    for page in range(max_pages):
        frame = _tushare_frame(
            api_name, required=page == 0, offset=page * page_size, limit=page_size, **kwargs
        )
        if frame is None or frame.empty:
            break
        frames.append(frame)
        if len(frame) < page_size:
            break
    else:
        raise CatalogFormatError(f"tushare {api_name} 翻页超过 {max_pages} 页仍未到末页")
    return frames


def load_tushare_us_basic() -> List[CatalogRow]:
    rows: List[CatalogRow] = []
    for frame in _tushare_pages(
        "us_basic",
        page_size=US_BASIC_PAGE_SIZE,
        max_pages=US_BASIC_MAX_PAGES,
        fields="ts_code,name,enname,classify,list_date,delist_date",
    ):
        rows += rows_from_us_basic(frame)
    return rows


@dataclass(frozen=True)
class LoaderSpec:
    source: str
    markets: Tuple[str, ...]
    loader: Callable[[], List[CatalogRow]]
    needs_tushare: bool


# 顺序即列级覆盖优先级：后跑的非空值覆盖先跑的（hk_basic 的简体名补在 HKEX 繁体行上）
LOADERS: Tuple[LoaderSpec, ...] = (
    LoaderSpec("tushare-stock_basic", (A_MARKET,), load_tushare_stock_basic, True),
    LoaderSpec("tushare-fund_basic", (A_MARKET,), load_tushare_fund_basic, True),
    LoaderSpec("hkex-list", (HK_MARKET,), load_hkex_list, False),
    LoaderSpec("tushare-hk_basic", (HK_MARKET,), load_tushare_hk_basic, True),
    LoaderSpec("tushare-us_basic", (US_MARKET,), load_tushare_us_basic, True),
)
LOADER_BY_SOURCE = {spec.source: spec for spec in LOADERS}


# ---------------------------------------------------------------- 写库

_COALESCE_COLUMNS = (
    "name", "name_en", "name_trad", "pinyin", "board", "exchange", "list_date", "delist_date",
)


def _row_values(row: CatalogRow, source: str) -> Dict[str, Any]:
    return {
        "symbol": row.symbol,
        "market": row.market,
        "name": row.name,
        "name_en": row.name_en,
        "name_trad": row.name_trad,
        "pinyin": row.pinyin,
        "currency": row.currency,
        "currency_source": row.currency_source,
        "security_type": row.security_type or "unknown",
        "board": row.board,
        "exchange": row.exchange,
        "list_status": row.list_status or "unknown",
        "list_date": row.list_date,
        "delist_date": row.delist_date,
        "source": source,
        "detail": row.detail or {},
    }


def upsert_catalog_rows(db: Session, rows: Iterable[CatalogRow], *, source: str) -> int:
    """按 (symbol, market) upsert。描述列 COALESCE 保留库内值；type/status 传 unknown 时
    保留库内值；币种若已由港交所日报官方回填则不被其他来源覆盖；detail 做 JSONB 合并。
    同一批内重复键只留最后一条（PG 不允许一条语句两次命中同一行）。"""
    deduped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        deduped[(row.symbol, row.market)] = _row_values(row, source)
    values = list(deduped.values())
    if not values:
        return 0
    table = SecurityCatalogEntry
    for start in range(0, len(values), UPSERT_CHUNK):
        chunk = values[start : start + UPSERT_CHUNK]
        stmt = pg_insert(table).values(chunk)
        excluded = stmt.excluded
        set_: Dict[str, Any] = {
            column: func.coalesce(getattr(excluded, column), getattr(table, column))
            for column in _COALESCE_COLUMNS
        }
        set_["security_type"] = case(
            (excluded.security_type == "unknown", table.security_type),
            else_=excluded.security_type,
        )
        set_["list_status"] = case(
            (excluded.list_status == "unknown", table.list_status), else_=excluded.list_status
        )
        official = table.currency_source == SOURCE_HKEX_DAYQUOT
        set_["currency"] = case(
            (official, table.currency), else_=func.coalesce(excluded.currency, table.currency)
        )
        set_["currency_source"] = case(
            (official, table.currency_source),
            else_=func.coalesce(excluded.currency_source, table.currency_source),
        )
        set_["detail"] = table.detail.op("||")(excluded.detail)
        set_["source"] = excluded.source
        set_["synced_at"] = func.now()
        stmt = stmt.on_conflict_do_update(
            constraint="uq_security_catalog_symbol_market", set_=set_
        )
        db.execute(stmt)
    db.commit()
    return len(values)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sync_row(db: Session, spec: LoaderSpec) -> SecurityCatalogSync:
    row = db.get(SecurityCatalogSync, spec.source)
    if row is None:
        row = SecurityCatalogSync(
            source=spec.source, markets=list(spec.markets), status="never", detail={}
        )
        db.add(row)
        db.flush()
    return row


def _mark(db: Session, spec: LoaderSpec, **fields: Any) -> None:
    row = _sync_row(db, spec)
    row.markets = list(spec.markets)
    for key, value in fields.items():
        setattr(row, key, value)
    db.commit()


_sync_lock = threading.Lock()


def is_sync_running() -> bool:
    return _sync_lock.locked()


def sync_security_catalog(
    db: Session,
    *,
    markets: Optional[Iterable[str]] = None,
    sources: Optional[Iterable[str]] = None,
    force: bool = True,
) -> Dict[str, Any]:
    """逐 loader 同步。一源失败记 failed 后继续；无 token 时 Tushare 源 skipped；
    某 Tushare 源报 fatal（token/权限）后其余 Tushare 源本轮直接 skipped。
    force=False 时 last_success_at 在 interval 内的源跳过（周期任务用）。"""
    wanted_markets = set(markets) if markets else None
    wanted_sources = set(sources) if sources else None
    interval = timedelta(hours=settings.security_catalog_sync_interval_hours)
    started = _now()
    report: List[Dict[str, Any]] = []
    tushare_ok = tushare_available()
    tushare_dead = False

    if not _sync_lock.acquire(blocking=False):
        raise RuntimeError("目录同步进行中")
    try:
        for spec in LOADERS:
            if wanted_sources and spec.source not in wanted_sources:
                continue
            if wanted_markets and not (set(spec.markets) & wanted_markets):
                continue
            entry: Dict[str, Any] = {"source": spec.source, "rows_seen": 0, "rows_upserted": 0}
            existing = db.get(SecurityCatalogSync, spec.source)
            if (
                not force
                and existing is not None
                and existing.last_success_at is not None
                and _now() - existing.last_success_at < interval
            ):
                entry.update(status="skipped", reason="fresh")
                report.append(entry)
                continue
            if spec.needs_tushare and (not tushare_ok or tushare_dead):
                reason = "tushare_fatal" if tushare_dead else "no_token"
                _mark(
                    db, spec, status="skipped", started_at=_now(), finished_at=_now(),
                    error=None, detail={"reason": reason},
                )
                entry.update(status="skipped", reason=reason)
                report.append(entry)
                continue
            _mark(db, spec, status="running", started_at=_now(), finished_at=None, error=None)
            try:
                rows = spec.loader()
                upserted = upsert_catalog_rows(db, rows, source=spec.source)
            except Exception as exc:  # noqa: BLE001 - 一源失败不拖垮其他来源
                db.rollback()
                message = str(exc)[:500]
                logger.warning("目录来源 %s 同步失败: %s", spec.source, message)
                if spec.needs_tushare and classify_tushare_error(message) == "fatal":
                    tushare_dead = True
                _mark(db, spec, status="failed", finished_at=_now(), error=message, detail={})
                entry.update(status="failed", error=message)
                report.append(entry)
                continue
            finished = _now()
            _mark(
                db, spec, status="ok", finished_at=finished, last_success_at=finished,
                rows_seen=len(rows), rows_upserted=upserted, error=None, detail={},
            )
            entry.update(status="ok", rows_seen=len(rows), rows_upserted=upserted)
            report.append(entry)
    finally:
        _sync_lock.release()
    return {"sources": report, "duration_seconds": (_now() - started).total_seconds()}


def run_sync_in_background(*, markets: Optional[Iterable[str]] = None, force: bool = True) -> None:
    """API 触发的后台同步：自开会话、吞异常（BackgroundTasks 里的异常只会进日志）。"""
    db = SessionLocal()
    try:
        sync_security_catalog(db, markets=markets, force=force)
    except Exception as exc:  # noqa: BLE001
        logger.warning("后台目录同步失败: %s", str(exc)[:200])
    finally:
        db.close()


def refresh_security_catalog() -> int:
    """周期任务入口：返回本轮成功的来源数；异常只记日志不上抛。"""
    if not settings.security_catalog_sync_enabled:
        return 0
    if is_sync_running():
        return 0
    db = SessionLocal()
    try:
        result = sync_security_catalog(db, force=False)
    except Exception as exc:  # noqa: BLE001 - 周期线程必须自吞异常
        logger.warning("标的目录周期同步失败: %s", str(exc)[:200])
        return 0
    finally:
        db.close()
    ok = [item for item in result["sources"] if item["status"] == "ok"]
    if ok:
        logger.info(
            "标的目录同步完成: %s",
            ", ".join(f"{item['source']}={item['rows_upserted']}" for item in ok),
        )
    return len(ok)


# ---------------------------------------------------------------- 读


def catalog_health(db: Session) -> Dict[str, Any]:
    sync_rows = db.query(SecurityCatalogSync).all()
    total = db.query(func.count(SecurityCatalogEntry.id)).scalar() or 0
    successes = [row.last_success_at for row in sync_rows if row.last_success_at is not None]
    latest = max(successes) if successes else None
    interval = timedelta(hours=settings.security_catalog_sync_interval_hours)
    return {
        "ready": bool(successes) and total > 0,
        "stale": latest is None or (_now() - latest) > 2 * interval,
        "last_success_at": latest,
        "failing_sources": [row.source for row in sync_rows if row.status == "failed"],
        "capabilities": {"pinyin": PINYIN_AVAILABLE, "simplified": ZHCONV_AVAILABLE},
    }


def catalog_status(db: Session) -> Dict[str, Any]:
    by_source = {row.source: row for row in db.query(SecurityCatalogSync).all()}
    sources = []
    for spec in LOADERS:
        row = by_source.get(spec.source)
        if row is None:
            sources.append({"source": spec.source, "markets": list(spec.markets), "status": "never"})
            continue
        sources.append(
            {
                "source": row.source,
                "markets": list(row.markets or spec.markets),
                "status": row.status if row.status != "never" else "never",
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "last_success_at": row.last_success_at,
                "rows_seen": row.rows_seen,
                "rows_upserted": row.rows_upserted,
                "error": row.error,
                "detail": row.detail or {},
            }
        )
    by_market = dict(
        db.query(SecurityCatalogEntry.market, func.count(SecurityCatalogEntry.id))
        .group_by(SecurityCatalogEntry.market)
        .all()
    )
    return {
        "health": catalog_health(db),
        "sources": sources,
        "total_rows": sum(by_market.values()),
        "by_market": by_market,
        "coverage_notes": list(COVERAGE_NOTES),
    }


def _escape_like(text: str) -> str:
    return re.sub(r"([\\%_])", r"\\\1", text)


def search_catalog(
    db: Session,
    *,
    q: str,
    market: Optional[str],
    limit: int,
    exclude_keys: Optional[Set[Tuple[str, str]]] = None,
) -> List[SecurityCatalogEntry]:
    """目录检索：代码前缀 > 拼音前缀 > 代码包含 > 名称（简/繁/英）包含；在市先于退市。
    空查询不查目录（1.8 万行没有意义的"前 20 条"）。"""
    query_text = q.strip()
    if not query_text:
        return []
    upper = _escape_like(query_text.upper())
    raw = _escape_like(query_text)
    table = SecurityCatalogEntry
    tier = case(
        (table.symbol.like(f"{upper}%", escape="\\"), 0),
        (table.pinyin.like(f"{upper}%", escape="\\"), 1),
        (table.symbol.like(f"%{upper}%", escape="\\"), 2),
        (
            or_(
                table.name.ilike(f"%{raw}%", escape="\\"),
                table.name_trad.ilike(f"%{raw}%", escape="\\"),
                table.name_en.ilike(f"%{raw}%", escape="\\"),
            ),
            3,
        ),
        else_=None,
    )
    stmt = db.query(table).filter(tier.isnot(None))
    if market:
        stmt = stmt.filter(table.market == market)
    exclude_keys = exclude_keys or set()
    rows = (
        stmt.order_by(
            tier, case((table.list_status == "listed", 0), else_=1), table.symbol, table.market
        )
        .limit(limit + len(exclude_keys))
        .all()
    )
    return [row for row in rows if (row.symbol, row.market) not in exclude_keys][:limit]


def collect_ledger_candidates(db: Session, user_id: int) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """账本候选：持仓（quantity>0）∪ 自选 ∪ 每 (symbol, market) 最新一笔交易。
    持仓最优先提供名称（人工修订过的概率最高）。"""
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def upsert(symbol, market, *, name=None, currency=None, last_used=None, origin=None):
        entry = merged.setdefault(
            (symbol, market),
            {
                "symbol": symbol, "market": market, "name": None, "currency": None,
                "last_used": None, "origins": [],
            },
        )
        if name and not entry["name"]:
            entry["name"] = name
        if currency and not entry["currency"]:
            entry["currency"] = currency
        if last_used and (entry["last_used"] is None or last_used > entry["last_used"]):
            entry["last_used"] = last_used
        if origin and origin not in entry["origins"]:
            entry["origins"].append(origin)

    for row in (
        db.query(Holding.symbol, Holding.market, Holding.name, Holding.currency)
        .filter(Holding.user_id == user_id, Holding.quantity > 0)
        .all()
    ):
        upsert(row.symbol, row.market, name=row.name, currency=row.currency, origin="holding")
    for row in (
        db.query(WatchlistItem.symbol, WatchlistItem.market, WatchlistItem.name)
        .filter(WatchlistItem.user_id == user_id)
        .all()
    ):
        upsert(row.symbol, row.market, name=row.name, origin="watchlist")
    latest_tx = (
        db.query(
            Transaction.symbol, Transaction.market, Transaction.name,
            Transaction.currency, Transaction.transaction_date,
        )
        .filter(Transaction.user_id == user_id)
        .distinct(Transaction.symbol, Transaction.market)
        .order_by(
            Transaction.symbol, Transaction.market,
            Transaction.transaction_date.desc(), Transaction.id.desc(),
        )
        .all()
    )
    for row in latest_tx:
        upsert(
            row.symbol, row.market, name=row.name, currency=row.currency,
            last_used=row.transaction_date, origin="history",
        )
    return merged


def _entry_item(entry: SecurityCatalogEntry) -> Dict[str, Any]:
    return {
        "symbol": entry.symbol,
        "market": entry.market,
        "name": entry.name,
        "name_en": entry.name_en,
        "name_trad": entry.name_trad,
        "pinyin": entry.pinyin,
        "currency": entry.currency,
        "security_type": entry.security_type or "unknown",
        "board": entry.board,
        "exchange": entry.exchange,
        "list_status": entry.list_status or "unknown",
        "in_catalog": True,
        "origins": [],
        "last_used": None,
    }


def enrich_with_catalog(db: Session, candidates: Dict[Tuple[str, str], Dict[str, Any]]) -> None:
    """账本候选补目录字段：账本已有的 name/currency 不覆盖，只补空。"""
    for candidate in candidates.values():
        candidate.setdefault("name_en", None)
        candidate.setdefault("name_trad", None)
        candidate.setdefault("pinyin", None)
        candidate.setdefault("security_type", "unknown")
        candidate.setdefault("board", None)
        candidate.setdefault("exchange", None)
        candidate.setdefault("list_status", "unknown")
        candidate.setdefault("in_catalog", False)
    if not candidates:
        return
    rows = (
        db.query(SecurityCatalogEntry)
        .filter(tuple_(SecurityCatalogEntry.symbol, SecurityCatalogEntry.market).in_(list(candidates)))
        .all()
    )
    for entry in rows:
        candidate = candidates.get((entry.symbol, entry.market))
        if candidate is None:
            continue
        candidate["in_catalog"] = True
        if not candidate["name"]:
            candidate["name"] = entry.name
        if not candidate["currency"]:
            candidate["currency"] = entry.currency
        candidate["name_en"] = entry.name_en
        candidate["name_trad"] = entry.name_trad
        candidate["pinyin"] = entry.pinyin
        candidate["security_type"] = entry.security_type or "unknown"
        candidate["board"] = entry.board
        candidate["exchange"] = entry.exchange
        candidate["list_status"] = entry.list_status or "unknown"


def _ledger_tier(entry: Dict[str, Any], query_upper: str, query_raw: str) -> Optional[int]:
    if not query_upper:
        return 0
    symbol = str(entry["symbol"]).upper()
    if symbol.startswith(query_upper):
        return 0
    if query_upper in symbol:
        return 1
    pinyin = (entry.get("pinyin") or "").upper()
    if pinyin.startswith(query_upper):
        return 1
    haystack = " ".join(
        str(entry.get(key) or "") for key in ("name", "name_trad", "name_en")
    ).lower()
    if query_raw.lower() in haystack:
        return 2
    return None


def search_securities(
    db: Session,
    *,
    user_id: int,
    q: str = "",
    market: Optional[str] = None,
    limit: int = 20,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """账本候选（持仓优先、最近交易优先）排前，目录命中排后，截 limit。"""
    query_raw = q.strip()
    query_upper = query_raw.upper()
    candidates = collect_ledger_candidates(db, user_id)
    if market:
        candidates = {key: value for key, value in candidates.items() if key[1] == market}
    enrich_with_catalog(db, candidates)
    ranked = [
        entry for entry in candidates.values()
        if _ledger_tier(entry, query_upper, query_raw) is not None
    ]
    ranked.sort(key=lambda entry: entry["symbol"])
    ranked.sort(key=lambda entry: entry["last_used"] or date.min, reverse=True)
    ranked.sort(
        key=lambda entry: (
            _ledger_tier(entry, query_upper, query_raw),
            0 if "holding" in entry["origins"] else 1,
        )
    )
    items = ranked[:limit]
    remaining = limit - len(items)
    if remaining > 0 and query_raw:
        catalog_rows = search_catalog(
            db, q=query_raw, market=market, limit=remaining, exclude_keys=set(candidates),
        )
        items.extend(_entry_item(row) for row in catalog_rows)
    return items, catalog_health(db)


def lookup_catalog_name(symbol: str, market: str) -> Optional[str]:
    """目录里的简体名（供分析/摘要落 name 时优先使用）；未命中返回 None。"""
    normalized = normalize_manual_symbol(symbol, market)
    db = SessionLocal()
    try:
        entry = (
            db.query(SecurityCatalogEntry.name)
            .filter(SecurityCatalogEntry.symbol == normalized, SecurityCatalogEntry.market == market)
            .first()
        )
        return entry[0] if entry and entry[0] else None
    finally:
        db.close()


def _exchange_for(symbol: str, market: str) -> Optional[str]:
    if market in (A_MARKET, B_MARKET):
        return {"sh": "SSE", "sz": "SZSE", "bj": "BSE"}.get(get_exchange_type(symbol))
    if market == HK_MARKET:
        return "HKEX"
    if market == US_MARKET:
        return "US"
    return None


def _resolve_payload(entry: SecurityCatalogEntry, resolved_from: str) -> Dict[str, Any]:
    return {
        "symbol": entry.symbol,
        "market": entry.market,
        "name": entry.name,
        "name_en": entry.name_en,
        "currency": entry.currency,
        "security_type": entry.security_type or "unknown",
        "list_status": entry.list_status or "unknown",
        "in_catalog": True,
        "resolved_from": resolved_from,
        "error": None,
    }


def resolve_security(db: Session, *, symbol: str, market: str) -> Dict[str, Any]:
    """按需解析：目录命中直接返回；未命中且市场受支持则向腾讯行情取名并沉淀
    （B股由此逐渐补齐）；解析不到显式返回 name=None + error，不落库。"""
    normalized = normalize_manual_symbol(symbol, market)
    base = {
        "symbol": normalized, "market": market, "name": None, "name_en": None,
        "currency": None, "security_type": "unknown", "list_status": "unknown",
        "in_catalog": False, "resolved_from": None, "error": None,
    }
    if not normalized:
        return {**base, "error": "代码不能为空"}
    entry = (
        db.query(SecurityCatalogEntry)
        .filter(SecurityCatalogEntry.symbol == normalized, SecurityCatalogEntry.market == market)
        .first()
    )
    if entry is not None:
        return _resolve_payload(entry, "catalog")
    if market not in TENCENT_RESOLVE_MARKETS:
        return {**base, "error": "该市场没有自动解析来源，请手工填写名称"}
    name = _clean(fetch_tencent_quote_name(normalized, market))  # 全角 Ｂ → B
    if not name:
        return {**base, "error": "腾讯行情未返回该代码，请确认代码与市场后手工填写名称"}
    if market == A_MARKET:
        currency, currency_source = "CNY", "inferred"
    elif market == B_MARKET:
        currency, currency_source = infer_b_share_currency(normalized), "inferred"
    elif market == HK_MARKET:
        currency, currency_source = infer_hk_currency(normalized)
        currency, currency_source = currency or "HKD", currency_source or "inferred"
    else:
        currency, currency_source = "USD", "inferred"
    upsert_catalog_rows(
        db,
        [
            CatalogRow(
                symbol=normalized, market=market, name=name,
                pinyin=pinyin_abbreviation(name), currency=currency,
                currency_source=currency_source, exchange=_exchange_for(normalized, market),
            )
        ],
        source=SOURCE_TENCENT,
    )
    entry = (
        db.query(SecurityCatalogEntry)
        .filter(SecurityCatalogEntry.symbol == normalized, SecurityCatalogEntry.market == market)
        .one()
    )
    return _resolve_payload(entry, SOURCE_TENCENT)


def apply_hk_dayquot_metadata(db: Session, quotes: Dict[int, Any]) -> int:
    """用港交所日报（每 6h 已在下载）的官方 CUR 与英文短名回填港股目录行。
    只改有变化的行；返回改动行数。调用方须包裹异常——绝不阻断价格入库。"""
    if not quotes:
        return 0
    changed = 0
    rows = db.query(SecurityCatalogEntry).filter(SecurityCatalogEntry.market == HK_MARKET).all()
    for entry in rows:
        if not str(entry.symbol).isdigit():
            continue
        quote = quotes.get(int(entry.symbol))
        if quote is None:
            continue
        currency = getattr(quote, "currency", None)
        name_en = getattr(quote, "name", None)
        touched = False
        if currency and (entry.currency != currency or entry.currency_source != SOURCE_HKEX_DAYQUOT):
            entry.currency = currency
            entry.currency_source = SOURCE_HKEX_DAYQUOT
            touched = True
        if name_en and not entry.name_en:
            entry.name_en = name_en
            touched = True
        if touched:
            changed += 1
    if changed:
        db.commit()
    return changed
