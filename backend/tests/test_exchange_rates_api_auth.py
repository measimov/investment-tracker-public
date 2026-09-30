"""汇率 API 的认证闸门。

汇率是全局表且是所有用户金额折算的唯一数据源，此前整个 router 没有任何认证
依赖——匿名即可增删改汇率、触发外呼刷新，静默污染每个用户的全部金额展示。
这里逐端点钉死「匿名一律 401」，避免将来新增端点时又漏挂依赖。
"""

import httpx
import pytest

from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.exchange_rate import ExchangeRate
from app.models.user import User


# (method, path, json_body)：覆盖 exchange_rates.py 的全部 7 个端点
ENDPOINTS = [
    ("GET", "/api/exchange-rates/latest", None),
    ("GET", "/api/exchange-rates/source-checks", None),
    ("GET", "/api/exchange-rates", None),
    (
        "POST",
        "/api/exchange-rates",
        {
            "from_currency": "USD",
            "to_currency": "CNY",
            "rate": "7.2",
            "effective_date": "2026-01-01",
        },
    ),
    ("PUT", "/api/exchange-rates/1", {"rate": "7.3"}),
    ("DELETE", "/api/exchange-rates/1", None),
    ("POST", "/api/exchange-rates/refresh-from-api", None),
]


@pytest.mark.anyio
@pytest.mark.parametrize("method,path,body", ENDPOINTS)
async def test_exchange_rate_endpoints_reject_anonymous(method, path, body):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.request(method, path, json=body)

    # 401 而非 403/404：未认证请求必须在业务逻辑之前被拦下，
    # 404 会说明它已经查过库（即闸门没生效）。
    assert response.status_code == 401, (
        f"{method} {path} 返回 {response.status_code}，匿名请求必须 401"
    )


def _set_password(username, password):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).one()
        original = user.hashed_password
        user.hashed_password = get_password_hash(password)
        db.commit()
        return original
    finally:
        db.close()


def _restore_password(username, original):
    db = SessionLocal()
    try:
        db.query(User).filter(User.username == username).update({"hashed_password": original})
        db.commit()
    finally:
        db.close()


@pytest.fixture
def tokens():
    """demo（日常使用者，非管理员）与 admin 的口令，结束时恢复并清理测试汇率。"""
    password = "exchange-rate-auth-password"
    originals = {name: _set_password(name, password) for name in ("demo", "admin")}
    yield password
    db = SessionLocal()
    try:
        db.query(ExchangeRate).filter(ExchangeRate.from_currency == "ZZT").delete()
        db.commit()
    finally:
        db.close()
    for name, original in originals.items():
        _restore_password(name, original)


