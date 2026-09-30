"""
Stock Price Service Module

Fetches real-time stock prices from different sources:
- A股/B股: Tushare rt_k
- 港股: Tushare hk_daily latest bar
- 美股: Tushare us_daily latest bar → Tiingo (IEX / EOD) → 雪球（美股交易时段内 Tiingo 提前）
- 加密货币: Tushare coin_bar latest bar

Optimizations:
- Request pooling and session reuse
- Exponential backoff with jitter
- Rate limiting and queue management
- Comprehensive error handling
- User-Agent rotation
"""

from enum import Enum
from typing import TypedDict, Optional, Dict, Any, Iterable, List, Tuple
from decimal import Decimal
from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import bisect
import time
import os
import random
from functools import partial
from threading import Lock, local

from sqlalchemy.orm import Session
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..config import settings
from ..core.logging import get_app_logger
from .http_source import BROWSER_USER_AGENT, throttle
from .market_sessions import market_timezone

# Configure logging
logger = get_app_logger(__name__)
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q={codes}"
DEFAULT_TUSHARE_API_BASE_URL = "https://api.waditu.com/dataapi"


def get_exchange_type(symbol: str) -> str:
    """
    根据股票代码判断交易所类型

    北交所股票代码规则:
    - 8xxxxx: 新三板精选层转板股票 (如 830799)
    - 4xxxxx: 北交所直接上市股票 (如 430047)
    - 920xxx: 北交所直接上市股票 (如 920599)

    Returns:
        'bj' - 北交所
        'sh' - 上海
        'sz' - 深圳
    """
    if symbol.startswith("8") or symbol.startswith("4") or symbol.startswith("920"):
        return "bj"
    elif symbol.startswith("6") or symbol.startswith("5") or symbol.startswith("900"):
        return "sh"
    else:
        return "sz"


# Per-thread requests sessions with connection pooling
_thread_sessions = local()

# 腾讯行情 / K 线 / 取名 / 美股代码探测共用一个限速桶与请求头（#281：此前四条路径
# 各自发请求、没有任何限速，K 线还借用了雅虎的 UA 常量）
TENCENT_THROTTLE_KEY = "tencent"
TENCENT_MIN_INTERVAL_SECONDS = 0.1
TENCENT_HEADERS = {"Referer": "https://gu.qq.com/", "User-Agent": BROWSER_USER_AGENT}


def throttle_tencent() -> None:
    throttle(TENCENT_THROTTLE_KEY, TENCENT_MIN_INTERVAL_SECONDS)


def get_session():
    """
    Get or create a global requests session with connection pooling
    Uses retry strategy and connection pooling for better performance
    """
    session = getattr(_thread_sessions, "session", None)
    if session is None:
        # Thread-local: no lock needed, each thread builds its own session.
        session = requests.Session()

        # Configure retry strategy
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )

        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=20)

        session.mount("http://", adapter)
        session.mount("https://", adapter)

        # Set random User-Agent
        session.headers.update({"User-Agent": BROWSER_USER_AGENT})
        _thread_sessions.session = session

    return session


class Market(str, Enum):
    """Market type enumeration"""

    A_STOCK = "A股"
    B_STOCK = "B股"
    HK_STOCK = "港股"
    US_STOCK = "美股"
    SG_STOCK = "新加坡股"
    CRYPTO = "加密货币"


class PriceResult(TypedDict):
    """Standardized price result format"""

    price: Optional[Decimal]
    timestamp: datetime
    # 行情所属交易日（#217）：timestamp 是抓取时刻，周六刷新到的是周五收盘。
    # 报价源拿不到日期时为 None（前端退回显示刷新时刻），绝不拿 today 冒充。
    as_of: Optional[date]
    source: str
    success: bool
    error: Optional[str]
    # 昨收（腾讯报价字段[4]、Tiingo IEX prevClose）：价格异动提醒的基准。
    # 其他源拿不到时为 None——没有昨收就不判异动，绝不拿更早的收盘价凑数。
    prev_close: Optional[Decimal]


def price_result(
    *,
    price: Optional[Decimal],
    source: str,
    success: bool,
    error: Optional[str] = None,
    as_of: Optional[date] = None,
    prev_close: Optional[Decimal] = None,
) -> PriceResult:
    return {
        "price": price,
        # aware UTC：naive 本地时间写入 timestamptz 会被按 UTC 解释，
        # 在 UTC+8 环境产生"未来 8 小时"的时间戳，令新鲜度判定恒真、
        # 主动刷新长期全部跳过（实测 elapsed = -4556s）。
        "timestamp": datetime.now(timezone.utc),
        "as_of": as_of if success else None,
        "source": source,
        "success": success,
        "error": error,
        "prev_close": prev_close if success else None,
    }


