"""删除用户时，所有用户级表必须由数据库级联清空。

压缩迁移基线（20260728_0001）把 5 张表的 user_id 外键从 NO ACTION 改成
ON DELETE CASCADE，并删掉了 api/users.py 里逐表手工删除的兜底代码。
本测试守住那份兜底代码被删掉后仍然成立的前提：级联真的发生，且不留孤儿行。
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.database import SessionLocal
from app.models.auth_session import AuthSession
from app.models.background_job import BackgroundJob
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.cash_event import CashEvent
from app.models.corporate_action import CorporateAction
from app.models.excluded_security import ExcludedSecurity
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.import_batch import ImportBatch
from app.models.llm_report import LlmReport, LlmReportMessage, LlmReportSchedule
from app.models.reconciliation_snapshot import ReconciliationSnapshot
from app.models.transaction import Transaction
from app.models.user import User

# 每一张直接持有 user_id 的表都必须被级联覆盖。
# 这份清单由 test_table_list_matches_schema 对着库里的实际情况断言，
# 新增用户级表却忘了加进来会立刻失败。
USER_SCOPED_TABLES = [
    "auth_sessions",
    "background_jobs",
    "broker_accounts",
    "broker_fund_flows",
    "cash_events",
    "corporate_actions",
    "excluded_securities",
    "holdings",
    "ibkr_activity_flows",
    "import_batches",
    "llm_report_messages",
    "llm_report_schedules",
    "llm_reports",
    "reconciliation_snapshots",
    "transactions",
]


@pytest.fixture
def doomed_user():
    """建一个带全套关联数据的用户，测试结束确保清理。"""
    db = SessionLocal()
    # 上一轮若中途失败可能留下残行，先清干净保证可重复运行
    db.execute(text("DELETE FROM users WHERE username = 'cascade_probe'"))
    db.commit()
    user = User(
        username="cascade_probe",
        email="cascade_probe@example.com",
        hashed_password="x",
        is_active=True,
        is_admin=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    uid = user.id

    account = BrokerAccount(user_id=uid, broker="TEST", account_name="probe")
    batch = ImportBatch(user_id=uid, broker="TEST", source_type="STANDARD_CSV")
    db.add_all([account, batch])
    db.commit()
    db.refresh(account)
    db.refresh(batch)

    txn = Transaction(
        user_id=uid,
        broker_account_id=account.id,
        symbol="AAPL",
        name="Apple",
        market="美股",
        transaction_type="BUY",
        quantity=Decimal("10"),
        price=Decimal("100"),
        fee=Decimal("1"),
        transaction_date=date(2026, 1, 5),
        currency="USD",
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)

    report = LlmReport(
        user_id=uid,
        title="cascade-probe",
        content="x",
        model="m",
        trigger_source="manual",
        input_payload={},
    )
    db.add(report)
    db.flush()

    db.add_all([
        Holding(
            user_id=uid,
            broker_account_id=account.id,
            symbol="AAPL",
            market="美股",
            quantity=Decimal("10"),
            avg_cost=Decimal("100.1"),
            total_cost=Decimal("1001"),
            currency="USD",
        ),
        CorporateAction(
            user_id=uid,
            broker_account_id=account.id,
            symbol="AAPL",
            market="美股",
            action_type="CASH_DIVIDEND",
            ex_date=date(2026, 2, 1),
            currency="USD",
        ),
        ExcludedSecurity(
            user_id=uid,
            symbol="511880",
            market="A股",
        ),
        LlmReportSchedule(user_id=uid, cadence="weekly"),
        LlmReportMessage(report_id=report.id, user_id=uid, role="user", content="q"),
        BrokerFundFlow(
            user_id=uid,
            broker_account_id=account.id,
            transaction_id=txn.id,
            broker="TEST",
            business_name="证券买入",
            trade_date=date(2026, 1, 5),
            trade_price=Decimal("100"),
            trade_quantity=Decimal("10"),
            amount=Decimal("-1001"),
            currency="USD",
            row_hash="cascade-probe-flow",
            created_at=datetime.now(timezone.utc),
        ),
        IbkrActivityFlow(
            user_id=uid,
            broker_account_id=account.id,
            transaction_id=txn.id,
            broker="IBKR",
            activity_type="TRADE",
            base_currency="USD",
            source_row_number=1,
            trade_date=date(2026, 1, 5),
            row_hash="cascade-probe-ibkr",
        ),
        CashEvent(
            user_id=uid,
            broker_account_id=account.id,
            event_type="DEPOSIT",
            amount=Decimal("5000"),
            event_date=date(2026, 1, 2),
            currency="USD",
        ),
        ReconciliationSnapshot(
            user_id=uid,
            broker_account_id=account.id,
            import_batch_id=batch.id,
            snapshot_date=date(2026, 1, 31),
            cash_balances={"USD": "3999"},
            positions=[{"symbol": "AAPL", "market": "美股", "quantity": "10"}],
        ),
        BackgroundJob(
            id="cascade-probe-job",
            user_id=uid,
            job_type="price_refresh",
            status="succeeded",
            data={},
        ),
        AuthSession(
            id="cascade-probe-session",
            user_id=uid,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ),
    ])
    db.commit()
    db.close()

    yield uid

    cleanup = SessionLocal()
    try:
        cleanup.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
        cleanup.commit()
    finally:
        cleanup.close()


def test_table_list_matches_schema():
    """USER_SCOPED_TABLES 必须等于库里所有直接含 user_id 的表。

    新增用户级表却忘了纳入级联覆盖时，这里立刻失败 —— 否则遗漏的表
    只会在真正删用户时才暴露（外键报错，或悄悄留下孤儿行）。
    """
    db = SessionLocal()
    try:
        actual = {
            r[0]
            for r in db.execute(
                text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND column_name = 'user_id'"
                )
            )
        }
    finally:
        db.close()
    assert set(USER_SCOPED_TABLES) == actual, (
        f"清单与 schema 不一致；漏了 {sorted(actual - set(USER_SCOPED_TABLES))}，"
        f"多了 {sorted(set(USER_SCOPED_TABLES) - actual)}"
    )


def _counts(db, uid):
    return {
        table: db.execute(
            text(f"SELECT count(*) FROM {table} WHERE user_id = :u"), {"u": uid}
        ).scalar()
        for table in USER_SCOPED_TABLES
    }


def test_deleting_user_cascades_every_user_scoped_table(doomed_user):
    uid = doomed_user
    db = SessionLocal()
    try:
        before = _counts(db, uid)
        assert all(n > 0 for n in before.values()), f"前置数据未建全: {before}"

        # 一条语句，不做任何手工按序删除
        db.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
        db.commit()

        after = _counts(db, uid)
        assert after == {t: 0 for t in USER_SCOPED_TABLES}, f"级联未清空: {after}"
        assert db.query(User).filter(User.id == uid).first() is None
    finally:
        db.close()


def test_no_orphan_rows_left_behind(doomed_user):
    """级联后不得留下指向已删用户的孤儿行。"""
    uid = doomed_user
    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
        db.commit()
        for table in USER_SCOPED_TABLES:
            orphans = db.execute(
                text(
                    f"SELECT count(*) FROM {table} t "
                    "LEFT JOIN users u ON u.id = t.user_id WHERE u.id IS NULL"
                )
            ).scalar()
            assert orphans == 0, f"{table} 残留 {orphans} 条孤儿行"
    finally:
        db.close()
