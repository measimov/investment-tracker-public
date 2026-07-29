"""账户级持仓：按账户分桶重放、比例行动跨桶、归属矛盾降级合并桶。"""

from datetime import date
from decimal import Decimal

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.transaction import Transaction
from app.services.holding_service import recalculate_holdings
from app.services.statistics_service import (
    calculate_current_holdings_performance,
    get_statistics_by_market,
    get_summary_statistics,
    resolve_server_prices,
)


def reset_tables(db):
    for model in (
        BrokerFundFlow,
        IbkrActivityFlow,
        Holding,
        CorporateAction,
        Transaction,
        BrokerAccount,
    ):
        db.query(model).delete()
    db.commit()


def make_account(db, name):
    account = BrokerAccount(user_id=1, broker=name, account_name=name, base_currency="CNY")
    db.add(account)
    db.flush()
    return account


def add_txn(db, *, account_id=None, txn_type="BUY", quantity="100", price="10",
            fee="0", txn_date=date(2026, 1, 1), symbol="AAPL", market="美股"):
    txn = Transaction(
        user_id=1,
        broker_account_id=account_id,
        symbol=symbol,
        name=symbol,
        market=market,
        transaction_type=txn_type,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fee=Decimal(fee),
        transaction_date=txn_date,
        currency="USD",
    )
    db.add(txn)
    db.flush()
    return txn


def get_rows(db, symbol="AAPL", market="美股"):
    return (
        db.query(Holding)
        .filter(Holding.user_id == 1, Holding.symbol == symbol, Holding.market == market)
        .order_by(Holding.id)
        .all()
    )


def test_two_accounts_produce_two_holding_rows():
    db = SessionLocal()
    reset_tables(db)
    try:
        cmb = make_account(db, "CMB")
        ibkr = make_account(db, "IBKR")
        add_txn(db, account_id=cmb.id, quantity="100", price="10", fee="1")
        add_txn(db, account_id=ibkr.id, quantity="50", price="12", fee="1",
                txn_date=date(2026, 2, 1))
        db.commit()

        recalculate_holdings(db, 1, "AAPL", "美股")
        rows = get_rows(db)
        assert len(rows) == 2
        by_account = {row.broker_account_id: row for row in rows}
        assert by_account[cmb.id].quantity == Decimal("100")
        assert by_account[cmb.id].total_cost == Decimal("1001")
        assert by_account[ibkr.id].quantity == Decimal("50")
        assert by_account[ibkr.id].total_cost == Decimal("601")
    finally:
        reset_tables(db)
        db.close()

def test_sell_only_consumes_own_account_bucket():
    db = SessionLocal()
    reset_tables(db)
    try:
        cmb = make_account(db, "CMB")
        ibkr = make_account(db, "IBKR")
        add_txn(db, account_id=cmb.id, quantity="100", price="10")
        add_txn(db, account_id=ibkr.id, quantity="50", price="12")
        add_txn(db, account_id=cmb.id, txn_type="SELL", quantity="40", price="15",
                txn_date=date(2026, 3, 1))
        db.commit()

        recalculate_holdings(db, 1, "AAPL", "美股")
        by_account = {row.broker_account_id: row for row in get_rows(db)}
        assert by_account[cmb.id].quantity == Decimal("60")
        assert by_account[ibkr.id].quantity == Decimal("50")
    finally:
        reset_tables(db)
        db.close()


def test_ratio_action_applies_to_all_account_buckets():
    db = SessionLocal()
    reset_tables(db)
    try:
        cmb = make_account(db, "CMB")
        ibkr = make_account(db, "IBKR")
        add_txn(db, account_id=cmb.id, quantity="100", price="10")
        add_txn(db, account_id=ibkr.id, quantity="50", price="12")
        db.add(CorporateAction(
            user_id=1, symbol="AAPL", name="AAPL", market="美股",
            action_type="STOCK_SPLIT", ex_date=date(2026, 4, 1),
            split_ratio="1:2", currency="USD",
        ))
        db.commit()

        recalculate_holdings(db, 1, "AAPL", "美股")
        by_account = {row.broker_account_id: row for row in get_rows(db)}
        assert by_account[cmb.id].quantity == Decimal("200")
        assert by_account[ibkr.id].quantity == Decimal("100")
        # 拆股不改总成本
        assert by_account[cmb.id].total_cost == Decimal("1000")
    finally:
        reset_tables(db)
        db.close()


