"""
Stock Price Service Module

Fetches real-time stock prices from different sources:
- A股/B股: Tushare rt_k
- 港股: Tushare hk_daily latest bar
- 美股: Tushare us_daily latest bar
- 加密货币: Tushare coin_bar latest bar

Optimizations:
- Request pooling and session reuse
- Exponential backoff with jitter
- Rate limiting and queue management
- Comprehensive error handling
- User-Agent rotation
"""

from enum import Enum
from typing import TypedDict, Optional, Dict, Any
from decimal import Decimal
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import os
import random
from importlib import import_module
from threading import Lock, local

from sqlalchemy.orm import Session
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..config import settings
from ..core.logging import get_app_logger

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

# User-Agent rotation to avoid blocking
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
]


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
        session.headers.update({"User-Agent": random.choice(USER_AGENTS)})
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
    source: str
    success: bool
    error: Optional[str]


def price_result(
    *,
    price: Optional[Decimal],
    source: str,
    success: bool,
    error: Optional[str] = None,
) -> PriceResult:
    return {
        "price": price,
        # aware UTC：naive 本地时间写入 timestamptz 会被按 UTC 解释，
        # 在 UTC+8 环境产生"未来 8 小时"的时间戳，令新鲜度判定恒真、
        # 主动刷新长期全部跳过（实测 elapsed = -4556s）。
        "timestamp": datetime.now(timezone.utc),
        "source": source,
        "success": success,
        "error": error,
    }


_tushare_lock = Lock()
_tushare_pro = None
_tushare_rate_lock = Lock()
_tushare_last_call_by_api: Dict[str, float] = {}


# 全局限速键：所有 Tushare 调用共享（并发线程与不同 API 一并节流）。
_TUSHARE_GLOBAL_KEY = "__global__"


def get_tushare_min_interval(api_name: str) -> float:
    """Return provider-specific call spacing to avoid known Tushare quota bursts."""
    if api_name in {"hk_mins", "hk_daily"}:
        return float(os.environ.get("TUSHARE_HK_MIN_INTERVAL_SECONDS", "31"))
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
TUSHARE_FATAL_SIGNATURES = ("积分不足", "权限", "抱歉，您")

_tushare_cooldown_until: Dict[str, float] = {}
_tushare_cooldown_strikes: Dict[str, int] = {}


