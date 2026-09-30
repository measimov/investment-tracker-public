import html
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..core.logging import get_app_logger
from ..core.timeutil import local_today
from ..models.security_price import SecurityPrice
from . import tiingo_source
from .http_source import BROWSER_USER_AGENT
from .stock_price_service import (
    TENCENT_HEADERS,
    classify_tushare_error,
    get_exchange_type,
    get_tushare_pro,
    retry_with_backoff,
    throttle_tencent,
    to_tushare_a_code,
    to_tushare_hk_code,
    tushare_query_once,
    wait_for_tushare_rate_limit,
)


logger = get_app_logger(__name__)
YAHOO_CHART_URLS = (
    "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
)
YAHOO_USER_AGENT = BROWSER_USER_AGENT
STOCKANALYSIS_HISTORY_URL = "https://stockanalysis.com/quote/sgx/{symbol}/history/"
SHORT_NO_DATA_RANGE_DAYS = 7
# 腾讯历史 K 线：Tushare 覆盖空洞的兜底源（实测 daily 不含沪深 B 股与可转债、
# hk_daily 不含港股 ETF/REIT，而腾讯 K 线全部覆盖）。返回不复权日线，
# 与 close_price 的存储口径一致（复权价另存 adj_close_price，本源不提供）。
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_KLINE_MAX_CANDLES = 640


def _parse_tushare_date(value: Any) -> date:
    return datetime.strptime(str(value), "%Y%m%d").date()


def _date_to_tushare(value: date) -> str:
    return value.strftime("%Y%m%d")


def _tushare_history_query(api_name: str, **kwargs):
    """Call Tushare history APIs without treating empty data as an exception.

    空表是合法结果（停牌/无交易日）；上游故障（HTTP ≥400、非 JSON）由严格客户端抛
    TushareUpstreamError——此前 SDK 把 5xx 吞成空表，尾部同步会把失败日当成停牌标为已处理。"""

    def fetch():
        wait_for_tushare_rate_limit(api_name)
        pro = get_tushare_pro()
        return getattr(pro, api_name)(**kwargs)

    return retry_with_backoff(
        fetch,
        max_retries=3,
        initial_delay=0.5,
        max_delay=3.0,
        non_retryable=lambda exc: classify_tushare_error(exc) != "other",
    )


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        if hasattr(value, "isna") and value.isna():
            return None
    except TypeError:
        pass
    text = str(value)
    if text in {"", "nan", "None", "NaT"}:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def is_a_share_fund_symbol(symbol: str) -> bool:
    normalized = str(symbol or "").strip()
    return normalized.startswith(("15", "16", "18", "5"))


def infer_price_currency(market: str, fallback: Optional[str] = None) -> str:
    if fallback:
        return fallback
    if market == "港股":
        return "HKD"
    if market == "美股":
        return "USD"
    if market == "新加坡股":
        return "SGD"
    return "CNY"


# 有确定报价币种规则的市场：交易币种不能当行情币种用——招商港股通按 CNY 记账、IBKR 可能按
# USD 入账，拿它写港股行情的 currency 就错了
_MARKET_QUOTE_CURRENCY = {"A股": "CNY", "港股": "HKD", "美股": "USD"}


def resolve_price_currency(
    db: Session, symbol: str, market: str, fallback: Optional[str] = None
) -> str:
    """security_prices.currency 的唯一解析（#276）。

    此前各写入方各自推断（B 股一律 CNY、港股一律 HKD，尾部同步不传币种），同一行的币种随
    写入顺序来回翻转：B 股美元/港元价被标成 CNY，人民币柜台被回填成 HKD，下游格雷厄姆估值
    与港股复权的换汇方向跟着错。优先级：标的全集的币种（港交所日报官方 CUR 会回填进去）→
    港股：该标的最近一行港交所日报行情的币种（标的全集缺这只时的官方来源，PR #299 评审：
    否则 9xxxx 美元柜台会被日报写 USD、history-sync 改回 HKD 来回翻转）→
    按代码推断（沪 B 美元、深 B 港元、港股 8xxxx 人民币柜台）→ 市场的报价币种 → 调用方给的
    交易币种（只用于没有确定规则的市场，如新加坡股、场外基金）→ 市场默认。"""
    from ..models.security_catalog import SecurityCatalogEntry
    from .hkex_dayquot_source import SOURCE as HKEX_DAYQUOT_SOURCE
    from .security_catalog_service import infer_b_share_currency, infer_hk_currency

    catalog = (
        db.query(SecurityCatalogEntry.currency)
        .filter(SecurityCatalogEntry.symbol == symbol, SecurityCatalogEntry.market == market)
        .scalar()
    )
    if catalog:
        return catalog
    if market == "港股":
        official = (
            db.query(SecurityPrice.currency)
            .filter(
                SecurityPrice.symbol == symbol,
                SecurityPrice.market == market,
                SecurityPrice.source == HKEX_DAYQUOT_SOURCE,
            )
            .order_by(SecurityPrice.price_date.desc())
            .limit(1)
            .scalar()
        )
        if official:
            return official
    if market == "B股":
        inferred = infer_b_share_currency(symbol)
        if inferred:
            return inferred
    if market == "港股":
        inferred, _source = infer_hk_currency(symbol)
        if inferred:
            return inferred
    if market in _MARKET_QUOTE_CURRENCY:
        return _MARKET_QUOTE_CURRENCY[market]
    return infer_price_currency(market, fallback)


