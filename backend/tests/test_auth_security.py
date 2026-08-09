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


# ---------------------------------------------------------------------------
# issue #131：认证与配置安全基线
# ---------------------------------------------------------------------------


def test_security_settings_default_to_fail_closed():
    """忘记配 env 的那次部署才最需要保护：代码默认必须是安全的一侧。

    conftest 把 REQUIRE_HTTPS 显式放宽成 false（TestClient 走明文），所以这里
    读的是类默认值而不是当前进程的 settings——否则测的就是测试环境自己。
    """
    from app.config import Settings

    defaults = {
        name: field.default
        for name, field in Settings.model_fields.items()
        if name in {"require_https", "enable_docs", "trust_proxy_headers"}
    }
    assert defaults == {
        "require_https": True,
        "enable_docs": False,
        "trust_proxy_headers": False,
    }


@pytest.mark.anyio
async def test_plaintext_login_is_rejected_when_https_is_required(
    prepared_auth_user, monkeypatch
):
    """require_https 打开后，明文登录必须被拒。"""
    from app.api import auth as auth_api

    monkeypatch.setattr(auth_api.settings, "require_https", True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/auth/login",
            json={"username": "demo", "password": "cookie-test-password"},
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "Login requires HTTPS"


@pytest.mark.anyio
async def test_forwarded_proto_header_alone_does_not_bypass_https_requirement(
    prepared_auth_user, monkeypatch
):
    """X-Forwarded-Proto 是客户端可伪造的请求头。

    只有在显式声明"反代会覆写它、后端端口不对外"时才采信；否则直连后端
    加一个请求头就能白嫖 require_https。
    """
    from app.api import auth as auth_api

    monkeypatch.setattr(auth_api.settings, "require_https", True)
    monkeypatch.setattr(auth_api.settings, "trust_proxy_headers", False)
    transport = httpx.ASGITransport(app=app)
    payload = {"username": "demo", "password": "cookie-test-password"}
    headers = {"X-Forwarded-Proto": "https"}
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        spoofed = await client.post("/api/auth/login", json=payload, headers=headers)
        assert spoofed.status_code == 400, "伪造的请求头不得绕过 require_https"

        # 显式信任反代后（compose 的拓扑）同一请求必须放行，否则生产登不进去
        monkeypatch.setattr(auth_api.settings, "trust_proxy_headers", True)
        behind_proxy = await client.post("/api/auth/login", json=payload, headers=headers)
        assert behind_proxy.status_code == 200


def test_session_renewal_stops_at_the_absolute_lifetime():
    """滑动续期不得让被窃 cookie 永久续命。

    续期链共享同一个 jti、每次都把 expires_at 往后推，只看 expires_at 的话
    "每 30 分钟刷一次"就能无限延长。到顶后必须吊销并强制重新登录。
    """
    from datetime import datetime, timedelta, timezone

    from app.models.auth_session import AuthSession
    from app.services import auth_session_service

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "demo").one()
        _token, jti = auth_session_service.issue_session(db, user)

        # 仍在窗口内：正常续期
        assert auth_session_service.renew_session(db, user, jti) is not None

        session = db.query(AuthSession).filter(AuthSession.id == jti).one()
        max_hours = auth_session_service.settings.session_absolute_max_hours
        session.created_at = datetime.now(timezone.utc) - timedelta(hours=max_hours + 1)
        db.commit()

        assert auth_session_service.renew_session(db, user, jti) is None, "到顶仍在续期"
        db.refresh(session)
        assert session.revoked_at is not None, "到顶必须吊销，而不是留着等下次再试"
    finally:
        db.query(AuthSession).filter(AuthSession.id == jti).delete(
            synchronize_session=False
        )
        db.commit()
        db.close()


def _session_fixture():
    """(db, user)：调用方负责关闭。"""
    db = SessionLocal()
    return db, db.query(User).filter(User.username == "demo").one()


