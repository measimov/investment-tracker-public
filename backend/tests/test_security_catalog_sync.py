"""标的全集同步：多源合成、失败隔离、无 token/fatal 跳过、退市翻转不删行、
新鲜度跳过、币种优先级与港交所日报回填。loader 全部打桩，零网络。"""

from datetime import datetime, timedelta, timezone

import pytest

from app.database import SessionLocal
from app.models.security_catalog import SecurityCatalogEntry, SecurityCatalogSync
from app.services import security_catalog_service as svc

from .helpers import reset_tables

RESET_MODELS = [SecurityCatalogEntry, SecurityCatalogSync]


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        reset_tables(session, RESET_MODELS)
        yield session
        session.rollback()
        reset_tables(session, RESET_MODELS)
    finally:
        session.close()


def _spec(source, markets, rows, needs_tushare=False):
    loader = rows if callable(rows) else (lambda: list(rows))
    return svc.LoaderSpec(source, tuple(markets), loader, needs_tushare)


A_ROWS = [
    svc.CatalogRow(symbol="600519", market="A股", name="贵州茅台", pinyin="GZMT", currency="CNY",
                   currency_source="tushare", security_type="stock", board="主板", exchange="SSE",
                   list_status="listed"),
]
FUND_ROWS = [
    svc.CatalogRow(symbol="510300", market="A股", name="沪深300ETF", pinyin="HS300ETF",
                   currency="CNY", currency_source="tushare", security_type="etf", exchange="SSE",
                   list_status="listed", detail={"fund_type": "股票型"}),
]
HKEX_ROWS = [
    svc.CatalogRow(symbol="00700", market="港股", name=None, name_trad="騰訊控股", pinyin=None,
                   security_type="stock", board="主板", exchange="HKEX", list_status="listed",
                   detail={"category": "股本", "isin": "KYG875721634"}),
    svc.CatalogRow(symbol="02800", market="港股", name="盈富基金", name_trad="盈富基金",
                   pinyin="YFJJ", security_type="etf", exchange="HKEX", list_status="listed"),
]
HK_BASIC_ROWS = [
    svc.CatalogRow(symbol="00700", market="港股", name="腾讯控股", name_en="Tencent Holdings Ltd.",
                   pinyin="TXKG", currency="HKD", currency_source="tushare", board="主板",
                   exchange="HKEX", list_status="listed"),
]
US_ROWS = [
    svc.CatalogRow(symbol="AAPL", market="美股", name="苹果", name_en="APPLE INC.", pinyin="PG",
                   currency="USD", currency_source="tushare", security_type="stock", exchange="US",
                   list_status="listed"),
]


def _all_specs(**overrides):
    base = {
        "tushare-stock_basic": _spec("tushare-stock_basic", ["A股"], A_ROWS, True),
        "tushare-fund_basic": _spec("tushare-fund_basic", ["A股"], FUND_ROWS, True),
        "hkex-list": _spec("hkex-list", ["港股"], HKEX_ROWS, False),
        "tushare-hk_basic": _spec("tushare-hk_basic", ["港股"], HK_BASIC_ROWS, True),
        "tushare-us_basic": _spec("tushare-us_basic", ["美股"], US_ROWS, True),
    }
    base.update(overrides)
    return tuple(base.values())


def _entry(db, symbol, market):
    return db.query(SecurityCatalogEntry).filter_by(symbol=symbol, market=market).one()


def test_full_sync_composes_rows_and_is_idempotent(db, monkeypatch):
    monkeypatch.setattr(svc, "LOADERS", _all_specs())
    monkeypatch.setattr(svc, "tushare_available", lambda: True)

    result = svc.sync_security_catalog(db)
    assert [item["status"] for item in result["sources"]] == ["ok"] * 5
    assert db.query(SecurityCatalogEntry).count() == 5  # 00700 两源合成一行

    tencent = _entry(db, "00700", "港股")
    assert tencent.name == "腾讯控股" and tencent.name_trad == "騰訊控股"  # 简体来自 hk_basic，繁体来自 HKEX
    assert tencent.pinyin == "TXKG" and tencent.name_en == "Tencent Holdings Ltd."
    assert tencent.security_type == "stock"  # hk_basic 传 None(unknown) 不覆盖 HKEX 的类型
    assert tencent.currency == "HKD" and tencent.source == "tushare-hk_basic"
    assert tencent.detail == {"category": "股本", "isin": "KYG875721634"}  # JSONB 合并保留
    assert _entry(db, "02800", "港股").security_type == "etf"

    statuses = {row.source: row for row in db.query(SecurityCatalogSync).all()}
    assert statuses["hkex-list"].status == "ok" and statuses["hkex-list"].rows_seen == 2
    assert statuses["hkex-list"].last_success_at is not None
    assert statuses["hkex-list"].markets == ["港股"]

    again = svc.sync_security_catalog(db)
    assert [item["status"] for item in again["sources"]] == ["ok"] * 5
    assert db.query(SecurityCatalogEntry).count() == 5
    health = svc.catalog_health(db)
    assert health["ready"] is True and health["stale"] is False and health["failing_sources"] == []