def test_cross_account_sell_falls_back_to_merged_bucket():
    db = SessionLocal()
    reset_tables(db)
    try:
        cmb = make_account(db, "CMB")
        add_txn(db, account_id=cmb.id, quantity="100", price="10")
        # 未指定账户卖出 120：CMB 桶只有 100 → 归属矛盾 → 合并桶重放
        add_txn(db, account_id=None, txn_type="SELL", quantity="120", price="15",
                txn_date=date(2026, 2, 1))
        db.commit()

        try:
            recalculate_holdings(db, 1, "AAPL", "美股")
        except ValueError:
            # 合并后仍超卖（100 < 120），沿用历史行为抛 ValueError
            pass
        else:
            raise AssertionError("merged replay should still oversell and raise")

        # 数量对得上时应正常降级为单一 NULL 桶
        db.query(Transaction).filter(Transaction.transaction_type == "SELL").delete()
        add_txn(db, account_id=None, txn_type="SELL", quantity="80", price="15",
                txn_date=date(2026, 2, 1))
        db.commit()
        recalculate_holdings(db, 1, "AAPL", "美股")
        rows = get_rows(db)
        assert len(rows) == 1
        assert rows[0].broker_account_id is None
        assert rows[0].quantity == Decimal("20")
    finally:
        reset_tables(db)
        db.close()


def test_null_account_absolute_action_targets_single_holder():
    db = SessionLocal()
    reset_tables(db)
    try:
        cmb = make_account(db, "CMB")
        add_txn(db, account_id=cmb.id, quantity="100", price="10")
        # 无账户归属、无比例字段的送股（绝对数量）：唯一持仓桶 → CMB
        db.add(CorporateAction(
            user_id=1, symbol="AAPL", name="AAPL", market="美股",
            action_type="BONUS_ISSUE", ex_date=date(2026, 2, 1),
            shares_received=Decimal("10"), currency="USD",
        ))
        db.commit()

        recalculate_holdings(db, 1, "AAPL", "美股")
        rows = get_rows(db)
        assert len(rows) == 1
        assert rows[0].broker_account_id == cmb.id
        assert rows[0].quantity == Decimal("110")
    finally:
        reset_tables(db)
        db.close()


def test_statistics_do_not_double_count_account_rows():
    db = SessionLocal()
    reset_tables(db)
    try:
        cmb = make_account(db, "CMB")
        ibkr = make_account(db, "IBKR")
        add_txn(db, account_id=cmb.id, quantity="100", price="10")
        add_txn(db, account_id=ibkr.id, quantity="50", price="10")
        db.commit()
        recalculate_holdings(db, 1, "AAPL", "美股")
        assert len(get_rows(db)) == 2

        result = calculate_current_holdings_performance(
            db, 1, {"AAPL:美股": 12.0}
        )
        # 150 股、成本 1500、现价 12 → 市值 1800、浮盈 300（USD 无汇率时原样累加）
        detail = result["holdings_detail"]
        assert len(detail) == 1
        assert detail[0]["quantity"] == 150.0
        assert detail[0]["unrealized_pnl"] == 300.0
    finally:
        reset_tables(db)
        db.close()


def test_resolve_server_prices_prefers_any_priced_account_row():
    db = SessionLocal()
    reset_tables(db)
    try:
        cmb = make_account(db, "CMB")
        ibkr = make_account(db, "IBKR")
        add_txn(db, account_id=cmb.id, quantity="100", price="10")
        add_txn(db, account_id=ibkr.id, quantity="50", price="10")
        db.commit()
        recalculate_holdings(db, 1, "AAPL", "美股")

        rows = get_rows(db)
        rows[0].current_price = None
        rows[1].current_price = Decimal("13.5")
        db.commit()

        prices, sources, freshness = resolve_server_prices(db, 1)
        assert prices["AAPL:美股"] == 13.5
        assert sources["AAPL:美股"] == "holding"
    finally:
        reset_tables(db)
        db.close()


