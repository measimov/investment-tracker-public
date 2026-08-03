from pathlib import Path

import httpx
import pytest

from app.core.security import AUTH_COOKIE_NAME, CSRF_COOKIE_NAME, get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.holding import Holding
from app.models.transaction import Transaction
from app.models.user import User



def _clean_auth_test_data(db, user_id: int) -> None:
    symbols = ["COOKIE001", "BEARER001"]
    db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.symbol.in_(symbols),
    ).delete(synchronize_session=False)
    db.query(Holding).filter(
        Holding.user_id == user_id,
        Holding.symbol.in_(symbols),
    ).delete(synchronize_session=False)


@pytest.fixture
def prepared_auth_user():
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "demo").one()
        original_password = user.hashed_password
        user.hashed_password = get_password_hash("cookie-test-password")
        _clean_auth_test_data(db, user.id)
        db.commit()
        yield
        user.hashed_password = original_password
        _clean_auth_test_data(db, user.id)
        db.commit()
    finally:
        db.close()


def _transaction(symbol: str):
    return {
        "symbol": symbol,
        "name": "Auth Security Test",
        "market": "A股",
        "transaction_type": "BUY",
        "quantity": 1,
        "price": 1,
        "fee": 0,
        "transaction_date": "2026-07-11",
        "currency": "CNY",
    }


@pytest.mark.anyio
async def test_browser_cookie_auth_requires_csrf_and_logout_clears_session(
    prepared_auth_user,
):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/api/auth/login",
            json={"username": "demo", "password": "cookie-test-password"},
        )

        assert login.status_code == 200
        assert set(login.json()) == {"user"}
        auth_cookie_header = next(
            value
            for value in login.headers.get_list("set-cookie")
            if value.startswith(f"{AUTH_COOKIE_NAME}=")
        )
        assert "HttpOnly" in auth_cookie_header
        assert client.cookies.get(AUTH_COOKIE_NAME)
        csrf_token = client.cookies.get(CSRF_COOKIE_NAME)
        assert csrf_token

        me = await client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["username"] == "demo"

        rejected = await client.post("/api/transactions", json=_transaction("COOKIE001"))
        assert rejected.status_code == 403
        assert rejected.json()["detail"] == "CSRF validation failed"

        accepted = await client.post(
            "/api/transactions",
            json=_transaction("COOKIE001"),
            headers={"X-CSRF-Token": csrf_token},
        )
        assert accepted.status_code == 201

        logout = await client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": csrf_token},
        )
        assert logout.status_code == 204
        assert not client.cookies.get(AUTH_COOKIE_NAME)
        assert not client.cookies.get(CSRF_COOKIE_NAME)
        assert (await client.get("/api/auth/me")).status_code == 401


@pytest.mark.anyio
async def test_api_token_keeps_bearer_clients_compatible_without_csrf(prepared_auth_user):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/auth/token",
            json={"username": "demo", "password": "cookie-test-password"},
        )
        assert response.status_code == 200
        token = response.json()["access_token"]

        created = await client.post(
            "/api/transactions",
            json=_transaction("BEARER001"),
            headers={"Authorization": f"Bearer {token}"},
        )
        assert created.status_code == 201


@pytest.mark.anyio
async def test_logout_revokes_replayed_jwt(prepared_auth_user):
    """Issue #36: a JWT copied before logout must be rejected after logout."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/api/auth/login",
            json={"username": "demo", "password": "cookie-test-password"},
        )
        assert login.status_code == 200
        stolen_jwt = client.cookies.get(AUTH_COOKIE_NAME)
        csrf_token = client.cookies.get(CSRF_COOKIE_NAME)

        # Replay works while the session lives.
        replay = await client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {stolen_jwt}"}
        )
        assert replay.status_code == 200

        logout = await client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf_token})
        assert logout.status_code == 204

        # The same JWT (still unexpired) is now revoked server-side.
        replay_after = await client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {stolen_jwt}"}
        )
        assert replay_after.status_code == 401


@pytest.mark.anyio
async def test_password_change_revokes_all_outstanding_tokens(prepared_auth_user):
    """Issue #36: changing the password invalidates every existing session."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.post(
            "/api/auth/token",
            json={"username": "demo", "password": "cookie-test-password"},
        )
        second = await client.post(
            "/api/auth/token",
            json={"username": "demo", "password": "cookie-test-password"},
        )
        token_a = first.json()["access_token"]
        token_b = second.json()["access_token"]

        changed = await client.put(
            "/api/auth/me/password",
            json={
                "old_password": "cookie-test-password",
                "new_password": "rotated-password-123",
            },
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert changed.status_code == 200

        # Both pre-change tokens are dead, including the one that made the change.
        for token in (token_a, token_b):
            check = await client.get(
                "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
            )
            assert check.status_code == 401

        # The new password issues a working session.
        relogin = await client.post(
            "/api/auth/token",
            json={"username": "demo", "password": "rotated-password-123"},
        )
        assert relogin.status_code == 200


@pytest.mark.anyio
async def test_reactivated_user_cannot_reuse_pre_deactivation_token(prepared_auth_user):
    """Issue #36: deactivate-then-reactivate must not resurrect old tokens."""
    from app.services.auth_session_service import revoke_user_sessions

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = (
            await client.post(
                "/api/auth/token",
                json={"username": "demo", "password": "cookie-test-password"},
            )
        ).json()["access_token"]

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.username == "demo").one()
            user.is_active = False
            db.commit()
            # What the admin update endpoint performs on deactivation:
            revoke_user_sessions(db, user.id)
            user.is_active = True
            db.commit()
        finally:
            db.close()

        check = await client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert check.status_code == 401


