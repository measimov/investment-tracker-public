"""管理员账号的两条自锁防护 + 口令强度下限（issue #131）。

两人小系统里一次误操作就能让全员失去管理入口，之后只能进库改；停用自己还会
顺带吊销自己的会话，当场锁在门外。所以拦在 API 层，而不是靠使用者小心。
"""

import httpx
import pytest

from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.user import User

ADMIN_PASSWORD = "admin-guard-password"


@pytest.fixture
def admin_client_state():
    """把内置 admin 的口令换成已知值，测试结束后还原并清掉临时用户。

    还原必须走**批量 UPDATE**，不能改 ORM 实例：本文件的用例会通过 HTTP 或
    另一条会话把内置 admin 降权，而这条长生命周期 session 的 identity map 里
    仍缓存着 is_admin=True。那样 `admin.is_admin = True` 是 no-op，SQLAlchemy
    不会发 UPDATE，临时 admin 又被同一段 teardown 删掉——库里就剩 0 个管理员，
    后续用例登录成功但建用户 403，取 `["id"]` 抛 KeyError。CI 上那次
    `1 failed, 802 passed` 走的正是这条路径（本地跑到的是竞态的另一侧）。
    """
    db = SessionLocal()
    original = db.query(User).filter(User.username == "admin").one().hashed_password
    try:
        db.query(User).filter(User.username == "admin").update(
            {"hashed_password": get_password_hash(ADMIN_PASSWORD)},
            synchronize_session=False,
        )
        db.commit()
        admin_id = db.query(User).filter(User.username == "admin").one().id
        yield admin_id
    finally:
        db.rollback()
        db.query(User).filter(User.username.like("guard-tmp-%")).delete(
            synchronize_session=False
        )
        db.query(User).filter(User.username == "admin").update(
            {"hashed_password": original, "is_admin": True, "is_active": True},
            synchronize_session=False,
        )
        db.commit()
        db.expire_all()
        db.close()


async def _login_admin(client):
    response = await client.post(
        "/api/auth/login",
        json={"username": "admin", "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200
    from app.core.security import CSRF_COOKIE_NAME

    return {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME)}


@pytest.mark.anyio
async def test_admin_cannot_strip_or_deactivate_their_own_admin_rights(admin_client_state):
    admin_id = admin_client_state
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        headers = await _login_admin(client)

        demote = await client.put(
            f"/api/users/{admin_id}", json={"is_admin": False}, headers=headers
        )
        assert demote.status_code == 400
        assert "管理员" in demote.json()["detail"]

        deactivate = await client.put(
            f"/api/users/{admin_id}", json={"is_active": False}, headers=headers
        )
        assert deactivate.status_code == 400

    # 被拒的请求不得留下半套写入（异常在 commit 之前抛出，会话关闭即回滚）
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.id == admin_id).one()
        assert admin.is_admin is True
        assert admin.is_active is True
    finally:
        db.close()