def test_refreshing_just_before_the_deadline_cannot_extend_past_it():
    """截止前一瞬刷新，也不能把会话推到绝对上限之外。

    只在入口比一次"现在到顶了没"是不够的：还差一秒到顶时刷新，仍会把
    expires_at 与 JWT 的 exp 一起推到 now+lifetime，于是会话实际多活一个
    lifetime（默认 30 分钟）。而 is_session_valid 原来只看 expires_at。
    """
    from datetime import datetime, timedelta, timezone

    from app.core.security import decode_access_token
    from app.models.auth_session import AuthSession
    from app.services import auth_session_service

    db, user = _session_fixture()
    try:
        _token, jti = auth_session_service.issue_session(db, user)
        session = db.query(AuthSession).filter(AuthSession.id == jti).one()
        max_hours = auth_session_service.settings.session_absolute_max_hours
        created_at = datetime.now(timezone.utc) - timedelta(hours=max_hours) + timedelta(minutes=1)
        session.created_at = created_at
        db.commit()

        renewed = auth_session_service.renew_session(db, user, jti)
        assert renewed is not None, "还没到顶，这次刷新本身应当成功"

        deadline = created_at + timedelta(hours=max_hours)
        db.refresh(session)
        stored = session.expires_at
        if stored.tzinfo is None:
            stored = stored.replace(tzinfo=timezone.utc)
        assert stored <= deadline, "DB 有效期越过了绝对截止点"

        token_exp = datetime.fromtimestamp(decode_access_token(renewed)["exp"], timezone.utc)
        # 不留容差：exp 由钳位算出的**绝对时刻**直接编码（jwt 只保留整秒）。
        # 走 delta 的话 helper 会在更晚的 t1 重新加一遍，exp 变成
        # deadline + (t1 − t0)，那时才需要"允许一秒误差"——那个容差本身就是
        # 契约不一致的味道。
        assert token_exp == deadline.replace(microsecond=0), "JWT exp 没有落在绝对截止点上"
    finally:
        db.query(AuthSession).filter(AuthSession.id == jti).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_ordinary_requests_fail_once_the_absolute_deadline_passes():
    """兜底：即使某条路径漏钳、或上限被调小，越界会话也必须立刻失效。"""
    from datetime import datetime, timedelta, timezone

    from app.models.auth_session import AuthSession
    from app.services import auth_session_service

    db, user = _session_fixture()
    try:
        _token, jti = auth_session_service.issue_session(db, user)
        session = db.query(AuthSession).filter(AuthSession.id == jti).one()
        max_hours = auth_session_service.settings.session_absolute_max_hours
        # 模拟"钳位之前签发"的历史行：有效期还很长，但创建时间早已越界
        session.created_at = datetime.now(timezone.utc) - timedelta(hours=max_hours + 1)
        session.expires_at = datetime.now(timezone.utc) + timedelta(days=30)
        db.commit()

        assert auth_session_service.is_session_valid(db, jti, user.id) is False
    finally:
        db.query(AuthSession).filter(AuthSession.id == jti).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_issuing_a_lifetime_longer_than_the_absolute_max_is_clamped():
    """单次 lifetime 大于绝对上限时，第一张令牌就不能越界。"""
    from datetime import timedelta, timezone

    from app.models.auth_session import AuthSession
    from app.services import auth_session_service

    db, user = _session_fixture()
    try:
        max_hours = auth_session_service.settings.session_absolute_max_hours
        _token, jti = auth_session_service.issue_session(
            db, user, expires_delta=timedelta(hours=max_hours + 24)
        )
        session = db.query(AuthSession).filter(AuthSession.id == jti).one()
        created_at = session.created_at
        expires_at = session.expires_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        assert expires_at - created_at <= timedelta(hours=max_hours)
    finally:
        db.query(AuthSession).filter(AuthSession.id == jti).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_a_slow_commit_cannot_push_the_jwt_past_the_deadline():
    """DB 提交慢 2 秒时，JWT 的 exp 仍须落在绝对截止点上。

    钳位算出的是"到期时刻"，但 create_access_token 若按 delta 在**更晚**的
    时刻重新取 utcnow()，exp 会变成 deadline + (提交耗时)。这条用一次慢
    commit 把那段偏移放大到肉眼可见，防止实现悄悄退回 delta 口径。
    """
    import time
    from datetime import datetime, timedelta, timezone

    from app.core.security import decode_access_token
    from app.models.auth_session import AuthSession
    from app.services import auth_session_service

    db, user = _session_fixture()
    jti = None
    try:
        _token, jti = auth_session_service.issue_session(db, user)
        session = db.query(AuthSession).filter(AuthSession.id == jti).one()
        max_hours = auth_session_service.settings.session_absolute_max_hours
        created_at = datetime.now(timezone.utc) - timedelta(hours=max_hours) + timedelta(minutes=1)
        session.created_at = created_at
        db.commit()

        original_commit = db.commit

        def slow_commit():
            time.sleep(2)
            original_commit()

        db.commit = slow_commit
        try:
            renewed = auth_session_service.renew_session(db, user, jti)
        finally:
            db.commit = original_commit

        assert renewed is not None
        deadline = (created_at + timedelta(hours=max_hours)).replace(microsecond=0)
        token_exp = datetime.fromtimestamp(decode_access_token(renewed)["exp"], timezone.utc)
        assert token_exp == deadline, f"慢提交把 exp 推到了 {token_exp}，截止点是 {deadline}"
    finally:
        if jti:
            db.query(AuthSession).filter(AuthSession.id == jti).delete(synchronize_session=False)
            db.commit()
        db.close()


def test_non_positive_session_lifetimes_are_rejected_at_startup():
    """0/负值必须在配置层直接失败，而不是在运行时变成难查的行为。

    配 0 时登录仍返回 200，但会话行签发即过期；而零 timedelta 在
    create_access_token 里是 falsy，会退回默认的 30 分钟——客户端拿到一张
    "看着有效"的 token，下一次 /me 立刻 401。
    """
    import pydantic

    from app.config import Settings

    base = {
        "database_url": "postgresql://u:p@127.0.0.1:5432/investment_test",
        "secret_key": "x",
        "admin_initial_password": "x",
        "demo_initial_password": "x",
    }
    for field, bad in (
        ("session_absolute_max_hours", 0),
        ("session_absolute_max_hours", -1),
        ("access_token_expire_minutes", 0),
        ("access_token_expire_minutes", -5),
    ):
        with pytest.raises(pydantic.ValidationError):
            Settings(**base, **{field: bad})
