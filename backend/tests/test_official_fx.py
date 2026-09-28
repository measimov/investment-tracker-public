"""#200 官方汇率源：中国货币网人民币汇率中间价为主源，第三方只比对/兜底。

解析层用真实响应裁剪的金样（`fixtures/chinamoney/ccpr_hist.json`，2026-09 中秋 09-25
无发布）；刷新编排把两路外呼都打桩，只验证写库口径：官方覆盖的区间以官方为准、
周末残留的第三方行停用、手工行不动、官方持续失败才降级写第三方。
"""

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models.exchange_rate import ExchangeRate, ExchangeRateCheck
from app.services import chinamoney_source, exchange_rate_service as fx
from app.services.chinamoney_source import CcprRow, ChinamoneyError

FIXTURE = Path(__file__).parent / "fixtures" / "chinamoney" / "ccpr_hist.json"
TODAY = date(2026, 9, 28)  # 周一


def _payload():
    return json.loads(FIXTURE.read_text())


def test_parse_ccpr_history_maps_values_by_searchlist_and_sorts():
    rows = chinamoney_source.parse_ccpr_history(_payload())
    assert [r.rate_date for r in rows] == [
        date(2026, 9, 17), date(2026, 9, 18), date(2026, 9, 21),
        date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24),
    ]
    assert rows[-1].rates == {
        "USD": Decimal("6.7489"), "HKD": Decimal("0.86054"), "SGD": Decimal("5.2598"),
    }


def test_parse_ccpr_history_uses_searchlist_order_not_request_order():
    payload = _payload()
    payload["data"]["searchlist"] = ["SGD/CNY", "USD/CNY"]
    payload["records"] = [{"date": "2026-09-24", "values": ["5.2598", "---"]}]
    rows = chinamoney_source.parse_ccpr_history(payload)
    # USD 缺值（---）不出现，SGD 按 searchlist 第 0 位取
    assert rows == [CcprRow(date(2026, 9, 24), {"SGD": Decimal("5.2598")})]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["head"].update(rep_code="500", rep_message="系统繁忙"),
        lambda p: p.pop("records"),
        # 区间超过一年：上游拒绝而不是没有中间价
        lambda p: (p.update(records=[]), p["data"].update(flagMessage="只提供一年历史数据查询及下载")),
        lambda p: p["data"].update(searchlist=["EUR/CNY"]),
    ],
)
def test_parse_ccpr_history_rejects_error_payloads(mutate):
    payload = _payload()
    mutate(payload)
    with pytest.raises(ChinamoneyError):
        chinamoney_source.parse_ccpr_history(payload)


def test_query_windows_split_by_one_year():
    windows = chinamoney_source.query_windows(date(2024, 1, 1), date(2026, 9, 28))
    assert windows[0] == (date(2024, 1, 1), date(2024, 12, 30))
    assert windows[-1][1] == date(2026, 9, 28)
    assert all((end - start).days < 365 for start, end in windows)
    assert chinamoney_source.query_windows(date(2026, 1, 2), date(2026, 1, 1)) == []


@pytest.mark.parametrize(
    "now,expected",
    [
        (datetime(2026, 9, 28, 8, 0), date(2026, 9, 25)),  # 周一开盘前 → 上周五
        (datetime(2026, 9, 28, 10, 0), date(2026, 9, 28)),
        (datetime(2026, 9, 27, 15, 0), date(2026, 9, 25)),  # 周日 → 周五
        (datetime(2026, 9, 26, 9, 0), date(2026, 9, 25)),  # 周六
    ],
)
def test_expected_official_date_skips_weekends_and_pre_publish(now, expected):
    assert fx.expected_official_date(now) == expected


@pytest.fixture
def db():
    session = SessionLocal()
    session.query(ExchangeRateCheck).delete()
    session.query(ExchangeRate).delete()
    session.commit()
    yield session
    session.rollback()
    session.query(ExchangeRateCheck).delete()
    session.query(ExchangeRate).delete()
    session.commit()
    session.close()


def _rate(db, currency, day, rate, source):
    db.add(ExchangeRate(
        from_currency=currency, to_currency="CNY", rate=Decimal(rate),
        effective_date=day, source=source, is_active=True,
    ))
    db.commit()


def _rows(db, currency="USD"):
    return {
        r.effective_date: (r.source, Decimal(str(r.rate)), r.is_active)
        for r in db.query(ExchangeRate).filter(ExchangeRate.from_currency == currency)
    }


@pytest.fixture
def patched(monkeypatch):
    state = {"official": chinamoney_source.parse_ccpr_history(_payload()), "official_error": None,
             "third": ("api-ecb", {"USD": Decimal("6.7600"), "HKD": Decimal("0.8610"),
                                   "SGD": Decimal("5.2700")})}

    def fake_ccpr(start, end, currencies=None):
        if state["official_error"]:
            raise state["official_error"]
        return [r for r in state["official"] if start <= r.rate_date <= end]

    def fake_third():
        if state["third"] is None:
            raise RuntimeError("第三方汇率源全部失败")
        return state["third"]

    monkeypatch.setattr(chinamoney_source, "fetch_ccpr_history", fake_ccpr)
    monkeypatch.setattr(fx, "_fetch_third_party_quotes", fake_third)
    monkeypatch.setattr(fx, "local_today", lambda: TODAY)
    return state


