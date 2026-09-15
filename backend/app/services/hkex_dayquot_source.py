"""港交所《每日行情报表》（Daily Quotations）——港股收盘价的官方 T+1 源。

URL: https://www.hkex.com.hk/eng/stat/smstat/dayquot/dYYMMDDe.htm

- 官方、免鉴权：无 token、无 Cookie、无签名，可靠性优先于即时性的场景下
  是港股收盘价的第一权威来源（Tushare hk_daily 与腾讯 K 线都是二手转发）。
- 每个交易日收市后发布一份定宽文本（≈25MB，主板 + GEM 全部证券，含
  人民币柜台 / ETF / 窝轮牛熊证）；非交易日 404——不存在"空报表"，所以
  404 就是"当天休市"的判据，不需要另查交易日历。
- 站点只保留约一个月存档（实测 2026-08-03 可取、07-02 起 404），因此这是
  **只向前**的日更源：冷启动 / 深历史回填仍由用户区间驱动的 history-sync
  （Tushare hk_daily → 腾讯 K 线兜底）负责，与基准指数补尾同一策略；
  周期任务只把已跟踪标的的尾部推进到最近交易日。
- 报表只有 前收 / 收盘 / 最高 / 最低 / 买卖盘 / 成交，**没有开盘价**：写库
  时保留已有行的 open_price（COALESCE），绝不用 NULL 覆盖；复权字段不碰。
- 官方收盘价覆盖其他源的同日收盘价，覆盖前逐行比对，不一致的记 conflicts
  并告警——差异本身是数据质量信号（某一侧口径错了），不能静默吞掉。
- "该日已处理" 以 `hkex_dayquot_reports` 表的标记为准（与价格行同一事务写入，
  重启后仍有效），**不看价格行**：跟踪集当天全部停牌 / 全部 N/A / 代码全部
  未匹配时报表成功解析却写入 0 行，按价格行判定会每个 tick 重下同一份 25MB，
  并在 max_reports 预算内反复耗尽、饿死更早的日期。

解析层（`parse_dayquot`）是纯函数，金样固件在
`tests/fixtures/hkex/dayquot_d260901e_sample.htm`（真实报表裁剪）。
"""

from __future__ import annotations

import html
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Set
from zoneinfo import ZoneInfo

import requests
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..config import settings
from ..core.logging import get_app_logger
from ..database import SessionLocal
from ..models.hkex_dayquot_report import HkexDayquotReport
from ..models.holding import Holding
from ..models.security_price import SecurityPrice
from ..models.watchlist_item import WatchlistItem
from .stock_price_service import to_tushare_hk_code

logger = get_app_logger(__name__)

HK_MARKET = "港股"
SOURCE = "hkex-dayquot"
DAYQUOT_URL = "https://www.hkex.com.hk/eng/stat/smstat/dayquot/d{yymmdd}e.htm"
PERIODIC_INTERVAL_SECONDS = 6 * 3600

_HKT = ZoneInfo("Asia/Hong_Kong")
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}
_FETCH_TIMEOUT = (10, 180)  # 单份 25MB，慢链路也给足读超时
_MIN_INTERVAL_SECONDS = 2.0

_TAG_RE = re.compile(r"<[^>]+>")
_DATE_RE = re.compile(r"DATE:\s*(\d{1,2}) ([A-Z]{3}) (\d{4})")
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
# 行 1：[*|#] 代码 名称 币种 前收 卖盘 最高 成交股数；行 2：收盘 买盘 最低 成交额。
# 名称可含空格/&/#（窝轮名如 "HS#HSI RC2810F"），靠行尾恰好四个值锚定。
_ROW1_RE = re.compile(r"^([*#])?\s*(\d{1,5})\s+(.*?)\s+([A-Z]{3})\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$")
_ROW2_RE = re.compile(r"^\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$")
_SUSPENDED_RE = re.compile(r"^([*#])?\s*(\d{1,5})\s+(.*?)\s+([A-Z]{3})\s+TRADING SUSPENDED\s*$")
# 栏目边界按锚点 <a name="quotations"> / <a name="sales_all"> 定位：目录
# （TABLE OF CONTENT）里同样有一行纯文本 "QUOTATIONS"，按文本找会锚到目录
# 上、栏目为空。锚点缺失时退化到列头行。
_SECTION_START_RE = re.compile(r'name\s*=\s*"quotations"')
_SECTION_END_RE = re.compile(r'name\s*=\s*"sales_all"')
_COLUMN_HEADER = "CODE  NAME OF STOCK    CUR PRV.CLO./"
_EMPTY_TOKENS = {"-", "N/A", ""}


