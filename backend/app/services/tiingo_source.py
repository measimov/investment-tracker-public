"""Tiingo 数据源（api.tiingo.com，免费档）薄 wrapper —— 美股报价与日线。

位置：美股报价链 Tushare us_daily → **Tiingo** → 雪球（Cookie 约 15 天过期，降为最后兜底）；
美股日线链 Tushare us_daily_adj（当前积分档无权限）→ **Tiingo EOD** → 腾讯 K 线（不复权）。

对外契约与其他源一致：报价返回 `PriceResult`（绝不抛），日线返回规整后的 bar 字典列表
（由 market_data_service 转成 SecurityPrice 落库）。Tiingo ticker 只在本模块内部出现，
边界一律经 `to_tiingo_ticker` 转换（`BRK.B` → `BRK-B`），不落库为主键。

四条约束：

1. **未配置显式降级**：`TIINGO_API_TOKEN` 为空时所有外呼入口抛 `TiingoNotConfigured`
   （报价返回 success=False），绝不返回空列表冒充成功——那会让「没配 Token」与
   「该区间无交易日」在下游长得一模一样。
2. **进程级限速**：免费档约 50 次/小时、1000 次/天。批量刷新在线程池里跑，所有请求经
   `_throttle` 串行化到 `tiingo_min_interval_seconds`；撞 429 后进程内冷却
   `RATE_LIMIT_COOLDOWN_SECONDS` 不再外呼（冷却中直接抛 `TiingoRateLimited`），
   否则一次批量刷新会把剩余标的逐个打成 429 并继续消耗额度。
3. **解析与 IO 分离**：`parse_eod_bars` / `parse_iex_quote` / `interpret_error` 是纯函数，
   金样在 `tests/fixtures/tiingo/`（按官方文档形状构造）。
4. **日期口径**：EOD 的 `date` 是交易所交易日的 UTC 零点（`2026-09-25T00:00:00.000Z`），
   直接取前 10 位，**不能**再按纽约时区换算（会退一天）；IEX 的 `timestamp` 是真实时刻，
   按纽约时区换算成交易日。
"""

import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

import requests

from ..config import settings
from ..core.logging import get_app_logger
from .http_source import reset_throttle, throttle
from .market_sessions import market_timezone
from .stock_price_service import PriceResult, positive_decimal_price, price_result

logger = get_app_logger(__name__)

BASE_URL = "https://api.tiingo.com"
EOD_SOURCE = "tiingo-eod"
IEX_SOURCE = "tiingo-iex"
QUOTE_SOURCE = "tiingo"

NOT_CONFIGURED_MESSAGE = "未配置 TIINGO_API_TOKEN（Tiingo 美股行情未启用）"
# 429 后的进程内冷却：免费档额度按小时/天计，短时间内重试只会继续消耗并继续 429
RATE_LIMIT_COOLDOWN_SECONDS = 600
# IEX 报价的新鲜度上限：覆盖周末 + 一个休市日（周五收盘到周二亚洲早盘约 3.5 天）。
# 更旧的 IEX 报价（冷门标的长期无 IEX 成交）改取最新日线收盘
IEX_MAX_AGE = timedelta(days=4)

_NY_TZ = market_timezone("美股")
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]*$")
_FRACTION_RE = re.compile(r"\.(\d{6})\d+")

THROTTLE_KEY = "tiingo"
_cooldown_until = 0.0


class TiingoError(RuntimeError):
    """Tiingo 数据源不可用或返回错误。"""


class TiingoNotConfigured(TiingoError):
    """未配置 TIINGO_API_TOKEN。"""


class TiingoNotFound(TiingoError):
    """Tiingo 不认识该 ticker（HTTP 404）。"""


class TiingoAuthError(TiingoError):
    """Token 无效或被拒（HTTP 401/403）。"""


class TiingoRateLimited(TiingoError):
    """撞免费档额度（HTTP 429）或处于冷却期。"""


def is_configured() -> bool:
    return bool((settings.tiingo_api_token or "").strip())


def reset_state() -> None:
    """清空限速时钟与 429 冷却（测试与轮换 Token 后用）。"""
    global _cooldown_until
    reset_throttle(THROTTLE_KEY)
    _cooldown_until = 0.0


