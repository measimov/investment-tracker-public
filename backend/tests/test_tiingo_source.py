"""Tiingo 美股数据源：纯函数解析、错误映射、未配置降级、报价链与日线兜底链顺序。

全部离线：HTTP 一律 monkeypatch，固件在 tests/fixtures/tiingo/（按官方文档形状构造）。
"""

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
import requests

from app.config import settings
from app.database import SessionLocal
from app.models.security_price import SecurityPrice
from app.services import market_data_service as mds
from app.services import stock_price_service as sps
from app.services import http_source
from app.services import tiingo_source as ts
from app.services import xueqiu_source
from app.services.stock_price_service import price_result

FIXTURES = Path(__file__).parent / "fixtures" / "tiingo"
FORBIDDEN_QUOTA_WORDS = ("每分钟最多访问", "权限", "积分不足", "抱歉，您")


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _tiingo_state(monkeypatch):
    """每个用例：默认已配置、零限速间隔、清空冷却；用例可再覆盖。"""
    monkeypatch.setattr(settings, "tiingo_api_token", "test-token")
    monkeypatch.setattr(settings, "tiingo_min_interval_seconds", 0.0)
    ts.reset_state()
    yield
    ts.reset_state()


class FakeResponse:
    def __init__(self, payload, status_code=200, text=None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class Recorder:
    """按 URL 路由的假 requests.get，记录每次调用。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, params=None, headers=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        for prefix, handler in self.routes:
            if url.startswith(prefix):
                return handler(url, params) if callable(handler) else handler
        raise AssertionError(f"未路由的请求: {url}")

    def urls(self):
        return [call["url"] for call in self.calls]


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("symbol", "ticker"),
    [("pdd", "PDD"), (" BABA ", "BABA"), ("BRK.B", "BRK-B"), ("brk/b", "BRK-B"), ("BF-B", "BF-B")],
)
def test_ticker_normalization(symbol, ticker):
    assert ts.to_tiingo_ticker(symbol) == ticker


@pytest.mark.parametrize("symbol", ["", "   ", "A B", "PDD?", "../x", None])
def test_ticker_normalization_rejects_garbage(symbol):
    with pytest.raises(ts.TiingoError):
        ts.to_tiingo_ticker(symbol)


def test_parse_eod_bars_takes_exchange_date_and_drops_null_close():
    bars = ts.parse_eod_bars(_fixture("eod_prices_pdd.json"))
    # 2026-09-24 那行 close=null 丢弃；日期取前 10 位（UTC 零点不按纽约换算，否则退一天）
    assert [bar["date"] for bar in bars] == [
        date(2026, 9, 22),
        date(2026, 9, 23),
        date(2026, 9, 25),
    ]
    assert bars[1]["close"] == Decimal("121.05")
    assert bars[1]["open"] == Decimal("118.6")
    assert bars[1]["adj_close"] == Decimal("121.05")
    assert bars[-1]["volume"] == Decimal("6540000")


def test_parse_eod_bars_keeps_adjusted_close_separate():
    bars = ts.parse_eod_bars(_fixture("eod_prices_dividend.json"))
    assert bars[0]["close"] == Decimal("50.0")
    assert bars[0]["adj_close"] == Decimal("49.5")


@pytest.mark.parametrize("payload", [{"detail": "x"}, "oops", None])
def test_parse_eod_bars_rejects_unknown_shape(payload):
    with pytest.raises(ts.TiingoError):
        ts.parse_eod_bars(payload)


def test_parse_eod_bars_empty_list_is_empty():
    assert ts.parse_eod_bars([]) == []


def test_parse_iex_quote_prefers_tngo_last():
    quote = ts.parse_iex_quote(_fixture("iex_pdd.json"))
    assert quote["price"] == Decimal("120.0")
    assert quote["prev_close"] == Decimal("121.05")
    assert quote["timestamp"] == datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)
    assert quote["as_of"] == date(2026, 9, 25)  # 16:00 ET 收盘，纽约交易日


def test_parse_iex_quote_falls_back_to_last_with_nanosecond_timestamp():
    quote = ts.parse_iex_quote(_fixture("iex_last_only.json"))
    assert quote["price"] == Decimal("471.5")
    # lastSaleTimestamp 带 9 位小数：截断到微秒后可解析
    assert quote["timestamp"].utcoffset() == timedelta(hours=-4)
    assert quote["timestamp"].microsecond == 123456
    assert quote["as_of"] == date(2026, 9, 25)


def test_parse_iex_quote_unknown_ticker_is_none():
    assert ts.parse_iex_quote([]) is None
    assert ts.parse_iex_quote([{"ticker": "X", "tngoLast": None, "last": None}]) is None
    with pytest.raises(ts.TiingoError):
        ts.parse_iex_quote("not json shape")


def test_iex_freshness_window():
    quote = ts.parse_iex_quote(_fixture("iex_pdd.json"))
    assert ts.iex_quote_is_fresh(quote, quote["timestamp"] + timedelta(days=3, hours=12))
    assert not ts.iex_quote_is_fresh(quote, quote["timestamp"] + timedelta(days=5))
    assert not ts.iex_quote_is_fresh(
        {"price": Decimal("1"), "timestamp": None}, datetime.now(timezone.utc)
    )


def test_interpret_error_maps_status_codes():
    errors = _fixture("errors.json")
    assert isinstance(ts.interpret_error(404, errors["not_found"], "ZZZZQ"), ts.TiingoNotFound)
    assert isinstance(ts.interpret_error(401, errors["invalid_token"]), ts.TiingoAuthError)
    assert isinstance(ts.interpret_error(403, {}), ts.TiingoAuthError)
    assert isinstance(ts.interpret_error(429, errors["rate_limit"]), ts.TiingoRateLimited)
    server = ts.interpret_error(502, "<html>bad gateway</html>")
    assert type(server) is ts.TiingoError and "502" in str(server)
    # 200 带 detail 的错误对象（额度用尽时出现过）同样是错误
    assert isinstance(ts.interpret_error(200, errors["rate_limit"]), ts.TiingoRateLimited)
    assert isinstance(ts.interpret_error(200, errors["not_found"]), ts.TiingoNotFound)
    assert isinstance(ts.interpret_error(200, errors["invalid_token"]), ts.TiingoAuthError)
    # 正常响应
    assert ts.interpret_error(200, []) is None
    assert ts.interpret_error(200, _fixture("iex_pdd.json")) is None


def test_error_messages_are_chinese_and_never_look_like_tushare_quota_errors():
    """performance_history_jobs 靠「权限」等字样识别 Tushare 配额错误并中止整批；
    Tiingo 的错误文案不能撞上这些签名。"""
    errors = _fixture("errors.json")
    for status, payload in [
        (401, errors["invalid_token"]),
        (403, {}),
        (404, errors["not_found"]),
        (429, errors["rate_limit"]),
        (500, {}),
    ]:
        message = str(ts.interpret_error(status, payload, "PDD"))
        assert "Tiingo" in message
        assert not any(word in message for word in FORBIDDEN_QUOTA_WORDS), message


# ---------------------------------------------------------------------------
# IO：鉴权、降级、超时、限速冷却
# ---------------------------------------------------------------------------


def test_unconfigured_raises_explicitly_and_never_calls_network(monkeypatch):
    monkeypatch.setattr(settings, "tiingo_api_token", "  ")
    recorder = Recorder([])
    monkeypatch.setattr(ts.requests, "get", recorder)

    assert ts.is_configured() is False
    with pytest.raises(ts.TiingoNotConfigured, match="未配置 TIINGO_API_TOKEN"):
        ts.fetch_eod_history("PDD", date(2026, 9, 1), date(2026, 9, 25))
    result = ts.fetch_tiingo_stock_price("PDD")
    assert result["success"] is False and result["price"] is None
    assert "未配置 TIINGO_API_TOKEN" in result["error"]
    assert recorder.calls == []


def test_request_sends_token_header_and_dashed_ticker(monkeypatch):
    recorder = Recorder([(ts.BASE_URL, FakeResponse(_fixture("eod_prices_pdd.json")))])
    monkeypatch.setattr(ts.requests, "get", recorder)

    bars = ts.fetch_eod_history("BRK.B", date(2026, 9, 22), date(2026, 9, 25))

    assert len(bars) == 3
    call = recorder.calls[0]
    assert call["url"] == "https://api.tiingo.com/tiingo/daily/BRK-B/prices"
    assert call["params"] == {"startDate": "2026-09-22", "endDate": "2026-09-25"}
    assert call["headers"]["Authorization"] == "Token test-token"
    assert call["timeout"] == settings.tiingo_timeout_seconds


def test_latest_eod_omits_dates(monkeypatch):
    recorder = Recorder([(ts.BASE_URL, FakeResponse(_fixture("eod_latest_pdd.json")))])
    monkeypatch.setattr(ts.requests, "get", recorder)
    bar = ts.fetch_latest_eod("PDD")
    assert bar["date"] == date(2026, 9, 25) and bar["close"] == Decimal("120.0")
    assert recorder.calls[0]["params"] is None


def test_timeout_and_network_errors_become_tiingo_errors(monkeypatch):
    def timeout(*args, **kwargs):
        raise requests.Timeout("read timed out")

    monkeypatch.setattr(ts.requests, "get", timeout)
    with pytest.raises(ts.TiingoError, match="超时"):
        ts.fetch_iex_quote("PDD")

    def conn_error(*args, **kwargs):
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(ts.requests, "get", conn_error)
    with pytest.raises(ts.TiingoError, match="网络错误"):
        ts.fetch_iex_quote("PDD")


def test_non_json_success_is_an_error_not_empty(monkeypatch):
    response = FakeResponse(ValueError("no json"), status_code=200, text="<html>login</html>")
    monkeypatch.setattr(ts.requests, "get", lambda *a, **k: response)
    with pytest.raises(ts.TiingoError, match="不是 JSON"):
        ts.fetch_eod_history("PDD", date(2026, 9, 1), date(2026, 9, 25))


def test_rate_limit_starts_process_cooldown(monkeypatch):
    recorder = Recorder([(ts.BASE_URL, FakeResponse(_fixture("errors.json")["rate_limit"], 429))])
    monkeypatch.setattr(ts.requests, "get", recorder)

    with pytest.raises(ts.TiingoRateLimited):
        ts.fetch_iex_quote("PDD")
    # 冷却期内不再外呼
    with pytest.raises(ts.TiingoRateLimited, match="冷却"):
        ts.fetch_latest_eod("BABA")
    assert len(recorder.calls) == 1

    ts.reset_state()
    recorder.routes = [(ts.BASE_URL, FakeResponse(_fixture("eod_latest_pdd.json")))]
    assert ts.fetch_latest_eod("PDD")["close"] == Decimal("120.0")


def test_cooldown_set_while_queued_blocks_the_queued_request(monkeypatch):
    """PR #246 评审 P2：排队等节流期间别的线程收到 429 → 等待结束后不得再外呼。"""
    recorder = Recorder([(ts.BASE_URL, FakeResponse(_fixture("eod_latest_pdd.json")))])
    monkeypatch.setattr(ts.requests, "get", recorder)
    monkeypatch.setattr(settings, "tiingo_min_interval_seconds", 1.0)
    clock = {"now": 1000.0}
    monkeypatch.setattr(ts.time, "monotonic", lambda: clock["now"])
    http_source._last_request_at[ts.THROTTLE_KEY] = clock[
        "now"
    ]  # 上一个请求刚发出 → 本次必须排队等待

    def sleep_while_other_thread_hits_429(seconds):
        ts._note_rate_limited()
        clock["now"] += seconds

    monkeypatch.setattr(ts.time, "sleep", sleep_while_other_thread_hits_429)
    with pytest.raises(ts.TiingoRateLimited, match="冷却"):
        ts.fetch_latest_eod("PDD")
    assert recorder.calls == []


def test_queued_threads_stop_after_first_429(monkeypatch):
    """真实多线程：第一个请求 429 后，已在节流锁上排队的请求一个都不发出。"""
    import threading

    monkeypatch.setattr(settings, "tiingo_min_interval_seconds", 0.05)
    calls = []
    first_call_started = threading.Event()

    def fake_get(url, **kwargs):
        calls.append(url)
        first_call_started.set()
        return FakeResponse(_fixture("errors.json")["rate_limit"], 429)

    monkeypatch.setattr(ts.requests, "get", fake_get)
    results = []

    def worker(symbol):
        try:
            ts.fetch_latest_eod(symbol)
            results.append("ok")
        except ts.TiingoRateLimited:
            results.append("limited")

    first = threading.Thread(target=worker, args=("PDD",))
    first.start()
    assert first_call_started.wait(5)
    queued = [threading.Thread(target=worker, args=(s,)) for s in ("BABA", "JD", "BIDU")]
    for thread in queued:
        thread.start()
    for thread in [first, *queued]:
        thread.join(5)
    assert len(calls) == 1
    assert results.count("limited") == 4


def test_throttle_spaces_requests(monkeypatch):
    monkeypatch.setattr(settings, "tiingo_min_interval_seconds", 1.5)
    clock = {"now": 1000.0}
    sleeps = []
    monkeypatch.setattr(ts.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(ts.time, "sleep", lambda seconds: sleeps.append(seconds))

    ts._throttle()
    clock["now"] += 0.5
    ts._throttle()
    assert sleeps == [pytest.approx(1.0)]


# ---------------------------------------------------------------------------
# 报价：Tiingo 内部 IEX → EOD
# ---------------------------------------------------------------------------

NOW = datetime(2026, 9, 26, 3, 0, tzinfo=timezone.utc)


def _iex_eod_routes(iex_response, eod_response):
    return Recorder(
        [
            (f"{ts.BASE_URL}/iex/", iex_response),
            (f"{ts.BASE_URL}/tiingo/daily/", eod_response),
        ]
    )


def test_quote_uses_fresh_iex(monkeypatch):
    recorder = _iex_eod_routes(FakeResponse(_fixture("iex_pdd.json")), FakeResponse([]))
    monkeypatch.setattr(ts.requests, "get", recorder)

    result = ts.fetch_tiingo_stock_price("PDD", now=NOW)

    assert result["success"] is True
    assert result["source"] == "tiingo-iex"
    assert result["price"] == Decimal("120.0")
    assert result["as_of"] == date(2026, 9, 25)
    assert len(recorder.calls) == 1  # 新鲜 IEX 不再追加 EOD 请求


@pytest.mark.parametrize("iex_payload", [[], "stale"])
def test_quote_falls_back_to_latest_eod(monkeypatch, iex_payload):
    if iex_payload == "stale":
        iex_payload = _fixture("iex_pdd.json")
        iex_payload[0]["timestamp"] = "2026-09-01T20:00:00+00:00"
    recorder = _iex_eod_routes(
        FakeResponse(iex_payload), FakeResponse(_fixture("eod_latest_pdd.json"))
    )
    monkeypatch.setattr(ts.requests, "get", recorder)

    result = ts.fetch_tiingo_stock_price("PDD", now=NOW)

    assert result["success"] is True
    assert result["source"] == "tiingo-eod"
    assert result["price"] == Decimal("120.0")
    assert result["as_of"] == date(2026, 9, 25)


def test_quote_iex_not_found_still_tries_eod(monkeypatch):
    recorder = _iex_eod_routes(
        FakeResponse(_fixture("errors.json")["not_found"], 404),
        FakeResponse(_fixture("eod_latest_pdd.json")),
    )
    monkeypatch.setattr(ts.requests, "get", recorder)
    result = ts.fetch_tiingo_stock_price("PDD", now=NOW)
    assert result["success"] is True and result["source"] == "tiingo-eod"


@pytest.mark.parametrize("status", [401, 429])
def test_quote_auth_or_rate_limit_stops_without_eod(monkeypatch, status):
    recorder = _iex_eod_routes(FakeResponse({"detail": "x"}, status), FakeResponse([]))
    monkeypatch.setattr(ts.requests, "get", recorder)

    result = ts.fetch_tiingo_stock_price("PDD", now=NOW)

    assert result["success"] is False and result["price"] is None
    assert result["error"].startswith("Tiingo 行情获取失败")
    assert len(recorder.calls) == 1  # 同一原因必然再失败，不多耗额度


def test_quote_both_endpoints_fail(monkeypatch):
    recorder = _iex_eod_routes(
        FakeResponse([]), FakeResponse(_fixture("errors.json")["not_found"], 404)
    )
    monkeypatch.setattr(ts.requests, "get", recorder)
    result = ts.fetch_tiingo_stock_price("ZZZZQ", now=NOW)
    assert result["success"] is False
    assert "IEX 无报价" in result["error"] and "404" in result["error"]


# ---------------------------------------------------------------------------
# 美股报价链：Tushare → Tiingo → 雪球
# ---------------------------------------------------------------------------


def _ok(source, price="1"):
    return price_result(price=Decimal(price), source=source, success=True, as_of=date(2026, 9, 25))


def _fail(source, error):
    return price_result(price=None, source=source, success=False, error=error)


@pytest.fixture
def chain(monkeypatch):
    calls = []
    outcomes = {}

    def make(name):
        def fetch(symbol, *args, **kwargs):
            calls.append(name)
            return outcomes[name]

        return fetch

    # 盘外顺序（盘中 Tiingo 提前见 test_quote_auto_refresh）；固定下来免得测试结果随运行时刻变
    monkeypatch.setattr(sps, "us_session_open", lambda now=None: False)
    monkeypatch.setattr(sps, "fetch_us_stock_price_tushare", make("tushare"))
    monkeypatch.setattr(ts, "fetch_tiingo_stock_price", make("tiingo"))
    monkeypatch.setattr(xueqiu_source, "fetch_xueqiu_stock_price", make("xueqiu"))
    return calls, outcomes


def test_chain_tushare_first(chain):
    calls, outcomes = chain
    outcomes["tushare"] = _ok("tushare-us_daily")
    assert sps.fetch_us_stock_price("PDD")["source"] == "tushare-us_daily"
    assert calls == ["tushare"]


def test_chain_tiingo_before_xueqiu(chain):
    calls, outcomes = chain
    outcomes["tushare"] = _fail("tushare-us_daily", "美股 PDD Tushare获取失败")
    outcomes["tiingo"] = _ok("tiingo-iex", "120")
    result = sps.fetch_us_stock_price("PDD")
    assert result["source"] == "tiingo-iex" and result["price"] == Decimal("120")
    assert calls == ["tushare", "tiingo"]


def test_chain_xueqiu_is_last_resort(chain):
    calls, outcomes = chain
    outcomes["tushare"] = _fail("tushare-us_daily", "t")
    outcomes["tiingo"] = _fail("tiingo", ts.NOT_CONFIGURED_MESSAGE)
    outcomes["xueqiu"] = _ok("xueqiu-quote")
    assert sps.fetch_us_stock_price("PDD")["source"] == "xueqiu-quote"
    assert calls == ["tushare", "tiingo", "xueqiu"]


def test_chain_all_failed_combines_errors(chain):
    _, outcomes = chain
    outcomes["tushare"] = _fail("tushare-us_daily", "E-tushare")
    outcomes["tiingo"] = _fail("tiingo", "E-tiingo")
    outcomes["xueqiu"] = _fail("xueqiu-quote", "E-xueqiu")
    result = sps.fetch_us_stock_price("PDD")
    assert result["success"] is False and result["source"] == "all-failed"
    assert result["error"] == "E-tushare; E-tiingo; E-xueqiu"


# ---------------------------------------------------------------------------
# 美股日线：Tushare → Tiingo EOD → 腾讯 K 线
# ---------------------------------------------------------------------------

SYMBOL = "ZTNG"


def _no_permission(*args, **kwargs):
    raise RuntimeError("抱歉，您没有接口(us_daily_adj)访问权限")


def _tencent_handler(url, params):
    code = params["param"].split(",")[0]
    candles = [["2026-09-24", "10.0", "10.5", "10.8", "9.9", "100"]] if code.endswith(".OQ") else []
    return FakeResponse({"code": 0, "data": {code: {"day": candles}}})


@pytest.fixture
def db():
    session = SessionLocal()
    session.query(SecurityPrice).filter_by(symbol=SYMBOL, market="美股").delete()
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.query(SecurityPrice).filter_by(symbol=SYMBOL, market="美股").delete()
        session.commit()
        session.close()


def _stored(db):
    return (
        db.query(SecurityPrice)
        .filter_by(symbol=SYMBOL, market="美股")
        .order_by(SecurityPrice.price_date)
        .all()
    )


def test_history_uses_tiingo_after_tushare_and_skips_tencent(monkeypatch, db):
    monkeypatch.setattr(mds, "_tushare_history_query", _no_permission)
    monkeypatch.setattr(mds, "_tencent_us_code_cache", {})
    recorder = Recorder(
        [
            (ts.BASE_URL, FakeResponse(_fixture("eod_prices_dividend.json"))),
            (mds.TENCENT_KLINE_URL, _tencent_handler),
        ]
    )
    monkeypatch.setattr(ts.requests, "get", recorder)

    result = mds.fetch_and_store_security_price_history(
        db,
        symbol=SYMBOL,
        market="美股",
        start_date=date(2026, 8, 7),
        end_date=date(2026, 8, 10),
    )

    assert result["success"] is True and result["rows"] == 2
    assert result["source"] == "tiingo-eod"
    assert result["coverage_status"] == "covered"
    assert all(url.startswith(ts.BASE_URL) for url in recorder.urls())  # 腾讯未被调用
    stored = _stored(db)
    assert [row.price_date for row in stored] == [date(2026, 8, 7), date(2026, 8, 10)]
    first, second = stored
    assert first.source == "tiingo-eod" and first.currency == "USD" and first.ts_code == SYMBOL
    assert first.close_price == Decimal("50.0")
    assert first.adj_close_price == Decimal("49.5")
    assert first.adj_factor == Decimal("0.99")  # adjClose / close，与 Yahoo 归一化同口径
    assert first.pre_close_price is None
    assert second.pre_close_price == Decimal("50.0")
    assert second.adj_factor == Decimal("1")


def test_history_tiingo_after_tushare_empty(monkeypatch, db):
    monkeypatch.setattr(mds, "_tushare_history_query", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(mds, "_tencent_us_code_cache", {})
    recorder = Recorder([(ts.BASE_URL, FakeResponse(_fixture("eod_prices_pdd.json")))])
    monkeypatch.setattr(ts.requests, "get", recorder)

    result = mds.fetch_and_store_security_price_history(
        db,
        symbol=SYMBOL,
        market="美股",
        start_date=date(2026, 9, 22),
        end_date=date(2026, 9, 25),
    )
    assert result["success"] is True and result["rows"] == 3 and result["source"] == "tiingo-eod"
    assert len(recorder.calls) == 1


def test_history_tiingo_failure_falls_back_to_tencent(monkeypatch, db):
    monkeypatch.setattr(mds, "_tushare_history_query", _no_permission)
    monkeypatch.setattr(mds, "_tencent_us_code_cache", {})
    recorder = Recorder(
        [
            (ts.BASE_URL, FakeResponse(_fixture("errors.json")["not_found"], 404)),
            (mds.TENCENT_KLINE_URL, _tencent_handler),
        ]
    )
    monkeypatch.setattr(ts.requests, "get", recorder)

    result = mds.fetch_and_store_security_price_history(
        db,
        symbol=SYMBOL,
        market="美股",
        start_date=date(2026, 9, 20),
        end_date=date(2026, 9, 26),
    )

    assert result["success"] is True and result["source"] == "tencent-kline"
    urls = recorder.urls()
    assert urls[0].startswith(ts.BASE_URL)  # Tiingo 先于腾讯
    assert sum(url.startswith(ts.BASE_URL) for url in urls) == 1  # 只问一次
    assert _stored(db)[0].source == "tencent-kline"


def test_history_unconfigured_goes_straight_to_tencent(monkeypatch, db):
    monkeypatch.setattr(settings, "tiingo_api_token", "")
    monkeypatch.setattr(mds, "_tushare_history_query", _no_permission)
    monkeypatch.setattr(mds, "_tencent_us_code_cache", {})
    recorder = Recorder([(mds.TENCENT_KLINE_URL, _tencent_handler)])
    monkeypatch.setattr(ts.requests, "get", recorder)

    result = mds.fetch_and_store_security_price_history(
        db,
        symbol=SYMBOL,
        market="美股",
        start_date=date(2026, 9, 20),
        end_date=date(2026, 9, 26),
    )

    assert result["success"] is True and result["source"] == "tencent-kline"
    assert not any(url.startswith(ts.BASE_URL) for url in recorder.urls())
    assert "fallback_errors" not in result


def test_history_tiingo_short_range_no_data_is_final(monkeypatch, db):
    """Tiingo 明确回答短区间无交易日（周末）即采用，不再多探测腾讯。"""
    monkeypatch.setattr(mds, "_tushare_history_query", _no_permission)
    monkeypatch.setattr(mds, "_tencent_us_code_cache", {})
    recorder = Recorder([(ts.BASE_URL, FakeResponse([]))])
    monkeypatch.setattr(ts.requests, "get", recorder)

    result = mds.fetch_and_store_security_price_history(
        db,
        symbol=SYMBOL,
        market="美股",
        start_date=date(2026, 9, 26),
        end_date=date(2026, 9, 27),
    )
    assert result["success"] is True and result["rows"] == 0
    assert result["source"] == "tiingo-eod" and result["coverage_status"] == "no_data"
    assert len(recorder.calls) == 1


def test_history_all_sources_fail_keeps_tushare_error_for_quota_detection(monkeypatch, db):
    monkeypatch.setattr(mds, "_tushare_history_query", _no_permission)
    monkeypatch.setattr(mds, "_tencent_us_code_cache", {})
    recorder = Recorder(
        [
            (ts.BASE_URL, FakeResponse({"detail": "Invalid token."}, 401)),
            (mds.TENCENT_KLINE_URL, lambda url, params: FakeResponse({"code": 0, "data": {}})),
        ]
    )
    monkeypatch.setattr(ts.requests, "get", recorder)

    result = mds.fetch_and_store_security_price_history(
        db,
        symbol=SYMBOL,
        market="美股",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 9, 26),
    )

    assert result["success"] is False
    assert result["error"].startswith("抱歉，您没有接口")  # 主源原文，配额识别不变
    assert any("Tiingo" in item and "401" in item for item in result["fallback_errors"])
    assert _stored(db) == []
