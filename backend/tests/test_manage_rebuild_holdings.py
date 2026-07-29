"""manage.py rebuild-holdings：键集合须含现存持仓行，交易全删后的孤儿行被清理。"""

from datetime import date
from decimal import Decimal

import manage
from app.database import SessionLocal
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.transaction import Transaction
from app.models.user import User

SYMBOLS = ["ORPHAN01", "ALIVE001"]


def _cleanup(db, user_id):
    db.query(Transaction).filter(
        Transaction.user_id == user_id, Transaction.symbol.in_(SYMBOLS)
    ).delete(synchronize_session=False)
    db.query(CorporateAction).filter(
        CorporateAction.user_id == user_id, CorporateAction.symbol.in_(SYMBOLS)
    ).delete(synchronize_session=False)
    db.query(Holding).filter(
        Holding.user_id == user_id, Holding.symbol.in_(SYMBOLS)
    ).delete(synchronize_session=False)
    db.commit()


def test_rebuild_holdings_cleans_orphans_and_rebuilds_live_keys():
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "demo").one()
        _cleanup(db, user.id)

        # 孤儿：持仓行存在但交易已全删（重建实测中手工清理过的形态）
        db.add(
            Holding(
                user_id=user.id,
                broker_account_id=None,
                symbol="ORPHAN01",
                name="孤儿持仓",
                market="A股",
                quantity=Decimal("100"),
                avg_cost=Decimal("1"),
                total_cost=Decimal("100"),
                currency="CNY",
            )
        )
        # 正常键：有交易支撑，重放应产出持仓
        db.add(
            Transaction(
                user_id=user.id,
                symbol="ALIVE001",
                name="正常标的",
                market="A股",
                transaction_type="BUY",
                quantity=Decimal("10"),
                price=Decimal("2"),
                fee=Decimal("0"),
                transaction_date=date(2026, 1, 5),
                currency="CNY",
            )
        )
        db.commit()

        assert manage.rebuild_holdings() == 0

        db.expire_all()
        orphan = (
            db.query(Holding)
            .filter(Holding.user_id == user.id, Holding.symbol == "ORPHAN01")
            .first()
        )
        assert orphan is None, "交易全删的孤儿持仓行应被清理"

        alive = (
            db.query(Holding)
            .filter(Holding.user_id == user.id, Holding.symbol == "ALIVE001")
            .one()
        )
        assert alive.quantity == Decimal("10")
    finally:
        _cleanup(db, user.id)
        db.close()