def parse_quote_date(value: Any) -> Optional[date]:
    """报价源的日期/时间字段 -> 行情所属交易日；解析不了返回 None（绝不抛）。

    覆盖的形态都已是**交易所本地时间**的字符串：Tushare trade_date `20260925`、
    trade_time `2026-09-25 16:00:00`、腾讯 A 股 `20260924161444` / 港股
    `2026/09/25 16:08:20`——统一取前 8 位数字。datetime/date（含 pandas
    Timestamp）直接取日期。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) < 8:
        return None
    try:
        return datetime.strptime(digits[:8], "%Y%m%d").date()
    except ValueError:
        return None


def _row_quote_date(row: Any, *columns: str) -> Optional[date]:
    """DataFrame 行里第一个能解析的日期列。"""
    for column in columns:
        try:
            parsed = parse_quote_date(row.get(column))
        except Exception:  # noqa: BLE001 - 日期是锦上添花，不能拖垮取价
            parsed = None
        if parsed is not None:
            return parsed
    return None


def quote_date_from_epoch_ms(milliseconds: Any, market: str) -> Optional[date]:
    """毫秒时间戳 -> 该市场交易所本地日期；非法值返回 None。"""
    if isinstance(milliseconds, bool) or not isinstance(milliseconds, (int, float)):
        return None
    if not milliseconds or milliseconds != milliseconds:  # 0 / NaN
        return None
    # 交易所本地时区（market_sessions）：按业务时区换算会把美股周五收盘记成周六
    tz = market_timezone(market)
    if tz is None:
        return None
    try:
        moment = datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.astimezone(tz).date()


_tushare_lock = Lock()
_tushare_pro = None
_tushare_rate_lock = Lock()
_tushare_last_call_by_api: Dict[str, float] = {}
# 全局闸已预约的放行时刻（升序，两两相距 ≥ 全局间隔）：所有 Tushare 调用共享
_tushare_global_slots: List[float] = []


def reset_tushare_rate_gate() -> None:
    """清空限速状态（测试用）。"""
    with _tushare_rate_lock:
        _tushare_last_call_by_api.clear()
        _tushare_global_slots.clear()


def _earliest_global_slot(earliest: float, interval: float) -> float:
    """不早于 earliest、与每个已预约时刻都相距 ≥ interval 的最早时刻（槽位按升序扫描）。"""
    candidate = earliest
    for slot in _tushare_global_slots:
        if slot + interval <= candidate:
            continue  # 这个槽位早就过去了
        if candidate + interval <= slot:
            break  # candidate 与这个（及其后所有）槽位都不冲突
        candidate = slot + interval
    return candidate


def get_tushare_min_interval(api_name: str) -> float:
    """Return provider-specific call spacing to avoid known Tushare quota bursts."""
    if api_name in {"hk_mins", "hk_daily"}:
        return float(settings.tushare_hk_min_interval_seconds)
    return 0.0


# ---------------------------------------------------------------------------
# 接口级频率错误的自适应冷却
#
# Tushare 的低频接口（fina_audit / pledge_stat / stk_holdertrade 等在低积分
# 账号上是"每分钟 1 次"）没有 per-interface 闸，批量分析连打必然撞限。
# 预设固定长间隔会拖慢正常路径（11 个数据集里 3 个各等 60s，单标的分析从
# 1.5 分钟变 4.5 分钟），所以改为**错误驱动**：只有真撞限了才对该接口冷却。
#
# 冷却状态是进程内的（与 _tushare_last_call_by_api 同前提）。当前部署是单
# uvicorn 进程无 --workers；若将来多进程，两者需一并迁到 Redis/DB。
# ---------------------------------------------------------------------------

# 频率类：接口级、可等待恢复。致命类：token 失效/无权限，对整批等价。
# 判定顺序敏感——"抱歉，您"是频率消息的前缀（"抱歉，您每分钟最多访问该接口500次"），
# 必须先判频率。
TUSHARE_RATE_SIGNATURES = ("每分钟最多访问", "每小时最多访问", "每天最多访问")
# 「您的token不对，请确认。」是网关对失效/错误 token 的原文：同样对整批等价，重试无意义
TUSHARE_FATAL_SIGNATURES = ("积分不足", "权限", "抱歉，您", "token不对")

_tushare_cooldown_until: Dict[str, float] = {}
_tushare_cooldown_strikes: Dict[str, int] = {}


def classify_tushare_error(message: Any) -> str:
    """ "rate"（接口级频率，可冷却降级）/ "fatal"（token 或权限，整批等价）/ "other"。"""
    text = str(message or "")
    if any(signature in text for signature in TUSHARE_RATE_SIGNATURES):
        return "rate"
    if any(signature in text for signature in TUSHARE_FATAL_SIGNATURES):
        return "fatal"
    return "other"


def is_tushare_quota_failure(result: Dict[str, Any]) -> bool:
    """同步结果是否是 Tushare 配额/权限类失败（对整批标的等价，命中即中止整批）。

    优先读结构化的 error_kind（由 Tushare 调用点按 classify_tushare_error 写入）；没有时
    按 error 原文分类。此前 performance_history_jobs 另有一份签名清单（漏了每小时/每天
    频率），与 classify_tushare_error 口径不一，price_tail_sync 还跨模块 import 它的私有
    函数（#275）。"""
    if result.get("success"):
        return False
    kind = result.get("error_kind") or classify_tushare_error(result.get("error"))
    return kind in ("rate", "fatal")


def note_tushare_rate_error(api_name: str, message: Any = "") -> float:
    """记录接口频率错误并置冷却截止；返回本次冷却秒数。

    指数退避 base→2×base→…，封顶 tushare_cooldown_max_seconds。base 取 65s
    而非 60s：覆盖"每分钟"滑动窗口的边界。
    """
    with _tushare_rate_lock:
        strikes = _tushare_cooldown_strikes.get(api_name, 0) + 1
        _tushare_cooldown_strikes[api_name] = strikes
        seconds = min(
            settings.tushare_cooldown_base_seconds * (2 ** (strikes - 1)),
            settings.tushare_cooldown_max_seconds,
        )
        _tushare_cooldown_until[api_name] = time.monotonic() + seconds
    logger.warning(
        "Tushare %s 触发接口频率限制（第 %d 次），冷却 %.0fs：%s",
        api_name,
        strikes,
        seconds,
        str(message)[:150],
    )
    return seconds


def clear_tushare_cooldown(api_name: str) -> None:
    """调用成功即清零该接口的冷却与连击计数。"""
    if api_name not in _tushare_cooldown_until and api_name not in _tushare_cooldown_strikes:
        return
    with _tushare_rate_lock:
        _tushare_cooldown_until.pop(api_name, None)
        _tushare_cooldown_strikes.pop(api_name, None)


def tushare_cooldown_remaining(api_name: str) -> float:
    """该接口剩余冷却秒数；<=0 表示无冷却。

    调用方（sync_symbol_profile）据此决定"短冷却等一下"还是"长冷却跳过"。
    **绝不能把这个等待放进 wait_for_tushare_rate_limit**——那里按预约制给后续调用
    排时刻，塞进十几分钟的冷却会把后面所有 Tushare 调用（行情刷新、汇率、历史同步）
    的放行时刻一起推后。
    """
    until = _tushare_cooldown_until.get(api_name)
    if until is None:
        return 0.0
    return max(0.0, until - time.monotonic())


def wait_for_tushare_rate_limit(api_name: str):
    """全局闸 + per-API 闸：在锁内一次算出放行时刻并**原子地预约**，出锁后再睡。

    - per-API 闸只看本接口上次预约的时刻（hk_mins/hk_daily 31 秒）。
    - 全局闸按**槽位**预约：找不早于 per-API 放行时刻、与所有已预约时刻都相距 ≥ 全局间隔的
      最早时刻。hk_mins 预约在 31 秒之后的那个槽位不会把其他接口一起推到 31 秒后——此前只存
      一个「最后预约时刻」，零间隔接口也得排在它后面，锁虽不再贯穿 sleep，等待照旧（#275，
      PR #298 评审）。
    两类约束同时写入，不会出现跨接口突刺；sleep 期间不持锁。"""
    global_interval = settings.tushare_global_min_interval_seconds
    api_interval = get_tushare_min_interval(api_name)
    if global_interval <= 0 and api_interval <= 0:
        return

    with _tushare_rate_lock:
        now = time.monotonic()
        release_at = now
        last_api = _tushare_last_call_by_api.get(api_name)
        if api_interval > 0 and last_api is not None:
            release_at = max(release_at, last_api + api_interval)
        if global_interval > 0:
            # 过去的槽位只需保留一个间隔之内的（仍可能与 now 冲突）
            _tushare_global_slots[:] = [
                slot for slot in _tushare_global_slots if slot + global_interval > now
            ]
            release_at = _earliest_global_slot(release_at, global_interval)
            bisect.insort(_tushare_global_slots, release_at)
        _tushare_last_call_by_api[api_name] = max(release_at, last_api or release_at)

    sleep_seconds = release_at - now
    if sleep_seconds > 0:
        if sleep_seconds > 1:
            logger.info("Tushare %s 限速等待 %.1fs，避免触发接口频率限制", api_name, sleep_seconds)
        time.sleep(sleep_seconds)


