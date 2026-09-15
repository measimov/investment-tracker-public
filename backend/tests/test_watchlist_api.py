"""观察清单 API：CRUD、唯一约束、所有权隔离与格雷厄姆摘要聚合。"""

import httpx
import pytest

from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.security_profile import SecurityProfileData
from app.models.user import User
from app.models.watchlist_item import WatchlistItem


@pytest.fixture
def api_users():
    db = SessionLocal()
    try:
        demo = db.query(User).filter(User.username == "demo").one()
        admin = db.query(User).filter(User.username == "admin").one()
        originals = {u.id: u.hashed_password for u in (demo, admin)}
        for u in (demo, admin):
            u.hashed_password = get_password_hash("watchlist-api-password")
        db.query(WatchlistItem).delete()
        db.query(SecurityProfileData).filter(
            SecurityProfileData.symbol.like("600WATCH%")
        ).delete(synchronize_session=False)
        db.commit()
        yield
        for u in (demo, admin):
            u.hashed_password = originals[u.id]
        db.query(WatchlistItem).delete()
        db.query(SecurityProfileData).filter(
            SecurityProfileData.symbol.like("600WATCH%")
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


async def _token(client, username):
    response = await client.post(
        "/api/auth/token",
        json={"username": username, "password": "watchlist-api-password"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


@pytest.mark.anyio
async def test_watchlist_crud_unique_and_ownership(api_users):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        user_auth = {"Authorization": f"Bearer {await _token(client, 'demo')}"}
        admin_auth = {"Authorization": f"Bearer {await _token(client, 'admin')}"}

        created = await client.post(
            "/api/watchlist",
            json={"symbol": "600WATCH", "market": "A股", "note": "观察理由"},
            headers=user_auth,
        )
        assert created.status_code == 201
        item = created.json()
        assert item["symbol"] == "600WATCH"
        # 库内无该标的档案数据 → 摘要为 None，而不是 0/7 的误导计数
        assert item["graham_summary"] is None

        # 重复加入：唯一约束 → 409
        duplicate = await client.post(
            "/api/watchlist",
            json={"symbol": "600WATCH", "market": "A股"},
            headers=user_auth,
        )
        assert duplicate.status_code == 409

        # 非法市场被 schema 拒绝
        bad_market = await client.post(
            "/api/watchlist",
            json={"symbol": "X", "market": "火星"},
            headers=user_auth,
        )
        assert bad_market.status_code == 422

        # 所有权隔离：admin 看不到、也改不了 demo 的条目
        admin_list = await client.get("/api/watchlist", headers=admin_auth)
        assert admin_list.json() == []
        stranger_update = await client.put(
            f"/api/watchlist/{item['id']}", json={"note": "越权"}, headers=admin_auth
        )
        assert stranger_update.status_code == 404

        updated = await client.put(
            f"/api/watchlist/{item['id']}",
            json={"note": "更新后的理由", "name": "观察标的"},
            headers=user_auth,
        )
        assert updated.status_code == 200
        assert updated.json()["note"] == "更新后的理由"

        listed = await client.get("/api/watchlist", headers=user_auth)
        assert [row["id"] for row in listed.json()] == [item["id"]]

        removed = await client.delete(f"/api/watchlist/{item['id']}", headers=user_auth)
        assert removed.status_code == 200
        assert (await client.get("/api/watchlist", headers=user_auth)).json() == []


@pytest.mark.anyio
async def test_graham_summary_appears_when_profile_data_exists(api_users):
    db = SessionLocal()
    try:
        # 最小档案：一年利润+资产负债行 → graham_screen 可算出 ok 结果
        for dataset, payload in (
            ("income", {"end_date": "20251231", "n_income_attr_p": 100.0, "basic_eps": 1.0}),
            (
                "balancesheet",
                {
                    "end_date": "20251231",
                    "total_cur_assets": 500.0,
                    "total_cur_liab": 100.0,
                    "total_assets": 1000.0,
                    "total_liab": 300.0,
                    "money_cap": 200.0,
                    "lt_borr": 50.0,
                },
            ),
        ):
            db.add(SecurityProfileData(
                symbol="600WATCH", market="A股", dataset=dataset,
                period_key="20251231", payload=payload,
            ))
        db.commit()
    finally:
        db.close()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        user_auth = {"Authorization": f"Bearer {await _token(client, 'demo')}"}
        created = await client.post(
            "/api/watchlist",
            json={"symbol": "600WATCH", "market": "A股"},
            headers=user_auth,
        )
        assert created.status_code == 201
        summary = created.json()["graham_summary"]
        assert summary is not None
        assert summary["total"] == 7
        assert summary["as_of_year"] == "2025"
        assert summary["passed"] + summary["failed"] + summary["indeterminate"] == 7


@pytest.mark.anyio
async def test_symbol_is_normalized_uppercase_and_case_dupes_409(api_users):
    """[评审 P1] symbol 写入前规范为大写：aapl/AAPL 视为同一标的（409），
    小写请求也能命中既有大写档案的准则摘要。"""
    db = SessionLocal()
    try:
        db.add(SecurityProfileData(
            symbol="600WATCH", market="A股", dataset="income", period_key="20251231",
            payload={"end_date": "20251231", "n_income_attr_p": 100.0, "basic_eps": 1.0},
        ))
        db.add(SecurityProfileData(
            symbol="600WATCH", market="A股", dataset="balancesheet", period_key="20251231",
            payload={"end_date": "20251231", "total_cur_assets": 500.0, "total_cur_liab": 100.0,
                     "total_assets": 1000.0, "total_liab": 300.0, "money_cap": 200.0,
                     "lt_borr": 50.0},
        ))
        db.commit()
    finally:
        db.close()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        user_auth = {"Authorization": f"Bearer {await _token(client, 'demo')}"}
        lower = await client.post(
            "/api/watchlist", json={"symbol": " 600watch ", "market": "A股"}, headers=user_auth,
        )
        assert lower.status_code == 201
        assert lower.json()["symbol"] == "600WATCH"
        # 小写写入也命中大写档案 → 摘要非空
        assert lower.json()["graham_summary"] is not None
        # 大小写重复 → 唯一约束 409
        upper = await client.post(
            "/api/watchlist", json={"symbol": "600WATCH", "market": "A股"}, headers=user_auth,
        )
        assert upper.status_code == 409
        # membership 端点对小写查询同样命中
        contains = await client.get(
            "/api/watchlist/contains", params={"symbol": "600watch", "market": "A股"},
            headers=user_auth,
        )
        assert contains.status_code == 200
        assert contains.json()["watching"] is True
        absent = await client.get(
            "/api/watchlist/contains", params={"symbol": "NOPE", "market": "A股"},
            headers=user_auth,
        )
        assert absent.json()["watching"] is False


@pytest.mark.anyio
async def test_list_summaries_batched_not_per_item(api_users):
    """[评审 P2] 列表摘要必须批量聚合：N 条观察项不得产生 3-4×N 次 profile 查询。
    query-count 回归：统计 security_profile_data 上的 SELECT 次数。"""
    from sqlalchemy import event
    from app.database import engine

    db = SessionLocal()
    try:
        for i in range(6):
            db.add(SecurityProfileData(
                symbol=f"600WATCH{i}", market="A股", dataset="income",
                period_key="20251231",
                payload={"end_date": "20251231", "n_income_attr_p": 100.0 + i},
            ))
        db.commit()
    finally:
        db.close()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        user_auth = {"Authorization": f"Bearer {await _token(client, 'demo')}"}
        for i in range(6):
            created = await client.post(
                "/api/watchlist", json={"symbol": f"600WATCH{i}", "market": "A股"},
                headers=user_auth,
            )
            assert created.status_code == 201

        profile_selects = []

        def _count(conn, cursor, statement, parameters, context, executemany):
            if "security_profile_data" in statement and statement.lstrip().upper().startswith("SELECT"):
                profile_selects.append(statement)

        event.listen(engine, "before_cursor_execute", _count)
        try:
            listed = await client.get("/api/watchlist", headers=user_auth)
        finally:
            event.remove(engine, "before_cursor_execute", _count)
        assert listed.status_code == 200
        assert len(listed.json()) == 6
        # 批量：档案取数为常数次（1 条 IN 查询），而非 6×(3~4)
        assert len(profile_selects) <= 2, profile_selects
        # 批量结果与逐条一致（同口径）
        for row in listed.json():
            assert row["graham_summary"] is not None
            assert row["graham_summary"]["total"] == 7

    db = SessionLocal()
    try:
        db.query(SecurityProfileData).filter(
            SecurityProfileData.symbol.like("600WATCH%")
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()