async def _headers(client, username, password):
    token = (
        await client.post(
            "/api/auth/token",
            json={"username": username, "password": password},
        )
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


RATE = {
    "from_currency": "ZZT",
    "to_currency": "CNY",
    "rate": "7.21",
    "effective_date": "2020-01-02",
}


@pytest.mark.anyio
async def test_non_admin_can_read_but_not_write(tokens):
    """#277：汇率是所有人折算的唯一数据源，写入仅管理员（前端对非管理员隐藏写操作，
    不会出现「看得见、点了就 403」）。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        headers = await _headers(client, "demo", tokens)
        assert (await client.get("/api/exchange-rates/latest", headers=headers)).status_code == 200
        assert (await client.get("/api/exchange-rates", headers=headers)).status_code == 200
        for method, path, body in (
            ("POST", "/api/exchange-rates", RATE),
            ("PUT", "/api/exchange-rates/1", {"rate": "7.3"}),
            ("DELETE", "/api/exchange-rates/1", None),
            ("POST", "/api/exchange-rates/refresh-from-api", None),
        ):
            response = await client.request(method, path, json=body, headers=headers)
            assert response.status_code == 403, f"{method} {path} → {response.status_code}"


@pytest.mark.anyio
async def test_admin_writes_are_manual_and_delete_deactivates(tokens):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        headers = await _headers(client, "admin", tokens)
        # 客户端自称官方来源无效：来源由服务端定为 manual
        created = await client.post(
            "/api/exchange-rates", headers=headers, json={**RATE, "source": "chinamoney-ccpr"}
        )
        assert created.status_code == 200, created.text
        assert created.json()["source"] == "manual"
        rate_id = created.json()["id"]

        edited = await client.put(
            f"/api/exchange-rates/{rate_id}", headers=headers, json={"rate": "7.3"}
        )
        assert edited.status_code == 200 and edited.json()["source"] == "manual"

        removed = await client.delete(f"/api/exchange-rates/{rate_id}", headers=headers)
        assert removed.status_code == 204
        listed = await client.get("/api/exchange-rates?from_currency=ZZT", headers=headers)
        assert listed.json() == []  # 默认不列停用行
        audit = await client.get(
            "/api/exchange-rates?from_currency=ZZT&include_inactive=true", headers=headers
        )
        assert [(row["id"], row["is_active"]) for row in audit.json()] == [(rate_id, False)]


@pytest.mark.anyio
async def test_auto_sourced_rows_cannot_be_deactivated_and_null_active_serializes(tokens):
    """PR #300 评审：官方/第三方行会被下一次刷新原样重建并重新启用，停用兑现不了，409；
    is_active 为 NULL 的历史行在 include_inactive 列表里按停用序列化，不 500。"""
    from datetime import date
    from decimal import Decimal

    db = SessionLocal()
    try:
        db.query(ExchangeRate).filter(ExchangeRate.from_currency == "ZZO").delete()
        official = ExchangeRate(
            from_currency="ZZO",
            to_currency="CNY",
            rate=Decimal("7"),
            effective_date=date(2020, 1, 2),
            source="cfets-ccpr",
            is_active=True,
        )
        legacy = ExchangeRate(
            from_currency="ZZO",
            to_currency="CNY",
            rate=Decimal("7"),
            effective_date=date(2020, 1, 1),
            source="manual",
        )
        db.add_all([official, legacy])
        db.commit()
        db.query(ExchangeRate).filter(ExchangeRate.id == legacy.id).update({"is_active": None})
        db.commit()
        official_id = official.id

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            headers = await _headers(client, "admin", tokens)
            refused = await client.delete(f"/api/exchange-rates/{official_id}", headers=headers)
            assert refused.status_code == 409 and "编辑" in refused.json()["detail"]
            audit = await client.get(
                "/api/exchange-rates?from_currency=ZZO&include_inactive=true", headers=headers
            )
            assert audit.status_code == 200
            assert sorted(row["is_active"] for row in audit.json()) == [False, True]
    finally:
        db.query(ExchangeRate).filter(ExchangeRate.from_currency == "ZZO").delete()
        db.commit()
        db.close()


def test_edited_official_row_becomes_manual_and_deactivated_manual_does_not_block():
    """改过数值的官方行此前保留官方标签、下次刷新被静默覆盖；停用的手工行此前会
    永久挡住该日的官方中间价。"""
    from datetime import date
    from decimal import Decimal

    from app.services.exchange_rate_service import _upsert_unless_manual

    db = SessionLocal()
    try:
        db.query(ExchangeRate).filter(ExchangeRate.from_currency == "ZZT").delete()
        db.add(
            ExchangeRate(
                from_currency="ZZT",
                to_currency="CNY",
                rate=Decimal("7"),
                effective_date=date(2020, 1, 2),
                source="manual",
                is_active=True,
            )
        )
        db.commit()
        assert _upsert_unless_manual(db, "ZZT", Decimal("7.1"), date(2020, 1, 2), "ccpr") is False

        db.query(ExchangeRate).filter(ExchangeRate.from_currency == "ZZT").update(
            {"is_active": False}
        )
        db.commit()
        assert _upsert_unless_manual(db, "ZZT", Decimal("7.1"), date(2020, 1, 2), "ccpr") is True
        row = db.query(ExchangeRate).filter(ExchangeRate.from_currency == "ZZT").one()
        assert (row.source, row.is_active, row.rate) == ("ccpr", True, Decimal("7.1"))
    finally:
        db.query(ExchangeRate).filter(ExchangeRate.from_currency == "ZZT").delete()
        db.commit()
        db.close()


@pytest.mark.anyio
async def test_endpoint_list_covers_every_route_on_the_router():
    """新增汇率端点却忘了加进上面的清单时，这条会红。"""
    covered = {(method, path) for method, path, _ in ENDPOINTS}
    actual = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/exchange-rates"):
            continue
        for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}:
            actual.add((method, path))

    # 清单里的路径带具体参数值（/1），实际路由是模板（/{rate_id}），
    # 故按 (method, 段数) 比对，只求「端点数量与方法组合无遗漏」。
    def shape(pairs):
        return sorted((m, len(p.rstrip("/").split("/"))) for m, p in pairs)

    assert shape(covered) == shape(actual), (
        f"汇率端点清单与实际路由不一致：清单={sorted(covered)} 实际={sorted(actual)}"
    )