class TushareEmptyResult(Exception):
    """Tushare 调用成功但没有数据（合法的空结果，如稀疏数据集、非交易日）。

    刻意**不继承 ValueError**：此前「空数据」与 JSON 解析失败、各种 ValueError 共用同一个
    异常类，调用方 `except ValueError: return []` 把上游故障也当成了空结果（#275）。"""


class TushareUpstreamError(RuntimeError):
    """Tushare 网关失败：网络异常、HTTP ≥400、非 JSON、响应结构不符。

    SDK 的 DataApi 在 HTTP ≥400 时直接返回空 DataFrame——网关 5xx / 维护页会被当成
    「成功 0 行」，档案/分红/事件同步记成功、目录分页被当成尾页截断、日线尾部把失败日标成
    已处理（#275）。文案刻意避开 TUSHARE_*_SIGNATURES 里的词，分类为 other（可重试）。"""


TUSHARE_HTTP_TIMEOUT_SECONDS = 30


class StrictTushareClient:
    """Tushare Pro HTTP 客户端：与 SDK DataApi 同一请求格式，但失败就是失败。

    `client.<api_name>(**params)` 与 `client.query(api_name, fields=..., **params)` 两种调用
    形态都与 SDK 一致，调用方（tushare_query / tushare_query_once / 历史日线）无需改动；
    测试替换 get_tushare_pro 的钩子也照旧可用。"""

    def __init__(self, token: str, endpoint: str, timeout: float = TUSHARE_HTTP_TIMEOUT_SECONDS):
        self._token = token
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout

    def query(self, api_name: str, fields: str = "", **kwargs):
        import pandas as pd

        # 与 SDK 一致：服务端会读这个参数（SDK 把自己的 http_url 放进去）
        kwargs.setdefault("ts_type_name", self._endpoint)
        payload = {"api_name": api_name, "token": self._token, "params": kwargs, "fields": fields}
        try:
            response = requests.post(
                f"{self._endpoint}/{api_name}", json=payload, timeout=self._timeout
            )
        except requests.RequestException as exc:
            raise TushareUpstreamError(
                f"tushare {api_name} 请求失败：{type(exc).__name__}"
            ) from exc
        if response.status_code >= 400:
            raise TushareUpstreamError(f"tushare {api_name} 网关返回 HTTP {response.status_code}")
        try:
            result = response.json()
        except ValueError as exc:
            raise TushareUpstreamError(f"tushare {api_name} 返回的不是 JSON") from exc
        if not isinstance(result, dict) or "code" not in result:
            raise TushareUpstreamError(f"tushare {api_name} 响应结构不符（缺 code）")
        if result["code"] != 0:
            # 业务错误原文（频率/积分/权限……）交给 classify_tushare_error 分类
            raise Exception(result.get("msg") or f"tushare {api_name} code={result['code']}")
        data = result.get("data")
        columns = data.get("fields") if isinstance(data, dict) else None
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(columns, list) or not isinstance(items, list):
            raise TushareUpstreamError(f"tushare {api_name} 响应结构不符（缺 fields/items）")
        return pd.DataFrame(items, columns=columns)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return partial(self.query, name)


def tushare_token() -> str:
    """TUSHARE_TOKEN 的唯一读取处（#278）。

    进程环境变量优先于 settings：历史部署只在环境里设了它（CLAUDE.md 记载的唯一
    `os.environ` 兼容读），其余配置一律走 settings。
    """
    return (os.environ.get("TUSHARE_TOKEN") or settings.tushare_token or "").strip()


def tushare_configured() -> bool:
    """是否配置了 Tushare token（此前 5 个模块各写一份，判空口径还不一致）。"""
    return bool(tushare_token())


def get_tushare_pro():
    """Lazy-load the strict Tushare client using TUSHARE_TOKEN."""
    global _tushare_pro

    if _tushare_pro is None:
        with _tushare_lock:
            if _tushare_pro is None:
                token = tushare_token()
                if not token:
                    raise RuntimeError("未设置 TUSHARE_TOKEN 环境变量，请提供 Tushare API key")
                _tushare_pro = StrictTushareClient(token, get_tushare_api_base_url())

    return _tushare_pro


def get_tushare_api_base_url() -> str:
    """Return the Tushare HTTPS data API base URL."""
    configured = (settings.tushare_api_base_url or "").strip()
    return (configured or DEFAULT_TUSHARE_API_BASE_URL).rstrip("/")


def tushare_query(api_name: str, **kwargs):
    """Call a Tushare Pro API with the service's existing retry behavior.

    这里是唯一同时持有 api_name 与原始错误文案的位置，因此接口级频率错误的
    冷却记录挂在这一层（retry_with_backoff 对这些文案是快速失败，不会退避）。
    """

    def fetch():
        wait_for_tushare_rate_limit(api_name)
        pro = get_tushare_pro()
        return getattr(pro, api_name)(**kwargs)

    try:
        # 只重试上游故障：频率/积分/权限类快速失败（由 classify_tushare_error 判定）
        data = retry_with_backoff(
            fetch,
            max_retries=3,
            initial_delay=0.5,
            max_delay=3.0,
            non_retryable=lambda exc: classify_tushare_error(exc) != "other",
        )
    except Exception as exc:
        if classify_tushare_error(exc) == "rate":
            note_tushare_rate_error(api_name, exc)
        raise
    clear_tushare_cooldown(api_name)
    # 空结果是合法答案，不重试（此前空结果按失败重试 3 次，hk_mins 空一次要等 60s 以上）
    if data is None or data.empty:
        raise TushareEmptyResult(f"tushare {api_name} 返回空数据")
    return data


def tushare_query_once(api_name: str, **kwargs):
    """Single-attempt Tushare call for lookups where an empty result is a
    definitive answer (e.g. name resolution), not a transient failure to retry."""
    wait_for_tushare_rate_limit(api_name)
    pro = get_tushare_pro()
    return getattr(pro, api_name)(**kwargs)


def positive_decimal_price(value: Any) -> Decimal:
    """Convert a provider value to a finite, positive Decimal price."""
    price = Decimal(str(value))
    if not price.is_finite() or price <= 0:
        raise ValueError(f"无效价格数据: {value}")
    return price


def to_tencent_quote_code(symbol: str, market: Market) -> str:
    text = str(symbol or "").strip().upper()
    if market in {Market.A_STOCK, Market.B_STOCK}:
        exchange = get_exchange_type(text)
        prefix = {"sh": "sh", "sz": "sz", "bj": "bj"}[exchange]
        return f"{prefix}{text}"
    if market == Market.HK_STOCK:
        code = text[:-3] if text.endswith(".HK") else text
        return f"hk{code.zfill(5)}" if code.isdigit() else f"hk{code}"
    raise ValueError(f"腾讯行情不支持市场类型: {market.value}")


