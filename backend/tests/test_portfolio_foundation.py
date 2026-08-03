from datetime import date
from uuid import uuid4

import httpx
import pytest

from app.database import SessionLocal
from app.main import app
from app.models.user import User
from app.services.auth_session_service import issue_session
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.cash_event import CashEvent
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.import_batch import ImportBatch
from app.models.reconciliation_snapshot import ReconciliationSnapshot
from app.models.transaction import Transaction



def _headers(username: str) -> dict[str, str]:
    # Tokens must be backed by a server-side session since issue #36.
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).one()
        token, _ = issue_session(db, user)
    finally:
        db.close()
    return {"Authorization": f"Bearer {token}"}


def _row_hash() -> str:
    return uuid4().hex * 2


@pytest.fixture
def foundation_tag():
    tag = f"foundation-{uuid4().hex[:12]}"
    yield tag

    db = SessionLocal()
    try:
        db.query(BrokerFundFlow).filter(
            BrokerFundFlow.source_filename == tag
        ).delete(synchronize_session=False)
        db.query(IbkrActivityFlow).filter(
            IbkrActivityFlow.source_filename == tag
        ).delete(synchronize_session=False)
        db.query(CorporateAction).filter(
            CorporateAction.notes.like(f"{tag}%")
        ).delete(synchronize_session=False)
        db.query(Transaction).filter(
            Transaction.notes.like(f"{tag}%")
        ).delete(synchronize_session=False)
        db.query(Holding).filter(
            Holding.symbol.like("FD%")
        ).delete(synchronize_session=False)
        db.query(CashEvent).filter(
            CashEvent.notes.like(f"{tag}%")
        ).delete(synchronize_session=False)
        db.query(ReconciliationSnapshot).filter(
            ReconciliationSnapshot.notes.like(f"{tag}%")
        ).delete(synchronize_session=False)
        db.query(ImportBatch).filter(
            ImportBatch.source_filename == tag
        ).delete(synchronize_session=False)
        db.query(BrokerAccount).filter(
            BrokerAccount.notes.like(f"{tag}%")
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_broker_flow_metadata_declares_unassigned_partial_unique_index():
    index = next(
        index
        for index in BrokerFundFlow.__table__.indexes
        if index.name == "uix_broker_flow_user_hash_unassigned"
    )

    assert index.unique is True
    assert [column.name for column in index.columns] == ["user_id", "row_hash"]
    assert (
        str(index.dialect_options["postgresql"]["where"])
        == "broker_account_id IS NULL"
    )


@pytest.mark.anyio
async def test_account_cash_and_reconciliation_crud_are_user_isolated_and_auditable(
    foundation_tag,
):
    transport = httpx.ASGITransport(app=app)
    user_headers = _headers("demo")
    admin_headers = _headers("admin")

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        created_account = await client.post(
            "/api/broker-accounts",
            headers=user_headers,
            json={
                "broker": foundation_tag,
                "account_name": "Primary",
                "account_number_masked": "****1234",
                "base_currency": "USD",
                "notes": foundation_tag,
            },
        )
        assert created_account.status_code == 201
        account_id = created_account.json()["id"]
        assert created_account.json()["account_number_masked"] == "****1234"
        assert "account_number" not in created_account.json()

        account_list = await client.get(
            "/api/broker-accounts",
            headers=user_headers,
            params={"broker": foundation_tag},
        )
        assert [account["id"] for account in account_list.json()] == [account_id]
        assert (
            await client.get(
                f"/api/broker-accounts/{account_id}",
                headers=admin_headers,
            )
        ).status_code == 404

        updated_account = await client.put(
            f"/api/broker-accounts/{account_id}",
            headers=user_headers,
            json={"account_name": "Long-term"},
        )
        assert updated_account.status_code == 200
        assert updated_account.json()["account_name"] == "Long-term"

        foreign_cash = await client.post(
            "/api/cash-events",
            headers=admin_headers,
            json={
                "broker_account_id": account_id,
                "event_type": "DEPOSIT",
                "amount": "1",
                "currency": "USD",
                "event_date": "2026-07-26",
            },
        )
        assert foreign_cash.status_code == 404

        created_cash = await client.post(
            "/api/cash-events",
            headers=user_headers,
            json={
                "broker_account_id": account_id,
                "event_type": "DEPOSIT",
                "amount": "1000.50",
                "currency": "USD",
                "event_date": "2026-07-25",
                "notes": foundation_tag,
            },
        )
        assert created_cash.status_code == 201
        cash_id = created_cash.json()["id"]
        frozen_broker = await client.put(
            f"/api/broker-accounts/{account_id}",
            headers=user_headers,
            json={"broker": "Changed broker"},
        )
        assert frozen_broker.status_code == 409

        updated_cash = await client.put(
            f"/api/cash-events/{cash_id}",
            headers=user_headers,
            json={"notes": f"{foundation_tag}-updated"},
        )
        assert updated_cash.status_code == 200
        assert updated_cash.json()["notes"].endswith("-updated")
        cash_list = await client.get(
            "/api/cash-events",
            headers=user_headers,
            params={"broker_account_id": account_id},
        )
        assert cash_id in [event["id"] for event in cash_list.json()]

        foreign_snapshot = await client.post(
            "/api/reconciliation-snapshots",
            headers=admin_headers,
            json={
                "broker_account_id": account_id,
                "snapshot_date": "2026-07-25",
            },
        )
        assert foreign_snapshot.status_code == 404

        created_snapshot = await client.post(
            "/api/reconciliation-snapshots",
            headers=user_headers,
            json={
                "broker_account_id": account_id,
                "snapshot_date": "2026-07-25",
                "source_filename": "statement.pdf",
                "cash_balances": {"USD": "1000.50", "HKD": "80"},
                "positions": [
                    {
                        "symbol": "AAPL",
                        "market": "美股",
                        "quantity": "2.5",
                        "currency": "USD",
                    }
                ],
                "notes": foundation_tag,
            },
        )
        assert created_snapshot.status_code == 201
        snapshot_id = created_snapshot.json()["id"]
        assert created_snapshot.json()["source_filename"] == "statement.pdf"
        assert created_snapshot.json()["positions"][0]["symbol"] == "AAPL"

        # status 由自动比对派生：账本里没有 AAPL 2.5 股 → 必为 MISMATCHED，
        # 且客户端不能再手工设置 status（schema 已移除该字段）。
        assert created_snapshot.json()["status"] == "MISMATCHED"
        assert created_snapshot.json()["diff_detail"]["summary"]["position_mismatches"] >= 1

        updated_snapshot = await client.put(
            f"/api/reconciliation-snapshots/{snapshot_id}",
            headers=user_headers,
            json={"notes": foundation_tag + " updated"},
        )
        assert updated_snapshot.status_code == 200
        assert updated_snapshot.json()["status"] == "MISMATCHED"
        assert updated_snapshot.json()["compared_at"] is not None
        snapshot_list = await client.get(
            "/api/reconciliation-snapshots",
            headers=user_headers,
            params={"broker_account_id": account_id},
        )
        assert snapshot_id in [snapshot["id"] for snapshot in snapshot_list.json()]

        deleted_account = await client.delete(
            f"/api/broker-accounts/{account_id}",
            headers=user_headers,
        )
        assert deleted_account.status_code == 409
        assert "Deactivate" in deleted_account.json()["detail"]
        assert (
            await client.get(f"/api/cash-events/{cash_id}", headers=user_headers)
        ).json()["broker_account_id"] == account_id
        assert (
            await client.get(
                f"/api/reconciliation-snapshots/{snapshot_id}",
                headers=user_headers,
            )
        ).json()["broker_account_id"] == account_id

        assert (
            await client.delete(f"/api/cash-events/{cash_id}", headers=user_headers)
        ).status_code == 204
        assert (
            await client.delete(
                f"/api/reconciliation-snapshots/{snapshot_id}",
                headers=user_headers,
            )
        ).status_code == 204
        assert (
            await client.delete(
                f"/api/broker-accounts/{account_id}",
                headers=user_headers,
            )
        ).status_code == 204


@pytest.mark.anyio
async def test_manual_sell_validation_does_not_borrow_another_accounts_position(
    foundation_tag,
):
    transport = httpx.ASGITransport(app=app)
    headers = _headers("demo")
    symbol = f"FDA{foundation_tag.rsplit('-', 1)[-1][:8].upper()}"[:20]

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        account_ids = []
        for name in ("First", "Second"):
            response = await client.post(
                "/api/broker-accounts",
                headers=headers,
                json={
                    "broker": foundation_tag,
                    "account_name": name,
                    "base_currency": "USD",
                    "notes": foundation_tag,
                },
            )
            assert response.status_code == 201
            account_ids.append(response.json()["id"])

        buy = await client.post(
            "/api/transactions",
            headers=headers,
            json={
                "broker_account_id": account_ids[0],
                "symbol": symbol,
                "market": "美股",
                "transaction_type": "BUY",
                "quantity": "10",
                "price": "1",
                "fee": "0",
                "transaction_date": "2026-07-20",
                "currency": "USD",
                "notes": foundation_tag,
            },
        )
        assert buy.status_code == 201

        cross_account_sell = await client.post(
            "/api/transactions",
            headers=headers,
            json={
                "broker_account_id": account_ids[1],
                "symbol": symbol,
                "market": "美股",
                "transaction_type": "SELL",
                "quantity": "1",
                "price": "1",
                "fee": "0",
                "transaction_date": "2026-07-21",
                "currency": "USD",
                "notes": foundation_tag,
            },
        )
        assert cross_account_sell.status_code == 400
        assert "available quantity 0" in cross_account_sell.json()["detail"]


@pytest.mark.anyio
async def test_import_audit_fields_and_imported_records_are_read_only(
    foundation_tag,
):
    suffix = foundation_tag.rsplit("-", 1)[-1][:8].upper()
    db = SessionLocal()
    try:
        account = BrokerAccount(
            user_id=2,
            broker=foundation_tag,
            account_name="Immutable imports",
            notes=foundation_tag,
        )
        admin_account = BrokerAccount(
            user_id=1,
            broker=foundation_tag,
            account_name="Admin batch account",
            notes=foundation_tag,
        )
        db.add_all([account, admin_account])
        db.flush()
        batch = ImportBatch(
            user_id=2,
            broker_account_id=account.id,
            broker=foundation_tag,
            source_type="ACTIVITY",
            source_filename=foundation_tag,
            status="COMPLETED",
        )
        admin_batch = ImportBatch(
            user_id=1,
            broker_account_id=admin_account.id,
            broker=foundation_tag,
            source_type="ACTIVITY",
            source_filename=foundation_tag,
            status="COMPLETED",
        )
        db.add_all([batch, admin_batch])
        db.flush()
        user_account_id = account.id
        admin_account_id = admin_account.id
        user_batch_id = batch.id
        admin_batch_id = admin_batch.id

        broker_transaction = Transaction(
            user_id=2,
            broker_account_id=account.id,
            import_batch_id=batch.id,
            symbol=f"FDTB{suffix}"[:20],
            name="Broker imported transaction",
            market="美股",
            transaction_type="BUY",
            quantity=1,
            price=10,
            fee=0,
            transaction_date=date(2026, 7, 20),
            currency="USD",
            notes=foundation_tag,
        )
        ibkr_transaction = Transaction(
            user_id=2,
            broker_account_id=account.id,
            import_batch_id=batch.id,
            symbol=f"FDTI{suffix}"[:20],
            name="IBKR imported transaction",
            market="美股",
            transaction_type="BUY",
            quantity=1,
            price=10,
            fee=0,
            transaction_date=date(2026, 7, 20),
            currency="USD",
            notes=foundation_tag,
        )
        broker_action = CorporateAction(
            user_id=2,
            broker_account_id=account.id,
            import_batch_id=batch.id,
            symbol=f"FDAB{suffix}"[:20],
            name="Broker imported action",
            market="港股",
            action_type="CASH_DIVIDEND",
            ex_date=date(2026, 7, 21),
            total_dividend=10,
            net_dividend=10,
            currency="HKD",
            notes=foundation_tag,
        )
        ibkr_action = CorporateAction(
            user_id=2,
            broker_account_id=account.id,
            import_batch_id=batch.id,
            symbol=f"FDAI{suffix}"[:20],
            name="IBKR imported action",
            market="美股",
            action_type="CASH_DIVIDEND",
            ex_date=date(2026, 7, 21),
            total_dividend=10,
            net_dividend=10,
            currency="USD",
            notes=foundation_tag,
        )
        batch_only_transaction = Transaction(
            user_id=2,
            broker_account_id=account.id,
            import_batch_id=batch.id,
            symbol=f"FDTX{suffix}"[:20],
            name="Synthetic imported transaction",
            market="美股",
            transaction_type="BUY",
            quantity=1,
            price=10,
            fee=0,
            transaction_date=date(2026, 7, 20),
            currency="USD",
            notes=foundation_tag,
        )
        batch_only_action = CorporateAction(
            user_id=2,
            broker_account_id=account.id,
            import_batch_id=batch.id,
            symbol=f"FDAX{suffix}"[:20],
            name="Synthetic imported action",
            market="美股",
            action_type="CASH_DIVIDEND",
            ex_date=date(2026, 7, 21),
            total_dividend=10,
            net_dividend=10,
            currency="USD",
            notes=foundation_tag,
        )
        cash_event = CashEvent(
            user_id=2,
            broker_account_id=account.id,
            event_type="FEE",
            amount=1,
            currency="HKD",
            event_date=date(2026, 7, 22),
            notes=foundation_tag,
        )
        snapshot = ReconciliationSnapshot(
            user_id=2,
            broker_account_id=account.id,
            import_batch_id=batch.id,
            snapshot_date=date(2026, 7, 23),
            status="MISMATCHED",
            source_filename=foundation_tag,
            cash_balances={"HKD": "100"},
            positions=[],
            notes=foundation_tag,
        )
        db.add_all(
            [
                broker_transaction,
                ibkr_transaction,
                broker_action,
                ibkr_action,
                batch_only_transaction,
                batch_only_action,
                cash_event,
                snapshot,
            ]
        )
        db.flush()

        db.add_all(
            [
                BrokerFundFlow(
                    user_id=2,
                    broker_account_id=account.id,
                    transaction_id=broker_transaction.id,
                    import_batch_id=batch.id,
                    broker=foundation_tag,
                    row_hash=_row_hash(),
                    source_filename=foundation_tag,
                    trade_date=date(2026, 7, 20),
                    trade_price=10,
                    trade_quantity=1,
                    amount=-10,
                    business_name="BUY",
                ),
                BrokerFundFlow(
                    user_id=2,
                    broker_account_id=account.id,
                    corporate_action_id=broker_action.id,
                    import_batch_id=batch.id,
                    broker=foundation_tag,
                    row_hash=_row_hash(),
                    source_filename=foundation_tag,
                    trade_date=date(2026, 7, 21),
                    trade_price=0,
                    trade_quantity=0,
                    amount=10,
                    business_name="DIVIDEND",
                ),
                BrokerFundFlow(
                    user_id=2,
                    broker_account_id=account.id,
                    cash_event_id=cash_event.id,
                    import_batch_id=batch.id,
                    broker=foundation_tag,
                    row_hash=_row_hash(),
                    source_filename=foundation_tag,
                    trade_date=date(2026, 7, 22),
                    trade_price=0,
                    trade_quantity=0,
                    amount=-1,
                    business_name="FEE",
                ),
                IbkrActivityFlow(
                    user_id=2,
                    transaction_id=ibkr_transaction.id,
                    import_batch_id=batch.id,
                    broker="IBKR",
                    row_hash=_row_hash(),
                    source_filename=foundation_tag,
                    source_row_number=1,
                    trade_date=date(2026, 7, 20),
                    activity_type="BUY",
                ),
                IbkrActivityFlow(
                    user_id=2,
                    corporate_action_id=ibkr_action.id,
                    import_batch_id=batch.id,
                    broker="IBKR",
                    row_hash=_row_hash(),
                    source_filename=foundation_tag,
                    source_row_number=2,
                    trade_date=date(2026, 7, 21),
                    activity_type="DIVIDEND",
                ),
            ]
        )
        db.commit()

        protected_records = [
            (
                f"/api/transactions/{broker_transaction.id}",
                {"notes": f"{foundation_tag}-changed"},
            ),
            (
                f"/api/transactions/{ibkr_transaction.id}",
                {"notes": f"{foundation_tag}-changed"},
            ),
            (
                f"/api/corporate-actions/{broker_action.id}",
                {"notes": f"{foundation_tag}-changed"},
            ),
            (
                f"/api/corporate-actions/{ibkr_action.id}",
                {"notes": f"{foundation_tag}-changed"},
            ),
            (
                f"/api/transactions/{batch_only_transaction.id}",
                {"notes": f"{foundation_tag}-changed"},
            ),
            (
                f"/api/corporate-actions/{batch_only_action.id}",
                {"notes": f"{foundation_tag}-changed"},
            ),
            (
                f"/api/cash-events/{cash_event.id}",
                {"notes": f"{foundation_tag}-changed"},
            ),
            (
                f"/api/reconciliation-snapshots/{snapshot.id}",
                {"status": "MATCHED"},
            ),
        ]
    finally:
        db.close()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        for endpoint, update_payload in protected_records:
            updated = await client.put(
                endpoint,
                headers=_headers("demo"),
                json=update_payload,
            )
            assert updated.status_code == 409
            assert "Imported" in updated.json()["detail"]

            deleted = await client.delete(
                endpoint,
                headers=_headers("demo"),
            )
            assert deleted.status_code == 409
            assert "Imported" in deleted.json()["detail"]

    db = SessionLocal()
    try:
        assert db.get(Transaction, broker_transaction.id).notes == foundation_tag
        assert db.get(Transaction, ibkr_transaction.id).notes == foundation_tag
        assert db.get(CorporateAction, broker_action.id).notes == foundation_tag
        assert db.get(CorporateAction, ibkr_action.id).notes == foundation_tag
        assert db.get(Transaction, batch_only_transaction.id).notes == foundation_tag
        assert db.get(CorporateAction, batch_only_action.id).notes == foundation_tag
        assert db.get(CashEvent, cash_event.id).notes == foundation_tag
        assert db.get(ReconciliationSnapshot, snapshot.id).status == "MISMATCHED"
    finally:
        db.close()

    transport = httpx.ASGITransport(app=app)
    user_headers = _headers("demo")
    suffix = foundation_tag.rsplit("-", 1)[-1][:8].upper()
    symbol = f"FDB{suffix}"[:20]

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        batches = await client.get(
            "/api/import-batches",
            headers=user_headers,
            params={"broker": foundation_tag},
        )
        assert batches.status_code == 200
        assert [batch["id"] for batch in batches.json()] == [user_batch_id]
        assert (
            await client.get(
                f"/api/import-batches/{admin_batch_id}",
                headers=user_headers,
            )
        ).status_code == 404
        assert (
            await client.post(
                "/api/import-batches",
                headers=user_headers,
                json={},
            )
        ).status_code == 405

        forged_transaction = await client.post(
            "/api/transactions",
            headers=user_headers,
            json={
                "broker_account_id": user_account_id,
                "import_batch_id": admin_batch_id,
                "symbol": symbol,
                "name": "Audit field",
                "market": "美股",
                "transaction_type": "BUY",
                "quantity": "1",
                "price": "1",
                "fee": "0",
                "transaction_date": "2026-07-26",
                "currency": "USD",
                "notes": foundation_tag,
            },
        )
        assert forged_transaction.status_code == 422

        forged_action = await client.post(
            "/api/corporate-actions",
            headers=user_headers,
            json={
                "broker_account_id": user_account_id,
                "import_batch_id": admin_batch_id,
                "symbol": symbol,
                "market": "美股",
                "action_type": "CASH_DIVIDEND",
                "ex_date": "2026-07-26",
                "currency": "USD",
                "notes": foundation_tag,
            },
        )
        assert forged_action.status_code == 422

        wrong_action_account = await client.post(
            "/api/corporate-actions",
            headers=user_headers,
            json={
                "broker_account_id": admin_account_id,
                "symbol": symbol,
                "market": "美股",
                "action_type": "CASH_DIVIDEND",
                "ex_date": "2026-07-26",
                "currency": "USD",
                "notes": foundation_tag,
            },
        )
        assert wrong_action_account.status_code == 404

        assigned_action = await client.post(
            "/api/corporate-actions",
            headers=user_headers,
            json={
                "broker_account_id": user_account_id,
                "symbol": symbol,
                "market": "美股",
                "action_type": "CASH_DIVIDEND",
                "ex_date": "2026-07-24",
                "currency": "USD",
                "notes": foundation_tag,
            },
        )
        assert assigned_action.status_code == 201
        unassigned_action = await client.post(
            "/api/corporate-actions",
            headers=user_headers,
            json={
                "symbol": symbol,
                "market": "美股",
                "action_type": "CASH_DIVIDEND",
                "ex_date": "2026-07-23",
                "currency": "USD",
                "notes": foundation_tag,
            },
        )
        assert unassigned_action.status_code == 201

        assigned_list = await client.get(
            "/api/corporate-actions",
            headers=user_headers,
            params={"broker_account_id": user_account_id, "symbol": symbol},
        )
        assert [item["id"] for item in assigned_list.json()] == [
            assigned_action.json()["id"]
        ]
        unassigned_count = await client.get(
            "/api/corporate-actions/count",
            headers=user_headers,
            params={"unassigned_account": True, "symbol": symbol},
        )
        assert unassigned_count.json()["total"] == 1

    db = SessionLocal()
    try:
        audit_transaction = Transaction(
            user_id=2,
            broker_account_id=user_account_id,
            import_batch_id=user_batch_id,
            symbol=symbol,
            name="Imported",
            market="美股",
            transaction_type="BUY",
            quantity=1,
            price=1,
            fee=0,
            transaction_date=date(2026, 7, 26),
            currency="USD",
            notes=foundation_tag,
        )
        audit_action = CorporateAction(
            user_id=2,
            broker_account_id=user_account_id,
            import_batch_id=user_batch_id,
            symbol=symbol,
            market="美股",
            action_type="CASH_DIVIDEND",
            ex_date=date(2026, 7, 26),
            currency="USD",
            notes=foundation_tag,
        )
        broker_flow = BrokerFundFlow(
            user_id=2,
            import_batch_id=user_batch_id,
            row_hash=uuid4().hex,
            source_filename=foundation_tag,
            trade_date=date(2026, 7, 26),
            trade_price=0,
            trade_quantity=0,
            amount=0,
            business_name="OTHER",
        )
        ibkr_flow = IbkrActivityFlow(
            user_id=2,
            import_batch_id=user_batch_id,
            row_hash=uuid4().hex,
            source_filename=foundation_tag,
            source_row_number=1,
            trade_date=date(2026, 7, 26),
            activity_type="OTHER",
        )
        db.add_all([audit_transaction, audit_action, broker_flow, ibkr_flow])
        db.commit()
        assert audit_transaction.import_batch_id == user_batch_id
        assert audit_action.import_batch_id == user_batch_id
        assert broker_flow.import_batch_id == user_batch_id
        assert ibkr_flow.import_batch_id == user_batch_id
    finally:
        db.close()
