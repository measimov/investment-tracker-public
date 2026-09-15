"""/api/securities/search·resolve·catalog-status·catalog-sync 与 /known 兼容包装。"""

from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest

from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.holding import Holding
from app.models.security_catalog import SecurityCatalogEntry, SecurityCatalogSync
from app.models.transaction import Transaction
from app.models.user import User
from app.models.watchlist_item import WatchlistItem
from app.services import security_catalog_service as svc

from .helpers import reset_tables

RESET_MODELS = [WatchlistItem, Holding, Transaction, SecurityCatalogEntry, SecurityCatalogSync]
PASSWORD = "catalog-api-password"


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


@pytest.fixture
def api_users():
    """demo（普通用户）与 admin 都临时改成已知密码，结束后还原。返回 {username: id}。"""
    session = SessionLocal()
    try:
        users = session.query(User).filter(User.username.in_(["demo", "admin"])).all()
        originals = {user.username: user.hashed_password for user in users}
        for user in users:
            user.hashed_password = get_password_hash(PASSWORD)
        session.commit()
        yield {user.username: user.id for user in users}
        for user in users:
            user.hashed_password = originals[user.username]
        session.commit()
    finally:
        session.close()


async def _auth(client, username="demo"):
    login = await client.post("/api/auth/token", json={"username": username, "password": PASSWORD})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _seed_catalog(db):
    rows = [
        svc.CatalogRow(symbol="600519", market="A股", name="贵州茅台", pinyin="GZMT", currency="CNY",
                       currency_source="tushare", security_type="stock", board="主板", exchange="SSE",
                       list_status="listed"),
        svc.CatalogRow(symbol="600518", market="A股", name="康美药业", pinyin="KMYY", currency="CNY",
                       currency_source="tushare", security_type="stock", board="主板", exchange="SSE",
                       list_status="delisted"),
        svc.CatalogRow(symbol="000001", market="A股", name="平安银行", pinyin="PAYH", currency="CNY",
                       currency_source="tushare", security_type="stock", exchange="SZSE",
                       list_status="listed"),
        svc.CatalogRow(symbol="00700", market="港股", name="腾讯控股", name_trad="騰訊控股",
                       name_en="Tencent Holdings Ltd.", pinyin="TXKG", currency="HKD",
                       currency_source="tushare", security_type="stock", board="主板", exchange="HKEX",
                       list_status="listed"),
        svc.CatalogRow(symbol="02800", market="港股", name="盈富基金", name_trad="盈富基金",
                       pinyin="YFJJ", security_type="etf", exchange="HKEX", list_status="listed"),
    ]
    svc.upsert_catalog_rows(db, rows, source="test-seed")
    db.add(SecurityCatalogSync(
        source="hkex-list", markets=["港股"], status="ok",
        last_success_at=datetime.now(timezone.utc), rows_seen=5, rows_upserted=5, detail={},
    ))
    db.commit()


@pytest.mark.anyio
async def test_search_merges_ledger_sources_and_ranks_them_first(db, api_users):
    uid = api_users["demo"]
    db.add(Holding(
        user_id=uid, symbol="00700", name="腾讯控股", market="港股",
        quantity=Decimal("100"), avg_cost=Decimal("300"), total_cost=Decimal("30000"), currency="HKD",
    ))
    db.add(WatchlistItem(user_id=uid, symbol="09988", market="港股", name="阿里巴巴"))
    for symbol, name, market, when in [
        ("00700", "腾讯控股", "港股", date(2026, 8, 1)),
        ("600519", "贵州茅台", "A股", date(2026, 7, 1)),
        ("00883", "中国海洋石油", "港股", date(2026, 8, 20)),
    ]:
        db.add(Transaction(
            user_id=uid, symbol=symbol, name=name, market=market, transaction_type="BUY",
            quantity=Decimal("10"), price=Decimal("1"), fee=Decimal("0"), currency="HKD",
            transaction_date=when,
        ))
    db.commit()

    async with _client() as client:
        auth = await _auth(client)
        empty = (await client.get("/api/securities/search", headers=auth)).json()
        items = empty["items"]
        by_symbol = {item["symbol"]: item for item in items}
        assert sorted(by_symbol["00700"]["origins"]) == ["history", "holding"]
        assert by_symbol["00700"]["name"] == "腾讯控股" and by_symbol["00700"]["in_catalog"] is False
        assert by_symbol["09988"]["origins"] == ["watchlist"]
        assert items[0]["symbol"] == "00700"  # 持仓优先
        assert empty["catalog"]["ready"] is False  # 目录尚未同步：显式降级，只出账本行
        assert empty["catalog"]["capabilities"].keys() == {"pinyin", "simplified"}

        prefix = (await client.get("/api/securities/search?q=008", headers=auth)).json()
        assert [item["symbol"] for item in prefix["items"]] == ["00883"]
        by_name = (await client.get("/api/securities/search?q=茅台", headers=auth)).json()
        assert [item["symbol"] for item in by_name["items"]] == ["600519"]
        miss = (await client.get("/api/securities/search?q=zzz", headers=auth)).json()
        assert miss["items"] == []
        only_hk = (await client.get("/api/securities/search?market=港股", headers=auth)).json()
        assert {item["market"] for item in only_hk["items"]} == {"港股"}