class DayquotFormatError(ValueError):
    """报表结构与预期不符（栏目锚点缺失 / 日期不匹配 / 零行）——绝不静默入库。"""


@dataclass(frozen=True)
class DayquotRow:
    code: int
    name: str
    currency: str
    prev_close: Optional[Decimal]
    close: Optional[Decimal]
    high: Optional[Decimal]
    low: Optional[Decimal]
    ask: Optional[Decimal]
    bid: Optional[Decimal]
    shares_traded: Optional[int]
    turnover: Optional[int]
    suspended: bool = False
    most_active: bool = False  # 行首 "*"：当日成交额前二十
    halt_from_today: bool = False  # 行首 "#"：当日起停牌 / 暂停买卖


def hkt_today() -> date:
    """以香港时间判定"今天"：容器系统时区是 UTC，直接 date.today() 在
    港股收市后的 UTC 傍晚仍是"昨天"，会把当天报表推迟一整个 tick。"""
    return datetime.now(_HKT).date()


def report_url(report_date: date) -> str:
    return DAYQUOT_URL.format(yymmdd=report_date.strftime("%y%m%d"))


def _decimal(token: str) -> Optional[Decimal]:
    text = token.strip()
    if text in _EMPTY_TOKENS:
        return None
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def _int(token: str) -> Optional[int]:
    value = _decimal(token)
    return int(value) if value is not None else None


def dayquot_report_date(text: str) -> date:
    """报表头 `DATE: 01 SEP 2026 (TUESDAY)` → date；缺失即结构异常。"""
    match = _DATE_RE.search(text[:4000])
    if not match:
        raise DayquotFormatError("报表头未找到 DATE 字段")
    day, month, year = match.groups()
    return date(int(year), _MONTHS[month], int(day))


