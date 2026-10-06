"""交易日期筛选：真实 API 列表与计数共用含首尾的范围，且不越过用户/账户边界。"""

from datetime import date
from uuid import uuid4

import httpx
import pytest

from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.user import User

from .helpers import add_transaction, make_account


@pytest.fixture
def ledger():
    password = "date-filter-test-password"
    db = SessionLocal()
    users = [
        User(
            username=f"date_filter_{uuid4().hex}",
            hashed_password=get_password_hash(password),
            is_active=True,
            is_admin=False,
        )
        for _ in range(2)
    ]
    try:
        db.add_all(users)
        db.flush()
        owner, other = users
        account = make_account(db, user_id=owner.id)
        foreign_account = make_account(db, user_id=other.id)
        rows = {}
        for day, account_id in [(1, account.id), (2, account.id), (3, None), (4, account.id)]:
            row = add_transaction(
                db,
                user_id=owner.id,
                transaction_date=date(2026, 1, day),
                broker_account_id=account_id,
            )
            rows[day] = row.id
        add_transaction(
            db,
            user_id=other.id,
            transaction_date=date(2026, 1, 2),
            broker_account_id=foreign_account.id,
        )
        db.commit()
        yield {
            "username": owner.username,
            "password": password,
            "account": account.id,
            "foreign_account": foreign_account.id,
            "rows": rows,
        }
    finally:
        db.rollback()
        for user in users:
            db.query(User).filter(User.id == user.id).delete()
        db.commit()
        db.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "params,days",
    [
        ({}, [4, 3, 2, 1]),
        ({"start_date": "2026-01-02", "end_date": "2026-01-03"}, [3, 2]),
        ({"start_date": "2026-01-02", "end_date": "2026-01-02"}, [2]),
        ({"start_date": "2026-01-03"}, [4, 3]),
        ({"end_date": "2026-01-02"}, [2, 1]),
        ({"start_date": "2026-02-01"}, []),
        ({"start_date": "2026-01-02", "broker_account_id": "account"}, [4, 2]),
        ({"start_date": "2026-01-02", "unassigned_account": True}, [3]),
        ({"start_date": "2026-01-02", "broker_account_id": "foreign_account"}, []),
        (
            {
                "start_date": "2026-01-02",
                "symbol": "AAPL",
                "market": "美股",
                "transaction_type": "BUY",
            },
            [4, 3, 2],
        ),
    ],
)
async def test_date_filtered_list_count_ownership_and_pagination(ledger, params, days):
    params = {
        key: ledger[value] if value in ("account", "foreign_account") else value
        for key, value in params.items()
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        login = await client.post(
            "/api/auth/token",
            json={"username": ledger["username"], "password": ledger["password"]},
        )
        assert login.status_code == 200, login.text
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        listing = await client.get("/api/transactions", params=params)
        count = await client.get("/api/transactions/count", params=params)
        assert listing.status_code == count.status_code == 200
        assert [row["id"] for row in listing.json()] == [ledger["rows"][day] for day in days]
        assert count.json() == {"total": len(days)}
        page = await client.get("/api/transactions", params={**params, "skip": 1, "limit": 1})
        assert page.status_code == 200
        assert [row["id"] for row in page.json()] == [ledger["rows"][day] for day in days[1:2]]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "params",
    [
        {"start_date": "2026-01-04", "end_date": "2026-01-01"},
        {"start_date": "2026-02-30"},
        {"end_date": "not-a-date"},
    ],
)
async def test_invalid_date_range_rejected_by_list_and_count(ledger, params):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        login = await client.post(
            "/api/auth/token",
            json={"username": ledger["username"], "password": ledger["password"]},
        )
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        for path in ("/api/transactions", "/api/transactions/count"):
            response = await client.get(path, params=params)
            assert response.status_code == 422, response.text
            if params.get("start_date") == "2026-01-04":
                assert response.json()["detail"] == "开始日期不能晚于结束日期"