def test_split_buckets_inherit_security_level_price():
    """升级路径回归：带 current_price 的旧 NULL 行拆成账户桶后价格不丢失。"""
    from datetime import datetime, timezone

    db = SessionLocal()
    reset_tables(db)
    try:
        cmb = make_account(db, "CMB")
        ibkr = make_account(db, "IBKR")
        add_txn(db, account_id=cmb.id, quantity="100", price="10")
        add_txn(db, account_id=ibkr.id, quantity="50", price="12")
        # 模拟迁移后的旧持仓行：单一 NULL 桶、带手工估值
        stamp = datetime(2026, 7, 1, tzinfo=timezone.utc)
        db.add(Holding(
            user_id=1, broker_account_id=None, symbol="AAPL", name="AAPL",
            market="美股", quantity=Decimal("150"), avg_cost=Decimal("10.67"),
            total_cost=Decimal("1600"), currency="USD",
            current_price=Decimal("13.5"), price_updated_at=stamp,
        ))
        db.commit()

        recalculate_holdings(db, 1, "AAPL", "美股")
        rows = get_rows(db)
        assert len(rows) == 2
        for row in rows:
            assert row.current_price == Decimal("13.5")
            assert row.price_updated_at is not None
    finally:
        reset_tables(db)
        db.close()


def test_user_visible_counts_dedupe_account_rows():
    """total_holdings 与 by-market holdings_count 按证券去重，不按账户行计数。"""
    db = SessionLocal()
    reset_tables(db)
    try:
        cmb = make_account(db, "CMB")
        ibkr = make_account(db, "IBKR")
        add_txn(db, account_id=cmb.id, quantity="100", price="10")
        add_txn(db, account_id=ibkr.id, quantity="50", price="10")
        add_txn(db, account_id=cmb.id, symbol="0700", market="港股",
                quantity="100", price="300")
        db.commit()
        recalculate_holdings(db, 1, "AAPL", "美股")
        recalculate_holdings(db, 1, "0700", "港股")
        assert len(get_rows(db)) == 2  # AAPL 两个账户行

        summary = get_summary_statistics(db, 1)
        assert summary["total_holdings"] == 2  # AAPL + 0700，非 3 行

        by_market = {item["market"]: item for item in get_statistics_by_market(db, 1)}
        assert by_market["美股"]["holdings_count"] == 1
        assert by_market["港股"]["holdings_count"] == 1
    finally:
        reset_tables(db)
        db.close()


def test_manual_price_update_syncs_all_account_rows():
    """PUT /holdings/{id}/price 是证券级操作：更新任一账户行即同步全部行。"""
    from app.api.holdings import update_holding_price
    from app.models.user import User
    from app.schemas.holding import HoldingPriceUpdate

    db = SessionLocal()
    reset_tables(db)
    try:
        cmb = make_account(db, "CMB")
        ibkr = make_account(db, "IBKR")
        add_txn(db, account_id=cmb.id, quantity="100", price="10")
        add_txn(db, account_id=ibkr.id, quantity="50", price="10")
        db.commit()
        recalculate_holdings(db, 1, "AAPL", "美股")
        rows = get_rows(db)
        assert len(rows) == 2

        user = db.query(User).filter(User.id == 1).one()
        update_holding_price(
            rows[0].id,
            HoldingPriceUpdate(current_price=Decimal("15.5")),
            current_user=user,
            db=db,
        )

        for row in get_rows(db):
            db.refresh(row)
            assert row.current_price == Decimal("15.5")
            assert row.price_updated_at is not None
    finally:
        reset_tables(db)
        db.close()


def test_get_holding_rejects_cross_market_aggregation():
    """未传 market 且同一代码存在于多个市场时返回 422，不做跨币种混合聚合。"""
    from fastapi import HTTPException

    from app.api.holdings import get_holding
    from app.models.user import User

    db = SessionLocal()
    reset_tables(db)
    try:
        cmb = make_account(db, "CMB")
        add_txn(db, account_id=cmb.id, symbol="PCT", market="港股",
                quantity="100", price="5")
        add_txn(db, account_id=cmb.id, symbol="PCT", market="新加坡股",
                quantity="50", price="1")
        db.commit()
        recalculate_holdings(db, 1, "PCT", "港股")
        recalculate_holdings(db, 1, "PCT", "新加坡股")

        user = db.query(User).filter(User.id == 1).one()
        try:
            get_holding("PCT", market=None, current_user=user, db=db)
        except HTTPException as exc:
            assert exc.status_code == 422
            assert "multiple markets" in exc.detail
        else:
            raise AssertionError("expected 422 for cross-market symbol")

        # 指定 market 后正常返回
        result = get_holding("PCT", market="港股", current_user=user, db=db)
        assert result.market == "港股"
    finally:
        reset_tables(db)
        db.close()
