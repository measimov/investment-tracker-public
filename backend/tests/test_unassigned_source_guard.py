"""领养路径退役后的最小守卫：NULL 账户历史来源必须让导入响亮失败。

回归背景（PR #89 检视）：账户级判重看不见 NULL 桶，库约束允许同一 hash
在 NULL 桶与已分配账户各存一份——没有守卫时重新导入会静默双记。
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.import_batch import ImportBatch
from app.models.transaction import Transaction
from app.services import cmb_fund_flow_importer as cmb
from app.services import eastmoney_statement_importer as em


def _reset(db):
    for model in (BrokerFundFlow, Holding, CorporateAction, Transaction):
        db.query(model).delete()
    db.query(ImportBatch).delete()
    db.query(BrokerAccount).delete()
    db.commit()


def _account(db, broker, mask):
    account = BrokerAccount(
        user_id=1,
        broker=broker,
        account_name=f"{broker} 测试账户",
        account_number_masked=mask,
        base_currency="CNY",
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def _seed_unassigned_source(db, broker, *, with_canonical=True):
    """预置 NULL 账户来源行 + 已链接 canonical 交易（旧库典型形态）。"""
    transaction_id = None
    if with_canonical:
        txn = Transaction(
            user_id=1,
            broker_account_id=None,
            symbol="600000",
            name="浦发银行",
            market="A股",
            transaction_type="BUY",
            quantity=Decimal("100"),
            price=Decimal("10"),
            fee=Decimal("5"),
            transaction_date=date(2025, 1, 6),
            currency="CNY",
        )
        db.add(txn)
        db.flush()
        transaction_id = txn.id
    db.add(
        BrokerFundFlow(
            user_id=1,
            broker_account_id=None,
            broker=broker,
            business_name="证券买入",
            trade_date=date(2025, 1, 6),
            amount=Decimal("-1005"),
            currency="CNY",
            transaction_id=transaction_id,
            row_hash="1" * 64,
            created_at=datetime.now(timezone.utc),
        )
    )
    db.commit()


def _counts(db):
    return (
        db.query(Transaction).count(),
        db.query(CorporateAction).count(),
        db.query(BrokerFundFlow).count(),
    )


def test_cmb_preview_and_import_reject_unassigned_sources(monkeypatch):
    db = SessionLocal()
    _reset(db)
    try:
        account = _account(db, "招商证券", "****A123")
        _seed_unassigned_source(db, "招商证券")
        monkeypatch.setattr(
            cmb, "parse_rows_with_warnings", lambda contents, filename, **kwargs: ([], {}, 0, [], [])
        )
        monkeypatch.setattr(cmb, "validate_cmb_statement_filename", lambda filename: None)
        before = _counts(db)

        with pytest.raises(ValueError, match="未分配账户"):
            cmb.preview_cmb_fund_flow(db, 1, b"pdf", "x.pdf", broker_account_id=account.id)
        with pytest.raises(ValueError, match="未分配账户"):
            cmb.import_cmb_fund_flow(db, 1, b"pdf", "x.pdf", broker_account_id=account.id)

        db.rollback()
        assert _counts(db) == before
    finally:
        _reset(db)
        db.close()


def test_eastmoney_preview_and_import_reject_unassigned_sources(monkeypatch):
    db = SessionLocal()
    _reset(db)
    try:
        account = _account(db, "东方财富证券", "****5678")
        _seed_unassigned_source(db, "东方财富证券")
        before = _counts(db)

        with pytest.raises(ValueError, match="未分配账户"):
            em.preview_eastmoney_statement(db, 1, b"pdf", "x.pdf", broker_account_id=account.id)
        with pytest.raises(ValueError, match="未分配账户"):
            em.import_eastmoney_statement(db, 1, b"pdf", "x.pdf", broker_account_id=account.id)

        db.rollback()
        assert _counts(db) == before
    finally:
        _reset(db)
        db.close()


def test_guard_ignores_other_brokers_and_assigned_rows(monkeypatch):
    """守卫只看本券商的 NULL 桶：他券商 NULL 行与本券商已分配行都不拦。"""
    db = SessionLocal()
    _reset(db)
    try:
        _account(db, "招商证券", "****A123")
        # 东财的 NULL 行不应拦招商导入
        _seed_unassigned_source(db, "东方财富证券", with_canonical=False)
        cmb.reject_unassigned_legacy_sources(db, 1)  # 不抛
        # 招商已分配行同样不拦
        account = db.query(BrokerAccount).first()
        db.add(
            BrokerFundFlow(
                user_id=1,
                broker_account_id=account.id,
                broker="招商证券",
                business_name="证券买入",
                trade_date=date(2025, 1, 7),
                amount=Decimal("-1"),
                currency="CNY",
                row_hash="2" * 64,
                created_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
        cmb.reject_unassigned_legacy_sources(db, 1)  # 仍不抛
        with pytest.raises(ValueError):
            em.reject_unassigned_legacy_sources(db, 1)  # 东财自己的守卫要抛
    finally:
        _reset(db)
        db.close()