@pytest.mark.anyio
async def test_refresh_extends_same_session_and_keeps_in_flight_tokens_valid(
    prepared_auth_user,
):
    """滑动续期 = 延长同一会话（同 jti 重签）：会话不增行、有效期后移，
    旧 JWT 在自身剩余寿命内仍有效（在途请求不因轮换被误判 401）。"""
    from app.core.security import decode_access_token
    from app.models.auth_session import AuthSession

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/api/auth/login",
            json={"username": "demo", "password": "cookie-test-password"},
        )
        assert login.status_code == 200
        old_jwt = client.cookies.get(AUTH_COOKIE_NAME)
        old_csrf = client.cookies.get(CSRF_COOKIE_NAME)
        jti = decode_access_token(old_jwt)["jti"]

        db = SessionLocal()
        try:
            expires_before = db.query(AuthSession).filter(AuthSession.id == jti).one().expires_at
        finally:
            db.close()

        refreshed = await client.post("/api/auth/refresh", headers={"X-CSRF-Token": old_csrf})
        assert refreshed.status_code == 200
        assert refreshed.json()["user"]["username"] == "demo"

        new_jwt = client.cookies.get(AUTH_COOKIE_NAME)
        new_csrf = client.cookies.get(CSRF_COOKIE_NAME)
        assert new_jwt and new_csrf and new_csrf != old_csrf
        # 同一 jti：续期链共享一个服务器侧会话
        assert decode_access_token(new_jwt)["jti"] == jti

        db = SessionLocal()
        try:
            sessions = db.query(AuthSession).filter(AuthSession.id == jti).all()
            assert len(sessions) == 1
            assert sessions[0].expires_at > expires_before  # 有效期确实后移
        finally:
            db.close()

        # 新 Cookie 正常工作；旧令牌在自身剩余寿命内仍有效
        assert (await client.get("/api/auth/me")).status_code == 200
        replay = await client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {old_jwt}"}
        )
        assert replay.status_code == 200


@pytest.mark.anyio
async def test_logout_revokes_entire_refresh_chain(prepared_auth_user):
    """检视意见回归：login → refresh → logout 后，续期前的旧 JWT 必须 401。

    旧 JWT 与新 JWT 共享 jti，登出吊销该 jti 即终止整条续期链——
    泄露的旧令牌不能在登出后继续存活。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/api/auth/login",
            json={"username": "demo", "password": "cookie-test-password"},
        )
        assert login.status_code == 200
        pre_refresh_jwt = client.cookies.get(AUTH_COOKIE_NAME)

        refreshed = await client.post(
            "/api/auth/refresh",
            headers={"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME)},
        )
        assert refreshed.status_code == 200
        post_refresh_jwt = client.cookies.get(AUTH_COOKIE_NAME)

        logout = await client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME)},
        )
        assert logout.status_code == 204

        # 链上所有令牌（续期前 + 续期后）全部失效
        for stale_jwt in (pre_refresh_jwt, post_refresh_jwt):
            replay = await client.get(
                "/api/auth/me", headers={"Authorization": f"Bearer {stale_jwt}"}
            )
            assert replay.status_code == 401


@pytest.mark.anyio
async def test_refresh_requires_csrf_and_live_session(prepared_auth_user):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 未登录：401
        assert (await client.post("/api/auth/refresh")).status_code == 401

        login = await client.post(
            "/api/auth/login",
            json={"username": "demo", "password": "cookie-test-password"},
        )
        assert login.status_code == 200
        csrf_token = client.cookies.get(CSRF_COOKIE_NAME)

        # Cookie 认证缺 CSRF 头：403
        assert (await client.post("/api/auth/refresh")).status_code == 403

        # 登出吊销当前会话后，被窃 JWT 无法续期
        stolen_jwt = client.cookies.get(AUTH_COOKIE_NAME)
        logout = await client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf_token})
        assert logout.status_code == 204
        hijack = await client.post(
            "/api/auth/refresh", headers={"Authorization": f"Bearer {stolen_jwt}"}
        )
        assert hijack.status_code == 401


def test_production_security_headers_and_auth_rate_limit_do_not_regress():
    nginx_config = (
        Path(__file__).resolve().parents[2] / "frontend" / "nginx.conf.template"
    ).read_text()

    assert "script-src 'self';" in nginx_config
    assert "'unsafe-eval'" not in nginx_config
    assert "script-src 'self' 'unsafe-inline'" not in nginx_config
    assert 'Strict-Transport-Security "max-age=31536000; includeSubDomains" always' in nginx_config
    assert 'X-Content-Type-Options "nosniff" always' in nginx_config
    assert "location ~ ^/api/auth/(login|token)$" in nginx_config
    assert "limit_req zone=login_limit" in nginx_config