def parse_tencent_quote_fields(text: str, quote_code: str) -> list:
    """腾讯行情 `v_{code}="a~b~c~…";` 载荷 → 字段列表（字段[1] 为名称、[3] 为最新价）。"""
    marker = f'v_{quote_code}="'
    start = text.find(marker)
    if start < 0:
        raise ValueError(f"腾讯行情未返回 {quote_code}")
    start += len(marker)
    end = text.find('";', start)
    if end < 0:
        raise ValueError(f"腾讯行情响应格式异常: {quote_code}")
    return text[start:end].split("~")


def to_tencent_name_code(symbol: str, market: str) -> Optional[str]:
    """名称解析用的腾讯代码（比 to_tencent_quote_code 多支持美股 us{TICKER}）。
    刻意不放宽 to_tencent_quote_code：价格链依赖它对美股抛 ValueError 走别的源。"""
    text = str(symbol or "").strip().upper()
    if not text:
        return None
    if market in {"A股", "B股"}:
        return f"{get_exchange_type(text)}{text}"
    if market == "港股":
        code = text[:-3] if text.endswith(".HK") else text
        return f"hk{code.zfill(5)}" if code.isdigit() else f"hk{code}"
    if market == "美股":
        return f"us{text}"
    return None


class QuoteNameLookupError(Exception):
    """取名调用本身失败（网络/HTTP/解析）——与「腾讯确认没有这个代码」区分开（#275）。"""


def fetch_tencent_quote_name(symbol: str, market: str) -> Optional[str]:
    """按需取标的中文名（A/B/港/美）。腾讯应答了但没有这个代码 → None；调用失败 → 抛
    QuoteNameLookupError（此前一律返回 None，网络故障被提示成「请确认代码与市场」）。
    响应是 GBK：价格解析从不在乎编码，名称必须显式设定。"""
    quote_code = to_tencent_name_code(symbol, market)
    if quote_code is None:
        return None
    try:
        throttle_tencent()
        response = get_session().get(
            TENCENT_QUOTE_URL.format(codes=quote_code), headers=TENCENT_HEADERS, timeout=(3, 8)
        )
        response.raise_for_status()
        response.encoding = "gbk"
        text = response.text
    except Exception as exc:  # noqa: BLE001
        logger.warning("腾讯行情取名失败 %s/%s: %s", market, symbol, str(exc)[:160])
        raise QuoteNameLookupError(str(exc)[:160]) from exc
    if f'v_{quote_code}="' not in text:
        return None  # 腾讯正常应答但没有这个代码的字段 = 不认识它
    try:
        fields = parse_tencent_quote_fields(text, quote_code)
    except ValueError as exc:  # 有字段却截断/格式异常：应答本身坏了，不等于没有这个代码
        raise QuoteNameLookupError(str(exc)[:160]) from exc
    name = fields[1].strip() if len(fields) > 1 else ""
    return name or None


def parse_tencent_quote_date(text: str, quote_code: str) -> Optional[date]:
    """腾讯行情字段[30] 是行情时间（A 股 `20260924161444`、港股 `2026/09/25 16:08:20`，
    均为交易所本地时间）；缺失或格式异常返回 None。"""
    try:
        fields = parse_tencent_quote_fields(text, quote_code)
    except ValueError:
        return None
    return parse_quote_date(fields[30]) if len(fields) > 30 else None


def parse_tencent_quote_prev_close(text: str, quote_code: str) -> Optional[Decimal]:
    """腾讯行情字段[4] 是昨收；缺失或非正数返回 None（只影响异动判断，不影响取价）。"""
    try:
        fields = parse_tencent_quote_fields(text, quote_code)
        return positive_decimal_price(fields[4]) if len(fields) > 4 else None
    except (ValueError, ArithmeticError):
        return None


def parse_tencent_quote_price(text: str, quote_code: str) -> Decimal:
    fields = parse_tencent_quote_fields(text, quote_code)
    if len(fields) < 4:
        raise ValueError(f"腾讯行情字段不足: {quote_code}")
    name = fields[1] if len(fields) > 1 else quote_code
    price = positive_decimal_price(fields[3])
    logger.info("✓ 腾讯行情 %s %s 成功: %s", quote_code, name, price)
    return price


def fetch_tencent_stock_price(symbol: str, market: Market) -> PriceResult:
    quote_code = to_tencent_quote_code(symbol, market)
    try:
        throttle_tencent()
        response = get_session().get(
            TENCENT_QUOTE_URL.format(codes=quote_code), headers=TENCENT_HEADERS, timeout=(3, 8)
        )
        response.raise_for_status()
        price = parse_tencent_quote_price(response.text, quote_code)
        return price_result(
            price=price,
            source="tencent-quote",
            success=True,
            as_of=parse_tencent_quote_date(response.text, quote_code),
            prev_close=parse_tencent_quote_prev_close(response.text, quote_code),
        )
    except Exception as exc:
        error_msg = f"腾讯行情获取失败: {str(exc)[:200]}"
        logger.warning("  %s %s %s", market.value, symbol, error_msg)
        return price_result(price=None, source="tencent-quote", success=False, error=error_msg)


def to_tushare_a_code(symbol: str) -> str:
    """Convert A/B-share code to Tushare ts_code."""
    text = str(symbol or "").strip().upper()
    if "." in text:
        return text

    exchange = get_exchange_type(text)
    suffix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}[exchange]
    return f"{text}.{suffix}"


def to_tushare_hk_code(symbol: str) -> str:
    """Convert HK code to Tushare's 5-digit .HK format."""
    text = str(symbol or "").strip().upper()
    code = text[:-3] if text.endswith(".HK") else text
    return f"{code.zfill(5)}.HK" if code.isdigit() else text


def to_tushare_crypto_code(symbol: str) -> str:
    """Convert BTC, BTC-USD, BTC/USDT into Tushare coin_bar format."""
    text = str(symbol or "").strip().upper().replace("-", "_").replace("/", "_")
    if "_" not in text:
        return f"{text}_USDT"
    if text.endswith("_USD"):
        return f"{text}T"
    return text


# 通用快速失败判据（Yahoo 等非 Tushare 调用点的默认值）；Tushare 调用点传 classify_tushare_error
_DEFAULT_NON_RETRYABLE = (
    "not found",
    "不存在",
    "invalid symbol",
    "无效",
    "每分钟最多访问",
    "每小时最多访问",
    "每天最多访问",
    "权限",
    "积分不足",
    "抱歉，您",
)


def _default_non_retryable(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _DEFAULT_NON_RETRYABLE)


