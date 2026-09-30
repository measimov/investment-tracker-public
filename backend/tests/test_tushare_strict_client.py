"""#275：Tushare 层的失败语义。

SDK 的 DataApi 在 HTTP ≥400 时直接返回空 DataFrame，调用方又把「空数据」与各种 ValueError
混在一起 `except ValueError: return []`——网关 5xx / 维护页被当成「成功 0 行」：档案、分红、
事件同步记成功，目录分页被当成尾页截断，日线尾部把失败日标成已处理。
"""

import threading
import time
from datetime import date

import pandas as pd
import pytest
import requests

from app.services import market_data_service as mds
from app.services import security_catalog_service as catalog
from app.services import stock_price_service as sps
from app.services.stock_price_service import (
    StrictTushareClient,
    TushareEmptyResult,
    TushareUpstreamError,
    classify_tushare_error,
    is_tushare_quota_failure,
)


class _Response:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _client_returning(monkeypatch, response):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json))
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(sps.requests, "post", fake_post)
    return StrictTushareClient("token", "https://tushare.example/dataapi"), calls


def _ok(fields, items):
    return _Response(payload={"code": 0, "msg": "", "data": {"fields": fields, "items": items}})


# --------------------------------------------------------------------------- 严格客户端


def test_success_builds_a_frame_and_mirrors_the_sdk_request(monkeypatch):
    client, calls = _client_returning(monkeypatch, _ok(["ts_code", "close"], [["600000.SH", 9.5]]))
    frame = client.daily(ts_code="600000.SH")  # SDK 同款 __getattr__ 调用形态
    assert list(frame.columns) == ["ts_code", "close"]
    assert frame.iloc[0]["close"] == 9.5
    url, payload = calls[0]
    assert url == "https://tushare.example/dataapi/daily"
    assert payload["api_name"] == "daily" and payload["token"] == "token"
    assert payload["params"]["ts_code"] == "600000.SH"


@pytest.mark.parametrize(
    "response",
    [
        _Response(status_code=502, text="<html>Bad Gateway</html>"),
        _Response(status_code=200, text="<html>维护中</html>"),  # 非 JSON
        _Response(payload={"unexpected": True}),  # 缺 code
        _Response(payload={"code": 0, "data": None}),  # 缺 fields/items
        requests.ConnectionError("reset"),
    ],
)
def test_gateway_failures_raise_instead_of_returning_an_empty_frame(monkeypatch, response):
    client, _ = _client_returning(monkeypatch, response)
    with pytest.raises(TushareUpstreamError) as info:
        client.query("daily", ts_code="600000.SH")
    # 文案不得撞上配额签名：分类为 other（可重试），不会让整批被当成配额错误中止
    assert classify_tushare_error(info.value) == "other"


def test_business_error_keeps_the_original_message_for_classification(monkeypatch):
    client, _ = _client_returning(
        monkeypatch, _Response(payload={"code": 40203, "msg": "抱歉，您每分钟最多访问该接口1次"})
    )
    with pytest.raises(Exception) as info:
        client.query("fina_audit")
    assert classify_tushare_error(info.value) == "rate"


# --------------------------------------------------------------------------- tushare_query


@pytest.fixture
def no_rate_gate(monkeypatch):
    monkeypatch.setattr(sps, "wait_for_tushare_rate_limit", lambda api_name: None)


def test_empty_result_is_its_own_exception_and_is_not_retried(monkeypatch, no_rate_gate):
    client, calls = _client_returning(monkeypatch, _ok(["ts_code"], []))
    monkeypatch.setattr(sps, "get_tushare_pro", lambda: client)
    with pytest.raises(TushareEmptyResult):
        sps.tushare_query("forecast", ts_code="600000.SH")
    assert len(calls) == 1  # 空结果是合法答案：此前按失败重试 3 次
    assert not issubclass(TushareEmptyResult, ValueError)


def test_upstream_failure_is_retried_then_propagates(monkeypatch, no_rate_gate):
    client, calls = _client_returning(monkeypatch, _Response(status_code=503))
    monkeypatch.setattr(sps, "get_tushare_pro", lambda: client)
    monkeypatch.setattr(sps.time, "sleep", lambda seconds: None)
    with pytest.raises(TushareUpstreamError):
        sps.tushare_query("dividend", ts_code="600000.SH")
    assert len(calls) == 3