@pytest.mark.anyio
async def test_last_active_admin_cannot_be_removed(admin_client_state):
    """降权的目标不是自己时，仍要保证系统剩下至少一个活跃管理员。"""
    admin_id = admin_client_state
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        headers = await _login_admin(client)

        created = await client.post(
            "/api/users",
            json={
                "username": "guard-tmp-admin",
                "password": "another-admin-password",
                "is_admin": True,
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        other_id = created.json()["id"]

        # 还剩自己这个活跃管理员，降别人是允许的
        demote_other = await client.put(
            f"/api/users/{other_id}", json={"is_admin": False}, headers=headers
        )
        assert demote_other.status_code == 200

        # 把自己降权仍然被第一条守卫挡住（此时也确实是最后一个）
        assert (
            await client.put(
                f"/api/users/{admin_id}", json={"is_admin": False}, headers=headers
            )
        ).status_code == 400


@pytest.mark.anyio
async def test_password_below_the_minimum_length_is_rejected(admin_client_state):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        headers = await _login_admin(client)

        from app.schemas.user import MIN_PASSWORD_LENGTH

        # 字面量而非 MIN_PASSWORD_LENGTH-1：用常量算出来的话这条断言在任何
        # 下限取值下都成立，把下限调回 6 也照样绿。
        assert MIN_PASSWORD_LENGTH >= 10, "口令下限不得低于 10"
        too_short = await client.post(
            "/api/users",
            json={"username": "guard-tmp-weak", "password": "x" * 9, "is_admin": False},
            headers=headers,
        )
        assert too_short.status_code == 422

        accepted = await client.post(
            "/api/users",
            json={
                "username": "guard-tmp-strong",
                "password": "x" * MIN_PASSWORD_LENGTH,
                "is_admin": False,
            },
            headers=headers,
        )
        assert accepted.status_code == 201, accepted.text


@pytest.mark.anyio
async def test_concurrent_demotions_cannot_drain_the_admin_set(admin_client_state):
    """两个活跃管理员并发互相降权，不得双双成功。

    没有串行化时：两个事务各自把对方排除在目标之外，于是都看到"还剩一个
    活跃管理员"，更新落在不同的行、都能提交，活跃管理员归零。用两条真实的
    并发 HTTP 请求复现——单线程顺序调用永远测不出这个。
    """
    import anyio

    admin_id = admin_client_state
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        headers = await _login_admin(client)
        created = await client.post(
            "/api/users",
            json={
                "username": "guard-tmp-race",
                "password": "race-admin-password",
                "is_admin": True,
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        other_id = created.json()["id"]

        # 用另一个管理员的会话发第二条请求，两条请求互相降对方
        other_client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
        try:
            login_other = await other_client.post(
                "/api/auth/login",
                json={"username": "guard-tmp-race", "password": "race-admin-password"},
            )
            assert login_other.status_code == 200
            from app.core.security import CSRF_COOKIE_NAME

            other_headers = {"X-CSRF-Token": other_client.cookies.get(CSRF_COOKIE_NAME)}

            results: list[int] = []

            async def demote(caller, caller_headers, victim_id):
                response = await caller.put(
                    f"/api/users/{victim_id}", json={"is_admin": False}, headers=caller_headers
                )
                results.append(response.status_code)

            async with anyio.create_task_group() as tg:
                tg.start_soon(demote, client, headers, other_id)
                tg.start_soon(demote, other_client, other_headers, admin_id)
        finally:
            await other_client.aclose()

    # 不钉具体状态码：败的那条可能是 400（被守卫挡住），也可能是 403
    # （对手先提交，它的管理员依赖当场失效）。不变量是"只有一条成功"。
    assert results.count(200) == 1, f"两条并发降权的结果是 {results}"

    db = SessionLocal()
    try:
        live_admins = (
            db.query(User)
            .filter(User.is_admin.is_(True), User.is_active.is_(True))
            .count()
        )
        assert live_admins >= 1, "活跃管理员被并发降权清空"
    finally:
        db.close()


@pytest.mark.anyio
async def test_deleting_the_last_other_admin_still_leaves_one(admin_client_state):
    """删除路径同样受守卫：不能把最后一个活跃管理员删掉。"""
    admin_id = admin_client_state
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        headers = await _login_admin(client)
        created = await client.post(
            "/api/users",
            json={
                "username": "guard-tmp-victim",
                "password": "victim-admin-password",
                "is_admin": True,
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        other_id = created.json()["id"]

        # 先把自己停用是不允许的，所以换个路子：让自己不再是唯一活跃管理员的
        # 反面——把别人删掉应当成功（自己还在），再删自己被既有守卫挡住。
        assert (await client.delete(f"/api/users/{other_id}", headers=headers)).status_code == 204
        assert (await client.delete(f"/api/users/{admin_id}", headers=headers)).status_code == 400


def test_admin_guard_serializes_two_interleaved_transactions(admin_client_state):
    """确定性复现竞态：两个事务显式交错，第二个必须看到第一个提交后的结果。

    HTTP 层的并发用例只能碰运气撞上时序；这里直接用两条真实 DB 会话把顺序
    摆出来——B 在 A 提交**之前**进入守卫。没有事务级顾问锁时，B 的计数看不到
    A 未提交的降权，于是判定"还剩一个活跃管理员"放行，两笔都提交、归零。
    有锁时 B 阻塞到 A 提交后才计数，看到 0，抛 400。
    """
    import threading

    from fastapi import HTTPException

    from app.api.users import _guard_last_active_admin

    admin_id = admin_client_state
    setup = SessionLocal()
    try:
        peer = User(
            username="guard-tmp-serial",
            hashed_password=get_password_hash("serial-admin-password"),
            is_admin=True,
            is_active=True,
        )
        setup.add(peer)
        setup.commit()
        peer_id = peer.id
    finally:
        setup.close()

    first_in_guard = threading.Event()
    first_may_commit = threading.Event()
    outcome: dict[str, object] = {}

    def demote(session_user_id, target_id, *, is_first):
        db = SessionLocal()
        try:
            actor = db.query(User).filter(User.id == session_user_id).one()
            target = db.query(User).filter(User.id == target_id).one()
            target.is_admin = False
            try:
                _guard_last_active_admin(db, target=target, current_user=actor)
            except HTTPException as exc:
                outcome["second"] = exc.status_code
                db.rollback()
                return
            if is_first:
                first_in_guard.set()
                first_may_commit.wait(timeout=10)
            db.commit()
            outcome["first" if is_first else "second"] = 200
        finally:
            db.close()

    # A 降 peer：进入守卫、拿到锁后停住不提交
    thread_a = threading.Thread(target=demote, args=(admin_id, peer_id), kwargs={"is_first": True})
    thread_a.start()
    assert first_in_guard.wait(timeout=10), "第一个事务没能进入守卫"

    # B 降 admin：此时 A 尚未提交，B 必须被锁挡在计数之前
    thread_b = threading.Thread(target=demote, args=(peer_id, admin_id), kwargs={"is_first": False})
    thread_b.start()
    thread_b.join(timeout=2)
    assert thread_b.is_alive(), "第二个事务没有被串行化，直接越过了守卫"

    first_may_commit.set()
    thread_a.join(timeout=10)
    thread_b.join(timeout=10)

    assert outcome.get("first") == 200
    assert outcome.get("second") == 400, f"第二个事务应被挡住，实得 {outcome.get('second')}"

    db = SessionLocal()
    try:
        live = db.query(User).filter(User.is_admin.is_(True), User.is_active.is_(True)).count()
        assert live >= 1, "活跃管理员被交错的两笔更新清空"
        restored = db.query(User).filter(User.id == admin_id).one()
        restored.is_admin = True
        db.commit()
    finally:
        db.close()
