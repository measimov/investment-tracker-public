"""腾讯 K 线兜底源：Tushare 覆盖空洞（B 股/可转债/港股 ETF·REIT）的历史行情补齐。"""

from datetime import date
from decimal import Decimal

import pandas as pd

from app.database import SessionLocal
from app.models.security_price import SecurityPrice
from app.services import market_data_service as mds


def test_tencent_kline_code_mapping():
    assert mds.to_tencent_kline_code("900926", "B股") == "sh900926"
    assert mds.to_tencent_kline_code("200596", "B股") == "sz200596"
    assert mds.to_tencent_kline_code("123266", "A股") == "sz123266"
    assert mds.to_tencent_kline_code("823", "港股") == "hk00823"
    assert mds.to_tencent_kline_code("PCT", "新加坡股") is None
    assert mds.to_tencent_kline_code("AAPL", "美股") is None
    assert mds.to_tencent_kline_code("", "A股") is None


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _tencent_payload(code, candles):
    return {"code": 0, "data": {code: {"day": candles}}}


def test_fallback_stores_rows_when_tushare_returns_empty(monkeypatch):
    """Tushare 空返回（覆盖空洞）时走腾讯兜底，不复权日线入库 source=tencent-kline。"""
    monkeypatch.setattr(
        mds, "_tushare_history_query", lambda *a, **k: pd.DataFrame()
    )
    candles = [
        ["2025-11-03", "1.08", "1.094", "1.10", "1.07", "20248"],
        ["2025-11-04", "1.09", "1.101", "1.11", "1.08", "18000"],
    ]
    monkeypatch.setattr(
        mds.requests,
        "get",
        lambda *a, **k: _FakeResponse(_tencent_payload("sh900926", candles)),
    )

    db = SessionLocal()
    try:
        db.query(SecurityPrice).filter_by(symbol="900926", market="B股").delete()
        db.commit()
        result = mds.fetch_and_store_security_price_history(
            db,
            symbol="900926",
            market="B股",
            start_date=date(2025, 11, 1),
            end_date=date(2025, 11, 30),
        )
        assert result["success"] is True
        assert result["rows"] == 2
        assert result["source"] == "tencent-kline"

        stored = (
            db.query(SecurityPrice)
            .filter_by(symbol="900926", market="B股")
            .order_by(SecurityPrice.price_date)
            .all()
        )
        assert [row.price_date for row in stored] == [date(2025, 11, 3), date(2025, 11, 4)]
        assert stored[0].close_price == Decimal("1.094")
        assert stored[0].currency == "CNY"
        assert stored[0].adj_close_price is None  # 不复权源不产复权价
    finally:
        db.query(SecurityPrice).filter_by(symbol="900926", market="B股").delete()
        db.commit()
        db.close()


def test_double_empty_short_range_is_success(monkeypatch):
    """短区间（≤7 天）双空 = 假日无交易日，按既有外部源约定算成功。"""
    monkeypatch.setattr(
        mds, "_tushare_history_query", lambda *a, **k: pd.DataFrame()
    )
    monkeypatch.setattr(
        mds.requests,
        "get",
        lambda *a, **k: _FakeResponse({"code": 0, "data": {}}),
    )
    db = SessionLocal()
    try:
        result = mds.fetch_and_store_security_price_history(
            db,
            symbol="200596",
            market="B股",
            start_date=date(2026, 3, 1),
            end_date=date(2026, 3, 5),
        )
        assert result["success"] is True
        assert result["rows"] == 0
        assert result["coverage_status"] == "no_data"
    finally:
        db.close()


def test_double_empty_long_range_is_uncovered_failure(monkeypatch):
    """长区间双空必须判失败（uncovered）：否则增量同步会把数据源缺口
    静默计成已覆盖——正是"同步 255 ok 却一只没补"的根因形态。"""
    monkeypatch.setattr(
        mds, "_tushare_history_query", lambda *a, **k: pd.DataFrame()
    )
    monkeypatch.setattr(
        mds.requests,
        "get",
        lambda *a, **k: _FakeResponse({"code": 0, "data": {}}),
    )
    db = SessionLocal()
    try:
        result = mds.fetch_and_store_security_price_history(
            db,
            symbol="200596",
            market="B股",
            start_date=date(2026, 3, 1),
            end_date=date(2026, 3, 31),
        )
        assert result["success"] is False
        assert result["rows"] == 0
        assert result["coverage_status"] == "uncovered"
    finally:
        db.close()