def test_refresh_writes_official_and_retires_third_party_weekend_rows(db, patched):
    # 旧口径每天写第三方：发布日的行被官方改写，周末/中秋的行停用，手工行不动
    _rate(db, "USD", date(2026, 9, 24), "6.7700", "api-ecb")
    _rate(db, "USD", date(2026, 9, 26), "6.7710", "api-ecb")  # 周六
    _rate(db, "USD", date(2026, 9, 25), "6.7705", "api-ecb")  # 中秋无中间价
    _rate(db, "USD", date(2026, 9, 23), "6.7000", "manual")
    _rate(db, "USD", date(2026, 9, 1), "6.8000", "api-ecb")  # 回看窗口之外：不碰

    current = fx.fetch_latest_rates_from_api(db)

    assert current == {"USD": Decimal("6.7489"), "HKD": Decimal("0.86054"), "SGD": Decimal("5.2598")}
    usd = _rows(db)
    assert usd[date(2026, 9, 24)] == ("cfets-ccpr", Decimal("6.7489"), True)
    assert usd[date(2026, 9, 26)][2] is False and usd[date(2026, 9, 25)][2] is False
    assert usd[date(2026, 9, 23)] == ("manual", Decimal("6.7000"), True)
    assert usd[date(2026, 9, 1)] == ("api-ecb", Decimal("6.8000"), True)
    # 按日查找与「最新汇率」都回到官方值
    assert fx.get_latest_rate(db, "USD") == Decimal("6.7489")

    checks = {c.from_currency: c for c in db.query(ExchangeRateCheck)}
    assert set(checks) == {"USD", "HKD", "SGD"}
    usd_check = checks["USD"]
    assert usd_check.check_date == TODAY and usd_check.official_date == date(2026, 9, 24)
    assert Decimal(str(usd_check.diff_pct)) == ((Decimal("6.7600") / Decimal("6.7489") - 1) * 100).quantize(Decimal("0.0001"))
    # 第三方报价只进比对表，不写 exchange_rates
    assert TODAY not in usd

    # 重复刷新幂等：比对行覆盖不新增，官方行不重复写
    fx.fetch_latest_rates_from_api(db)
    assert db.query(ExchangeRateCheck).count() == 3


def test_official_failure_within_stale_window_does_not_write_third_party(db, patched):
    _rate(db, "USD", date(2026, 9, 24), "6.7489", "cfets-ccpr")
    patched["official_error"] = ChinamoneyError("系统繁忙")
    current = fx.fetch_latest_rates_from_api(db)
    assert current["USD"] == Decimal("6.7489")
    assert TODAY not in _rows(db)
    # 其余币种从未有官方值 → 直接降级写第三方
    assert _rows(db, "HKD")[TODAY] == ("api-ecb", Decimal("0.8610"), True)


def test_fallback_rows_after_stale_official_are_kept_by_later_official_sync(db, patched):
    # 降级期写下的第三方行：官方恢复但最近一期仍陈旧时（例如上游只回了很旧的数据）不作废
    patched["official"] = [CcprRow(date(2026, 9, 10), {"USD": Decimal("6.8000")})]
    _rate(db, "USD", date(2026, 9, 27), "6.7600", "api-ecb")
    fx.fetch_latest_rates_from_api(db)
    assert _rows(db)[date(2026, 9, 27)] == ("api-ecb", Decimal("6.7600"), True)


def test_official_stale_beyond_window_falls_back_to_third_party(db, patched):
    _rate(db, "USD", date(2026, 9, 10), "6.8000", "cfets-ccpr")  # 18 天前
    patched["official_error"] = ChinamoneyError("系统繁忙")
    current = fx.fetch_latest_rates_from_api(db)
    assert current["USD"] == Decimal("6.7600")
    assert _rows(db)[TODAY] == ("api-ecb", Decimal("6.7600"), True)
    warnings = fx.fx_source_warnings(db)
    assert any("USD/CNY 当前使用第三方报价（api-ecb" in w for w in warnings)


def test_both_sources_down_returns_existing_official_only(db, patched):
    patched["official_error"] = ChinamoneyError("down")
    patched["third"] = None
    assert fx.fetch_latest_rates_from_api(db) == {}
    _rate(db, "USD", date(2026, 9, 24), "6.7489", "cfets-ccpr")
    assert fx.fetch_latest_rates_from_api(db) == {"USD": Decimal("6.7489")}


def test_large_diff_surfaces_as_data_quality_warning(db, patched):
    # 常态差异（即期与中间价差 0.5%~1.5%）不告警：阈值取中间价 ±2% 的波动区间
    patched["third"] = ("api-ecb", {"USD": Decimal("6.8500")})  # +1.50%
    fx.fetch_latest_rates_from_api(db)
    assert fx.fx_source_warnings(db) == []

    patched["third"] = ("api-ecb", {"USD": Decimal("6.9000")})  # +2.24%
    fx.fetch_latest_rates_from_api(db)
    warnings = fx.fx_source_warnings(db)
    assert any("USD/CNY 第三方报价（api-ecb）与官方中间价（2026-09-24）相差 +2.24%" in w for w in warnings)
    assert not any("HKD" in w for w in warnings)
