"""期初建仓（OPENING_POSITION，#174）的 API / 转仓守卫 / 对账 / 迁移幂等。

内核四处重放的一致性在 test_corporate_action_semantics.py，导入器链路在
test_cmb_fund_flow_importer.py；这里只覆盖"用户能碰到的边"。
"""

import hashlib
from datetime import date
from decimal import Decimal

import httpx
import pytest
from fastapi import HTTPException

from app.api.transactions import create_transfer
from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.cash_event import CashEvent
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.import_batch import ImportBatch
from app.models.reconciliation_snapshot import ReconciliationSnapshot
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.reconciliation_snapshot import ReconciliationPosition, ReconciliationSnapshotCreate
from app.api.reconciliation_snapshots import create_reconciliation_snapshot
from app.schemas.transaction import TransferCreate
from app.services.holding_service import recalculate_holdings
from app.services.portfolio.fifo import fifo_data_quality
from app.services.statistics.fifo_results import fifo_results_for_user
from tests.helpers import get_user, make_account, reset_tables, run_migration

RESET_MODELS = (
    ReconciliationSnapshot,
    BrokerFundFlow,
    CashEvent,
    Holding,
    CorporateAction,
    Transaction,
    ImportBatch,
    BrokerAccount,
)
MIGRATION = "20260920_0020"


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        reset_tables(session, RESET_MODELS)
        yield session
        session.rollback()
        reset_tables(session, RESET_MODELS)
    finally:
        session.close()


@pytest.fixture
def api_user():
    session = SessionLocal()
    try:
        user = session.query(User).filter(User.username == "demo").one()
        original = user.hashed_password
        user.hashed_password = get_password_hash("known-api-password")
        session.commit()
        yield user.id
        user.hashed_password = original
        session.commit()
    finally:
        session.close()