def resolve_tushare_history_api(symbol: str, market: str) -> Optional[Dict[str, str]]:
    if market == "指数":
        # 基准指数（benchmark_service 目录）：index_daily / index_global，
        # ts_code 即目录 code，无复权概念
        from .benchmark_service import resolve_benchmark_history_api

        return resolve_benchmark_history_api(symbol)

    if market in {"A股", "B股"}:
        ts_code = to_tushare_a_code(symbol)
        if is_a_share_fund_symbol(symbol):
            return {"api": "fund_daily", "adjust_api": "fund_adj", "ts_code": ts_code}
        return {"api": "daily", "adjust_api": "adj_factor", "ts_code": ts_code}

    if market == "港股":
        return {"api": "hk_daily", "adjust_api": "", "ts_code": to_tushare_hk_code(symbol)}

    if market == "美股":
        return {"api": "us_daily_adj", "adjust_api": "", "ts_code": str(symbol or "").upper()}

    return None


def to_yahoo_symbol(symbol: str, market: str) -> Optional[str]:
    text = str(symbol or "").strip().upper()
    if not text:
        return None

    if market == "新加坡股":
        return text if text.endswith(".SI") else f"{text}.SI"

    return None


def to_tencent_kline_code(symbol: str, market: str) -> Optional[str]:
    """映射到腾讯 K 线代码：沪深（含 B 股/可转债）sh/sz 前缀，港股 hk+5 位。"""
    text = str(symbol or "").strip().upper()
    if not text:
        return None
    if market in {"A股", "B股"}:
        exchange = get_exchange_type(text)
        if exchange not in {"sh", "sz"}:
            return None
        return f"{exchange}{text}"
    if market == "港股" and text.isdigit():
        return f"hk{text.zfill(5)}"
    return None


# 美股腾讯 K 线代码要带交易所后缀（usPDD.OQ / usBABA.N），标的本身不带交易所信息：按常见
# 顺序探测一次、进程内缓存。无后缀的 usPDD 只返回最新 1 根，不能用作历史源
_TENCENT_US_SUFFIXES = (".OQ", ".N", ".AM")
_tencent_us_code_cache: Dict[str, Optional[str]] = {}


class TencentProbeInconclusive(RuntimeError):
    """腾讯美股代码探测本次没能下结论（网络错误 / 异常应答）——不同于「确定没有这个代码」。"""


def resolve_tencent_us_kline_code(symbol: str) -> Optional[str]:
    """美股 → 腾讯 K 线代码；探测不到（含本次没下结论）返回 None。

    需要区分「确定没有」与「本次没下结论」的调用方用 probe_tencent_us_kline_code。"""
    try:
        return probe_tencent_us_kline_code(symbol)
    except TencentProbeInconclusive:
        return None