def parse_dayquot(text: str, *, expected_date: Optional[date] = None) -> Dict[int, DayquotRow]:
    """解析 QUOTATIONS 栏目 → {证券代码(int): DayquotRow}。

    纯函数。HTML 只是 <pre> 外壳：先剥标签、反转义（名称里的 &amp;），再按
    定宽文本处理；分页处 `</font></pre><pre>` 会直接前缀在数据行上，剥完标签
    后与普通行无异。停牌行只有一行（TRADING SUSPENDED），其余两行一组。
    """
    if expected_date is not None:
        actual = dayquot_report_date(text)
        if actual != expected_date:
            raise DayquotFormatError(f"报表日期 {actual} 与请求日期 {expected_date} 不符")

    raw_lines = text.splitlines()
    start = next((i for i, raw in enumerate(raw_lines) if _SECTION_START_RE.search(raw)), None)
    if start is None:
        start = next(
            (i for i, raw in enumerate(raw_lines) if raw.strip().startswith(_COLUMN_HEADER)),
            None,
        )
    if start is None:
        raise DayquotFormatError("未找到 QUOTATIONS 栏目")
    end = next(
        (i for i in range(start + 1, len(raw_lines)) if _SECTION_END_RE.search(raw_lines[i])),
        len(raw_lines),
    )
    lines = [html.unescape(_TAG_RE.sub("", raw)).rstrip("\r") for raw in raw_lines[: end + 1]]

    rows: Dict[int, DayquotRow] = {}
    i = start + 1
    while i < end:
        line = lines[i]
        i += 1
        if not line.strip():
            continue
        suspended = _SUSPENDED_RE.match(line)
        if suspended:
            marker, code, name, currency = suspended.groups()
            rows[int(code)] = DayquotRow(
                code=int(code), name=name.strip(), currency=currency,
                prev_close=None, close=None, high=None, low=None, ask=None, bid=None,
                shares_traded=None, turnover=None, suspended=True,
                most_active=marker == "*", halt_from_today=marker == "#",
            )
            continue
        first = _ROW1_RE.match(line)
        if not first:
            continue
        j = i
        while j < end and not lines[j].strip():
            j += 1
        second = _ROW2_RE.match(lines[j]) if j < end else None
        if second is None:
            continue  # 不成对的行 1：跳过，行 j 留给下一轮按常规处理
        i = j + 1
        marker, code, name, currency, prev_close, ask, high, shares = first.groups()
        close, bid, low, turnover = second.groups()
        rows[int(code)] = DayquotRow(
            code=int(code), name=name.strip(), currency=currency,
            prev_close=_decimal(prev_close), close=_decimal(close),
            high=_decimal(high), low=_decimal(low),
            ask=_decimal(ask), bid=_decimal(bid),
            shares_traded=_int(shares), turnover=_int(turnover),
            most_active=marker == "*", halt_from_today=marker == "#",
        )
    if not rows:
        raise DayquotFormatError("QUOTATIONS 栏目未解析出任何行")
    return rows


_throttle_lock = threading.Lock()
_last_fetch_at = 0.0


def _throttle() -> None:
    global _last_fetch_at
    with _throttle_lock:
        elapsed = time.monotonic() - _last_fetch_at
        if elapsed < _MIN_INTERVAL_SECONDS:
            time.sleep(_MIN_INTERVAL_SECONDS - elapsed)
        _last_fetch_at = time.monotonic()


def fetch_dayquot_text(report_date: date) -> Optional[str]:
    """下载某日报表原文；404（非交易日 / 尚未发布 / 超出存档）返回 None，
    其他 HTTP 错误上抛。"""
    _throttle()
    response = requests.get(report_url(report_date), headers=_HEADERS, timeout=_FETCH_TIMEOUT)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    # 页面声明 iso-8859-1，且正文纯 ASCII；不用 response.text 的编码猜测
    return response.content.decode("latin-1", "replace")


def hk_universe_symbols(db: Session) -> Set[str]:
    """周期同步的标的集：全体用户的港股持仓（quantity>0）∪ 自选。
    已清仓标的的尾部对曲线无意义，不追。非纯数字代码（异常形态）跳过。"""
    holdings = (
        db.query(Holding.symbol)
        .filter(Holding.market == HK_MARKET, Holding.quantity > 0)
        .distinct()
    )
    watchlist = (
        db.query(WatchlistItem.symbol).filter(WatchlistItem.market == HK_MARKET).distinct()
    )
    symbols = {row[0] for row in holdings} | {row[0] for row in watchlist}
    return {str(symbol).strip() for symbol in symbols if str(symbol).strip().isdigit()}


