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


# (method, path, json_body)：覆盖 exchange_rates.py 的全部 8 个端点
ENDPOINTS = [
    ("GET", "/api/exchange-rates/latest", None),
    ("GET", "/api/exchange-rates/", None),
    ("GET", "/api/exchange-rates/USD/CNY", None),
    ("POST", "/api/exchange-rates/", {
        "from_currency": "USD", "to_currency": "CNY",
        "rate": "7.2", "effective_date": "2026-01-01",
    }),
    ("PUT", "/api/exchange-rates/1", {"rate": "7.3"}),
    ("DELETE", "/api/exchange-rates/1", None),
    ("POST", "/api/exchange-rates/convert", {
        "amount": "100", "from_currency": "USD", "to_currency": "CNY",
    }),
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


@pytest.fixture
def logged_in_non_admin():
    """demo 是日常使用者且 is_admin=False（见 user_seed）。

    汇率写操作刻意**不**收成 admin-only：日常账号非 admin，收紧会造成
    「汇率管理页看得见、点了就 403」。这条 fixture 把该前提钉死。
    """
    password = "exchange-rate-auth-password"
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "demo").one()
        assert user.is_admin is False, "前提变了：demo 若成为 admin，需重新评估写端点权限级别"
        original = user.hashed_password
        user.hashed_password = get_password_hash(password)
        db.commit()
        yield password
        db.query(ExchangeRate).filter(ExchangeRate.source == "auth-test").delete()
        user.hashed_password = original
        db.commit()
    finally:
        db.close()


@pytest.mark.anyio
async def test_authenticated_non_admin_can_read_and_write(logged_in_non_admin):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = (await client.post(
            "/api/auth/token",
            json={"username": "demo", "password": logged_in_non_admin},
        )).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        assert (await client.get("/api/exchange-rates/latest", headers=headers)).status_code == 200

        created = await client.post("/api/exchange-rates/", headers=headers, json={
            "from_currency": "USD", "to_currency": "CNY",
            "rate": "7.21", "effective_date": "2026-01-01", "source": "auth-test",
        })
        assert created.status_code == 200, created.text


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

    # 清单里的路径带具体参数值（/USD/CNY），实际路由是模板（/{from_currency}/...），
    # 故按 (method, 段数) 比对，只求「端点数量与方法组合无遗漏」。
    def shape(pairs):
        return sorted((m, len(p.rstrip("/").split("/"))) for m, p in pairs)

    assert shape(covered) == shape(actual), (
        f"汇率端点清单与实际路由不一致：清单={sorted(covered)} 实际={sorted(actual)}"
    )