def retry_with_backoff(
    func, max_retries=3, initial_delay=1.0, max_delay=10.0, *, non_retryable=None
):
    """
    Retry a function with exponential backoff and jitter

    Improvements:
    - Added jitter to prevent thundering herd
    - Added max_delay cap
    - Better error classification

    Args:
        func: Function to retry
        max_retries: Maximum number of retry attempts
        initial_delay: Initial delay in seconds
        max_delay: Maximum delay between retries

    Returns:
        Function result or raises last exception
    """
    last_exception = None

    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            last_exception = e

            # Don't retry on certain errors. 配额/权限类错误重试只会火上浇油
            # （限流窗口内连打 3 次），必须立刻失败。判据可由调用方注入：
            # Tushare 调用点传 classify_tushare_error（唯一判定入口，#275）
            if (non_retryable or _default_non_retryable)(e):
                logger.error(f"Non-retryable error: {str(e)}")
                raise

            if attempt == max_retries - 1:
                raise

            # Exponential backoff with jitter
            delay = min(initial_delay * (2**attempt), max_delay)
            jitter = random.uniform(0, delay * 0.1)  # 10% jitter
            total_delay = delay + jitter

            logger.warning(
                f"Attempt {attempt + 1}/{max_retries} failed: {str(e)}. "
                f"Retrying in {total_delay:.2f}s..."
            )
            time.sleep(total_delay)

    raise last_exception or Exception("Max retries exceeded")


def fetch_a_stock_price_tushare(symbol: str) -> PriceResult:
    """
    Fetch A/B-share current price from Tushare.

    Primary source is rt_k. If realtime is unavailable outside market/data
    windows, fall back to the latest daily close.
    """
    ts_code = to_tushare_a_code(symbol)
    logger.info(f"获取A股价格(Tushare): {symbol} -> {ts_code}")

    try:
        df = tushare_query("rt_k", ts_code=ts_code)
        row = df.iloc[0]
        price = positive_decimal_price(row.get("close"))
        logger.info(f"✓ A股 {symbol} Tushare rt_k成功: {price}")
        return price_result(
            price=price,
            source="tushare-rt_k",
            success=True,
            as_of=_row_quote_date(row, "trade_date", "trade_time"),
        )
    except Exception as e:
        logger.warning(f"  Tushare rt_k失败，尝试daily最新收盘价: {str(e)[:120]}")

    try:
        df = tushare_query("daily", ts_code=ts_code)
        row = df.sort_values("trade_date").iloc[-1]
        price = positive_decimal_price(row.get("close"))
        logger.info(f"✓ A股 {symbol} Tushare daily成功: {price}")
        return price_result(
            price=price,
            source="tushare-daily",
            success=True,
            as_of=_row_quote_date(row, "trade_date"),
        )
    except Exception as e:
        error_msg = f"Tushare获取A股价格失败: {str(e)[:200]}"
        logger.error(f"✗ A股 {symbol} 失败: {error_msg}")
        return price_result(price=None, source="all-failed", success=False, error=error_msg)


def fetch_a_stock_price(symbol: str, market: Market) -> PriceResult:
    """Fetch A/B-share price with fast public quote fallback before Tushare."""
    tencent_result = fetch_tencent_stock_price(symbol, market)
    if tencent_result["success"]:
        return tencent_result

    tushare_result = fetch_a_stock_price_tushare(symbol)
    if tushare_result["success"]:
        return tushare_result

    return price_result(
        price=None,
        source="all-failed",
        success=False,
        error=f"{tencent_result.get('error')}; {tushare_result.get('error')}",
    )


def fetch_hk_stock_price_tushare(symbol: str) -> PriceResult:
    """
    Fetch HK stock price from Tushare latest minute bar, with daily fallback.
    """
    ts_code = to_tushare_hk_code(symbol)
    logger.info(f"获取港股价格(Tushare): {symbol} -> {ts_code}")

    try:
        df = tushare_query("hk_mins", ts_code=ts_code, freq="1min")
        row = df.sort_values("trade_time").iloc[-1]
        price = positive_decimal_price(row.get("close"))
        logger.info(f"✓ 港股 {symbol} Tushare hk_mins成功: {price}")
        return price_result(
            price=price,
            source="tushare-hk_mins",
            success=True,
            as_of=_row_quote_date(row, "trade_time"),
        )
    except Exception as e:
        logger.warning(f"  Tushare hk_mins失败，尝试hk_daily最新收盘价: {str(e)[:120]}")

    try:
        df = tushare_query("hk_daily", ts_code=ts_code)
        row = df.sort_values("trade_date").iloc[-1]
        price = positive_decimal_price(row.get("close"))
        logger.info(f"✓ 港股 {symbol} Tushare hk_daily成功: {price}")
        return price_result(
            price=price,
            source="tushare-hk_daily",
            success=True,
            as_of=_row_quote_date(row, "trade_date"),
        )

    except Exception as e:
        error_msg = f"港股 {symbol} Tushare获取失败: {str(e)[:200]}"
        logger.error(error_msg)
        return price_result(price=None, source="all-failed", success=False, error=error_msg)


def fetch_hk_stock_price(symbol: str) -> PriceResult:
    """Fetch HK stock price from Tencent first to avoid slow Tushare minute limits."""
    tencent_result = fetch_tencent_stock_price(symbol, Market.HK_STOCK)
    if tencent_result["success"]:
        return tencent_result

    tushare_result = fetch_hk_stock_price_tushare(symbol)
    if tushare_result["success"]:
        return tushare_result

    return price_result(
        price=None,
        source="all-failed",
        success=False,
        error=f"{tencent_result.get('error')}; {tushare_result.get('error')}",
    )


def fetch_us_stock_price_tushare(symbol: str) -> PriceResult:
    """
    Fetch US stock price from Tushare us_daily latest bar.
    """
    ts_code = str(symbol or "").strip().upper()
    logger.info(f"获取美股价格(Tushare): {symbol} -> {ts_code}")

    try:
        df = tushare_query("us_daily", ts_code=ts_code)
        row = df.sort_values("trade_date").iloc[-1]
        price = positive_decimal_price(row.get("close"))
        logger.info(f"✓ 美股 {symbol} Tushare us_daily成功: {price}")
        return price_result(
            price=price,
            source="tushare-us_daily",
            success=True,
            as_of=_row_quote_date(row, "trade_date"),
        )

    except Exception as e:
        error_msg = f"美股 {symbol} Tushare获取失败: {str(e)[:200]}"
        logger.error(error_msg)
        return price_result(price=None, source="tushare-us_daily", success=False, error=error_msg)


def us_session_open(now: Optional[datetime] = None) -> bool:
    """美股是否处于交易时段（含收盘后余量）；单独成函数便于测试固定时刻。"""
    from .market_sessions import is_session_open

    return is_session_open("美股", now or datetime.now(timezone.utc))