def _detect_close_conflicts(
    db: Session, report_date: date, rows: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    if not rows:
        return []
    existing = (
        db.query(SecurityPrice.symbol, SecurityPrice.close_price, SecurityPrice.source)
        .filter(
            SecurityPrice.market == HK_MARKET,
            SecurityPrice.price_date == report_date,
            SecurityPrice.symbol.in_([row["symbol"] for row in rows]),
        )
        .all()
    )
    by_symbol = {symbol: (close, source) for symbol, close, source in existing}
    conflicts = []
    for row in rows:
        found = by_symbol.get(row["symbol"])
        if found is None or found[1] == SOURCE:
            continue
        if found[0] != row["close_price"]:
            conflicts.append(
                {
                    "symbol": row["symbol"],
                    "existing_close": found[0],
                    "existing_source": found[1],
                    "official_close": row["close_price"],
                }
            )
    return conflicts


def store_dayquot_closes(
    db: Session,
    *,
    report_date: date,
    quotes: Dict[int, DayquotRow],
    symbols: Iterable[str],
) -> Dict[str, Any]:
    """把报表中属于 symbols 的收盘价写入 security_prices（按库内原始代码落库），
    并在同一事务写下该日的处理完成标记（即使 0 行）。

    ON CONFLICT：收盘 / 最高 / 最低 / 前收 / 币种 / 来源以官方为准；开盘价与
    复权字段本源没有，保留已有值。
    """
    symbols = set(symbols)
    rows: List[Dict[str, Any]] = []
    missing: List[str] = []
    suspended: List[str] = []
    unpriced: List[str] = []
    for symbol in sorted(symbols):
        quote = quotes.get(int(symbol))
        if quote is None:
            missing.append(symbol)
            continue
        if quote.suspended:
            suspended.append(symbol)
            continue
        if quote.close is None:
            unpriced.append(symbol)
            continue
        rows.append(
            {
                "symbol": symbol,
                "market": HK_MARKET,
                "price_date": report_date,
                "ts_code": to_tushare_hk_code(symbol),
                "currency": quote.currency,
                "open_price": None,
                "high_price": quote.high,
                "low_price": quote.low,
                "close_price": quote.close,
                "pre_close_price": quote.prev_close,
                "source": SOURCE,
            }
        )
    conflicts = _detect_close_conflicts(db, report_date, rows)
    if rows:
        stmt = pg_insert(SecurityPrice).values(rows)
        excluded = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            constraint="uix_security_price_symbol_market_date",
            set_={
                "close_price": excluded.close_price,
                "high_price": func.coalesce(excluded.high_price, SecurityPrice.high_price),
                "low_price": func.coalesce(excluded.low_price, SecurityPrice.low_price),
                "pre_close_price": func.coalesce(
                    excluded.pre_close_price, SecurityPrice.pre_close_price
                ),
                "currency": excluded.currency,
                "ts_code": func.coalesce(SecurityPrice.ts_code, excluded.ts_code),
                "source": excluded.source,
                "updated_at": func.now(),
            },
        )
        db.execute(stmt)
    marker = pg_insert(HkexDayquotReport).values(
        report_date=report_date,
        universe_size=len(symbols),
        parsed_count=len(quotes),
        stored_count=len(rows),
        detail={
            "missing": missing,
            "suspended": suspended,
            "unpriced": unpriced,
            "conflicts": [
                {key: str(value) if isinstance(value, Decimal) else value for key, value in c.items()}
                for c in conflicts
            ],
        },
    )
    marker = marker.on_conflict_do_update(
        index_elements=[HkexDayquotReport.report_date],
        set_={
            "processed_at": func.now(),
            "universe_size": marker.excluded.universe_size,
            "parsed_count": marker.excluded.parsed_count,
            "stored_count": marker.excluded.stored_count,
            "detail": marker.excluded.detail,
        },
    )
    db.execute(marker)
    db.commit()
    return {
        "report_date": report_date,
        "stored": len(rows),
        "missing": missing,
        "suspended": suspended,
        "unpriced": unpriced,
        "conflicts": conflicts,
    }


def _date_already_synced(db: Session, report_date: date) -> bool:
    """该日报表已成功解析并写入（含 0 行）即视为已处理——标记表持久、与
    价格行无关。之后新增到跟踪集的标的，其历史由用户触发的 history-sync 补，
    与基准同策略。"""
    return (
        db.query(HkexDayquotReport.report_date)
        .filter(HkexDayquotReport.report_date == report_date)
        .first()
        is not None
    )