def test_failed_source_is_isolated_and_keeps_old_rows(db, monkeypatch):
    monkeypatch.setattr(svc, "tushare_available", lambda: True)
    monkeypatch.setattr(svc, "LOADERS", _all_specs())
    svc.sync_security_catalog(db)
    first_success = db.get(SecurityCatalogSync, "hkex-list").last_success_at

    def boom():
        raise RuntimeError("HKEX 503 Service Unavailable")

    monkeypatch.setattr(svc, "LOADERS", _all_specs(**{"hkex-list": _spec("hkex-list", ["港股"], boom)}))
    result = svc.sync_security_catalog(db)
    by_source = {item["source"]: item for item in result["sources"]}
    assert by_source["hkex-list"]["status"] == "failed"
    assert "503" in by_source["hkex-list"]["error"]
    assert by_source["tushare-hk_basic"]["status"] == "ok"
    # 旧行仍在、最近成功时间不动、健康度报出失败源
    assert _entry(db, "02800", "港股").security_type == "etf"
    db.expire_all()
    row = db.get(SecurityCatalogSync, "hkex-list")
    assert row.status == "failed" and row.last_success_at == first_success
    assert svc.catalog_health(db)["failing_sources"] == ["hkex-list"]


def test_no_token_skips_tushare_sources_but_hkex_runs(db, monkeypatch):
    monkeypatch.setattr(svc, "tushare_available", lambda: False)
    monkeypatch.setattr(svc, "LOADERS", _all_specs())
    result = svc.sync_security_catalog(db)
    by_source = {item["source"]: item for item in result["sources"]}
    assert by_source["hkex-list"]["status"] == "ok"
    for source in ("tushare-stock_basic", "tushare-fund_basic", "tushare-hk_basic", "tushare-us_basic"):
        assert by_source[source]["status"] == "skipped" and by_source[source]["reason"] == "no_token"
    assert db.get(SecurityCatalogSync, "tushare-us_basic").detail == {"reason": "no_token"}
    # 只有港股行；健康度仍算 ready（任一源成功）
    assert {row.market for row in db.query(SecurityCatalogEntry).all()} == {"港股"}
    assert svc.catalog_health(db)["ready"] is True


def test_tushare_fatal_error_skips_remaining_tushare_sources(db, monkeypatch):
    monkeypatch.setattr(svc, "tushare_available", lambda: True)

    def dead():
        raise ValueError("抱歉，您没有访问该接口的权限")

    monkeypatch.setattr(
        svc, "LOADERS",
        _all_specs(**{"tushare-stock_basic": _spec("tushare-stock_basic", ["A股"], dead, True)}),
    )
    result = svc.sync_security_catalog(db)
    by_source = {item["source"]: item for item in result["sources"]}
    assert by_source["tushare-stock_basic"]["status"] == "failed"
    assert by_source["tushare-fund_basic"] == {
        "source": "tushare-fund_basic", "rows_seen": 0, "rows_upserted": 0,
        "status": "skipped", "reason": "tushare_fatal",
    }
    assert by_source["tushare-us_basic"]["status"] == "skipped"
    assert by_source["hkex-list"]["status"] == "ok"


def test_delisting_flips_status_without_deleting(db, monkeypatch):
    monkeypatch.setattr(svc, "tushare_available", lambda: True)
    monkeypatch.setattr(svc, "LOADERS", (_spec("tushare-stock_basic", ["A股"], A_ROWS, True),))
    svc.sync_security_catalog(db)
    delisted = [
        svc.CatalogRow(symbol="600519", market="A股", name="贵州茅台", security_type="stock",
                       list_status="delisted"),
    ]
    monkeypatch.setattr(svc, "LOADERS", (_spec("tushare-stock_basic", ["A股"], delisted, True),))
    svc.sync_security_catalog(db)
    db.expire_all()
    entry = _entry(db, "600519", "A股")
    assert entry.list_status == "delisted" and entry.pinyin == "GZMT"  # 未给的列保留
    assert db.query(SecurityCatalogEntry).count() == 1


def test_fresh_sources_are_skipped_unless_forced(db, monkeypatch):
    monkeypatch.setattr(svc, "tushare_available", lambda: True)
    monkeypatch.setattr(svc, "LOADERS", (_spec("hkex-list", ["港股"], HKEX_ROWS),))
    svc.sync_security_catalog(db)

    def must_not_run():
        raise AssertionError("新鲜来源不得重拉")

    monkeypatch.setattr(svc, "LOADERS", (_spec("hkex-list", ["港股"], must_not_run),))
    result = svc.sync_security_catalog(db, force=False)
    assert result["sources"] == [
        {"source": "hkex-list", "rows_seen": 0, "rows_upserted": 0, "status": "skipped", "reason": "fresh"}
    ]
    # 过期后 force=False 也会重拉
    row = db.get(SecurityCatalogSync, "hkex-list")
    row.last_success_at = datetime.now(timezone.utc) - timedelta(hours=999)
    db.commit()
    monkeypatch.setattr(svc, "LOADERS", (_spec("hkex-list", ["港股"], HKEX_ROWS),))
    assert svc.sync_security_catalog(db, force=False)["sources"][0]["status"] == "ok"