def classify_tushare_error(message: Any) -> str:
    """"rate"（接口级频率，可冷却降级）/ "fatal"（token 或权限，整批等价）/ "other"。"""
    text = str(message or "")
    if any(signature in text for signature in TUSHARE_RATE_SIGNATURES):
        return "rate"
    if any(signature in text for signature in TUSHARE_FATAL_SIGNATURES):
        return "fatal"
    return "other"


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
        api_name, strikes, seconds, str(message)[:150],
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
    **绝不能把这个等待放进 wait_for_tushare_rate_limit**——那里的 sleep 在
    _tushare_rate_lock 临界区内，塞进十几分钟的冷却会让全进程所有 Tushare
    调用（行情刷新、汇率、历史同步）一起阻塞。
    """
    until = _tushare_cooldown_until.get(api_name)
    if until is None:
        return 0.0
    return max(0.0, until - time.monotonic())


def wait_for_tushare_rate_limit(api_name: str):
    """全局闸 + per-API 闸在同一个锁临界区内合并计算，只 sleep 一次，
    并在真正放行时同时更新两个时间戳。

    分两段等待会留竞态：线程 A 先记全局时间戳、再进 per-API 长等待期间，
    线程 B 会看到"全局间隔已过"而与 A 几乎同时放行不同 API，跨接口突刺
    依旧。锁贯穿 sleep 即天然串行化所有调用。"""
    global_interval = settings.tushare_global_min_interval_seconds
    api_interval = get_tushare_min_interval(api_name)
    if global_interval <= 0 and api_interval <= 0:
        return

    with _tushare_rate_lock:
        now = time.monotonic()
        release_at = now
        last_global = _tushare_last_call_by_api.get(_TUSHARE_GLOBAL_KEY)
        if global_interval > 0 and last_global is not None:
            release_at = max(release_at, last_global + global_interval)
        last_api = _tushare_last_call_by_api.get(api_name)
        if api_interval > 0 and last_api is not None:
            release_at = max(release_at, last_api + api_interval)

        sleep_seconds = release_at - now
        if sleep_seconds > 0:
            if sleep_seconds > 1:
                logger.info(
                    "Tushare %s 限速等待 %.1fs，避免触发接口频率限制",
                    api_name,
                    sleep_seconds,
                )
            time.sleep(sleep_seconds)

        stamp = time.monotonic()
        _tushare_last_call_by_api[_TUSHARE_GLOBAL_KEY] = stamp
        _tushare_last_call_by_api[api_name] = stamp


def get_tushare_pro():
    """Lazy-load Tushare Pro client using TUSHARE_TOKEN."""
    global _tushare_pro

    if _tushare_pro is None:
        with _tushare_lock:
            if _tushare_pro is None:
                token = (os.environ.get("TUSHARE_TOKEN") or settings.tushare_token).strip()
                if not token:
                    raise RuntimeError("未设置 TUSHARE_TOKEN 环境变量，请提供 Tushare API key")

                try:
                    ts = import_module("tushare")
                except ModuleNotFoundError as exc:
                    raise RuntimeError("未安装 tushare，请先安装 backend 依赖") from exc

                ts.set_token(token)
                _tushare_pro = ts.pro_api()
                configure_tushare_client_endpoint(_tushare_pro)

    return _tushare_pro


def get_tushare_api_base_url() -> str:
    """Return the Tushare HTTPS data API base URL used by the SDK client."""
    return (os.environ.get("TUSHARE_API_BASE_URL") or DEFAULT_TUSHARE_API_BASE_URL).rstrip("/")


def configure_tushare_client_endpoint(pro_client) -> None:
    """Patch Tushare SDK clients away from the legacy HTTP endpoint."""
    endpoint = get_tushare_api_base_url()
    private_url_attr = "_DataApi__http_url"
    if hasattr(pro_client, private_url_attr):
        setattr(pro_client, private_url_attr, endpoint)
    else:
        logger.warning("Tushare client endpoint patch skipped: unsupported SDK client shape")


def tushare_query(api_name: str, **kwargs):
    """Call a Tushare Pro API with the service's existing retry behavior.

    这里是唯一同时持有 api_name 与原始错误文案的位置，因此接口级频率错误的
    冷却记录挂在这一层（retry_with_backoff 对这些文案是快速失败，不会退避）。
    """

    def fetch():
        wait_for_tushare_rate_limit(api_name)
        pro = get_tushare_pro()
        data = getattr(pro, api_name)(**kwargs)
        if data is None or data.empty:
            raise ValueError(f"tushare {api_name} 返回空数据")
        return data

    try:
        result = retry_with_backoff(fetch, max_retries=3, initial_delay=0.5, max_delay=3.0)
    except Exception as exc:
        if classify_tushare_error(exc) == "rate":
            note_tushare_rate_error(api_name, exc)
        raise
    clear_tushare_cooldown(api_name)
    return result


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


def parse_tencent_quote_price(text: str, quote_code: str) -> Decimal:
    marker = f'v_{quote_code}="'
    start = text.find(marker)
    if start < 0:
        raise ValueError(f"腾讯行情未返回 {quote_code}")
    start += len(marker)
    end = text.find('";', start)
    if end < 0:
        raise ValueError(f"腾讯行情响应格式异常: {quote_code}")

    fields = text[start:end].split("~")
    if len(fields) < 4:
        raise ValueError(f"腾讯行情字段不足: {quote_code}")
    name = fields[1] if len(fields) > 1 else quote_code
    price = positive_decimal_price(fields[3])
    logger.info("✓ 腾讯行情 %s %s 成功: %s", quote_code, name, price)
    return price


def fetch_tencent_stock_price(symbol: str, market: Market) -> PriceResult:
    quote_code = to_tencent_quote_code(symbol, market)
    try:
        session = get_session()
        response = session.get(
            TENCENT_QUOTE_URL.format(codes=quote_code),
            headers={"Referer": "https://gu.qq.com/", "User-Agent": random.choice(USER_AGENTS)},
            timeout=(3, 8),
        )
        response.raise_for_status()
        price = parse_tencent_quote_price(response.text, quote_code)
        return price_result(price=price, source="tencent-quote", success=True)
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


def retry_with_backoff(func, max_retries=3, initial_delay=1.0, max_delay=10.0):
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
            # （限流窗口内连打 3 次），必须立刻失败。
            error_str = str(e).lower()
            non_retryable = [
                "not found", "不存在", "invalid symbol", "无效",
                "每分钟最多访问", "权限", "积分不足", "抱歉，您",
            ]
            if any(x in error_str for x in non_retryable):
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
        return price_result(price=price, source="tushare-rt_k", success=True)
    except Exception as e:
        logger.warning(f"  Tushare rt_k失败，尝试daily最新收盘价: {str(e)[:120]}")

    try:
        df = tushare_query("daily", ts_code=ts_code)
        row = df.sort_values("trade_date").iloc[-1]
        price = positive_decimal_price(row.get("close"))
        logger.info(f"✓ A股 {symbol} Tushare daily成功: {price}")
        return price_result(price=price, source="tushare-daily", success=True)
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
        return price_result(price=price, source="tushare-hk_mins", success=True)
    except Exception as e:
        logger.warning(f"  Tushare hk_mins失败，尝试hk_daily最新收盘价: {str(e)[:120]}")

    try:
        df = tushare_query("hk_daily", ts_code=ts_code)
        row = df.sort_values("trade_date").iloc[-1]
        price = positive_decimal_price(row.get("close"))
        logger.info(f"✓ 港股 {symbol} Tushare hk_daily成功: {price}")
        return price_result(price=price, source="tushare-hk_daily", success=True)

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
        return price_result(price=price, source="tushare-us_daily", success=True)

    except Exception as e:
        error_msg = f"美股 {symbol} Tushare获取失败: {str(e)[:200]}"
        logger.error(error_msg)
        return price_result(price=None, source="tushare-us_daily", success=False, error=error_msg)


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
        return price_result(price=price, source="tushare-coin_bar", success=True)

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
    else:
        return fetch_global_price_tushare(symbol, market_enum)


def update_all_holdings_prices(db: Session, user_id: int = None) -> Dict[str, Any]:
    """
    Batch update all holdings prices with protection mechanisms

    Args:
        db: Database session
        user_id: Optional user ID to filter holdings (if None, updates all holdings)

    Features:
    1. Frequency protection (10 minutes cooldown)
    2. Individual failure handling
    3. Adaptive sleep to prevent blocking
    4. Detailed success/failure reporting
    5. Continue past individual provider failures
    6. **MINIMAL DELAYS** for faster completion

    Optimizations:
    - Minimal delays (0.1-0.3s success, 0.5-1s failure)
    - Circuit breaker for protection
    - Detailed logging for debugging
    """
    from ..models.holding import Holding

    query = db.query(Holding)
    if user_id is not None:
        query = query.filter(Holding.user_id == user_id)

    holdings = query.all()
    success_list = []
    failed_list = []
    skipped_list = []
    pending_holdings = []

    now = datetime.now(timezone.utc)
    freshness_window = settings.price_refresh_freshness_seconds

    logger.info("======== 开始批量刷新股价 ========")
    logger.info(f"总持仓数: {len(holdings)}")
    logger.info(f"开始时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")

    for holding in holdings:
        if holding.price_updated_at:
            last_updated = holding.price_updated_at
            if last_updated.tzinfo is None:
                last_updated = last_updated.replace(tzinfo=timezone.utc)
            elapsed = (now - last_updated).total_seconds()
            # 负 elapsed = 时间戳在未来（历史 naive 本地时间写入造成的脏数据）
            # ——必须视为过期立即刷新，让首次刷新用 aware UTC 覆盖自愈。
            if 0 <= elapsed < freshness_window:
                logger.info(f"  ⏭️  跳过: 最近{int(elapsed)}秒前已更新")
                skipped_list.append(
                    {"symbol": holding.symbol, "reason": f"最近{int(elapsed)}秒前已更新"}
                )
                continue

        pending_holdings.append(
            {
                "id": holding.id,
                "symbol": holding.symbol,
                "market": holding.market,
            }
        )

    def fetch_pending_price(item: Dict[str, Any]) -> Dict[str, Any]:
        start_time = time.time()
        try:
            result = fetch_stock_price(item["symbol"], item["market"])
            return {
                **item,
                "result": result,
                "elapsed_time": time.time() - start_time,
                "error": None,
            }
        except Exception as exc:
            return {
                **item,
                "result": None,
                "elapsed_time": time.time() - start_time,
                "error": f"未预期的错误: {str(exc)}",
            }

    max_workers = max(1, min(settings.price_refresh_max_workers, len(pending_holdings) or 1))
    logger.info(f"待刷新持仓数: {len(pending_holdings)}, 并发数: {max_workers}")

    fetched_results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_by_item = {
            executor.submit(fetch_pending_price, item): item for item in pending_holdings
        }
        for index, future in enumerate(as_completed(future_by_item), start=1):
            item = future_by_item[future]
            fetched = future.result()
            fetched_results.append(fetched)

            result = fetched["result"]
            if result and result.get("success"):
                logger.info(
                    f"[{index}/{len(pending_holdings)}] {item['symbol']} 成功: "
                    f"{result.get('price')} ({result.get('source')}) - 耗时: "
                    f"{fetched['elapsed_time']:.2f}s"
                )
            else:
                error = fetched["error"] or (result or {}).get("error") or "未知错误"
                logger.warning(
                    f"[{index}/{len(pending_holdings)}] {item['symbol']} 失败: {error[:100]}"
                )

    holding_by_id = {holding.id: holding for holding in holdings}
    for fetched in fetched_results:
        holding = holding_by_id[fetched["id"]]
        result = fetched["result"]

        if (
            result
            and result["success"]
            and result["price"]
            and result["price"].is_finite()
            and result["price"] > 0
        ):
            holding.current_price = result["price"]
            holding.price_updated_at = result["timestamp"]
            success_list.append(
                {
                    "symbol": holding.symbol,
                    "market": holding.market,
                    "price": float(result["price"]),
                    "source": result["source"],
                }
            )
        else:
            failed_list.append(
                {
                    "symbol": holding.symbol,
                    "market": holding.market,
                    "error": fetched["error"] or (result or {}).get("error") or "未知错误",
                }
            )

    # Commit all successful updates
    try:
        db.commit()
        end_time = datetime.now(timezone.utc)
        total_time = (end_time - now).total_seconds()

        logger.info("\n======== 批量刷新完成 ========")
        logger.info(f"总耗时: {total_time:.2f}s")
        logger.info(
            f"成功: {len(success_list)}, 失败: {len(failed_list)}, 跳过: {len(skipped_list)}"
        )

        if len(success_list) > 0:
            avg_time = total_time / len(success_list)
            logger.info(f"平均每只耗时: {avg_time:.2f}s")

    except Exception as e:
        db.rollback()
        logger.error(f"❌ 数据库提交失败: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": f"数据库提交失败: {str(e)}",
            "success_count": 0,
            "failed_count": len(holdings),
            "skipped_count": 0,
        }

    return {
        "success": True,
        "success_count": len(success_list),
        "failed_count": len(failed_list),
        "skipped_count": len(skipped_list),
        "success_list": success_list,
        "failed_list": failed_list,
        "skipped_list": skipped_list,
    }