def test_catalog_paging_stops_on_upstream_error_instead_of_truncating(monkeypatch):
    """第 2 页网关故障：此前被当成「尾页」、目录静默截断且该源记成功。"""
    pages = []

    def fake_query(api_name, **kwargs):
        pages.append(kwargs["offset"])
        if kwargs["offset"] == 0:
            return pd.DataFrame([{"ts_code": f"X{i}"} for i in range(2)])
        raise TushareUpstreamError("tushare us_basic 网关返回 HTTP 502")

    monkeypatch.setattr(catalog, "tushare_query", fake_query)
    with pytest.raises(TushareUpstreamError):
        catalog._tushare_pages("us_basic", page_size=2, max_pages=5)
    assert pages == [0, 2]


# --------------------------------------------------------------------------- 配额判定


@pytest.mark.parametrize(
    "result, expected",
    [
        ({"success": False, "error_kind": "fatal", "error": "x"}, True),
        ({"success": False, "error_kind": "other", "error": "权限"}, False),  # 结构化优先
        ({"success": False, "error": "抱歉，您每小时最多访问该接口1次"}, True),  # 旧清单漏了
        ({"success": False, "error": "积分不足"}, True),
        ({"success": False, "error": "您的token不对，请确认。"}, True),  # 网关对失效 token 的原文
        ({"success": False, "error": "tushare daily 网关返回 HTTP 502"}, False),
        ({"success": True, "error_kind": "fatal"}, False),
    ],
)
def test_quota_failure_detection(result, expected):
    assert is_tushare_quota_failure(result) is expected


# --------------------------------------------------------------------------- 日线历史


def test_history_upstream_error_is_a_failure_with_error_kind(monkeypatch):
    def boom(*args, **kwargs):
        raise TushareUpstreamError("tushare daily 网关返回 HTTP 502")

    monkeypatch.setattr(mds, "_tushare_history_query", boom)
    monkeypatch.setattr(mds, "to_tencent_kline_code", lambda symbol, market: None)
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        result = mds.fetch_and_store_security_price_history(
            db,
            symbol="600000",
            market="A股",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 5),
        )
    finally:
        db.close()
    assert result["success"] is False
    assert result["error_kind"] == "other"
    assert is_tushare_quota_failure(result) is False


def test_empty_history_with_failing_fallback_is_one_request_and_a_failure(monkeypatch):
    """Tushare 空表 + 腾讯兜底失败：此前异常落进外层 except 又请求一次腾讯，
    并把腾讯的文本当成主源错误。"""
    monkeypatch.setattr(mds, "_tushare_history_query", lambda *a, **k: pd.DataFrame())
    tencent_calls = []

    def failing_tencent(*args, **kwargs):
        tencent_calls.append(1)
        raise RuntimeError("tencent down")

    monkeypatch.setattr(mds, "_fetch_and_store_tencent_history", failing_tencent)
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        result = mds.fetch_and_store_security_price_history(
            db,
            symbol="900926",
            market="B股",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 5),
        )
    finally:
        db.close()
    assert len(tencent_calls) == 1
    assert result["success"] is False
    assert "兜底源也失败" in result["error"]
    assert result["fallback_errors"] == ["腾讯K线: tencent down"]


def test_empty_us_history_with_inconclusive_fallbacks_is_a_failure(monkeypatch):
    """PR #298 评审：美股 Tushare 空表 → Tiingo 失败 → 腾讯代码探测没下结论，此前落到
    「成功 0 行」（fallback_errors 被丢掉），尾部同步会把这一天标为已处理。"""
    monkeypatch.setattr(mds, "_tushare_history_query", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(mds.tiingo_source, "is_configured", lambda: True)

    def failing_tiingo(*args, **kwargs):
        raise RuntimeError("tiingo 429")

    def inconclusive(symbol):
        raise mds.TencentProbeInconclusive("腾讯美股代码探测 usZZTEST.OQ 失败: timeout")

    monkeypatch.setattr(mds, "_fetch_and_store_tiingo_history", failing_tiingo)
    monkeypatch.setattr(mds, "probe_tencent_us_kline_code", inconclusive)
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        result = mds.fetch_and_store_security_price_history(
            db,
            symbol="ZZTEST",
            market="美股",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 5),
        )
    finally:
        db.close()
    assert result["success"] is False
    assert result["error_kind"] == "other"
    assert [e.split(":")[0] for e in result["fallback_errors"]] == ["Tiingo", "腾讯K线"]


