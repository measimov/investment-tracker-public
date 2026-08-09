"""admin 持仓端点的响应契约（issue #137 子项 4）。

- 不存在的 user_id 是 404，不是空列表（后者与"该用户没有持仓"混为一谈，
  与 GET /api/users/{user_id} 的行为也不一致）——修复前本用例红。
- username 只属于 admin 视图（AdminHoldingResponse）；普通持仓响应不再携带
  恒为 null 的 username 字段。
"""

from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.holding import Holding
from app.models.user import User


def _admin_client(db):
    user = db.query(User).filter(User.id == 1).one()
    original_password = user.hashed_password
    user.hashed_password = get_password_hash("admin-holdings-password")
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": user.username, "password": "admin-holdings-password"},
    )
    assert login.status_code == 200
    return client, user, original_password


def test_admin_user_holdings_returns_404_for_missing_user():
    db = SessionLocal()
    try:
        client, user, original_password = _admin_client(db)
        try:
            response = client.get("/api/holdings/admin/users/999999999")
            assert response.status_code == 404, (
                "不存在的用户应 404（空列表会与'该用户没有持仓'混为一谈）"
            )
        finally:
            user.hashed_password = original_password
            db.commit()
    finally:
        db.close()


def test_username_only_in_admin_view():
    db = SessionLocal()
    symbol = f"T{uuid4().hex[:8].upper()}"
    holding = Holding(
        user_id=1,
        symbol=symbol,
        name="契约标的",
        market="A股",
        quantity=Decimal("100"),
        avg_cost=Decimal("10"),
        total_cost=Decimal("1000"),
        currency="CNY",
    )
    try:
        db.add(holding)
        db.commit()

        client, user, original_password = _admin_client(db)
        try:
            admin_rows = client.get("/api/holdings/admin/all").json()
            admin_row = next(row for row in admin_rows if row["symbol"] == symbol)
            assert admin_row["username"] == user.username

            user_rows = client.get("/api/holdings").json()
            user_row = next(row for row in user_rows if row["symbol"] == symbol)
            assert "username" not in user_row, "普通持仓响应不应携带恒为 null 的 username"
        finally:
            user.hashed_password = original_password
            db.commit()
    finally:
        db.query(Holding).filter(Holding.symbol == symbol).delete(synchronize_session=False)
        db.commit()
        db.close()