def fetch_us_stock_price(symbol: str) -> PriceResult:
    """美股：Tushare us_daily → Tiingo（IEX 最新价 / 最新日线收盘）→ 雪球实时报价。

    腾讯行情不支持美股。Tiingo 免费档只要一个 API Token、不会过期，排在雪球之前；
    雪球 Cookie 约 15 天过期且每次请求限速 2-4s、全局串行，降为最后兜底。
    未配置 TIINGO_API_TOKEN 时 Tiingo 显式返回失败（不外呼），链条照常走到雪球。

    **美股交易时段内 Tiingo 提前**：us_daily 只有日线收盘，盘中排第一会永远成功地
    返回昨收，实时价就永远拿不到。盘外保持原顺序（us_daily 不耗 Tiingo 的小时额度）。
    """
    from . import tiingo_source, xueqiu_source

    def tushare() -> PriceResult:
        return fetch_us_stock_price_tushare(symbol)

    def tiingo() -> PriceResult:
        return tiingo_source.fetch_tiingo_stock_price(symbol)

    def xueqiu() -> PriceResult:
        return xueqiu_source.fetch_xueqiu_stock_price(symbol, Market.US_STOCK)

    chain = [tiingo, tushare, xueqiu] if us_session_open() else [tushare, tiingo, xueqiu]
    errors = []
    for fetch in chain:
        result = fetch()
        if result["success"]:
            return result
        errors.append(str(result.get("error")))

    return price_result(price=None, source="all-failed", success=False, error="; ".join(errors))


def fetch_crypto_price_tushare(symbol: str) -> PriceResult:
    """
    Fetch crypto price from Tushare coin_bar latest daily bar.
    """
    ts_code = to_tushare_crypto_code(symbol)
    logger.info(f"获取加密货币价格(Tushare): {symbol} -> {ts_code}")

    try:
        df = tushare_query(
            "coin_bar",
            exchange="binance",
            ts_code=ts_code,
            freq="1day",
        )
        date_col = "trade_time" if "trade_time" in df.columns else df.columns[0]
        row = df.sort_values(date_col).iloc[-1]
        price = positive_decimal_price(row.get("close"))
        logger.info(f"✓ 加密货币 {symbol} Tushare coin_bar成功: {price}")
        return price_result(
            price=price,
            source="tushare-coin_bar",
            success=True,
            as_of=_row_quote_date(row, date_col),
        )

    except Exception as e:
        error_msg = f"加密货币 {symbol} Tushare获取失败: {str(e)[:200]}"
        logger.error(error_msg)
        return price_result(price=None, source="tushare-coin_bar", success=False, error=error_msg)


def fetch_global_price_tushare(symbol: str, market: Market) -> PriceResult:
    """
    Fetch non-China-market prices using Tushare where supported.
    """
    if market == Market.US_STOCK:
        return fetch_us_stock_price_tushare(symbol)
    if market == Market.CRYPTO:
        return fetch_crypto_price_tushare(symbol)

    error_msg = f"Tushare当前未配置 {market.value} 价格接口: {symbol}"
    logger.error(error_msg)
    return price_result(price=None, source="tushare-unsupported", success=False, error=error_msg)


def fetch_stock_price(symbol: str, market: str) -> PriceResult:
    """
    Unified entry point for fetching stock prices
    Routes to appropriate API based on market type
    """
    try:
        market_enum = Market(market)
    except ValueError:
        return price_result(
            price=None,
            source="unknown",
            success=False,
            error=f"不支持的市场类型: {market}",
        )

    if market_enum in [Market.A_STOCK, Market.B_STOCK]:
        return fetch_a_stock_price(symbol, market_enum)
    elif market_enum == Market.HK_STOCK:
        return fetch_hk_stock_price(symbol)
    elif market_enum == Market.US_STOCK:
        return fetch_us_stock_price(symbol)
    else:
        return fetch_global_price_tushare(symbol, market_enum)


QuoteKey = Tuple[str, str]

# 自选条目的基准价在加入后多久内取到算「加入时报价」，超出算「加入后首次报价」
ADDED_PRICE_QUOTE_WINDOW = timedelta(hours=1)


def ensure_utc(moment: Optional[datetime]) -> Optional[datetime]:
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def added_price_basis_for(created_at: Optional[datetime], moment: datetime) -> Optional[str]:
    """这次报价能否作为自选条目的基准价；None = 不能（留给 `backfill_added_prices`）。

    只有加入后 ADDED_PRICE_QUOTE_WINDOW 内取到的报价才直接作基准（quote，加入时报价）。
    更晚的一律先按加入日收盘补（close_on_add）：用「加入后首次报价」会把加入前几天的
    价格差抹掉（周末加入、下周一才刷新到的就是下周一的价）；确认加入日前后都没有行情时
    回填会把口径标成 pending_quote，下一次报价才作为基准（first_quote）。
    """
    created, moment = ensure_utc(created_at), ensure_utc(moment)
    if created is None:
        return "quote"
    return "quote" if moment - created <= ADDED_PRICE_QUOTE_WINDOW else None


def _collect_quote_rows(
    db: Session,
    *,
    user_id: Optional[int],
    markets: Optional[Iterable[str]],
    keys: Optional[Iterable[QuoteKey]],
    only_open: bool,
) -> Dict[QuoteKey, Dict[str, list]]:
    """读出待刷新行的**快照**（列元组，不是 ORM 对象）：外呼期间不持有会话里的实体，
    写回一律按 id 发 UPDATE——外呼期间被删除的行（清仓、移出自选）只是影响 0 行，
    不会像 ORM flush 那样抛 StaleDataError 让整轮所有用户的报价一起回滚。"""
    from ..models.holding import Holding
    from ..models.user import User
    from ..models.watchlist_item import WatchlistItem

    holding_query = db.query(
        Holding.id,
        Holding.symbol,
        Holding.market,
        Holding.name,
        Holding.quantity,
        Holding.current_price,
        Holding.price_updated_at,
        Holding.price_as_of,
        Holding.price_source,
    )
    watch_query = db.query(
        WatchlistItem.id,
        WatchlistItem.symbol,
        WatchlistItem.market,
        WatchlistItem.current_price,
        WatchlistItem.price_updated_at,
        WatchlistItem.price_as_of,
        WatchlistItem.price_source,
        WatchlistItem.created_at,
        WatchlistItem.added_price,
        WatchlistItem.added_price_basis,
    )
    if user_id is not None:
        holding_query = holding_query.filter(Holding.user_id == user_id)
        watch_query = watch_query.filter(WatchlistItem.user_id == user_id)
    else:
        holding_query = holding_query.join(User, User.id == Holding.user_id).filter(
            User.is_active.is_(True)
        )
        watch_query = watch_query.join(User, User.id == WatchlistItem.user_id).filter(
            User.is_active.is_(True)
        )
    if only_open:
        holding_query = holding_query.filter(Holding.quantity > 0)
    if markets is not None:
        market_list = list(markets)
        holding_query = holding_query.filter(Holding.market.in_(market_list))
        watch_query = watch_query.filter(WatchlistItem.market.in_(market_list))
    key_filter = set(keys) if keys is not None else None
    if key_filter is not None:
        symbols = {symbol for symbol, _ in key_filter}
        holding_query = holding_query.filter(Holding.symbol.in_(symbols))
        watch_query = watch_query.filter(WatchlistItem.symbol.in_(symbols))

    rows_by_key: Dict[QuoteKey, Dict[str, list]] = {}
    for kind, rows in (("holdings", holding_query.all()), ("watch", watch_query.all())):
        for row in rows:
            key = (row.symbol, row.market)
            if key_filter is None or key in key_filter:
                rows_by_key.setdefault(key, {"holdings": [], "watch": []})[kind].append(row)
    return rows_by_key