def to_tiingo_ticker(symbol: str) -> str:
    """本项目美股代码 -> Tiingo ticker：大写，股份类别分隔符 `.` / `/` 改成 `-`。"""
    text = str(symbol or "").strip().upper().replace(".", "-").replace("/", "-")
    if not _TICKER_RE.match(text):
        raise TiingoError(f"无法识别的美股代码: {symbol!r}")
    return text


# ---------------------------------------------------------------------------
# 纯函数：错误映射与解析
# ---------------------------------------------------------------------------


def _detail_text(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("detail") or payload.get("message") or payload.get("error") or "")
    if isinstance(payload, str):
        return payload
    return ""


def interpret_error(status_code: int, payload: Any, ticker: str = "") -> Optional[TiingoError]:
    """HTTP 状态 + 响应体 -> 对应异常；正常响应返回 None。

    Tiingo 的错误体是 `{"detail": "..."}`。除 4xx/5xx 外也见过 HTTP 200 带 detail 的
    错误对象（额度用尽时），因此状态码正常但响应是只有 detail 的对象也按错误处理。
    """
    detail = _detail_text(payload)[:200]
    lowered = detail.lower()
    label = f"Tiingo {ticker}".strip()
    if status_code == 404:
        return TiingoNotFound(
            f"{label} 未找到该代码（HTTP 404）{('：' + detail) if detail else ''}"
        )
    if status_code in (401, 403):
        return TiingoAuthError(
            f"Tiingo 拒绝访问（HTTP {status_code}）：TIINGO_API_TOKEN 无效或已失效"
            f"{('；' + detail) if detail else ''}"
        )
    if status_code == 429:
        return TiingoRateLimited(
            f"Tiingo 请求过于频繁（HTTP 429，免费档额度约 50 次/小时、1000 次/天）"
            f"{('；' + detail) if detail else ''}"
        )
    if status_code >= 400:
        return TiingoError(
            f"{label} 请求失败（HTTP {status_code}）{('：' + detail) if detail else ''}"
        )
    if isinstance(payload, dict) and detail and not payload.get("ticker"):
        if "allocation" in lowered or "rate limit" in lowered or "too many" in lowered:
            return TiingoRateLimited(f"Tiingo 额度已用尽：{detail}")
        if "not found" in lowered:
            return TiingoNotFound(f"{label} 未找到该代码：{detail}")
        if "token" in lowered or "authoriz" in lowered or "authentic" in lowered:
            return TiingoAuthError(f"Tiingo 拒绝访问：TIINGO_API_TOKEN 无效或已失效；{detail}")
        return TiingoError(f"{label} 返回错误：{detail}")
    return None


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _eod_date(value: Any) -> Optional[date]:
    """EOD `date`（交易日的 UTC 零点）-> 交易日：只取前 10 位，不做时区换算。"""
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def parse_eod_bars(payload: Any) -> List[Dict[str, Any]]:
    """`/tiingo/daily/{ticker}/prices` 响应 -> 按日期升序的 bar 列表。

    每个 bar：`date`(date) / `open` / `high` / `low` / `close` / `adj_close` / `volume`
    （Decimal 或 None）。收盘价缺失或非正、日期无法解析的行丢弃；同日重复保留最后一条。
    响应不是列表即抛 TiingoError（结构无法识别，绝不当空结果）。
    """
    if not isinstance(payload, list):
        raise TiingoError("Tiingo 日线响应结构无法识别（应为数组）")
    bars: Dict[date, Dict[str, Any]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        bar_date = _eod_date(item.get("date"))
        close = _decimal(item.get("close"))
        if bar_date is None or close is None or close <= 0:
            continue
        bars[bar_date] = {
            "date": bar_date,
            "open": _decimal(item.get("open")),
            "high": _decimal(item.get("high")),
            "low": _decimal(item.get("low")),
            "close": close,
            "adj_close": _decimal(item.get("adjClose")),
            "volume": _decimal(item.get("volume")),
        }
    return [bars[key] for key in sorted(bars)]


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """IEX 时间戳 -> aware datetime。Tiingo 会给出纳秒小数（`.186520297-05:00`），
    `fromisoformat` 只认到微秒，先截断；无时区的按 UTC。"""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    text = _FRACTION_RE.sub(lambda match: "." + match.group(1), text)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def parse_iex_quote(payload: Any) -> Optional[Dict[str, Any]]:
    """`/iex/{ticker}` 响应 -> {price, timestamp, as_of, prev_close}；没有可用价格返回 None。

    价格优先 `tngoLast`（Tiingo 综合 last/mid 算出的最新价，盘后为官方收盘），缺失再用
    IEX 自身的 `last`（配 `lastSaleTimestamp`）。未知 ticker 时 Tiingo 返回空数组。
    """
    if isinstance(payload, dict):
        items = [payload]
    elif isinstance(payload, list):
        items = payload
    else:
        raise TiingoError("Tiingo IEX 响应结构无法识别")
    for item in items:
        if not isinstance(item, dict):
            continue
        candidates = (
            (item.get("tngoLast"), item.get("timestamp")),
            (item.get("last"), item.get("lastSaleTimestamp") or item.get("timestamp")),
        )
        for raw_price, raw_timestamp in candidates:
            price = _decimal(raw_price)
            if price is None or price <= 0:
                continue
            moment = _parse_timestamp(raw_timestamp)
            prev_close = _decimal(item.get("prevClose"))
            return {
                "price": price,
                "timestamp": moment,
                "as_of": moment.astimezone(_NY_TZ).date() if moment else None,
                # 昨收只用于异动提醒；缺失/非正数为 None
                "prev_close": prev_close if prev_close is not None and prev_close > 0 else None,
            }
    return None


def iex_quote_is_fresh(quote: Dict[str, Any], now: datetime) -> bool:
    """报价时刻在 `IEX_MAX_AGE` 之内才算新鲜；没有时间戳一律不新鲜（不拿抓取时刻冒充）。"""
    moment = quote.get("timestamp")
    if moment is None:
        return False
    return now - moment <= IEX_MAX_AGE


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------


def _check_cooldown() -> None:
    remaining = _cooldown_until - time.monotonic()
    if remaining > 0:
        raise TiingoRateLimited(f"Tiingo 额度冷却中（约 {int(remaining) + 1} 秒后恢复）")


def _note_rate_limited() -> None:
    # 不取节流锁：排队线程可能正拿着锁 sleep，冷却标记要立刻对它们可见（单个 float
    # 赋值在 CPython 下是原子的）；排队者在等待结束、发请求前会再查一次
    global _cooldown_until
    _cooldown_until = time.monotonic() + RATE_LIMIT_COOLDOWN_SECONDS


def _throttle() -> None:
    """占一个请求名额：冷却检查与节流在同一把锁里，**等待结束后再查一次冷却**。

    批量刷新多线程时，其他线程在锁外通过了冷却检查、在锁上排队；其中一个请求收到 429
    设置冷却后，只在入口查一次会让已排队的请求逐个照发（PR #246 评审 P2）。
    """
    interval = max(float(settings.tiingo_min_interval_seconds or 0), 0.0)
    throttle(THROTTLE_KEY, interval, check=_check_cooldown)


def _request_json(path: str, *, ticker: str, params: Optional[Dict[str, str]] = None) -> Any:
    """一次带鉴权的 GET；任何失败都抛 TiingoError 家族（Token 不会出现在消息里——
    它只在请求头，不进 URL）。"""
    token = (settings.tiingo_api_token or "").strip()
    if not token:
        raise TiingoNotConfigured(NOT_CONFIGURED_MESSAGE)
    _throttle()  # 含冷却检查（等待前后各一次）
    timeout = settings.tiingo_timeout_seconds
    try:
        response = requests.get(
            f"{BASE_URL}{path}",
            params=params,
            headers={"Authorization": f"Token {token}", "Content-Type": "application/json"},
            timeout=timeout,
        )
    except requests.Timeout as exc:
        raise TiingoError(f"Tiingo {ticker} 请求超时（{timeout}s）") from exc
    except requests.RequestException as exc:
        raise TiingoError(f"Tiingo {ticker} 网络错误: {str(exc)[:160]}") from exc

    try:
        payload = response.json()
    except ValueError:
        payload = (response.text or "")[:200] if response.status_code >= 400 else None
        if payload is None:
            raise TiingoError(f"Tiingo {ticker} 返回的不是 JSON（HTTP {response.status_code}）")
    error = interpret_error(response.status_code, payload, ticker)
    if error is not None:
        if isinstance(error, TiingoRateLimited):
            _note_rate_limited()
        raise error
    return payload


def fetch_eod_history(symbol: str, start_date: date, end_date: date) -> List[Dict[str, Any]]:
    """区间日线（不复权 close + 复权 adjClose）；空列表 = 该区间确实无交易日。"""
    ticker = to_tiingo_ticker(symbol)
    payload = _request_json(
        f"/tiingo/daily/{ticker}/prices",
        ticker=ticker,
        params={"startDate": start_date.isoformat(), "endDate": end_date.isoformat()},
    )
    return parse_eod_bars(payload)


def fetch_latest_eod(symbol: str) -> Optional[Dict[str, Any]]:
    """最新一根日线（不带日期参数时 Tiingo 只返回最后一根）。"""
    ticker = to_tiingo_ticker(symbol)
    bars = parse_eod_bars(_request_json(f"/tiingo/daily/{ticker}/prices", ticker=ticker))
    return bars[-1] if bars else None


def fetch_iex_quote(symbol: str) -> Optional[Dict[str, Any]]:
    ticker = to_tiingo_ticker(symbol)
    return parse_iex_quote(_request_json(f"/iex/{ticker}", ticker=ticker))


def fetch_tiingo_stock_price(symbol: str, *, now: Optional[datetime] = None) -> PriceResult:
    """美股报价 -> PriceResult：IEX 最新价（新鲜时）→ 最新日线收盘。绝不抛。

    Token 无效 / 撞额度时不再追加 EOD 请求（同一原因必然同样失败，只会多耗额度）；
    IEX 无报价、报价陈旧或 IEX 侧出错才退到 EOD。
    """
    if not is_configured():
        return price_result(
            price=None, source=QUOTE_SOURCE, success=False, error=NOT_CONFIGURED_MESSAGE
        )
    now = now or datetime.now(timezone.utc)
    iex_note = ""
    try:
        quote = fetch_iex_quote(symbol)
        if quote is not None and iex_quote_is_fresh(quote, now):
            price = positive_decimal_price(quote["price"])
            logger.info("✓ Tiingo IEX 美股 %s 成功: %s", symbol, price)
            return price_result(
                price=price,
                source=IEX_SOURCE,
                success=True,
                as_of=quote["as_of"],
                prev_close=quote.get("prev_close"),
            )
        iex_note = "IEX 无报价" if quote is None else "IEX 报价陈旧"
    except (TiingoAuthError, TiingoRateLimited, TiingoNotConfigured) as exc:
        error_msg = f"Tiingo 行情获取失败: {exc}"
        logger.warning("  美股 %s %s", symbol, error_msg)
        return price_result(price=None, source=QUOTE_SOURCE, success=False, error=error_msg)
    except Exception as exc:  # noqa: BLE001 - IEX 出错仍可退到 EOD
        iex_note = f"IEX 失败: {str(exc)[:120]}"

    try:
        bar = fetch_latest_eod(symbol)
        if bar is None:
            raise TiingoError("Tiingo 未返回日线")
        price = positive_decimal_price(bar["close"])
        logger.info("✓ Tiingo EOD 美股 %s 成功: %s（%s）", symbol, price, iex_note)
        return price_result(price=price, source=EOD_SOURCE, success=True, as_of=bar["date"])
    except Exception as exc:  # noqa: BLE001 - 报价入口绝不抛
        error_msg = f"Tiingo 行情获取失败: {iex_note}; EOD: {str(exc)[:160]}"
        logger.warning("  美股 %s %s", symbol, error_msg)
        return price_result(price=None, source=QUOTE_SOURCE, success=False, error=error_msg)
