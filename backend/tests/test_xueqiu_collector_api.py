"""采集器管理 API：读对登录用户开放，名单增删改与「立即运行」仅管理员；
立即运行只写请求标记（抓取不在 Web 进程里跑），采集器未启用时 409。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import xueqiu_collector as api_module
from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.user import User

PASSWORD = "collector-api-password"
NEW_ID = "1234567890123"


@pytest.fixture
def clients():
    db = SessionLocal()
    users = {u.username: u for u in db.query(User).filter(User.id.in_([1, 2])).all()}
    originals = {name: user.hashed_password for name, user in users.items()}
    for user in users.values():
        user.hashed_password = get_password_hash(PASSWORD)
    db.commit()
    result = {}
    for name in ("admin", "demo"):
        client = TestClient(app)
        token = client.post(
            "/api/auth/token", json={"username": name, "password": PASSWORD}
        ).json()["access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        result[name] = client
    try:
        yield result
    finally:
        db.execute(text("DELETE FROM xueqiu_collector_authors WHERE xueqiu_user_id = :id"), {"id": NEW_ID})
        db.execute(text("UPDATE xueqiu_collector_state SET run_requested_at = NULL WHERE id = 1"))
        for name, user in users.items():
            user.hashed_password = originals[name]
        db.commit()
        db.close()


def test_status_and_authors_readable_by_any_user(clients):
    status = clients["demo"].get("/api/xueqiu-collector/status")
    assert status.status_code == 200
    body = status.json()
    assert body["enabled"] is False  # 默认关闭
    assert body["alive"] in (True, False)
    assert {"level", "message"} <= set(body["cookie"])
    assert isinstance(body["authors"], list)  # 公开镜像的迁移不播种作者名单
    assert isinstance(body["recent_runs"], list)
    authors = clients["demo"].get("/api/xueqiu-collector/authors")
    assert authors.status_code == 200 and len(authors.json()) == len(body["authors"])


def test_author_mutations_are_admin_only(clients):
    user, admin = clients["demo"], clients["admin"]
    payload = {"xueqiu_user_id": NEW_ID, "display_name": "测试作者"}
    assert user.post("/api/xueqiu-collector/authors", json=payload).status_code == 403
    assert user.patch(f"/api/xueqiu-collector/authors/{NEW_ID}", json={}).status_code == 403
    assert user.delete(f"/api/xueqiu-collector/authors/{NEW_ID}").status_code == 403
    assert user.post("/api/xueqiu-collector/run-now").status_code == 403

    created = admin.post("/api/xueqiu-collector/authors", json=payload)
    assert created.status_code == 201
    assert created.json()["enabled"] is True and created.json()["display_name"] == "测试作者"
    assert admin.post("/api/xueqiu-collector/authors", json=payload).status_code == 409

    patched = admin.patch(
        f"/api/xueqiu-collector/authors/{NEW_ID}", json={"enabled": False, "note": " 暂停 "}
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False and patched.json()["note"] == "暂停"
    assert admin.patch("/api/xueqiu-collector/authors/999", json={}).status_code == 404

    assert admin.delete(f"/api/xueqiu-collector/authors/{NEW_ID}").status_code == 204
    assert admin.delete(f"/api/xueqiu-collector/authors/{NEW_ID}").status_code == 404


@pytest.mark.parametrize("bad_id", ["abc", "", "12a", "１２３", "1" * 21, "https://xueqiu.com/u/1"])
def test_author_id_must_be_numeric(clients, bad_id):
    response = clients["admin"].post("/api/xueqiu-collector/authors", json={"xueqiu_user_id": bad_id})
    assert response.status_code == 422


def test_run_now_requires_enabled_collector_and_only_sets_a_flag(clients, monkeypatch):
    admin = clients["admin"]
    disabled = admin.post("/api/xueqiu-collector/run-now")
    assert disabled.status_code == 409 and "未启用" in disabled.json()["detail"]

    monkeypatch.setattr(api_module.settings, "xueqiu_collector_enabled", True)
    requested = admin.post("/api/xueqiu-collector/run-now")
    assert requested.status_code == 200
    body = requested.json()
    assert body["enabled"] is True
    assert body["run_pending"] is True and body["run_requested_at"] is not None
