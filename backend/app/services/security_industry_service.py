"""标的行业分类（issue #235 第 3 项）：官方为主 + 东方财富 F10 补缺 + 用户规则覆盖。

来源与优先级（读取时合成，`resolve_industries`）：
1. **用户规则** `security_rules` 的 `INDUSTRY`（payload `{industry}`，用户域）——覆盖一切
2. **官方**：A股 = Tushare `stock_basic.industry`（复用 business_profile_service 的进程内
   目录缓存）；美股 = EDGAR submissions 的 `sic` / `sicDescription`，SIC 按大类映射成中文
   （`sic_industry_label`，原码与英文描述留在 raw）
3. **东方财富 F10**（非官方，补缺）：港股全部、B股全部（Tushare stock_basic 不含 B 股）、
   官方没有给出行业的 A股/美股。datacenter 端点免 Cookie，支持 `in (...)` 批量过滤，
   一次最多 `EASTMONEY_BATCH_SIZE` 只

存储：`security_industries` 全局表，每个 (symbol, market, source) 一行，只存取到的行业。
取不到（来源里没有该标的）与请求失败都**不写空行**——前者进同步结果的 missing（进程内按
刷新窗口记住，避免每天重打同一个空结果），后者进 errors 且该来源本轮状态为 failed/partial，
旧行保留。东方财富 `success=false` 只有 code 9201「返回数据为空」算无结果，其余一律是错误。

同步范围 = 全体活跃用户的持仓（数量>0）∪ 观察清单，市场限 A股/B股/港股/美股。周期任务
24h 一查，行按 `fetched_at` 与 `security_industry_refresh_days` 判新鲜；
`manage.py sync-security-industries [--force]` 手动。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import requests
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..config import settings
from ..core.logging import get_app_logger
from ..models.holding import Holding
from ..models.security_industry import SecurityIndustry
from ..models.user import User
from ..models.watchlist_item import WatchlistItem
from .security_rule_service import get_industry_overrides

logger = get_app_logger(__name__)

Key = Tuple[str, str]

SUPPORTED_MARKETS = ("A股", "B股", "港股", "美股")
SOURCE_TUSHARE = "tushare"
SOURCE_EDGAR = "edgar"
SOURCE_EASTMONEY = "eastmoney"
SOURCE_RULE = "rule"
# 市场 → 官方来源；不在表里的市场（港股/B股）没有官方源，直接走东方财富
OFFICIAL_SOURCE_BY_MARKET = {"A股": SOURCE_TUSHARE, "美股": SOURCE_EDGAR}
# 读取优先级（小者优先）；规则不在表里，在 resolve 中最先判
SOURCE_PRIORITY = {SOURCE_TUSHARE: 0, SOURCE_EDGAR: 0, SOURCE_EASTMONEY: 1}

PERIODIC_INTERVAL_SECONDS = 24 * 3600
EDGAR_MAX_CONSECUTIVE_FAILURES = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- SIC → 中文行业（纯函数）

# 最长前缀优先：4 位 > 3 位 > 2 位。只收录能区分出有意义行业的码段，其余落到大类。
_SIC_LABELS: Dict[str, str] = {
    # 4 位
    "2834": "医药", "2835": "医药", "2836": "生物制品",
    "3571": "计算机硬件", "3572": "计算机硬件", "3575": "计算机硬件", "3577": "计算机硬件",
    "3674": "半导体",
    "3711": "汽车", "3714": "汽车零部件",
    "4812": "电信服务", "4813": "电信服务",
    "5961": "电商零售",
    "6798": "REIT",
    "7370": "信息技术服务", "7371": "信息技术服务", "7372": "软件", "7373": "信息技术服务",
    "7374": "信息技术服务",
    "7389": "商业服务",
    "9995": "空壳公司",
    # 3 位
    "131": "石油天然气", "283": "医药", "357": "计算机硬件", "366": "通信设备",
    "367": "电子元器件", "384": "医疗器械", "737": "信息技术服务", "581": "餐饮",
    # 2 位（SIC Major Group）
    "01": "农林牧渔", "02": "农林牧渔", "07": "农林牧渔", "08": "农林牧渔", "09": "农林牧渔",
    "10": "金属采矿", "12": "煤炭", "13": "石油天然气", "14": "非金属采矿",
    "15": "建筑", "16": "建筑", "17": "建筑",
    "20": "食品饮料", "21": "烟草", "22": "纺织", "23": "服装", "24": "木材",
    "25": "家具", "26": "造纸", "27": "印刷出版", "28": "化工", "29": "石油炼化",
    "30": "橡胶塑料", "31": "皮革制品", "32": "建材", "33": "钢铁有色", "34": "金属制品",
    "35": "机械设备", "36": "电子电气设备", "37": "交通运输设备", "38": "仪器仪表",
    "39": "其他制造",
    "40": "铁路运输", "41": "公共交通", "42": "货运物流", "44": "水上运输", "45": "航空运输",
    "46": "管道运输", "47": "运输服务", "48": "通信", "49": "公用事业",
    "50": "批发", "51": "批发",
    "52": "零售", "53": "零售", "54": "零售", "55": "零售", "56": "零售", "57": "零售",
    "58": "餐饮", "59": "零售",
    "60": "银行", "61": "信贷金融", "62": "证券", "63": "保险", "64": "保险",
    "65": "房地产", "67": "投资控股",
    "70": "酒店", "72": "个人服务", "73": "商业服务", "75": "汽车服务", "76": "维修服务",
    "78": "影视", "79": "娱乐休闲", "80": "医疗服务", "81": "法律服务", "82": "教育",
    "83": "社会服务", "84": "文化场馆", "86": "会员组织", "87": "工程与专业服务",
    "88": "个人服务", "89": "其他服务",
    "91": "公共管理", "92": "公共管理", "93": "公共管理", "94": "公共管理",
    "95": "公共管理", "96": "公共管理", "97": "公共管理", "99": "未分类",
}


def sic_industry_label(sic: Any) -> Optional[str]:
    """SIC 码 → 中文行业（最长前缀匹配）；空/非数字/未收录返回 None。"""
    code = str(sic or "").strip()
    if not code.isdigit():
        return None
    code = code.zfill(4)
    for length in (4, 3, 2):
        label = _SIC_LABELS.get(code[:length])
        if label:
            return label
    return None


def parse_edgar_submissions(payload: Any) -> Optional[Dict[str, str]]:
    """submissions JSON → {sic, sic_description}；没有 SIC 返回 None。"""
    if not isinstance(payload, dict):
        raise ValueError("EDGAR submissions 响应不是 JSON 对象")
    sic = str(payload.get("sic") or "").strip()
    if not sic:
        return None
    return {"sic": sic, "sic_description": str(payload.get("sicDescription") or "").strip()}


# ---------------------------------------------------------------- 东方财富 F10（纯函数 + 取数）

EASTMONEY_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
EASTMONEY_BATCH_SIZE = 20
EASTMONEY_MIN_INTERVAL_SECONDS = 1.0
EASTMONEY_TIMEOUT_SECONDS = 15
# 精简 UA：非官方端点，不带 Cookie
EASTMONEY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
# success=false 且 code=9201「返回数据为空」= 查询的代码全都不在该报表里（2026-09-28 实测）
EASTMONEY_EMPTY_CODE = 9201


class EastmoneyError(RuntimeError):
    """东方财富响应不可用（HTTP/网络错误、非 JSON、success=false、未知结构）。"""


# 每个市场一张 F10 报表：请求代码的形态、按哪一列过滤、行业取哪一列
EASTMONEY_REPORTS: Dict[str, Dict[str, Any]] = {
    "港股": {
        "report": "RPT_HKF10_INFO_ORGPROFILE",
        "columns": "SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,BELONG_INDUSTRY",
        "filter_field": "SECUCODE",
    },
    "美股": {
        "report": "RPT_USF10_INFO_ORGPROFILE",
        "columns": "SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,BELONG_INDUSTRY",
        "filter_field": "SECURITY_CODE",
    },
    # A股与 B股 同一张报表（2026-09-28 实测 900901 云赛B股 / 200002 万科B 均在）
    "A股": {
        "report": "RPT_F10_BASIC_ORGINFO",
        "columns": "SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,EM2016,INDUSTRYCSRC1",
        "filter_field": "SECURITY_CODE",
    },
    "B股": {
        "report": "RPT_F10_BASIC_ORGINFO",
        "columns": "SECUCODE,SECURITY_CODE,SECURITY_NAME_ABBR,EM2016,INDUSTRYCSRC1",
        "filter_field": "SECURITY_CODE",
    },
}


def eastmoney_code(symbol: str, market: str) -> str:
    """账本代码 → 过滤值：港股 SECUCODE `00700.HK`；美股 `BRK.B` → `BRK_B`；A/B 股原样。"""
    if market == "港股":
        return f"{symbol}.HK"
    if market == "美股":
        return symbol.replace(".", "_")
    return symbol


def build_eastmoney_params(market: str, symbols: Sequence[str]) -> Dict[str, str]:
    spec = EASTMONEY_REPORTS[market]
    codes = ",".join(f'"{eastmoney_code(symbol, market)}"' for symbol in symbols)
    return {
        "reportName": spec["report"],
        "columns": spec["columns"],
        "filter": f"({spec['filter_field']} in ({codes}))",
        "pageNumber": "1",
        "pageSize": str(max(len(symbols) * 2, 50)),
        "source": "WEB",
        "client": "WEB",
    }


def parse_eastmoney_rows(payload: Any) -> List[Dict[str, Any]]:
    """响应 → 数据行；9201 = 空结果。其余 success=false / 未知结构一律 EastmoneyError。"""
    if not isinstance(payload, dict):
        raise EastmoneyError("东方财富响应不是 JSON 对象")
    if payload.get("success") is not True:
        if payload.get("code") == EASTMONEY_EMPTY_CODE:
            return []
        raise EastmoneyError(
            f"东方财富返回失败 code={payload.get('code')} message={payload.get('message')}"
        )
    result = payload.get("result")
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, list):
        raise EastmoneyError("东方财富响应缺少 result.data 数组")
    return [row for row in data if isinstance(row, dict)]


def eastmoney_industry(market: str, row: Dict[str, Any]) -> Optional[str]:
    """数据行 → 中文行业。港股/美股取 BELONG_INDUSTRY；A/B 股取东财 2016 分类第二级
    （「金融-银行-股份制与城商行」→「银行」，与 Tushare 行业的粒度相近），缺失时退回
    证监会分类的第二级。"""
    if market in ("港股", "美股"):
        value = str(row.get("BELONG_INDUSTRY") or "").strip()
        return value or None
    for field in ("EM2016", "INDUSTRYCSRC1"):
        parts = [part.strip() for part in str(row.get(field) or "").split("-") if part.strip()]
        if parts:
            return parts[1] if len(parts) > 1 else parts[0]
    return None


def eastmoney_symbol(market: str, row: Dict[str, Any]) -> Optional[str]:
    """数据行 → 账本代码（与 eastmoney_code 互逆）。"""
    if market == "港股":
        secucode = str(row.get("SECUCODE") or "").strip().upper()
        if secucode.endswith(".HK"):
            return secucode[: -len(".HK")]
        code = str(row.get("SECURITY_CODE") or "").strip()
        return code.zfill(5) if code.isdigit() else (code or None)
    code = str(row.get("SECURITY_CODE") or "").strip().upper()
    if not code:
        return None
    return code.replace("_", ".") if market == "美股" else code


def parse_eastmoney_industries(
    market: str, payload: Any
) -> Dict[str, Dict[str, Any]]:
    """响应 → {账本代码: {industry, raw}}；没有行业的行跳过。"""
    parsed: Dict[str, Dict[str, Any]] = {}
    for row in parse_eastmoney_rows(payload):
        symbol = eastmoney_symbol(market, row)
        industry = eastmoney_industry(market, row)
        if not symbol or not industry:
            continue
        raw = {key: row.get(key) for key in row if row.get(key) is not None}
        parsed.setdefault(symbol, {"industry": industry, "raw": raw})
    return parsed


_eastmoney_lock = threading.Lock()
_eastmoney_last_at = 0.0


def _eastmoney_get(params: Dict[str, str]) -> Any:
    """进程级限速（≥1s 间隔）的 GET；网络/HTTP/非 JSON 一律 EastmoneyError。"""
    global _eastmoney_last_at
    with _eastmoney_lock:
        wait = EASTMONEY_MIN_INTERVAL_SECONDS - (time.monotonic() - _eastmoney_last_at)
        if wait > 0:
            time.sleep(wait)
        _eastmoney_last_at = time.monotonic()
    try:
        response = requests.get(
            EASTMONEY_URL,
            params=params,
            headers=EASTMONEY_HEADERS,
            timeout=EASTMONEY_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        raise EastmoneyError(f"东方财富请求失败: {str(exc)[:160]}") from exc
    except ValueError as exc:
        raise EastmoneyError("东方财富响应不是合法 JSON") from exc


def fetch_eastmoney_industries(
    market: str, symbols: Sequence[str]
) -> Dict[str, Dict[str, Any]]:
    """一批（≤ EASTMONEY_BATCH_SIZE）代码的行业；失败抛 EastmoneyError。"""
    return parse_eastmoney_industries(market, _eastmoney_get(build_eastmoney_params(market, symbols)))


# ---------------------------------------------------------------- 官方来源取数


def load_tushare_industry_map() -> Dict[str, str]:
    """A股 代码 → Tushare 行业（进程内 24h 缓存的 stock_basic 目录）；失败上抛。"""
    from .business_profile_service import _load_stock_basic

    return {
        str(entry.get("symbol") or "").strip(): str(entry.get("industry") or "").strip()
        for entry in _load_stock_basic()
        if str(entry.get("industry") or "").strip()
    }


def fetch_edgar_sic(symbol: str) -> Optional[Dict[str, Any]]:
    """美股 → {sic, sic_description, cik}；EDGAR 未注册或无 SIC 返回 None，请求失败上抛。

    复用 report_fetchers 的 CIK 映射、EDGAR_USER_AGENT 与 edgar 限速桶。"""
    from .report_fetchers import edgar_lookup, edgar_submissions

    lookup = edgar_lookup(symbol)
    if not lookup:
        return None
    parsed = parse_edgar_submissions(edgar_submissions(lookup["cik"]))
    if parsed is None:
        return None
    return {**parsed, "cik": lookup["cik"]}


# ---------------------------------------------------------------- 范围与读取


def scope_keys(db: Session) -> List[Key]:
    """全体活跃用户的持仓（数量>0）∪ 观察清单，限 SUPPORTED_MARKETS，排序去重。"""
    holding_rows = (
        db.query(Holding.symbol, Holding.market)
        .join(User, User.id == Holding.user_id)
        .filter(User.is_active.is_(True), Holding.quantity > 0)
        .distinct()
        .all()
    )
    watch_rows = (
        db.query(WatchlistItem.symbol, WatchlistItem.market)
        .join(User, User.id == WatchlistItem.user_id)
        .filter(User.is_active.is_(True))
        .distinct()
        .all()
    )
    keys = {
        (symbol, market)
        for symbol, market in [*holding_rows, *watch_rows]
        if market in SUPPORTED_MARKETS
    }
    return sorted(keys)


def user_keys(db: Session, user_id: int) -> List[Key]:
    """当前用户的持仓（数量>0）∪ 观察清单（不限市场：规则可覆盖任意市场）。"""
    holding_rows = (
        db.query(Holding.symbol, Holding.market)
        .filter(Holding.user_id == user_id, Holding.quantity > 0)
        .distinct()
        .all()
    )
    watch_rows = (
        db.query(WatchlistItem.symbol, WatchlistItem.market)
        .filter(WatchlistItem.user_id == user_id)
        .all()
    )
    return sorted({(symbol, market) for symbol, market in [*holding_rows, *watch_rows]})


def _stored_rows(db: Session, keys: Iterable[Key]) -> Dict[Key, Dict[str, SecurityIndustry]]:
    wanted = set(keys)
    if not wanted:
        return {}
    symbols = sorted({symbol for symbol, _ in wanted})
    rows = db.query(SecurityIndustry).filter(SecurityIndustry.symbol.in_(symbols)).all()
    result: Dict[Key, Dict[str, SecurityIndustry]] = {}
    for row in rows:
        key = (row.symbol, row.market)
        if key in wanted:
            result.setdefault(key, {})[row.source] = row
    return result


def resolve_industries(
    db: Session, keys: Iterable[Key], user_id: Optional[int]
) -> Dict[Key, Dict[str, Any]]:
    """{key: {industry, source, fetched_at}}；无任何来源的 key 为 industry/source=None。

    优先级：用户规则 INDUSTRY > 官方（tushare/edgar）> 东方财富。user_id=None 只看来源表。"""
    keys = list(dict.fromkeys(keys))
    overrides = get_industry_overrides(db, user_id, keys) if user_id is not None else {}
    stored = _stored_rows(db, keys)
    result: Dict[Key, Dict[str, Any]] = {}
    for key in keys:
        if key in overrides:
            result[key] = {"industry": overrides[key], "source": SOURCE_RULE, "fetched_at": None}
            continue
        candidates = sorted(
            stored.get(key, {}).values(), key=lambda row: SOURCE_PRIORITY.get(row.source, 9)
        )
        if candidates:
            best = candidates[0]
            result[key] = {
                "industry": best.industry,
                "source": best.source,
                "fetched_at": best.fetched_at,
            }
        else:
            result[key] = {"industry": None, "source": None, "fetched_at": None}
    return result


# ---------------------------------------------------------------- 同步

# 「来源里没有这只」的进程内记忆：(source, symbol, market) → monotonic 时间。
# 不落库（绝不写空行业），重启后最多重问一次；force 同步无视它
_misses: Dict[Tuple[str, str, str], float] = {}
_misses_lock = threading.Lock()
_sync_lock = threading.Lock()


def reset_miss_cache() -> None:
    with _misses_lock:
        _misses.clear()


def _recently_missed(source: str, key: Key, window_seconds: float) -> bool:
    with _misses_lock:
        at = _misses.get((source, *key))
    return at is not None and time.monotonic() - at < window_seconds


def _note_miss(source: str, key: Key) -> None:
    with _misses_lock:
        _misses[(source, *key)] = time.monotonic()


def _upsert(db: Session, key: Key, source: str, industry: str, raw: Dict[str, Any]) -> None:
    statement = pg_insert(SecurityIndustry).values(
        symbol=key[0], market=key[1], source=source, industry=industry[:100], raw=raw,
        fetched_at=_now(),
    )
    db.execute(
        statement.on_conflict_do_update(
            constraint="uq_security_industries_key",
            set_={
                "industry": statement.excluded.industry,
                "raw": statement.excluded.raw,
                "fetched_at": statement.excluded.fetched_at,
            },
        )
    )


def _new_source_result() -> Dict[str, Any]:
    return {"status": "skipped", "requested": 0, "written": 0, "missing": [], "errors": []}


def _finish(result: Dict[str, Any]) -> None:
    if not result["requested"]:
        result["status"] = "skipped"
    elif result["errors"]:
        result["status"] = "partial" if result["written"] or result["missing"] else "failed"
    else:
        result["status"] = "ok"


def _key_text(key: Key) -> str:
    return f"{key[0]}:{key[1]}"


def _sync_tushare(
    db: Session, keys: List[Key], result: Dict[str, Any], *, force: bool, window: float
) -> None:
    keys = [key for key in keys if force or not _recently_missed(SOURCE_TUSHARE, key, window)]
    if not keys:
        return
    result["requested"] = len(keys)
    try:
        industry_map = load_tushare_industry_map()
    except Exception as exc:  # noqa: BLE001 - 一个来源失败不影响其他来源
        result["errors"].append(f"stock_basic: {str(exc)[:160]}")
        return
    for key in keys:
        industry = industry_map.get(key[0])
        if not industry:
            _note_miss(SOURCE_TUSHARE, key)
            result["missing"].append(_key_text(key))
            continue
        _upsert(db, key, SOURCE_TUSHARE, industry, {"industry": industry})
        result["written"] += 1
    db.commit()


def _sync_edgar(
    db: Session, keys: List[Key], result: Dict[str, Any], *, force: bool, window: float
) -> None:
    consecutive_failures = 0
    for key in keys:
        if not force and _recently_missed(SOURCE_EDGAR, key, window):
            continue
        if consecutive_failures >= EDGAR_MAX_CONSECUTIVE_FAILURES:
            # CIK 映射下载失败/EDGAR 不可达时每只都会重试同一个请求：连续失败即止损
            result["errors"].append(f"连续 {consecutive_failures} 只失败，本轮其余美股跳过")
            break
        result["requested"] += 1
        try:
            info = fetch_edgar_sic(key[0])
        except Exception as exc:  # noqa: BLE001 - 单只失败继续
            consecutive_failures += 1
            result["errors"].append(f"{_key_text(key)}: {str(exc)[:160]}")
            continue
        consecutive_failures = 0
        label = sic_industry_label(info["sic"]) if info else None
        if not info or not label:
            _note_miss(SOURCE_EDGAR, key)
            result["missing"].append(_key_text(key))
            continue
        _upsert(db, key, SOURCE_EDGAR, label, info)
        db.commit()
        result["written"] += 1


def _sync_eastmoney(
    db: Session,
    keys: List[Key],
    result: Dict[str, Any],
    *,
    force: bool,
    window: float,
    fetcher: Callable[[str, Sequence[str]], Dict[str, Dict[str, Any]]],
) -> None:
    by_market: Dict[str, List[str]] = {}
    for key in keys:
        if not force and _recently_missed(SOURCE_EASTMONEY, key, window):
            continue
        by_market.setdefault(key[1], []).append(key[0])
    for market in SUPPORTED_MARKETS:
        symbols = by_market.get(market) or []
        for start in range(0, len(symbols), EASTMONEY_BATCH_SIZE):
            chunk = symbols[start : start + EASTMONEY_BATCH_SIZE]
            result["requested"] += len(chunk)
            try:
                found = fetcher(market, chunk)
            except Exception as exc:  # noqa: BLE001 - 一批失败继续下一批
                result["errors"].append(f"{market} {','.join(chunk)}: {str(exc)[:160]}")
                continue
            for symbol in chunk:
                key = (symbol, market)
                item = found.get(symbol)
                if not item:
                    _note_miss(SOURCE_EASTMONEY, key)
                    result["missing"].append(_key_text(key))
                    continue
                _upsert(db, key, SOURCE_EASTMONEY, item["industry"], item["raw"])
                result["written"] += 1
            db.commit()


def sync_security_industries(
    db: Session,
    *,
    force: bool = False,
    keys: Optional[Iterable[Key]] = None,
    eastmoney_fetcher: Optional[Callable[[str, Sequence[str]], Dict[str, Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """拉取缺失或过期（> security_industry_refresh_days）的行业。

    官方来源先跑；东方财富只补「官方没有任何行」的 key（港股/B股 恒为缺口）。
    每个来源的失败相互隔离，结果 `sources[src] = {status, requested, written, missing, errors}`。
    """
    started = time.monotonic()
    keys = sorted(set(keys)) if keys is not None else scope_keys(db)
    keys = [key for key in keys if key[1] in SUPPORTED_MARKETS]
    window_days = settings.security_industry_refresh_days
    window = window_days * 86400.0
    cutoff = _now() - timedelta(days=window_days)
    stored = _stored_rows(db, keys)

    def needs(key: Key, source: str) -> bool:
        row = stored.get(key, {}).get(source)
        return force or row is None or row.fetched_at is None or row.fetched_at < cutoff

    sources = {
        SOURCE_TUSHARE: _new_source_result(),
        SOURCE_EDGAR: _new_source_result(),
        SOURCE_EASTMONEY: _new_source_result(),
    }
    tushare_keys = [k for k in keys if k[1] == "A股" and needs(k, SOURCE_TUSHARE)]
    if tushare_keys:
        _sync_tushare(db, tushare_keys, sources[SOURCE_TUSHARE], force=force, window=window)
    edgar_keys = [k for k in keys if k[1] == "美股" and needs(k, SOURCE_EDGAR)]
    if edgar_keys:
        _sync_edgar(db, edgar_keys, sources[SOURCE_EDGAR], force=force, window=window)

    # 官方跑完后重读：本轮新写的官方行也算「官方已有」
    stored = _stored_rows(db, keys)
    gap_keys = [
        key
        for key in keys
        if OFFICIAL_SOURCE_BY_MARKET.get(key[1]) not in stored.get(key, {})
        and needs(key, SOURCE_EASTMONEY)
    ]
    if gap_keys:
        _sync_eastmoney(
            db,
            gap_keys,
            sources[SOURCE_EASTMONEY],
            force=force,
            window=window,
            fetcher=eastmoney_fetcher or fetch_eastmoney_industries,
        )
    for item in sources.values():
        _finish(item)
    resolved = resolve_industries(db, keys, None)
    return {
        "scope": len(keys),
        "resolved": sum(1 for value in resolved.values() if value["industry"]),
        "unresolved": [_key_text(key) for key, value in resolved.items() if not value["industry"]],
        "sources": sources,
        "duration_seconds": time.monotonic() - started,
    }


def refresh_security_industries() -> int:
    """周期任务入口：返回本轮写入行数；异常只记日志不上抛。"""
    if not settings.security_industry_sync_enabled:
        return 0
    if not _sync_lock.acquire(blocking=False):
        return 0
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        result = sync_security_industries(db)
    except Exception as exc:  # noqa: BLE001 - 周期线程必须自吞异常
        db.rollback()
        logger.warning("行业分类周期同步失败: %s", str(exc)[:200])
        return 0
    finally:
        db.close()
        _sync_lock.release()
    written = sum(item["written"] for item in result["sources"].values())
    failing = {
        source: item["errors"][:3]
        for source, item in result["sources"].items()
        if item["status"] in ("failed", "partial")
    }
    if written or failing:
        logger.info(
            "行业分类同步：范围 %d 只，写入 %d 行，未取得 %d 只，失败来源 %s",
            result["scope"], written, len(result["unresolved"]), failing or "无",
        )
    return written