def test_backward_pagination_fetches_beyond_single_page(monkeypatch):
    """腾讯返回区间内【最近】的 640 根（实测形状）：必须从 end 向前翻页，
    否则更早行情被静默丢弃。构造两页数据验证完整取回。"""
    from datetime import timedelta

    start = date(2020, 1, 1)
    # 造 700 个连续"交易日"（跳过周末简化为连续日），末段 640 根为第一页
    all_days = [start + timedelta(days=i) for i in range(700)]
    candles = [
        [d.isoformat(), "1.0", f"{1 + i * 0.001:.3f}", "1.1", "0.9", "100"]
        for i, d in enumerate(all_days)
    ]

    def fake_get(url, params=None, **kwargs):
        # param: code,day,start,end,640,
        _, _, req_start, req_end, _, _ = params["param"].split(",")
        s, e = date.fromisoformat(req_start), date.fromisoformat(req_end)
        in_range = [c for c in candles if s <= date.fromisoformat(c[0]) <= e]
        page = in_range[-mds.TENCENT_KLINE_MAX_CANDLES:]  # 最近 N 根 —— 真实截断方向
        return _FakeResponse(_tencent_payload("sh900926", page))

    monkeypatch.setattr(mds.requests, "get", fake_get)

    rows = mds._fetch_tencent_kline_rows("sh900926", all_days[0], all_days[-1])

    assert len(rows) == 700  # 两页取齐，无静默丢失
    assert rows[0]["trade_date"] == all_days[0].strftime("%Y%m%d")
    assert rows[-1]["trade_date"] == all_days[-1].strftime("%Y%m%d")
    dates = [row["trade_date"] for row in rows]
    assert dates == sorted(dates) and len(set(dates)) == 700


def test_fallback_kicks_in_when_tushare_raises(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("tushare down")

    monkeypatch.setattr(mds, "_tushare_history_query", _boom)
    candles = [["2026-04-07", "142.0", "143.0", "144.0", "141.0", "999"]]
    monkeypatch.setattr(
        mds.requests,
        "get",
        lambda *a, **k: _FakeResponse(_tencent_payload("sz123266", candles)),
    )
    db = SessionLocal()
    try:
        db.query(SecurityPrice).filter_by(symbol="123266", market="A股").delete()
        db.commit()
        result = mds.fetch_and_store_security_price_history(
            db,
            symbol="123266",
            market="A股",
            start_date=date(2026, 4, 1),
            end_date=date(2026, 4, 15),
        )
        assert result["success"] is True
        assert result["rows"] == 1
        assert result["source"] == "tencent-kline"
    finally:
        db.query(SecurityPrice).filter_by(symbol="123266", market="A股").delete()
        db.commit()
        db.close()



def test_us_history_falls_back_to_tencent_when_tushare_has_no_permission(monkeypatch):
    """Tushare us_daily_adj 无权限（当前积分档）→ 探测腾讯美股代码（.OQ 命中）→ 不复权日线入库。
    此前美股没有任何兜底源，历史同步必失败并让整次任务按「配额受限」中止。"""
    def _no_permission(*a, **k):
        raise RuntimeError("抱歉，您没有接口(us_daily_adj)访问权限")

    monkeypatch.setattr(mds, "_tushare_history_query", _no_permission)
    monkeypatch.setattr(mds, "_tencent_us_code_cache", {})
    requested = []
    candles = [["2026-09-24", "77.0", "78.1", "79.0", "76.5", "100"], ["2026-09-25", "78.2", "77.57", "79.1", "77.0", "90"]]

    def fake_get(url, params=None, **kwargs):
        code = params["param"].split(",")[0]
        requested.append(code)
        if code == "usPDD.OQ":
            return _FakeResponse(_tencent_payload(code, candles))
        return _FakeResponse({"code": 0, "data": {code: {}}})

    monkeypatch.setattr(mds.requests, "get", fake_get)
    db = SessionLocal()
    try:
        db.query(SecurityPrice).filter_by(symbol="PDD", market="美股").delete()
        db.commit()
        result = mds.fetch_and_store_security_price_history(
            db, symbol="PDD", market="美股", start_date=date(2026, 9, 20), end_date=date(2026, 9, 26),
        )
        assert result["success"] is True and result["rows"] == 2
        assert result["source"] == "tencent-kline"
        stored = db.query(SecurityPrice).filter_by(symbol="PDD", market="美股").order_by(SecurityPrice.price_date).all()
        assert stored[-1].close_price == Decimal("77.57") and stored[-1].currency == "USD"
        assert requested[0] == "usPDD.OQ"  # 先探测交易所后缀
    finally:
        db.query(SecurityPrice).filter_by(symbol="PDD", market="美股").delete()
        db.commit()
        db.close()


def test_us_code_probe_tries_exchanges_and_caches(monkeypatch):
    monkeypatch.setattr(mds, "_tencent_us_code_cache", {})
    calls = []

    def fake_get(url, params=None, **kwargs):
        code = params["param"].split(",")[0]
        calls.append(code)
        rows = [["2026-09-25", "1", "2", "3", "0.5", "1"]] if code == "usBABA.N" else []
        return _FakeResponse({"code": 0, "data": {code: {"day": rows}}})

    monkeypatch.setattr(mds.requests, "get", fake_get)
    assert mds.resolve_tencent_us_kline_code("baba") == "usBABA.N"
    assert calls == ["usBABA.OQ", "usBABA.N"]
    assert mds.resolve_tencent_us_kline_code("BABA") == "usBABA.N" and len(calls) == 2  # 缓存
    assert mds.resolve_tencent_us_kline_code("ZZZZ") is None
    assert mds.resolve_tencent_us_kline_code("") is None