@pytest.mark.anyio
async def test_search_catalog_ranking_and_filters(db, api_users):
    _seed_catalog(db)
    async with _client() as client:
        auth = await _auth(client)

        async def symbols(query):
            response = await client.get(f"/api/securities/search?{query}", headers=auth)
            assert response.status_code == 200, response.text
            return [item["symbol"] for item in response.json()["items"]]

        assert await symbols("q=6005") == ["600519", "600518"]  # 代码前缀；在市先于退市
        assert await symbols("q=gzmt") == ["600519"]  # 拼音缩写不分大小写
        assert await symbols("q=腾讯") == ["00700"]  # 简体
        assert await symbols("q=騰訊") == ["00700"]  # 繁体
        assert await symbols("q=tencent") == ["00700"]  # 英文名
        assert await symbols("q=0&market=港股") == ["00700", "02800"]
        assert await symbols("q=平安") == ["000001"]
        assert await symbols("q=") == []  # 空查询不翻目录

        item = (await client.get("/api/securities/search?q=600519", headers=auth)).json()["items"][0]
        assert item == {
            "symbol": "600519", "market": "A股", "name": "贵州茅台", "name_en": None,
            "name_trad": None, "pinyin": "GZMT", "currency": "CNY", "security_type": "stock",
            "board": "主板", "exchange": "SSE", "list_status": "listed", "in_catalog": True,
            "origins": [], "last_used": None,
        }
        assert (await client.get("/api/securities/search?q=600519", headers=auth)).json()["catalog"]["ready"] is True

        assert (await client.get("/api/securities/search?limit=51", headers=auth)).status_code == 422
        assert (await client.get("/api/securities/search?market=火星", headers=auth)).status_code == 422
        # LIKE 通配符按字面处理
        assert await symbols("q=%") == []


@pytest.mark.anyio
async def test_search_ledger_row_is_enriched_from_catalog_once(db, api_users):
    _seed_catalog(db)
    uid = api_users["demo"]
    db.add(Holding(
        user_id=uid, symbol="00700", name=None, market="港股",
        quantity=Decimal("100"), avg_cost=Decimal("300"), total_cost=Decimal("30000"), currency="HKD",
    ))
    db.commit()
    async with _client() as client:
        auth = await _auth(client)
        items = (await client.get("/api/securities/search?q=00700", headers=auth)).json()["items"]
        assert len(items) == 1
        assert items[0]["origins"] == ["holding"] and items[0]["in_catalog"] is True
        assert items[0]["name"] == "腾讯控股" and items[0]["pinyin"] == "TXKG"
        assert items[0]["security_type"] == "stock" and items[0]["currency"] == "HKD"


@pytest.mark.anyio
async def test_resolve_uses_catalog_then_tencent_and_persists(db, api_users, monkeypatch):
    _seed_catalog(db)
    calls = []

    def fake_name(symbol, market):
        calls.append((symbol, market))
        return {"900926": "宝信Ｂ"}.get(symbol)

    monkeypatch.setattr(svc, "fetch_tencent_quote_name", fake_name)
    async with _client() as client:
        auth = await _auth(client)
        hit = (await client.get("/api/securities/resolve?symbol=600519&market=A股", headers=auth)).json()
        assert hit["resolved_from"] == "catalog" and hit["name"] == "贵州茅台" and calls == []

        b_share = await client.get("/api/securities/resolve?symbol=900926&market=B股", headers=auth)
        payload = b_share.json()
        assert payload["resolved_from"] == "tencent-quote"
        assert payload["name"] == "宝信B" and payload["currency"] == "USD"  # 全角 Ｂ 归一
        assert payload["security_type"] == "unknown" and payload["list_status"] == "unknown"
        assert payload["in_catalog"] is True and payload["error"] is None
        row = db.query(SecurityCatalogEntry).filter_by(symbol="900926", market="B股").one()
        assert row.source == "tencent-quote" and row.exchange == "SSE"
        assert row.pinyin == ("BXB" if svc.PINYIN_AVAILABLE else None)

        again = (await client.get("/api/securities/resolve?symbol=900926&market=B股", headers=auth)).json()
        assert again["resolved_from"] == "catalog" and calls == [("900926", "B股")]

        miss = (await client.get("/api/securities/resolve?symbol=999999&market=A股", headers=auth)).json()
        assert miss["name"] is None and miss["resolved_from"] is None and "腾讯行情" in miss["error"]
        assert db.query(SecurityCatalogEntry).filter_by(symbol="999999").count() == 0

        sg = (await client.get("/api/securities/resolve?symbol=d05&market=新加坡股", headers=auth)).json()
        assert sg["symbol"] == "D05" and sg["name"] is None and "手工" in sg["error"]
        assert ("D05", "新加坡股") not in calls

        hk = (await client.get("/api/securities/resolve?symbol=700&market=港股", headers=auth)).json()
        assert hk["symbol"] == "00700" and hk["resolved_from"] == "catalog"  # 归一化后命中目录

        assert (await client.get("/api/securities/resolve?symbol=1&market=火星", headers=auth)).status_code == 422


@pytest.mark.anyio
async def test_catalog_status_and_admin_sync_trigger(db, api_users, monkeypatch):
    started = []
    monkeypatch.setattr(svc, "run_sync_in_background", lambda **kwargs: started.append(kwargs))
    async with _client() as client:
        auth = await _auth(client)
        status = (await client.get("/api/securities/catalog-status", headers=auth)).json()
        assert [item["status"] for item in status["sources"]] == ["never"] * len(svc.LOADERS)
        assert status["total_rows"] == 0 and status["health"]["ready"] is False
        assert status["coverage_notes"]

        assert (await client.post("/api/securities/catalog-sync", headers=auth)).status_code == 403

        admin = await _auth(client, "admin")
        accepted = await client.post("/api/securities/catalog-sync?market=港股", headers=admin)
        assert accepted.status_code == 202
        assert accepted.json() == {"started": True, "sources": ["hkex-list", "tushare-hk_basic"]}
        assert (await client.post("/api/securities/catalog-sync?market=火星", headers=admin)).status_code == 422
    assert started == [{"markets": ["港股"], "force": True}]