def probe_tencent_us_kline_code(symbol: str) -> Optional[str]:
    """美股 → 腾讯 K 线代码（带交易所后缀）；确定探测不到返回 None，
    本次没下结论抛 TencentProbeInconclusive（PR #298 评审：两者不能共用 None）。

    只缓存确定结论（找到，或三个后缀都正常应答却无数据）；网络错误与异常应答不缓存。

    Tushare 的美股日线在当前积分档是试用限频（us_daily 每分钟/每小时 1 次）、复权日线
    us_daily_adj 无权限，美股历史只能靠兜底源——此前美股没有任何兜底，同步必失败。"""
    text = str(symbol or "").strip().upper()
    if not text or not text.replace(".", "").isalnum():
        return None
    if text in _tencent_us_code_cache:
        return _tencent_us_code_cache[text]
    probe_end = local_today()
    probe_start = probe_end - timedelta(days=14)
    found: Optional[str] = None
    for suffix in _TENCENT_US_SUFFIXES:
        code = f"us{text}{suffix}"
        try:
            throttle_tencent()
            response = requests.get(
                TENCENT_KLINE_URL,
                params={
                    "param": f"{code},day,{probe_start.isoformat()},{probe_end.isoformat()},20,"
                },
                headers=TENCENT_HEADERS,
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 — 探测失败不缓存，下次再试
            logger.warning("腾讯美股代码探测 %s 失败: %s", code, exc)
            raise TencentProbeInconclusive(f"腾讯美股代码探测 {code} 失败: {exc}") from exc
        # 只有正常的成功外层（code=0、data 为对象）才能下「这个后缀没有数据」的结论；
        # HTTP 200 的错误对象/限流页此前会让三个后缀都「未找到」并在进程内永久缓存成 None（#275）
        if (
            not isinstance(payload, dict)
            or str(payload.get("code")) != "0"
            or not isinstance(payload.get("data"), dict)
        ):
            logger.warning("腾讯美股代码探测 %s 返回异常结构，本次不下结论", code)
            raise TencentProbeInconclusive(f"腾讯美股代码探测 {code} 返回异常结构")
        node = payload["data"].get(code) or {}
        if isinstance(node, dict) and (node.get("day") or node.get("qfqday")):
            found = code
            break
    _tencent_us_code_cache[text] = found
    return found


def _fetch_tencent_kline_rows(
    kline_code: str, start_date: date, end_date: date
) -> List[Dict[str, Any]]:
    """拉取腾讯不复权日线。

    腾讯对超过单次上限的区间返回的是区间内**最近**的 640 根（实测
    2000-01-01~2026-07-28 恰好返回 640 根且首日在 2023-12），所以必须
    从 end_date 向**前**翻页：每页取回后把游标移到本页最早日期的前一天，
    直到返回不足一页或已覆盖 start_date。
    """
    rows: List[Dict[str, Any]] = []
    seen_dates: set[str] = set()
    cursor_end = end_date
    while cursor_end >= start_date:
        params = {
            "param": (
                f"{kline_code},day,{start_date.isoformat()},{cursor_end.isoformat()},"
                f"{TENCENT_KLINE_MAX_CANDLES},"
            )
        }
        throttle_tencent()
        response = requests.get(
            TENCENT_KLINE_URL, params=params, headers=TENCENT_HEADERS, timeout=20
        )
        response.raise_for_status()
        payload = response.json()
        node = (payload.get("data") or {}).get(kline_code) or {}
        candles = node.get("day") or node.get("qfqday") or []
        new_candles = [c for c in candles if c and c[0] not in seen_dates]
        if not new_candles:
            break
        for candle in new_candles:
            # [date, open, close, high, low, volume]
            seen_dates.add(candle[0])
            rows.append(
                {
                    "trade_date": candle[0].replace("-", ""),
                    "open": candle[1],
                    "close": candle[2],
                    "high": candle[3],
                    "low": candle[4],
                }
            )
        earliest = date.fromisoformat(new_candles[0][0])
        if len(candles) < TENCENT_KLINE_MAX_CANDLES or earliest <= start_date:
            break
        cursor_end = earliest - timedelta(days=1)
    rows.sort(key=lambda row: row["trade_date"])
    return rows


def _fetch_and_store_tencent_history(
    db: Session,
    *,
    symbol: str,
    market: str,
    kline_code: str,
    start_date: date,
    end_date: date,
    currency: Optional[str],
) -> Dict[str, Any]:
    candles = _fetch_tencent_kline_rows(kline_code, start_date, end_date)
    rows = _normalize_price_rows(
        ((None, row) for row in candles),
        symbol=symbol,
        market=market,
        ts_code=kline_code,
        currency=infer_price_currency(market, currency),
        source="tencent-kline",
    )
    if not rows:
        # 与其他外部源同一约定：短区间（≤7 天）无交易日算成功；
        # 长区间双空必须判失败（uncovered），否则增量同步会把数据源缺口
        # 静默计成"已覆盖"——正是本 PR 要修的那类根因。
        return _empty_external_history_result(
            symbol=symbol,
            market=market,
            source="tencent-kline",
            start_date=start_date,
            end_date=end_date,
        )
    changed = upsert_security_prices(db, rows)
    return {
        "symbol": symbol,
        "market": market,
        "success": True,
        "rows": changed,
        "source": "tencent-kline",
        **_history_coverage_payload(start_date, end_date, rows),
    }


def _normalize_tiingo_eod_bars(
    bars: List[Dict[str, Any]],
    *,
    symbol: str,
    market: str,
    ticker: str,
    currency: str,
) -> List[SecurityPrice]:
    """Tiingo EOD bar -> SecurityPrice，口径与 Yahoo 归一化一致：close_price 存不复权
    收盘，adj_close_price 存源给出的复权收盘（拆股+分红），adj_factor = adjClose / close；
    pre_close 取序列内前一根的收盘（区间首根没有前值留空）。"""
    rows: List[SecurityPrice] = []
    previous_close: Optional[Decimal] = None
    for bar in bars:
        close_price = bar["close"]
        adj_close_price = bar.get("adj_close")
        rows.append(
            SecurityPrice(
                symbol=symbol,
                market=market,
                ts_code=ticker,
                price_date=bar["date"],
                currency=currency,
                open_price=bar.get("open"),
                high_price=bar.get("high"),
                low_price=bar.get("low"),
                close_price=close_price,
                pre_close_price=previous_close,
                adj_factor=(adj_close_price / close_price)
                if adj_close_price and close_price
                else None,
                adj_close_price=adj_close_price,
                source=tiingo_source.EOD_SOURCE,
            )
        )
        previous_close = close_price
    return rows


def _fetch_and_store_tiingo_history(
    db: Session,
    *,
    symbol: str,
    market: str,
    start_date: date,
    end_date: date,
    currency: Optional[str],
) -> Dict[str, Any]:
    """Tiingo EOD 区间日线入库；外呼/解析失败抛 TiingoError 家族，由调用方决定兜底。"""
    bars = tiingo_source.fetch_eod_history(symbol, start_date, end_date)
    rows = _normalize_tiingo_eod_bars(
        [bar for bar in bars if start_date <= bar["date"] <= end_date],
        symbol=symbol,
        market=market,
        ticker=tiingo_source.to_tiingo_ticker(symbol),
        currency=infer_price_currency(market, currency),
    )
    if not rows:
        return _empty_external_history_result(
            symbol=symbol,
            market=market,
            source=tiingo_source.EOD_SOURCE,
            start_date=start_date,
            end_date=end_date,
        )
    changed = upsert_security_prices(db, rows)
    return {
        "symbol": symbol,
        "market": market,
        "success": True,
        "rows": changed,
        "source": tiingo_source.EOD_SOURCE,
        **_history_coverage_payload(start_date, end_date, rows),
    }


def _fetch_adjustment_factors(
    api_name: str, ts_code: str, start_date: date, end_date: date
) -> Dict[date, Decimal]:
    if not api_name:
        return {}

    df = _tushare_history_query(
        api_name,
        ts_code=ts_code,
        start_date=_date_to_tushare(start_date),
        end_date=_date_to_tushare(end_date),
    )
    if df is None or df.empty:
        return {}

    factors = {}
    for _, row in df.iterrows():
        factor = _decimal_or_none(row.get("adj_factor"))
        if factor is not None:
            factors[_parse_tushare_date(row.get("trade_date"))] = factor
    return factors


def _normalize_price_rows(
    rows: Iterable[Tuple[Any, Any]],
    *,
    symbol: str,
    market: str,
    ts_code: str,
    currency: str,
    source: str,
    factors: Optional[Dict[date, Decimal]] = None,
) -> List[SecurityPrice]:
    normalized = []
    factors = factors or {}

    for _, row in rows:
        price_date = _parse_tushare_date(row.get("trade_date"))
        close_price = _decimal_or_none(row.get("close"))
        if close_price is None:
            continue

        adj_factor = _decimal_or_none(row.get("adj_factor")) or factors.get(price_date)
        adj_close_price = close_price * adj_factor if adj_factor else None

        normalized.append(
            SecurityPrice(
                symbol=symbol,
                market=market,
                ts_code=ts_code,
                price_date=price_date,
                currency=currency,
                open_price=_decimal_or_none(row.get("open")),
                high_price=_decimal_or_none(row.get("high")),
                low_price=_decimal_or_none(row.get("low")),
                close_price=close_price,
                pre_close_price=_decimal_or_none(row.get("pre_close")),
                adj_factor=adj_factor,
                adj_close_price=adj_close_price,
                source=source,
            )
        )

    return normalized


def _fetch_yahoo_chart(symbol: str, start_date: date, end_date: date) -> Dict[str, Any]:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")

    def parse_response(response: requests.Response) -> Dict[str, Any]:
        response.raise_for_status()
        payload = response.json()
        chart = payload.get("chart") or {}
        error = chart.get("error")
        if error:
            raise ValueError(error.get("description") or str(error))
        results = chart.get("result") or []
        if not results:
            return {}
        return results[0]

    def request_date_range(url_template: str) -> requests.Response:
        period1 = int(
            datetime(
                start_date.year,
                start_date.month,
                start_date.day,
                tzinfo=timezone.utc,
            ).timestamp()
        )
        period2_date = end_date + timedelta(days=1)
        period2 = int(
            datetime(
                period2_date.year,
                period2_date.month,
                period2_date.day,
                tzinfo=timezone.utc,
            ).timestamp()
        )
        return requests.get(
            url_template.format(symbol=symbol),
            params={
                "period1": period1,
                "period2": period2,
                "interval": "1d",
                "events": "history",
                "includeAdjustedClose": "true",
            },
            headers={"User-Agent": YAHOO_USER_AGENT},
            timeout=(5, 20),
        )

    def fetch():
        last_error: Optional[Exception] = None
        for url_template in YAHOO_CHART_URLS:
            try:
                return parse_response(request_date_range(url_template))
            except requests.HTTPError as exc:
                last_error = exc
                status_code = exc.response.status_code if exc.response is not None else None
                if status_code != 429:
                    raise
                logger.info(
                    "Yahoo %s returned HTTP 429 for %s; trying the next chart endpoint",
                    exc.response.url if exc.response is not None else url_template,
                    symbol,
                )
        if last_error:
            raise last_error
        return {}

    return retry_with_backoff(fetch, max_retries=3, initial_delay=0.5, max_delay=3.0)


def _history_coverage_payload(
    start_date: date,
    end_date: date,
    rows: List[SecurityPrice],
) -> Dict[str, Any]:
    requested = {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }
    if not rows:
        return {
            "coverage_status": (
                "no_data"
                if (end_date - start_date).days <= SHORT_NO_DATA_RANGE_DAYS
                else "uncovered"
            ),
            "requested_coverage": requested,
            "actual_coverage": None,
        }

    actual_start = min(row.price_date for row in rows)
    actual_end = max(row.price_date for row in rows)
    status = "covered"
    if actual_start > start_date or actual_end < end_date:
        status = "partial"
    return {
        "coverage_status": status,
        "requested_coverage": requested,
        "actual_coverage": {
            "start_date": actual_start.isoformat(),
            "end_date": actual_end.isoformat(),
        },
    }


def _empty_external_history_result(
    *,
    symbol: str,
    market: str,
    source: str,
    start_date: date,
    end_date: date,
) -> Dict[str, Any]:
    coverage = _history_coverage_payload(start_date, end_date, [])
    if coverage["coverage_status"] == "no_data":
        return {
            "symbol": symbol,
            "market": market,
            "success": True,
            "rows": 0,
            "source": source,
            "message": "请求短区间内没有可用交易日数据",
            **coverage,
        }
    return {
        "symbol": symbol,
        "market": market,
        "success": False,
        "rows": 0,
        "source": source,
        "error": "数据源未返回请求区间内的历史行情",
        **coverage,
    }


def _normalize_yahoo_chart_prices(
    chart: Dict[str, Any],
    *,
    symbol: str,
    market: str,
    yahoo_symbol: str,
    currency: str,
) -> List[SecurityPrice]:
    timestamps = chart.get("timestamp") or []
    indicators = chart.get("indicators") or {}
    quote_items = indicators.get("quote") or []
    if not timestamps or not quote_items:
        return []

    quote = quote_items[0]
    adj_items = indicators.get("adjclose") or []
    adj_close_values = (adj_items[0].get("adjclose") if adj_items else []) or []
    rows: List[SecurityPrice] = []
    previous_close: Optional[Decimal] = None

    for index, timestamp in enumerate(timestamps):
        price_date = datetime.fromtimestamp(int(timestamp), tz=timezone.utc).date()
        close_price = _decimal_or_none(_yahoo_series_value(quote, "close", index))
        if price_date < chart.get("requested_start_date", price_date) or price_date > chart.get(
            "requested_end_date", price_date
        ):
            continue
        if close_price is None:
            continue

        open_price = _decimal_or_none(_yahoo_series_value(quote, "open", index))
        high_price = _decimal_or_none(_yahoo_series_value(quote, "high", index))
        low_price = _decimal_or_none(_yahoo_series_value(quote, "low", index))
        adj_close_price = _decimal_or_none(
            adj_close_values[index] if index < len(adj_close_values) else None
        )

        rows.append(
            SecurityPrice(
                symbol=symbol,
                market=market,
                ts_code=yahoo_symbol,
                price_date=price_date,
                currency=currency,
                open_price=open_price,
                high_price=high_price,
                low_price=low_price,
                close_price=close_price,
                pre_close_price=previous_close,
                adj_factor=(adj_close_price / close_price)
                if adj_close_price and close_price
                else None,
                adj_close_price=adj_close_price,
                source="yahoo-finance",
            )
        )
        previous_close = close_price

    return rows


def _yahoo_series_value(series: Dict[str, List[Any]], field: str, index: int) -> Any:
    values = series.get(field) or []
    return values[index] if index < len(values) else None


def _clean_stockanalysis_cell(value: str) -> str:
    text = re.sub(r"<!--.*?-->", "", value, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _fetch_stockanalysis_history(symbol: str) -> str:
    response = requests.get(
        STOCKANALYSIS_HISTORY_URL.format(symbol=symbol.lower()),
        headers={"User-Agent": YAHOO_USER_AGENT},
        timeout=(5, 20),
    )
    response.raise_for_status()
    return response.text


def _normalize_stockanalysis_history_prices(
    page_html: str,
    *,
    symbol: str,
    market: str,
    external_symbol: str,
    currency: str,
    start_date: date,
    end_date: date,
) -> List[SecurityPrice]:
    table_match = re.search(r"<table[^>]*>.*?</table>", page_html, flags=re.S | re.I)
    if not table_match:
        return []

    rows: List[SecurityPrice] = []
    previous_close: Optional[Decimal] = None
    for row_html in reversed(
        re.findall(r"<tr[^>]*>.*?</tr>", table_match.group(0), flags=re.S | re.I)
    ):
        cells = [
            _clean_stockanalysis_cell(cell)
            for cell in re.findall(r"<td[^>]*>(.*?)</td>", row_html, flags=re.S | re.I)
        ]
        if len(cells) < 6:
            continue

        try:
            price_date = datetime.strptime(cells[0], "%b %d, %Y").date()
        except ValueError:
            continue
        if price_date < start_date or price_date > end_date:
            continue

        close_price = _decimal_or_none(cells[4].replace(",", ""))
        if close_price is None:
            continue
        adj_close_price = _decimal_or_none(cells[5].replace(",", ""))

        rows.append(
            SecurityPrice(
                symbol=symbol,
                market=market,
                ts_code=external_symbol,
                price_date=price_date,
                currency=currency,
                open_price=_decimal_or_none(cells[1].replace(",", "")),
                high_price=_decimal_or_none(cells[2].replace(",", "")),
                low_price=_decimal_or_none(cells[3].replace(",", "")),
                close_price=close_price,
                pre_close_price=previous_close,
                adj_factor=(adj_close_price / close_price)
                if adj_close_price and close_price
                else None,
                adj_close_price=adj_close_price,
                source="stockanalysis",
            )
        )
        previous_close = close_price

    return rows


def fetch_and_store_stockanalysis_price_history(
    db: Session,
    *,
    symbol: str,
    market: str,
    start_date: date,
    end_date: date,
    currency: Optional[str] = None,
) -> Dict[str, Any]:
    if market != "新加坡股":
        return {
            "symbol": symbol,
            "market": market,
            "success": False,
            "rows": 0,
            "error": f"暂不支持通过 StockAnalysis 同步 {market} 历史行情",
        }

    try:
        page_html = _fetch_stockanalysis_history(symbol)
        rows = _normalize_stockanalysis_history_prices(
            page_html,
            symbol=symbol,
            market=market,
            external_symbol=f"SGX:{str(symbol).upper()}",
            currency=infer_price_currency(market, currency),
            start_date=start_date,
            end_date=end_date,
        )
        if not rows:
            return _empty_external_history_result(
                symbol=symbol,
                market=market,
                source="stockanalysis",
                start_date=start_date,
                end_date=end_date,
            )

        changed = upsert_security_prices(db, rows)
        return {
            "symbol": symbol,
            "market": market,
            "success": True,
            "rows": changed,
            "source": "stockanalysis",
            **_history_coverage_payload(start_date, end_date, rows),
        }
    except Exception as exc:
        db.rollback()
        logger.warning("同步 StockAnalysis %s %s 历史行情失败: %s", market, symbol, exc)
        return {
            "symbol": symbol,
            "market": market,
            "success": False,
            "rows": 0,
            "error": str(exc)[:240],
        }


def fetch_and_store_public_price_history(
    db: Session,
    *,
    symbol: str,
    market: str,
    start_date: date,
    end_date: date,
    currency: Optional[str] = None,
) -> Dict[str, Any]:
    yahoo_result = fetch_and_store_yahoo_price_history(
        db,
        symbol=symbol,
        market=market,
        start_date=start_date,
        end_date=end_date,
        currency=currency,
    )
    if yahoo_result.get("success"):
        return yahoo_result

    fallback_result = fetch_and_store_stockanalysis_price_history(
        db,
        symbol=symbol,
        market=market,
        start_date=start_date,
        end_date=end_date,
        currency=currency,
    )
    fallback_result["fallback_from"] = "yahoo-finance"
    fallback_result["fallback_error"] = yahoo_result.get("error")
    return fallback_result


def fetch_and_store_yahoo_price_history(
    db: Session,
    *,
    symbol: str,
    market: str,
    start_date: date,
    end_date: date,
    currency: Optional[str] = None,
) -> Dict[str, Any]:
    yahoo_symbol = to_yahoo_symbol(symbol, market)
    if not yahoo_symbol:
        return {
            "symbol": symbol,
            "market": market,
            "success": False,
            "rows": 0,
            "error": f"暂不支持通过 Yahoo Finance 同步 {market} 历史行情",
        }

    try:
        chart = _fetch_yahoo_chart(yahoo_symbol, start_date, end_date)
        chart["requested_start_date"] = start_date
        chart["requested_end_date"] = end_date
        rows = _normalize_yahoo_chart_prices(
            chart,
            symbol=symbol,
            market=market,
            yahoo_symbol=yahoo_symbol,
            currency=infer_price_currency(market, currency),
        )
        if not rows:
            return _empty_external_history_result(
                symbol=symbol,
                market=market,
                source="yahoo-finance",
                start_date=start_date,
                end_date=end_date,
            )

        changed = upsert_security_prices(db, rows)
        return {
            "symbol": symbol,
            "market": market,
            "success": True,
            "rows": changed,
            "source": "yahoo-finance",
            **_history_coverage_payload(start_date, end_date, rows),
        }
    except Exception as exc:
        db.rollback()
        logger.warning("同步 Yahoo %s %s 历史行情失败: %s", market, symbol, exc)
        return {
            "symbol": symbol,
            "market": market,
            "success": False,
            "rows": 0,
            "error": str(exc)[:240],
        }


def upsert_security_prices(db: Session, prices: List[SecurityPrice]) -> int:
    """按 (symbol, market, price_date) 原子 upsert（ON CONFLICT DO UPDATE）。

    此前是 query-then-insert：两个会话并发补同一批行时都看不见对方的未提交
    行、各自 INSERT，唯一键让后提交者整批回滚。全局基准指数把三只固定标的
    加进了每个用户的同步任务，把这个偶发竞态放大成常态，必须在数据库层原子化。
    """
    if not prices:
        return 0

    update_columns = (
        "ts_code",
        "currency",
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "pre_close_price",
        "adj_factor",
        "adj_close_price",
        "source",
    )
    rows = [
        {
            "symbol": price.symbol,
            "market": price.market,
            "price_date": price.price_date,
            **{column: getattr(price, column) for column in update_columns},
        }
        for price in prices
    ]
    stmt = pg_insert(SecurityPrice).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uix_security_price_symbol_market_date",
        set_={column: getattr(stmt.excluded, column) for column in update_columns},
    )
    db.execute(stmt)
    db.commit()
    return len(rows)


def fetch_and_store_security_price_history(
    db: Session,
    *,
    symbol: str,
    market: str,
    start_date: date,
    end_date: date,
    currency: Optional[str] = None,
) -> Dict[str, Any]:
    # 所有下游写入（Tushare / Tiingo / 腾讯 / Yahoo）共用同一个行情币种（#276）
    currency = resolve_price_currency(db, symbol, market, currency)
    resolved = resolve_tushare_history_api(symbol, market)
    if not resolved:
        if to_yahoo_symbol(symbol, market):
            return fetch_and_store_public_price_history(
                db,
                symbol=symbol,
                market=market,
                start_date=start_date,
                end_date=end_date,
                currency=currency,
            )
        return {
            "symbol": symbol,
            "market": market,
            "success": False,
            "rows": 0,
            "error": f"暂不支持同步 {market} 历史行情",
        }

    tencent_code = to_tencent_kline_code(symbol, market)

    def fallback_code() -> Optional[str]:
        # 美股的腾讯代码要联网探测交易所后缀，只在 Tushare 失败/空返回时才解析
        if tencent_code:
            return tencent_code
        if market == "美股":
            try:
                return probe_tencent_us_kline_code(symbol)
            except TencentProbeInconclusive as exc:
                fallback_errors.append(f"腾讯K线: {exc}")
                return None
        return None

    fallback_errors: List[str] = []
    tiingo_tried: List[bool] = []

    def try_tiingo() -> Optional[Dict[str, Any]]:
        """美股第二顺位：Tiingo EOD（带复权收盘）。成功（含短区间无交易日）直接采用；
        未配置 / 失败 / 长区间空返回记下原因后返回 None，交给腾讯 K 线。
        每次同步最多外呼一次（空返回分支里的腾讯兜底抛错会再落到 except 分支）。"""
        if market != "美股" or tiingo_tried:
            return None
        tiingo_tried.append(True)
        if not tiingo_source.is_configured():
            logger.debug("未配置 TIINGO_API_TOKEN，美股 %s 历史行情跳过 Tiingo", symbol)
            return None
        try:
            result = _fetch_and_store_tiingo_history(
                db,
                symbol=symbol,
                market=market,
                start_date=start_date,
                end_date=end_date,
                currency=currency,
            )
        except Exception as tiingo_exc:  # noqa: BLE001 - 兜底源失败交给下一源
            db.rollback()
            logger.warning("Tiingo 日线同步 %s %s 失败: %s", market, symbol, tiingo_exc)
            fallback_errors.append(f"Tiingo: {str(tiingo_exc)[:160]}")
            return None
        if result.get("success"):
            return result
        fallback_errors.append(f"Tiingo: {result.get('error')}")
        return None

    try:
        df = _tushare_history_query(
            resolved["api"],
            ts_code=resolved["ts_code"],
            start_date=_date_to_tushare(start_date),
            end_date=_date_to_tushare(end_date),
        )
        if df is None or df.empty:
            # Tushare 空返回可能是真无交易日，也可能是覆盖空洞
            # （daily 不含 B 股/可转债、hk_daily 不含港股 ETF/REIT）。
            # 美股先问 Tiingo；有腾讯映射就再问一次兜底源，仍为空才视为无数据。
            tiingo_result = try_tiingo()
            if tiingo_result is not None:
                return tiingo_result
            code = fallback_code()
            if code:
                # 单独 try：此前这里抛错会落进外层 except 再调一次腾讯（重复请求），
                # 且把腾讯的异常文本当成主源错误报出去
                try:
                    return _fetch_and_store_tencent_history(
                        db,
                        symbol=symbol,
                        market=market,
                        kline_code=code,
                        start_date=start_date,
                        end_date=end_date,
                        currency=currency,
                    )
                except Exception as fallback_exc:  # noqa: BLE001
                    db.rollback()
                    logger.warning(
                        "腾讯K线兜底同步 %s %s 失败（Tushare 空返回）: %s",
                        market,
                        symbol,
                        fallback_exc,
                    )
                    fallback_errors.append(f"腾讯K线: {str(fallback_exc)[:160]}")
                    return {
                        "symbol": symbol,
                        "market": market,
                        "success": False,
                        "rows": 0,
                        "error": f"tushare {resolved['api']} 返回空表，兜底源也失败",
                        "error_kind": "other",
                        "fallback_errors": fallback_errors,
                    }
            if fallback_errors:
                # 兜底源失败或没能下结论：不能当成「真没有交易日」，否则尾部同步会把这一天
                # 标为已处理（PR #298 评审）
                return {
                    "symbol": symbol,
                    "market": market,
                    "success": False,
                    "rows": 0,
                    "error": f"tushare {resolved['api']} 返回空表，兜底源失败或未下结论",
                    "error_kind": "other",
                    "fallback_errors": fallback_errors,
                }
            return {
                "symbol": symbol,
                "market": market,
                "success": True,
                "rows": 0,
                "source": f"tushare-{resolved['api']}",
                "message": "没有新增交易日数据",
            }

        # 复权因子目前没有任何读取方（为回测/复权展示预留）：拉取是 best-effort，失败只记日志。
        # 此前它在主 try 里，一报错（配额/权限）就丢掉已拿到的日线、整段改走不复权兜底源（#276）
        try:
            factors = _fetch_adjustment_factors(
                resolved.get("adjust_api", ""),
                resolved["ts_code"],
                start_date,
                end_date,
            )
        except Exception as factor_exc:  # noqa: BLE001
            logger.warning(
                "复权因子拉取失败（不影响日线入库）%s %s: %s", market, symbol, factor_exc
            )
            factors = {}
        rows = _normalize_price_rows(
            df.iterrows(),
            symbol=symbol,
            market=market,
            ts_code=resolved["ts_code"],
            currency=infer_price_currency(market, currency),
            source=f"tushare-{resolved['api']}",
            factors=factors,
        )
        changed = upsert_security_prices(db, rows)
        return {
            "symbol": symbol,
            "market": market,
            "success": True,
            "rows": changed,
            "source": f"tushare-{resolved['api']}",
        }
    except Exception as exc:
        db.rollback()
        logger.warning("同步 %s %s 历史行情失败: %s", market, symbol, exc)
        tiingo_result = try_tiingo()
        if tiingo_result is not None:
            return tiingo_result
        code = fallback_code()
        if code:
            try:
                return _fetch_and_store_tencent_history(
                    db,
                    symbol=symbol,
                    market=market,
                    kline_code=code,
                    start_date=start_date,
                    end_date=end_date,
                    currency=currency,
                )
            except Exception as fallback_exc:
                db.rollback()
                logger.warning("腾讯K线兜底同步 %s %s 也失败: %s", market, symbol, fallback_exc)
                fallback_errors.append(f"腾讯K线: {str(fallback_exc)[:160]}")
        # error 保持主源（Tushare）原文：performance_history_jobs 按它识别配额/权限类
        # 错误并中止整批；兜底源的失败原因另列，不混进去改变判定
        failure = {
            "symbol": symbol,
            "market": market,
            "success": False,
            "rows": 0,
            "error": str(exc)[:240],
            # 结构化分类只由 Tushare 调用点写入：下游按它判定整批中止，不再对 error 做子串匹配
            "error_kind": classify_tushare_error(exc),
        }
        if fallback_errors:
            failure["fallback_errors"] = fallback_errors
        return failure


def _today() -> date:
    """可注入的"今天"（测试 monkeypatch 用）。"""
    return local_today()


def _last_weekday_on_or_before(day: date) -> date:
    while day.weekday() >= 5:  # 5=Sat, 6=Sun
        day -= timedelta(days=1)
    return day


# Tushare 交易日历接口按市场分组；新加坡等无日历接口的市场退化为工作日启发。
_TRADE_CAL_API_BY_MARKET = {
    "A股": ("trade_cal", {"exchange": "SSE"}),
    "B股": ("trade_cal", {"exchange": "SSE"}),
    "港股": ("hk_tradecal", {}),
    "美股": ("us_tradecal", {}),
}

# 每市场每天缓存一次"最近已完成交易日"：{(api_name, today): last_open_date}
_trade_cal_cache: Dict[Tuple[str, date], date] = {}


def get_last_completed_trading_day(market: str, today: Optional[date] = None) -> date:
    """该市场最近一个已完成的交易日（≤ 昨天）。

    用 Tushare 交易日历精确判定（每市场每天最多 1 次调用，进程内缓存），
    法定假日与周末都不会再制造假尾部缺口；日历接口失败或市场无日历时
    退化为"最近工作日"启发（只覆盖周末）。绝不能用单个标的的空返回推断
    整市场休市——停牌/摘牌标的会污染其他活跃标的的更新。
    """
    today = today or _today()
    fallback = _last_weekday_on_or_before(today - timedelta(days=1))
    api_spec = _TRADE_CAL_API_BY_MARKET.get(market)
    if api_spec is None:
        return fallback

    api_name, extra = api_spec
    cache_key = (api_name, today)
    cached = _trade_cal_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        df = tushare_query_once(
            api_name,
            start_date=(today - timedelta(days=20)).strftime("%Y%m%d"),
            end_date=(today - timedelta(days=1)).strftime("%Y%m%d"),
            **extra,
        )
        open_days = [
            str(row["cal_date"]) for _, row in df.iterrows() if int(row.get("is_open", 0)) == 1
        ]
        if not open_days:
            raise ValueError("交易日历返回区间内无开市日")
        last_open = datetime.strptime(max(open_days), "%Y%m%d").date()
    except Exception as exc:
        logger.warning("获取 %s 交易日历失败，退化为工作日启发: %s", market, str(exc)[:120])
        last_open = fallback

    _trade_cal_cache[cache_key] = last_open
    return last_open


def get_security_price_coverage(
    db: Session,
    *,
    symbol: str,
    market: str,
) -> Dict[str, Any]:
    min_date, max_date, count = (
        db.query(
            func.min(SecurityPrice.price_date),
            func.max(SecurityPrice.price_date),
            func.count(SecurityPrice.id),
        )
        .filter(
            SecurityPrice.symbol == symbol,
            SecurityPrice.market == market,
        )
        .one()
    )

    return {
        "start_date": min_date,
        "end_date": max_date,
        "count": int(count or 0),
    }


def _missing_edge_ranges(
    coverage: Dict[str, Any],
    start_date: date,
    end_date: date,
) -> List[Tuple[date, date]]:
    if end_date < start_date:
        return []

    if not coverage["start_date"] or not coverage["end_date"]:
        return [(start_date, end_date)]

    ranges = []
    if coverage["start_date"] > start_date:
        ranges.append((start_date, coverage["start_date"] - timedelta(days=1)))
    if coverage["end_date"] < end_date:
        ranges.append((coverage["end_date"] + timedelta(days=1), end_date))
    return [(start, end) for start, end in ranges if start <= end]


def fetch_and_store_security_price_history_incremental(
    db: Session,
    *,
    symbol: str,
    market: str,
    start_date: date,
    end_date: date,
    currency: Optional[str] = None,
    calendar_market: Optional[str] = None,
    completed_through: Optional[date] = None,
) -> Dict[str, Any]:
    coverage_before = get_security_price_coverage(db, symbol=symbol, market=market)
    # 尾部端点钳到该市场最近一个已完成交易日（交易日历精确判定）：当日
    # 日线收盘前不存在，周末/法定假日也不存在——否则每次刷新会给每个标的
    # 造出假尾部缺口，短空探测后缓存不前移，下次刷新全量复现。
    # calendar_market 供无自有日历的市场借用（基准指数按目录日历市场钳制）。
    # completed_through：调用方已按该市场当地时钟确认收盘就绪的交易日（日线尾部同步
    # 在当地收盘后传入当天）；缺省按 UTC 今天、不含今天——其余调用方保持盘中防护。
    effective_end = min(
        end_date,
        completed_through or get_last_completed_trading_day(calendar_market or market),
    )
    ranges = (
        _missing_edge_ranges(coverage_before, start_date, effective_end)
        if effective_end >= start_date
        else []
    )
    if not ranges:
        return {
            "symbol": symbol,
            "market": market,
            "success": True,
            "skipped": True,
            "rows": 0,
            "coverage_before": {
                "start_date": coverage_before["start_date"].isoformat()
                if coverage_before["start_date"]
                else None,
                "end_date": coverage_before["end_date"].isoformat()
                if coverage_before["end_date"]
                else None,
                "count": coverage_before["count"],
            },
            "coverage_complete": True,
            "remaining_edge_ranges": [],
            "message": "历史行情缓存已覆盖当前区间",
        }

    rows = 0
    range_results = []
    for range_start, range_end in ranges:
        result = fetch_and_store_security_price_history(
            db,
            symbol=symbol,
            market=market,
            start_date=range_start,
            end_date=range_end,
            currency=currency,
        )
        range_results.append(
            {
                **result,
                "start_date": range_start.isoformat(),
                "end_date": range_end.isoformat(),
            }
        )
        if not result.get("success"):
            remaining_ranges = _missing_edge_ranges(
                get_security_price_coverage(db, symbol=symbol, market=market),
                start_date,
                end_date,
            )
            return {
                "symbol": symbol,
                "market": market,
                "success": False,
                "skipped": False,
                "rows": rows,
                "coverage_before": {
                    "start_date": coverage_before["start_date"].isoformat()
                    if coverage_before["start_date"]
                    else None,
                    "end_date": coverage_before["end_date"].isoformat()
                    if coverage_before["end_date"]
                    else None,
                    "count": coverage_before["count"],
                },
                "range_results": range_results,
                "coverage_complete": not remaining_ranges,
                "remaining_edge_ranges": [
                    {"start_date": start.isoformat(), "end_date": end.isoformat()}
                    for start, end in remaining_ranges
                ],
                "error": result.get("error"),
                "coverage_status": result.get("coverage_status"),
            }
        rows += int(result.get("rows") or 0)

    coverage_after = get_security_price_coverage(db, symbol=symbol, market=market)
    remaining_ranges = _missing_edge_ranges(coverage_after, start_date, end_date)
    return {
        "symbol": symbol,
        "market": market,
        "success": True,
        "skipped": rows == 0,
        "rows": rows,
        "coverage_before": {
            "start_date": coverage_before["start_date"].isoformat()
            if coverage_before["start_date"]
            else None,
            "end_date": coverage_before["end_date"].isoformat()
            if coverage_before["end_date"]
            else None,
            "count": coverage_before["count"],
        },
        "coverage_after": {
            "start_date": coverage_after["start_date"].isoformat()
            if coverage_after["start_date"]
            else None,
            "end_date": coverage_after["end_date"].isoformat()
            if coverage_after["end_date"]
            else None,
            "count": coverage_after["count"],
        },
        "coverage_complete": not remaining_ranges,
        "remaining_edge_ranges": [
            {"start_date": start.isoformat(), "end_date": end.isoformat()}
            for start, end in remaining_ranges
        ],
        "range_results": range_results,
    }
