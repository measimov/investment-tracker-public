"""导入产物的只读标志：交易、公司行动、现金事件同名同判据（#283）。

判据唯一实现在 api/_ownership（annotate_read_only / ensure_record_is_mutable）：带导入批次，
或被券商来源流水引用（无批次的历史回填也算）。此前交易响应没有这个标志、前端只看
import_batch_id——被来源流水链接但无批次的交易会显示「编辑」然后 409。
"""

import hashlib
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.cash_event import CashEvent
from app.models.holding import Holding
from app.models.import_batch import ImportBatch
from app.models.transaction import Transaction
from app.models.user import User
from tests.helpers import make_account, reset_tables

RESET_MODELS = (BrokerFundFlow, CashEvent, Holding, Transaction, ImportBatch, BrokerAccount)
PASSWORD = "read-only-flags-password"


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        reset_tables(session, RESET_MODELS)
        user = session.query(User).filter(User.username == "demo").one()
        original = user.hashed_password
        user.hashed_password = get_password_hash(PASSWORD)
        session.commit()
        yield session, user.id
        session.rollback()
        user = session.get(User, user.id)
        user.hashed_password = original
        session.commit()
        reset_tables(session, RESET_MODELS)
    finally:
        session.close()


def _flow(user_id, account_id, batch_id, key, **links):
    return BrokerFundFlow(
        user_id=user_id,
        broker_account_id=account_id,
        import_batch_id=batch_id,
        source_filename="a.pdf",
        source_row_number=1,
        row_hash=hashlib.sha256(key.encode()).hexdigest(),
        trade_date=date(2026, 7, 2),
        business_name="证券买入",
        security_code="600036",
        security_name="招商银行",
        trade_quantity=Decimal("0"),
        trade_price=Decimal("0"),
        amount=Decimal("0"),
        currency="CNY",
        **links,
    )


@pytest.mark.anyio
async def test_flow_linked_records_without_batch_are_read_only(db):
    session, user_id = db
    account = make_account(session, "招商证券", user_id=user_id, commit=True)
    batch = ImportBatch(
        user_id=user_id,
        broker_account_id=account.id,
        broker="招商证券",
        source_type="cmb_fund_flow",
        source_filename="a.pdf",
        status="COMPLETED",
        row_count=1,
    )
    session.add(batch)
    session.commit()

    def txn(day):
        return Transaction(
            user_id=user_id,
            broker_account_id=account.id,
            symbol="600036",
            name="招商银行",
            market="A股",
            transaction_type="BUY",
            quantity=Decimal("100"),
            price=Decimal("10"),
            fee=Decimal("0"),
            transaction_date=date(2026, 7, day),
            currency="CNY",
        )

    manual, linked = txn(1), txn(2)
    event_manual = CashEvent(
        user_id=user_id,
        broker_account_id=account.id,
        event_type="DEPOSIT",
        amount=Decimal("100"),
        currency="CNY",
        event_date=date(2026, 7, 1),
    )
    event_linked = CashEvent(
        user_id=user_id,
        broker_account_id=account.id,
        event_type="DEPOSIT",
        amount=Decimal("200"),
        currency="CNY",
        event_date=date(2026, 7, 2),
    )
    session.add_all([manual, linked, event_manual, event_linked])
    session.commit()
    session.add_all(
        [
            _flow(user_id, account.id, batch.id, "txn", transaction_id=linked.id),
            _flow(user_id, account.id, batch.id, "cash", cash_event_id=event_linked.id),
        ]
    )
    session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = (
            await client.post("/api/auth/token", json={"username": "demo", "password": PASSWORD})
        ).json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}

        listed = {
            row["id"]: row["read_only"]
            for row in (await client.get("/api/transactions", headers=auth)).json()
        }
        assert listed == {manual.id: False, linked.id: True}
        single = await client.get(f"/api/transactions/{linked.id}", headers=auth)
        assert single.json()["read_only"] is True
        blocked = await client.delete(f"/api/transactions/{linked.id}", headers=auth)
        assert blocked.status_code == 409

        events = {
            row["id"]: row["read_only"]
            for row in (await client.get("/api/cash-events", headers=auth)).json()
        }
        assert events == {event_manual.id: False, event_linked.id: True}
        single_event = await client.get(f"/api/cash-events/{event_linked.id}", headers=auth)
        assert single_event.json()["read_only"] is True
        blocked_event = await client.delete(f"/api/cash-events/{event_linked.id}", headers=auth)
        assert blocked_event.status_code == 409
