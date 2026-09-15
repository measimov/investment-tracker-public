"""手工入口的标的归一化（标的检索见 test_security_catalog_api.py）。

归一化只覆盖手工入口（交易/自选/公司行动的创建与更新）——导入器各有自己的
归一逻辑；Response 序列化刻意不归一（库里的历史形态要如实暴露，不做显示修复）。
"""


import httpx
import pytest

from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.security_catalog import SecurityCatalogEntry
from app.models.transaction import Transaction
from app.models.user import User
from app.models.watchlist_item import WatchlistItem
from app.services.symbol_normalization import normalize_manual_symbol

from .helpers import reset_tables

RESET_MODELS = [WatchlistItem, CorporateAction, Holding, Transaction, SecurityCatalogEntry]


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
def api_user():
    session = SessionLocal()
    try:
        user = session.query(User).filter(User.username == "demo").one()
        original = user.hashed_password
        user.hashed_password = get_password_hash("known-api-password")
        session.commit()
        yield user.id
        user.hashed_password = original
        session.commit()
    finally:
        session.close()


async def _client_auth(client):
    login = await client.post(
        "/api/auth/token",
        json={"username": "demo", "password": "known-api-password"},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("symbol", "market", "expected"),
    [
        ("700", "港股", "00700"),          # 补零：与导入器/行情源的 5 位口径一致
        ("3900", "港股", "03900"),
        ("00700", "港股", "00700"),        # 已是 5 位不动
        (" aapl ", "美股", "AAPL"),        # 去空白 + 大写
        ("brk.b", "美股", "BRK.B"),
        ("600519", "A股", "600519"),       # A股不动
        ("HKHSI", "港股", "HKHSI"),        # 港股非纯数字不补零
        ("123456", "港股", "123456"),      # 超 5 位数字不动（本就非法形态，如实保留）
    ],
)
def test_normalize_manual_symbol(symbol, market, expected):
    assert normalize_manual_symbol(symbol, market) == expected


# --------------------------------------------------------------------------- #
# 三个手工入口 + /known
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_manual_entries_normalize_hk_symbols(db, api_user):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _client_auth(client)

        created = await client.post("/api/transactions", headers=auth, json={
            "symbol": "700", "name": "腾讯控股", "market": "港股",
            "transaction_type": "BUY", "quantity": "100", "price": "300",
            "fee": "10", "currency": "HKD", "transaction_date": "2026-08-01",
        })
        assert created.status_code == 201
        assert created.json()["symbol"] == "00700"

        # 更新路径：只改 market 也要触发归一（误录市场后修正的场景）
        other = await client.post("/api/transactions", headers=auth, json={
            "symbol": "941", "name": "中国移动", "market": "美股",
            "transaction_type": "BUY", "quantity": "100", "price": "50",
            "currency": "HKD", "transaction_date": "2026-08-02",
        })
        fixed = await client.put(
            f"/api/transactions/{other.json()['id']}", headers=auth,
            json={"market": "港股"},
        )
        assert fixed.status_code == 200
        assert fixed.json()["symbol"] == "00941"

        watch = await client.post("/api/watchlist", headers=auth, json={
            "symbol": "1024", "market": "港股", "name": "快手"
        })
        assert watch.status_code in (200, 201)
        assert watch.json()["symbol"] == "01024"


@pytest.mark.anyio
async def test_corporate_action_create_and_update_normalize(db, api_user):
    """公司行动的创建与更新都必须归一（评审 P1：更新路径曾可写回未归一代码）。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _client_auth(client)

        created = await client.post("/api/corporate-actions", headers=auth, json={
            "symbol": "700", "market": "港股", "action_type": "CASH_DIVIDEND",
            "ex_date": "2026-08-01", "dividend_per_share": "1.2",
        })
        assert created.status_code == 201
        assert created.json()["symbol"] == "00700"
        action_id = created.json()["id"]

        # 只改 symbol：patch 值本身要按现有市场归一
        renamed = await client.put(
            f"/api/corporate-actions/{action_id}", headers=auth, json={"symbol": "3900"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["symbol"] == "03900"

        # 只改 market：既有 symbol 也要按新市场重归一（美股误录修正为港股）
        us_row = await client.post("/api/corporate-actions", headers=auth, json={
            "symbol": "941", "market": "美股", "action_type": "CASH_DIVIDEND",
            "ex_date": "2026-08-02", "dividend_per_share": "0.5",
        })
        fixed = await client.put(
            f"/api/corporate-actions/{us_row.json()['id']}", headers=auth,
            json={"market": "港股"},
        )
        assert fixed.status_code == 200
        assert fixed.json()["symbol"] == "00941"
        assert fixed.json()["market"] == "港股"