async def _client_auth(client):
    login = await client.post(
        "/api/auth/token", json={"username": "demo", "password": "known-api-password"},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _opening_action(db, *, account_id, quantity="269", cost_per_share=None, total_cost=None,
                    symbol="161226", market="A股", ex_date=date(2026, 1, 10), user_id=1, **extra):
    action = CorporateAction(
        user_id=user_id, broker_account_id=account_id, symbol=symbol, name=symbol, market=market,
        action_type="OPENING_POSITION", ex_date=ex_date, currency="CNY",
        adjusted_quantity=Decimal(quantity),
        adjusted_cost_per_share=Decimal(cost_per_share) if cost_per_share is not None else None,
        cost_basis_adjustment=Decimal(total_cost) if total_cost is not None else None,
        **extra,
    )
    db.add(action)
    db.commit()
    recalculate_holdings(db, user_id, symbol, market)
    db.refresh(action)
    return action


def _holding(db, symbol="161226", user_id=1):
    return db.query(Holding).filter_by(user_id=user_id, symbol=symbol).one()


# --------------------------------------------------------------------------- #
# API：创建 / 补录成本 / 不可变 / 删除
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_api_create_backfill_and_delete_opening_position(db, api_user):
    account = make_account(db, "招商证券", user_id=api_user, commit=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _client_auth(client)

        # 数量缺失 → 422（schema 校验）
        bad = await client.post("/api/corporate-actions", headers=auth, json={
            "symbol": "161226", "market": "A股", "action_type": "OPENING_POSITION",
            "ex_date": "2026-01-10", "broker_account_id": account.id,
        })
        assert bad.status_code == 422

        created = await client.post("/api/corporate-actions", headers=auth, json={
            "symbol": "161226", "name": "白银LOF", "market": "A股",
            "action_type": "OPENING_POSITION", "ex_date": "2026-01-10",
            "broker_account_id": account.id, "adjusted_quantity": "269",
        })
        assert created.status_code == 201, created.text
        action_id = created.json()["id"]
        holding = _holding(db, user_id=api_user)
        assert holding.quantity == Decimal("269") and holding.total_cost == Decimal("0")
        assert holding.unknown_cost_quantity == Decimal("269")
        assert holding.broker_account_id == account.id

        # 非期初建仓不能走补录端点
        dividend = await client.post("/api/corporate-actions", headers=auth, json={
            "symbol": "161226", "market": "A股", "action_type": "CASH_DIVIDEND",
            "ex_date": "2026-02-01", "dividend_per_share": "0.1",
        })
        wrong = await client.patch(
            f"/api/corporate-actions/{dividend.json()['id']}/cost-basis", headers=auth,
            json={"adjusted_cost_per_share": "1"},
        )
        assert wrong.status_code == 422

        # 两个成本不一致 → 422
        inconsistent = await client.patch(
            f"/api/corporate-actions/{action_id}/cost-basis", headers=auth,
            json={"adjusted_cost_per_share": "4", "cost_basis_adjustment": "999"},
        )
        assert inconsistent.status_code == 422

        filled = await client.patch(
            f"/api/corporate-actions/{action_id}/cost-basis", headers=auth,
            json={"adjusted_cost_per_share": "4.5", "notes": "按转出前场外净值补录"},
        )
        assert filled.status_code == 200, filled.text
        assert Decimal(filled.json()["adjusted_cost_per_share"]) == Decimal("4.5")
        db.expire_all()
        holding = _holding(db, user_id=api_user)
        assert holding.total_cost == Decimal("1210.5") and holding.unknown_cost_quantity == 0

        deleted = await client.delete(f"/api/corporate-actions/{action_id}", headers=auth)
        assert deleted.status_code == 204
        db.expire_all()
        assert db.query(Holding).filter_by(symbol="161226").count() == 0


@pytest.mark.anyio
async def test_api_imported_opening_position_only_accepts_cost_backfill(db, api_user):
    """导入建的行动整体只读（409），但 cost-basis 通道对它开放——否则成本永远补不上。"""
    account = make_account(db, "招商证券", user_id=api_user, commit=True)
    batch = ImportBatch(
        user_id=api_user, broker_account_id=account.id, broker="招商证券",
        source_type="cmb_fund_flow", source_filename="a.pdf", status="COMPLETED", row_count=1,
    )
    db.add(batch)
    db.commit()
    action = _opening_action(db, account_id=account.id, import_batch_id=batch.id, user_id=api_user)
    db.add(BrokerFundFlow(
        user_id=api_user, broker_account_id=account.id, import_batch_id=batch.id,
        source_filename="a.pdf", source_row_number=1,
        row_hash=hashlib.sha256(b"imported-opening").hexdigest(), trade_date=date(2026, 1, 10),
        business_name="转托转入", security_code="161226", security_name="白银LOF",
        trade_quantity=Decimal("269"), trade_price=Decimal("0"), amount=Decimal("0"),
        currency="CNY", corporate_action_id=action.id,
    ))
    db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _client_auth(client)
        blocked = await client.put(
            f"/api/corporate-actions/{action.id}", headers=auth, json={"notes": "改备注"},
        )
        assert blocked.status_code == 409
        filled = await client.patch(
            f"/api/corporate-actions/{action.id}/cost-basis", headers=auth,
            json={"cost_basis_adjustment": "1076"},
        )
        assert filled.status_code == 200, filled.text
        db.expire_all()
        assert _holding(db, user_id=api_user).total_cost == Decimal("1076")
        assert _holding(db, user_id=api_user).unknown_cost_quantity == 0
        removed = await client.delete(f"/api/corporate-actions/{action.id}", headers=auth)
        assert removed.status_code == 409


# --------------------------------------------------------------------------- #
# 转仓守卫 / FIFO 估计标记 / 对账
# --------------------------------------------------------------------------- #
def test_transfer_blocked_until_cost_is_backfilled(db):
    cmb = make_account(db, "招商证券")
    other = make_account(db, "东方财富")
    db.commit()
    action = _opening_action(db, account_id=cmb.id)

    def transfer():
        return create_transfer(
            TransferCreate(symbol="161226", market="A股", quantity=Decimal("100"),
                           from_broker_account_id=cmb.id, to_broker_account_id=other.id,
                           transfer_date=date(2026, 2, 1)),
            current_user=get_user(db), db=db,
        )

    with pytest.raises(HTTPException) as exc:
        transfer()
    assert exc.value.status_code == 422 and "补录成本" in exc.value.detail
    assert db.query(Transaction).count() == 0

    action.adjusted_cost_per_share = Decimal("4")
    db.commit()
    recalculate_holdings(db, 1, "161226", "A股")
    transfer()
    db.expire_all()
    quantities = {h.broker_account_id: h.quantity for h in db.query(Holding).filter_by(symbol="161226")}
    assert quantities == {cmb.id: Decimal("169"), other.id: Decimal("100")}


def test_fifo_marks_sales_out_of_unknown_cost_lots_as_estimated(db):
    account = make_account(db, "招商证券")
    db.commit()
    _opening_action(db, account_id=account.id)
    db.add(Transaction(
        user_id=1, broker_account_id=account.id, symbol="161226", name="白银LOF", market="A股",
        transaction_type="SELL", quantity=Decimal("269"), price=Decimal("5"), fee=Decimal("5"),
        currency="CNY", transaction_date=date(2026, 1, 29),
    ))
    db.commit()
    results = fifo_results_for_user(db, 1)
    result = results[("161226", "A股")]
    assert result["estimated_cost_trade_count"] == 1
    assert result["closed_trades"][0]["cost_estimated"] is True
    assert result["closed_trades"][0]["realized_pnl"] == pytest.approx(1340.0)  # 269×5 − 5 − 0
    quality = fifo_data_quality(results)
    assert quality["unknown_cost_lot_count"] == 0  # 已全部卖出，剩余批次里没有未知成本
    assert quality["estimated_cost_trade_count"] == 1
    assert any("成本未知" in w for w in quality["warnings"])


def test_reconciliation_matches_position_built_from_opening_position(db):
    account = make_account(db, "招商证券")
    db.commit()
    _opening_action(db, account_id=account.id)
    snapshot = create_reconciliation_snapshot(
        ReconciliationSnapshotCreate(
            broker_account_id=account.id, snapshot_date=date(2026, 1, 31),
            positions=[ReconciliationPosition(symbol="161226", market="A股", quantity=Decimal("269"))],
            cash_balances={"CNY": Decimal("0")},
        ),
        current_user=get_user(db), db=db,
    )
    assert snapshot.status == "MATCHED", snapshot.diff_detail
    assert snapshot.diff_detail["positions"][0]["status"] == "MATCH"


# --------------------------------------------------------------------------- #
# 迁移幂等
# --------------------------------------------------------------------------- #
def test_migration_backfill_is_idempotent_across_downgrade(db):
    account = make_account(db, "招商证券")
    db.flush()
    orphan = BrokerFundFlow(
        user_id=1, broker_account_id=account.id, source_filename="legacy.pdf",
        source_row_number=3, row_hash=hashlib.sha256(b"orphan").hexdigest(),
        trade_date=date(2025, 12, 20), business_name="转托转入", security_code="161226",
        security_name="白银LOF", trade_quantity=Decimal("269"), trade_price=Decimal("0"),
        amount=Decimal("0"), currency="CNY", notes="原备注",
    )
    out = BrokerFundFlow(
        user_id=1, broker_account_id=account.id, source_filename="legacy.pdf",
        source_row_number=4, row_hash=hashlib.sha256(b"out").hexdigest(),
        trade_date=date(2025, 12, 21), business_name="托管转出", security_code="161226",
        security_name="白银LOF", trade_quantity=Decimal("-10"), trade_price=Decimal("0"),
        amount=Decimal("0"), currency="CNY",
    )
    # 存管账户侧的记账行：同名但无代码、「资金」市场——不是持仓事件，回填必须跳过
    ledger_side = BrokerFundFlow(
        user_id=1, broker_account_id=account.id, source_filename="legacy.pdf",
        source_row_number=5, row_hash=hashlib.sha256(b"ledger").hexdigest(),
        trade_date=date(2025, 12, 22), business_name="转存管转入", security_code=None,
        security_name=None, trade_quantity=Decimal("0"), trade_price=Decimal("0"),
        amount=Decimal("0"), currency="CNY",
    )
    db.add_all([orphan, out, ledger_side])
    db.flush()

    for _ in range(2):
        run_migration(db, MIGRATION, "downgrade")
        run_migration(db, MIGRATION, "upgrade")
    db.expire_all()
    assert orphan.skip_reason == "unbooked_opening_position"
    assert orphan.notes.count("migration 20260920_0020") == 1 and orphan.notes.startswith("原备注")
    assert out.skip_reason is None  # 只回填转入行；托管转出另有语义
    assert ledger_side.skip_reason is None and not ledger_side.notes
    assert hasattr(Holding, "unknown_cost_quantity")