def _freshest_row(rows: List[Any], now: datetime) -> Tuple[Optional[Any], Optional[float]]:
    """同一标的里最近刷新过的一行及其距今秒数；未来时间戳（历史 naive 写入的脏数据）
    不算新鲜——必须刷新，让 aware UTC 覆盖自愈。"""
    freshest, freshest_elapsed = None, None
    for row in rows:
        updated = ensure_utc(row.price_updated_at)
        if row.current_price is None or updated is None:
            continue
        elapsed = (now - updated).total_seconds()
        if elapsed < 0:
            continue
        if freshest_elapsed is None or elapsed < freshest_elapsed:
            freshest, freshest_elapsed = row, elapsed
    return freshest, freshest_elapsed


def _write_quote(
    db: Session,
    group: Dict[str, list],
    *,
    price: Decimal,
    updated_at: datetime,
    as_of: Optional[date],
    source: Optional[str],
) -> None:
    """按 id 把一份报价写回同一标的的持仓行与自选行。

    守卫（都在 WHERE 里，与并发的手动刷新/加入自选不冲突）：
    - 不用更旧的报价覆盖更新的：行上的刷新时刻晚于本次报价时刻的不动（未来时间戳除外）；
    - 行情日期不倒退：行上已是更晚交易日的价格时不动（例如 Tiingo 限流后兜底到
      us_daily，拿到的是上一交易日收盘，不能盖掉今天的盘中价）；
    - 自选基准价只填空值，且只对近期加入的条目（见 added_price_basis_for）。
    """
    from sqlalchemy import or_

    from ..models.holding import Holding
    from ..models.watchlist_item import WatchlistItem

    updated_at = ensure_utc(updated_at)
    values = {
        "current_price": price,
        "price_updated_at": updated_at,
        # 行情日期与来源随价格整组覆盖：拿不到日期写 None，不保留上一次的旧日期
        "price_as_of": as_of,
        "price_source": (source or "")[:40] or None,
    }
    for model, rows in ((Holding, group["holdings"]), (WatchlistItem, group["watch"])):
        ids = [row.id for row in rows]
        if not ids:
            continue
        conditions = [
            model.id.in_(ids),
            or_(
                model.price_updated_at.is_(None),
                model.price_updated_at <= updated_at,
                # 未来时间戳是历史 naive 写入的脏数据，必须允许覆盖自愈
                model.price_updated_at > datetime.now(timezone.utc),
            ),
        ]
        if as_of is not None:
            conditions.append(or_(model.price_as_of.is_(None), model.price_as_of <= as_of))
        db.query(model).filter(*conditions).update(values, synchronize_session=False)

    for row in group["watch"]:
        if row.added_price is not None:
            continue
        if row.added_price_basis == "pending_quote":
            # 存量条目的历史回补已确认加入日前后都没有收盘：这次报价就是基准
            basis = "first_quote"
        else:
            basis = added_price_basis_for(row.created_at, updated_at)
        if basis is None:
            continue
        from ..core.timeutil import to_local_date

        db.query(WatchlistItem).filter(
            WatchlistItem.id == row.id, WatchlistItem.added_price.is_(None)
        ).update(
            {
                "added_price": price,
                "added_price_date": as_of or to_local_date(updated_at),
                "added_price_basis": basis,
            },
            synchronize_session=False,
        )


def apply_latest_closes(db: Session, keys: Iterable[QuoteKey]) -> int:
    """把更新的日线收盘追平到持仓/自选的现价（#267），返回更新的行数，调用方负责 commit。

    现价只由 15 分钟一次的盘中刷新推进：错过收盘后那次刷新（进程重启、报价源失败、
    港股这类只靠日报补收盘的市场），持仓现价就停在旧日期，而日线尾部同步已经把收盘入库，
    持仓页与仪表盘便各说各话。择优规则与估值取价同一份（`history_close_wins`）：收盘
    日期更新、或同日而现价不是手工价时才覆盖。只写现价四列，不碰自选基准价——
    基准价有自己的回补口径（watchlist_price_service）。"""
    from sqlalchemy import tuple_

    from ..models.holding import Holding
    from ..models.watchlist_item import WatchlistItem
    from .statistics.pricing import history_close_wins, latest_closes, quote_market_date

    closes = latest_closes(db, keys)
    if not closes:
        return 0
    now = datetime.now(timezone.utc)
    updated = 0
    for model in (Holding, WatchlistItem):
        rows = (
            db.query(
                model.id,
                model.symbol,
                model.market,
                model.current_price,
                model.price_as_of,
                model.price_updated_at,
                model.price_source,
            )
            .filter(tuple_(model.symbol, model.market).in_(list(closes)))
            .all()
        )
        for row in rows:
            close, close_date, close_source = closes[(row.symbol, row.market)]
            close_price = Decimal(str(close))
            source = f"close:{close_source}"[:40] if close_source else "close"
            # 已经是这份收盘：不重写。否则同日收盘每个 tick 都「胜出」一次、每小时把
            # price_updated_at 推到 now，手动刷新会被新鲜度窗口跳过（PR #293 评审）
            if (
                row.price_as_of == close_date
                and row.current_price == close_price
                and (row.price_source or "").startswith("close")
            ):
                continue
            row_date = (
                quote_market_date(row.market, row.price_as_of, row.price_updated_at)
                if row.current_price is not None
                else None
            )
            if not history_close_wins(close_date, row_date, row.price_source):
                continue
            # 按 id 的比较交换：读取之后若有报价/手工价写入（price_updated_at 变了），本行放弃，
            # 不拿更旧的收盘覆盖（与 _write_quote 同一类带守卫的 UPDATE）
            changed = (
                db.query(model)
                .filter(
                    model.id == row.id,
                    (
                        model.price_updated_at.is_(None)
                        if row.price_updated_at is None
                        else model.price_updated_at == row.price_updated_at
                    ),
                )
                .update(
                    {
                        model.current_price: close_price,
                        model.price_updated_at: now,
                        model.price_as_of: close_date,
                        model.price_source: source,
                    },
                    synchronize_session=False,
                )
            )
            updated += changed
    return updated