# 进程内的"该日无报表"缓存：只记今天之前的日期（休市），今天的 404 可能只是
# 尚未发布，必须在下一 tick 重试。
_missing_reports: Set[date] = set()


def sync_recent_dayquots(
    db: Session,
    *,
    today: Optional[date] = None,
    lookback_days: Optional[int] = None,
    max_reports: Optional[int] = None,
) -> Dict[str, Any]:
    """从今天（香港时间）向前回看 lookback_days 个自然日，补齐尚未处理的报表。

    每次最多下载 max_reports 份（25MB/份），余下的留给下一 tick——周期任务
    不该一次拖几百 MB。周末直接跳过；工作日 404 视为休市并缓存。
    """
    today = today or hkt_today()
    lookback_days = lookback_days or settings.hkex_dayquot_lookback_days
    max_reports = max_reports or settings.hkex_dayquot_max_reports_per_tick
    symbols = hk_universe_symbols(db)
    result: Dict[str, Any] = {
        "universe": len(symbols),
        "processed": [],
        "skipped_done": [],
        "no_report": [],
        "errors": [],
    }
    if not symbols:
        return result

    fetched = 0
    latest_quotes: Optional[Dict[int, DayquotRow]] = None
    for offset in range(lookback_days):
        day = today - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        if day in _missing_reports:
            result["no_report"].append(day)
            continue
        if _date_already_synced(db, day):
            result["skipped_done"].append(day)
            continue
        if fetched >= max_reports:
            break
        fetched += 1
        text = fetch_dayquot_text(day)
        if text is None:
            if day < today:
                _missing_reports.add(day)
            result["no_report"].append(day)
            continue
        try:
            quotes = parse_dayquot(text, expected_date=day)
        except DayquotFormatError as exc:
            logger.error("港交所日报 %s 解析失败: %s", day, exc)
            result["errors"].append({"report_date": day, "error": str(exc)})
            continue
        stored = store_dayquot_closes(db, report_date=day, quotes=quotes, symbols=symbols)
        result["processed"].append(stored)
        if latest_quotes is None:
            latest_quotes = quotes  # 窗口自新向旧遍历：第一份处理成功的就是最新报表
    if latest_quotes is not None:
        _apply_catalog_metadata(db, latest_quotes)
    return result


def _apply_catalog_metadata(db: Session, quotes: Dict[int, DayquotRow]) -> None:
    """顺手用官方 CUR / 英文短名回填标的全集的港股行——目录出错绝不阻断价格入库。"""
    try:
        from .security_catalog_service import apply_hk_dayquot_metadata

        changed = apply_hk_dayquot_metadata(db, quotes)
        if changed:
            logger.info("港交所日报回填标的全集港股币种/英文名 %s 行", changed)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning("港交所日报回填标的全集失败: %s", str(exc)[:200])


def refresh_hk_dayquot() -> int:
    """周期任务入口：返回本轮处理的报表份数；任何异常只记日志不上抛
    （周期线程上的异常不该影响其他周期任务）。"""
    if not settings.hkex_dayquot_sync_enabled:
        return 0
    db = SessionLocal()
    try:
        result = sync_recent_dayquots(db)
    except Exception as exc:  # noqa: BLE001 - 周期任务必须自吞异常
        logger.warning("港交所日报同步失败: %s", str(exc)[:200])
        return 0
    finally:
        db.close()
    for item in result["processed"]:
        logger.info(
            "港交所日报 %s: 入库 %s 只，报表缺席 %s，停牌 %s，无收盘 %s",
            item["report_date"], item["stored"], item["missing"], item["suspended"], item["unpriced"],
        )
        for conflict in item["conflicts"]:
            logger.warning(
                "港交所日报 %s 与库内 %s 收盘价不一致: %s 库内 %s → 官方 %s（已按官方覆盖）",
                item["report_date"], conflict["existing_source"], conflict["symbol"],
                conflict["existing_close"], conflict["official_close"],
            )
    return len(result["processed"])