# --------------------------------------------------------------------------- 腾讯探测与取名


class _TencentResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_us_code_probe_does_not_cache_an_error_envelope(monkeypatch):
    mds._tencent_us_code_cache.pop("ZZTEST", None)
    monkeypatch.setattr(
        mds.requests, "get", lambda *a, **k: _TencentResponse({"code": -1, "msg": "限流"})
    )
    assert mds.resolve_tencent_us_kline_code("ZZTEST") is None
    assert "ZZTEST" not in mds._tencent_us_code_cache

    monkeypatch.setattr(
        mds.requests, "get", lambda *a, **k: _TencentResponse({"code": 0, "data": {}})
    )
    assert mds.resolve_tencent_us_kline_code("ZZTEST") is None
    assert mds._tencent_us_code_cache["ZZTEST"] is None  # 正常应答却无数据：确定结论才缓存
    mds._tencent_us_code_cache.pop("ZZTEST", None)


class _QuoteSession:
    def __init__(self, text=None, error=None):
        self._text, self._error = text, error

    def get(self, *args, **kwargs):
        if self._error:
            raise self._error
        session = self

        class R:
            encoding = None
            text = session._text

            def raise_for_status(self):
                return None

        return R()


def test_name_lookup_distinguishes_unknown_code_from_a_failed_call(monkeypatch):
    monkeypatch.setattr(sps, "get_session", lambda: _QuoteSession(text='v_pv_none_match="1";'))
    assert sps.fetch_tencent_quote_name("600000", "A股") is None

    monkeypatch.setattr(
        sps, "get_session", lambda: _QuoteSession(error=requests.ConnectionError("down"))
    )
    with pytest.raises(sps.QuoteNameLookupError):
        sps.fetch_tencent_quote_name("600000", "A股")


def test_catalog_resolve_reports_an_unreachable_source_differently(monkeypatch):
    from app.database import SessionLocal

    def unreachable(symbol, market):
        raise sps.QuoteNameLookupError("down")

    monkeypatch.setattr(catalog, "fetch_tencent_quote_name", unreachable)
    db = SessionLocal()
    try:
        result = catalog.resolve_security(db, symbol="900999", market="B股")
    finally:
        db.close()
    assert "暂时无法访问" in result["error"]


# --------------------------------------------------------------------------- 限速预约


def test_rate_gate_does_not_hold_the_lock_while_sleeping(monkeypatch):
    """hk_mins 的长间隔不得把其他接口一起卡住（PR #298 评审：用非零全局间隔才测得到）。

    此前全局闸只存「最后预约时刻」：hk_mins 预约到 0.6s 后，daily 也得排在它后面、照样
    等 0.6s。按槽位预约后 daily 只需与已预约槽位相距一个全局间隔（0.05s）。"""
    sps.reset_tushare_rate_gate()
    monkeypatch.setattr(sps.settings, "tushare_global_min_interval_seconds", 0.05)
    monkeypatch.setattr(sps.settings, "tushare_hk_min_interval_seconds", 0.6)
    try:
        sps.wait_for_tushare_rate_limit("hk_mins")  # 第一次立即放行并预约
        sleeper = threading.Thread(target=sps.wait_for_tushare_rate_limit, args=("hk_mins",))
        sleeper.start()
        time.sleep(0.1)  # 让它预约到 0.6s 之后并进入等待

        started = time.monotonic()
        sps.wait_for_tushare_rate_limit("daily")
        assert time.monotonic() - started < 0.2
        # 两次 daily 之间仍受全局间隔约束
        started = time.monotonic()
        sps.wait_for_tushare_rate_limit("daily")
        assert time.monotonic() - started >= 0.04
        sleeper.join()
    finally:
        sps.reset_tushare_rate_gate()