def _price_move(symbol: str, market: str, group: Dict[str, list], result: PriceResult):
    """有昨收、且至少一条数量 > 0 的持仓才构成异动候选（阈值由提醒层判定）。"""
    prev_close = result.get("prev_close")
    open_holdings = [row for row in group["holdings"] if (row.quantity or 0) > 0]
    if not prev_close or prev_close <= 0 or not open_holdings:
        return None
    price = result["price"]
    as_of = result.get("as_of")
    return {
        "symbol": symbol,
        "market": market,
        "name": next((row.name for row in open_holdings if row.name), None),
        "price": float(price),
        "prev_close": float(prev_close),
        "pct": float((price / prev_close - 1) * 100),
        "as_of": as_of.isoformat() if as_of else None,
    }


def refresh_quotes(
    db: Session,
    *,
    user_id: Optional[int] = None,
    markets: Optional[Iterable[str]] = None,
    keys: Optional[Iterable[QuoteKey]] = None,
    only_open: bool = False,
    ignore_freshness: bool = False,
) -> Dict[str, Any]:
    """刷新持仓与自选的最新报价：按 (symbol, market) 去重，每个标的只请求一次，
    结果写回所有匹配的持仓行与自选行。

    - user_id：只刷该用户（手动「刷新价格」）；缺省 = 全体活跃用户（周期任务）。
    - markets / keys：限定市场或标的（周期任务只刷处于交易时段的市场；加入自选时只刷那一只）。
    - only_open：持仓只取数量 > 0 的行（周期任务不为已清仓的历史行耗配额）。
    - ignore_freshness：周期任务不跳过刚刷新过的标的——否则收盘前几分钟的一次手动刷新
      会让收盘后那一次周期刷新被跳过，盘中价就留到了第二天。

    新鲜度窗口（`price_refresh_freshness_seconds`）内刷新过的标的不请求，而是把最新那份
    报价同步到同一标的里更旧的行（例如刚加入自选、而持仓行几分钟前刚刷新过）。
    计数与 success/failed/skipped 列表按**标的**计。`moves` 是本次成功报价里有昨收、且
    至少有一条数量 > 0 持仓的标的（pct 为百分比），供价格异动提醒使用。
    """
    rows_by_key = _collect_quote_rows(
        db, user_id=user_id, markets=markets, keys=keys, only_open=only_open
    )
    # 结束读事务：外呼可能持续数十秒，期间不占着一个 idle-in-transaction 的连接
    db.commit()
    success_list: List[Dict[str, Any]] = []
    failed_list: List[Dict[str, Any]] = []
    skipped_list: List[Dict[str, Any]] = []
    moves: List[Dict[str, Any]] = []
    pending: List[QuoteKey] = []
    shares: List[Tuple[Dict[str, list], Any]] = []

    now = datetime.now(timezone.utc)
    freshness_window = 0 if ignore_freshness else settings.price_refresh_freshness_seconds
    logger.info(
        "======== 开始批量刷新股价：%s 个标的（持仓 %s 行、自选 %s 行）========",
        len(rows_by_key),
        sum(len(group["holdings"]) for group in rows_by_key.values()),
        sum(len(group["watch"]) for group in rows_by_key.values()),
    )

    for key, group in rows_by_key.items():
        freshest, elapsed = _freshest_row([*group["holdings"], *group["watch"]], now)
        if freshest is None or elapsed >= freshness_window:
            pending.append(key)
            continue
        shares.append((group, freshest))
        logger.info("  ⏭️  跳过 %s/%s: 最近%s秒前已更新", key[1], key[0], int(elapsed))
        skipped_list.append(
            {"symbol": key[0], "market": key[1], "reason": f"最近{int(elapsed)}秒前已更新"}
        )

    def fetch_pending_price(key: QuoteKey) -> Dict[str, Any]:
        start_time = time.time()
        try:
            result = fetch_stock_price(key[0], key[1])
            error = None
        except Exception as exc:
            result, error = None, f"未预期的错误: {str(exc)}"
        return {"key": key, "result": result, "elapsed": time.time() - start_time, "error": error}

    max_workers = max(1, min(settings.price_refresh_max_workers, len(pending) or 1))
    logger.info(f"待刷新标的数: {len(pending)}, 并发数: {max_workers}")

    fetched_results = []
    if pending:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(fetch_pending_price, key) for key in pending]
            for index, future in enumerate(as_completed(futures), start=1):
                fetched = future.result()
                fetched_results.append(fetched)
                result = fetched["result"]
                symbol = fetched["key"][0]
                if result and result.get("success"):
                    logger.info(
                        f"[{index}/{len(pending)}] {symbol} 成功: {result.get('price')} "
                        f"({result.get('source')}) - 耗时: {fetched['elapsed']:.2f}s"
                    )
                else:
                    error = fetched["error"] or (result or {}).get("error") or "未知错误"
                    logger.warning(f"[{index}/{len(pending)}] {symbol} 失败: {error[:100]}")

    try:
        # 新鲜度窗口内跳过的标的：把最新那份报价同步给同一标的里更旧的行（守卫在 WHERE 里）
        for group, freshest in shares:
            _write_quote(
                db,
                group,
                price=freshest.current_price,
                updated_at=freshest.price_updated_at,
                as_of=freshest.price_as_of,
                source=freshest.price_source,
            )
        for fetched in fetched_results:
            symbol, market = fetched["key"]
            group = rows_by_key[fetched["key"]]
            result = fetched["result"]
            price = result.get("price") if result else None
            if not (result and result["success"] and price and price.is_finite() and price > 0):
                failed_list.append(
                    {
                        "symbol": symbol,
                        "market": market,
                        "error": fetched["error"] or (result or {}).get("error") or "未知错误",
                    }
                )
                continue
            _write_quote(
                db,
                group,
                price=price,
                updated_at=result["timestamp"],
                as_of=result.get("as_of"),
                source=result.get("source"),
            )
            success_list.append(
                {
                    "symbol": symbol,
                    "market": market,
                    "price": float(price),
                    "source": result["source"],
                }
            )
            move = _price_move(symbol, market, group, result)
            if move is not None:
                moves.append(move)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"❌ 数据库提交失败: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"数据库提交失败: {str(e)}",
            "success_count": 0,
            "failed_count": len(rows_by_key),
            "skipped_count": 0,
            "moves": [],
        }

    logger.info(
        "======== 批量刷新完成：成功 %s、失败 %s、跳过 %s，耗时 %.2fs ========",
        len(success_list),
        len(failed_list),
        len(skipped_list),
        (datetime.now(timezone.utc) - now).total_seconds(),
    )
    return {
        "success": True,
        "success_count": len(success_list),
        "failed_count": len(failed_list),
        "skipped_count": len(skipped_list),
        "success_list": success_list,
        "failed_list": failed_list,
        "skipped_list": skipped_list,
        "moves": moves,
    }


def update_all_holdings_prices(db: Session, user_id: int = None) -> Dict[str, Any]:
    """手动「刷新价格」：刷新该用户（缺省 = 全体活跃用户）的全部持仓与自选报价。

    持仓不按数量过滤（与历史行为一致：已清仓的行也刷新）；实现见 `refresh_quotes`。
    """
    return refresh_quotes(db, user_id=user_id)