def test_periodic_entry_respects_switch_and_freshness(db, monkeypatch):
    monkeypatch.setattr(svc.settings, "security_catalog_sync_enabled", False)
    monkeypatch.setattr(svc, "LOADERS", (_spec("hkex-list", ["港股"], lambda: pytest.fail("开关关闭不得同步")),))
    assert svc.refresh_security_catalog() == 0

    monkeypatch.setattr(svc.settings, "security_catalog_sync_enabled", True)
    monkeypatch.setattr(svc, "LOADERS", (_spec("hkex-list", ["港股"], HKEX_ROWS),))
    assert svc.refresh_security_catalog() == 1
    monkeypatch.setattr(svc, "LOADERS", (_spec("hkex-list", ["港股"], lambda: pytest.fail("新鲜不得重拉")),))
    assert svc.refresh_security_catalog() == 0


def test_official_dayquot_currency_wins_over_later_loaders(db, monkeypatch):
    monkeypatch.setattr(svc, "tushare_available", lambda: True)
    monkeypatch.setattr(svc, "LOADERS", (_spec("hkex-list", ["港股"], HKEX_ROWS),))
    svc.sync_security_catalog(db)

    class Quote:
        def __init__(self, name, currency):
            self.name, self.currency = name, currency

    changed = svc.apply_hk_dayquot_metadata(db, {700: Quote("TENCENT", "HKD"), 2800: Quote("TRACKER FUND", "HKD"), 5: Quote("HSBC", "HKD")})
    assert changed == 2
    db.expire_all()
    tencent = _entry(db, "00700", "港股")
    assert (tencent.currency, tencent.currency_source, tencent.name_en) == ("HKD", "hkex-dayquot", "TENCENT")
    assert svc.apply_hk_dayquot_metadata(db, {700: Quote("TENCENT", "HKD")}) == 0

    # 之后 hk_basic 带着 tushare 的币种来：官方币种不被覆盖，但简体名/拼音照补
    usd_rows = [svc.CatalogRow(symbol="00700", market="港股", name="腾讯控股", pinyin="TXKG",
                               currency="USD", currency_source="tushare", exchange="HKEX",
                               list_status="listed")]
    monkeypatch.setattr(svc, "LOADERS", (_spec("tushare-hk_basic", ["港股"], usd_rows, True),))
    svc.sync_security_catalog(db)
    db.expire_all()
    tencent = _entry(db, "00700", "港股")
    assert (tencent.currency, tencent.currency_source) == ("HKD", "hkex-dayquot")
    assert tencent.name == "腾讯控股" and tencent.name_en == "TENCENT"


def test_catalog_status_lists_never_run_sources(db, monkeypatch):
    monkeypatch.setattr(svc, "tushare_available", lambda: True)
    monkeypatch.setattr(svc, "LOADERS", _all_specs())
    status = svc.catalog_status(db)
    assert [item["status"] for item in status["sources"]] == ["never"] * 5
    assert status["total_rows"] == 0 and status["health"]["ready"] is False
    assert status["coverage_notes"] and any("B股" in note for note in status["coverage_notes"])

    svc.sync_security_catalog(db, sources=["hkex-list"])
    status = svc.catalog_status(db)
    by_source = {item["source"]: item for item in status["sources"]}
    assert by_source["hkex-list"]["status"] == "ok" and by_source["tushare-us_basic"]["status"] == "never"
    assert status["by_market"] == {"港股": 2} and status["total_rows"] == 2


def test_empty_primary_query_marks_source_failed_and_keeps_last_success(db, monkeypatch):
    """走真实 loader：主查询空表 → 该源 failed、error 有值、last_success_at 不前移、旧行保留。"""
    import pandas as pd

    monkeypatch.setattr(svc, "tushare_available", lambda: True)
    good = pd.DataFrame([{"ts_code": "510300.SH", "name": "沪深300ETF", "fund_type": "股票型",
                          "status": "L"}])
    monkeypatch.setattr(svc, "tushare_query", lambda api_name, **kwargs: good)
    monkeypatch.setattr(svc, "LOADERS", (svc.LoaderSpec(
        "tushare-fund_basic", ("A股",), svc.load_tushare_fund_basic, True),))
    first = svc.sync_security_catalog(db)
    assert first["sources"][0]["status"] == "ok"
    success_at = db.get(SecurityCatalogSync, "tushare-fund_basic").last_success_at

    def empty(api_name, **kwargs):
        raise ValueError("tushare fund_basic 返回空数据")

    monkeypatch.setattr(svc, "tushare_query", empty)
    second = svc.sync_security_catalog(db)
    assert second["sources"][0]["status"] == "failed"
    assert "主查询返回空表" in second["sources"][0]["error"]
    db.expire_all()
    row = db.get(SecurityCatalogSync, "tushare-fund_basic")
    assert row.status == "failed" and row.last_success_at == success_at
    assert _entry(db, "510300", "A股").name == "沪深300ETF"
    assert svc.catalog_health(db)["failing_sources"] == ["tushare-fund_basic"]
