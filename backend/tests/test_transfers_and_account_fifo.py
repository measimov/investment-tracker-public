"""转仓交易对 + 账户级 FIFO：成本迁移、批次保留、降级一致性、用户级不变量。"""

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.api.transactions import create_transfer, delete_transaction
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.corporate_action import CorporateAction
from app.models.exchange_rate import ExchangeRate
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.transaction import TransferCreate
from app.services.holding_service import recalculate_holdings
from app.services.statistics_service import (
    _get_fifo_results_for_user,
    calculate_performance_summary,
    get_statistics_by_time,
    get_summary_statistics,
)


def reset_tables(db):
    for model in (
        BrokerFundFlow,
        IbkrActivityFlow,
        Holding,
        CorporateAction,
        Transaction,
        BrokerAccount,
        ExchangeRate,
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


def get_user(db):
    return db.query(User).filter(User.id == 1).one()


def get_rows(db, symbol="AAPL", market="美股"):
    return (
        db.query(Holding)
        .filter(Holding.user_id == 1, Holding.symbol == symbol, Holding.market == market)
        .order_by(Holding.id)
        .all()
    )


def do_transfer(db, *, from_id, to_id, quantity="60", transfer_date=date(2026, 2, 1)):
    return create_transfer(
        TransferCreate(
            symbol="AAPL",
            market="美股",
            quantity=Decimal(quantity),
            from_broker_account_id=from_id,
            to_broker_account_id=to_id,
            transfer_date=transfer_date,
        ),
        current_user=get_user(db),
        db=db,
    )


def test_transfer_moves_quantity_and_cost_between_accounts():
    db = SessionLocal()
    reset_tables(db)
    try:
        cmb = make_account(db, "CMB")
        ibkr = make_account(db, "IBKR")
        add_txn(db, account_id=cmb.id, quantity="100", price="10", fee="2")
        db.commit()
        recalculate_holdings(db, 1, "AAPL", "美股")

        legs = do_transfer(db, from_id=cmb.id, to_id=ibkr.id, quantity="60")
        assert legs[0].transaction_type == "TRANSFER_OUT"
        assert legs[1].transaction_type == "TRANSFER_IN"
        assert legs[0].linked_transaction_id == legs[1].id
        assert legs[1].linked_transaction_id == legs[0].id

        by_account = {row.broker_account_id: row for row in get_rows(db)}
        # 成本 1002，均价 10.02：60 股迁移 601.2
        assert by_account[cmb.id].quantity == Decimal("40")
        assert by_account[cmb.id].total_cost == Decimal("400.8")
        assert by_account[ibkr.id].quantity == Decimal("60")
        assert by_account[ibkr.id].total_cost == Decimal("601.2")
        assert by_account[ibkr.id].avg_cost == by_account[cmb.id].avg_cost
    finally:
        reset_tables(db)
        db.close()


def test_fifo_lots_keep_original_dates_and_costs_after_transfer():
    db = SessionLocal()
    reset_tables(db)
    try:
        cmb = make_account(db, "CMB")
        ibkr = make_account(db, "IBKR")
        # 两批不同成本：10 元 100 股（1 月）、20 元 100 股（1 月中）
        add_txn(db, account_id=cmb.id, quantity="100", price="10", txn_date=date(2026, 1, 1))
        add_txn(db, account_id=cmb.id, quantity="100", price="20", txn_date=date(2026, 1, 15))
        db.commit()
        recalculate_holdings(db, 1, "AAPL", "美股")

        # 转 150 股到 IBKR：应带走整批 10 元 100 股 + 半批 20 元 50 股
        do_transfer(db, from_id=cmb.id, to_id=ibkr.id, quantity="150")
        # 在 IBKR 卖 120 股 @25：FIFO 匹配 100@10 + 20@20 → pnl = 3000-1400 = 1600
        add_txn(db, account_id=ibkr.id, txn_type="SELL", quantity="120", price="25",
                txn_date=date(2026, 3, 1))
        db.commit()

        fifo = _get_fifo_results_for_user(db, 1, {("AAPL", "美股")})[("AAPL", "美股")]
        assert fifo['realized_pnl'] == pytest.approx(1600.0)
        assert fifo['sold_cost'] == pytest.approx(1400.0)
        # 剩余批次：IBKR 30@20 + CMB 50@20 = 1600
        assert fifo['current_holdings_cost'] == pytest.approx(1600.0)
        assert fifo['closed_trades'][0]['holding_days'] == (
            date(2026, 3, 1) - date(2026, 1, 1)
        ).days
    finally:
        reset_tables(db)
        db.close()


def test_account_scoped_fifo_matches_only_own_lots():
    db = SessionLocal()
    reset_tables(db)
    try:
        cmb = make_account(db, "CMB")
        ibkr = make_account(db, "IBKR")
        # CMB 先买便宜批，IBKR 后买贵批；在 IBKR 卖出
        add_txn(db, account_id=cmb.id, quantity="100", price="10", txn_date=date(2026, 1, 1))
        add_txn(db, account_id=ibkr.id, quantity="100", price="20", txn_date=date(2026, 1, 15))
        add_txn(db, account_id=ibkr.id, txn_type="SELL", quantity="50", price="25",
                txn_date=date(2026, 2, 1))
        db.commit()

        fifo = _get_fifo_results_for_user(db, 1, {("AAPL", "美股")})[("AAPL", "美股")]
        # 账户级：IBKR 卖出只匹配自己 20 元的批次 → pnl = 1250-1000 = 250
        # （旧的用户级合并 FIFO 会错误匹配 CMB 的 10 元批次得到 750）
        assert fifo['realized_pnl'] == pytest.approx(250.0)
        assert fifo['sold_cost'] == pytest.approx(1000.0)
    finally:
        reset_tables(db)
        db.close()


def test_cross_account_oversell_falls_back_to_merged_fifo():
    db = SessionLocal()
    reset_tables(db)
    try:
        cmb = make_account(db, "CMB")
        ibkr = make_account(db, "IBKR")
        add_txn(db, account_id=cmb.id, quantity="100", price="10", txn_date=date(2026, 1, 1))
        add_txn(db, account_id=ibkr.id, quantity="100", price="20", txn_date=date(2026, 1, 15))
        # IBKR 卖 150：桶内只有 100 → 降级合并重放（用户级共 200 股，可卖）
        add_txn(db, account_id=ibkr.id, txn_type="SELL", quantity="150", price="25",
                txn_date=date(2026, 2, 1))
        db.commit()

        fifo = _get_fifo_results_for_user(db, 1, {("AAPL", "美股")})[("AAPL", "美股")]
        # 合并 FIFO：100@10 + 50@20 = 2000 成本，收入 3750 → pnl 1750
        assert fifo['realized_pnl'] == pytest.approx(1750.0)
        assert fifo['sold_cost'] == pytest.approx(2000.0)
    finally:
        reset_tables(db)
        db.close()


def test_transfer_does_not_change_user_level_performance():
    db = SessionLocal()
    reset_tables(db)
    try:
        cmb = make_account(db, "CMB")
        ibkr = make_account(db, "IBKR")
        add_txn(db, account_id=cmb.id, quantity="100", price="10", fee="1")
        db.commit()
        recalculate_holdings(db, 1, "AAPL", "美股")

        prices = {"AAPL:美股": 15.0}
        before = calculate_performance_summary(db, 1, dict(prices))

        do_transfer(db, from_id=cmb.id, to_id=ibkr.id, quantity="60")
        after = calculate_performance_summary(db, 1, dict(prices))

        # 转仓对用户级口径完全透明
        assert after['current_performance']['unrealized_pnl_cny'] == pytest.approx(
            before['current_performance']['unrealized_pnl_cny']
        )
        assert after['realized_pnl']['realized_pnl_cny'] == pytest.approx(
            before['realized_pnl']['realized_pnl_cny']
        )
        assert after['account_return']['total_return_cny'] == pytest.approx(
            before['account_return']['total_return_cny']
        )
        assert after['account_return']['cash_flow_count'] == (
            before['account_return']['cash_flow_count']
        )

        # 汇总计数与时间分布同样透明（review #55 P2）
        summary = get_summary_statistics(db, 1)
        assert summary['total_transactions'] == 1  # 仅那笔 BUY，转仓对不计
        by_month = get_statistics_by_time(db, 1, "month")
        periods = [b['period'] for b in by_month]
        assert periods == ["2026-01"]  # 只有转仓的 2026-02 不产生全零 bucket
    finally:
        reset_tables(db)
        db.close()


def test_transfer_validation_rejects_same_account_and_oversell():
    db = SessionLocal()
    reset_tables(db)
    try:
        cmb = make_account(db, "CMB")
        ibkr = make_account(db, "IBKR")
        add_txn(db, account_id=cmb.id, quantity="100", price="10")
        db.commit()
        recalculate_holdings(db, 1, "AAPL", "美股")

        with pytest.raises(HTTPException) as exc:
            do_transfer(db, from_id=cmb.id, to_id=cmb.id, quantity="10")
        assert exc.value.status_code == 422

        with pytest.raises(HTTPException) as exc:
            do_transfer(db, from_id=cmb.id, to_id=ibkr.id, quantity="500")
        assert exc.value.status_code == 422
        assert "转仓无法成立" in exc.value.detail
    finally:
        reset_tables(db)
        db.close()


def test_deleting_one_leg_removes_pair_and_restores_holdings():
    db = SessionLocal()
    reset_tables(db)
    try:
        cmb = make_account(db, "CMB")
        ibkr = make_account(db, "IBKR")
        add_txn(db, account_id=cmb.id, quantity="100", price="10")
        db.commit()
        recalculate_holdings(db, 1, "AAPL", "美股")

        legs = do_transfer(db, from_id=cmb.id, to_id=ibkr.id, quantity="60")
        assert len(get_rows(db)) == 2

        delete_transaction(legs[0].id, current_user=get_user(db), db=db)
        remaining = db.query(Transaction).filter(
            Transaction.transaction_type.in_(["TRANSFER_OUT", "TRANSFER_IN"])
        ).count()
        assert remaining == 0

        rows = get_rows(db)
        assert len(rows) == 1
        assert rows[0].broker_account_id == cmb.id
        assert rows[0].quantity == Decimal("100")
    finally:
        reset_tables(db)
        db.close()


def test_transfer_pair_and_recalc_commit_atomically(monkeypatch):
    """重算失败时转仓对必须回滚，不留半完成状态（review #55 P1）。"""
    from app.api import transactions as transactions_api

    db = SessionLocal()
    reset_tables(db)
    try:
        cmb = make_account(db, "CMB")
        ibkr = make_account(db, "IBKR")
        add_txn(db, account_id=cmb.id, quantity="100", price="10")
        db.commit()
        recalculate_holdings(db, 1, "AAPL", "美股")
        before_rows = [(r.broker_account_id, r.quantity) for r in get_rows(db)]

        def boom(*args, **kwargs):
            raise RuntimeError("simulated recalc failure")

        monkeypatch.setattr(transactions_api, "recalculate_holdings", boom)
        with pytest.raises(RuntimeError):
            do_transfer(db, from_id=cmb.id, to_id=ibkr.id, quantity="60")

        # ledger 与派生持仓都保持原状
        transfers = db.query(Transaction).filter(
            Transaction.transaction_type.in_(["TRANSFER_OUT", "TRANSFER_IN"])
        ).count()
        assert transfers == 0
        assert [(r.broker_account_id, r.quantity) for r in get_rows(db)] == before_rows
    finally:
        reset_tables(db)
        db.close()


def test_backdated_transfer_without_shares_on_date_rejected():
    """历史日期转仓必须按 transfer_date 当天的账户余额校验（review #55 P1）。"""
    db = SessionLocal()
    reset_tables(db)
    try:
        cmb = make_account(db, "CMB")
        ibkr = make_account(db, "IBKR")
        # 2 月才买入；1 月的转仓当天无仓可转
        add_txn(db, account_id=cmb.id, quantity="100", price="10",
                txn_date=date(2026, 2, 1))
        db.commit()
        recalculate_holdings(db, 1, "AAPL", "美股")

        with pytest.raises(HTTPException) as exc:
            do_transfer(db, from_id=cmb.id, to_id=ibkr.id, quantity="60",
                        transfer_date=date(2026, 1, 15))
        assert exc.value.status_code == 422
        assert db.query(Transaction).filter(
            Transaction.transaction_type.in_(["TRANSFER_OUT", "TRANSFER_IN"])
        ).count() == 0
    finally:
        reset_tables(db)
        db.close()


def test_transfer_conflicting_with_future_sell_rejected():
    """转出后源账户未来卖出会超卖的转仓必须被拒绝，而非静默降级。"""
    db = SessionLocal()
    reset_tables(db)
    try:
        cmb = make_account(db, "CMB")
        ibkr = make_account(db, "IBKR")
        add_txn(db, account_id=cmb.id, quantity="100", price="10",
                txn_date=date(2026, 1, 1))
        add_txn(db, account_id=cmb.id, txn_type="SELL", quantity="80", price="15",
                txn_date=date(2026, 3, 1))
        db.commit()
        recalculate_holdings(db, 1, "AAPL", "美股")

        with pytest.raises(HTTPException) as exc:
            do_transfer(db, from_id=cmb.id, to_id=ibkr.id, quantity="60",
                        transfer_date=date(2026, 2, 1))
        assert exc.value.status_code == 422
        assert "转仓无法成立" in exc.value.detail
    finally:
        reset_tables(db)
        db.close()


def test_same_day_chained_transfers_replay_in_pair_order():
    """同日链式转仓 A→B→C 按对的创建顺序重放（review #55 P1）。"""
    db = SessionLocal()
    reset_tables(db)
    try:
        a = make_account(db, "A")
        b = make_account(db, "B")
        c = make_account(db, "C")
        add_txn(db, account_id=a.id, quantity="100", price="10",
                txn_date=date(2026, 1, 1))
        db.commit()
        recalculate_holdings(db, 1, "AAPL", "美股")

        same_day = date(2026, 2, 1)
        do_transfer(db, from_id=a.id, to_id=b.id, quantity="100", transfer_date=same_day)
        do_transfer(db, from_id=b.id, to_id=c.id, quantity="100", transfer_date=same_day)

        rows = get_rows(db)
        assert len(rows) == 1
        assert rows[0].broker_account_id == c.id
        assert rows[0].quantity == Decimal("100")
        assert rows[0].total_cost == Decimal("1000")

        # FIFO 同样成立：批次最终在 C 账户，原始日期保留
        fifo = _get_fifo_results_for_user(db, 1, {("AAPL", "美股")})[("AAPL", "美股")]
        assert fifo['current_holdings_cost'] == pytest.approx(1000.0)
        assert fifo['buy_queue'][0]['date'] == "2026-01-01"
    finally:
        reset_tables(db)
        db.close()


def test_sell_exceeding_post_transfer_balance_rejected():
    """转出 60 后源账户只剩 40：新增卖出 80 必须被通用校验拒绝（review #55 P1）。"""
    from app.api.transactions import create_transaction
    from app.schemas.transaction import TransactionCreate

    db = SessionLocal()
    reset_tables(db)
    try:
        cmb = make_account(db, "CMB")
        ibkr = make_account(db, "IBKR")
        add_txn(db, account_id=cmb.id, quantity="100", price="10",
                txn_date=date(2026, 1, 1))
        db.commit()
        recalculate_holdings(db, 1, "AAPL", "美股")
        do_transfer(db, from_id=cmb.id, to_id=ibkr.id, quantity="60",
                    transfer_date=date(2026, 2, 1))

        with pytest.raises(HTTPException) as exc:
            create_transaction(
                TransactionCreate(
                    broker_account_id=cmb.id,
                    symbol="AAPL",
                    market="美股",
                    transaction_type="SELL",
                    quantity=Decimal("80"),
                    price=Decimal("15"),
                    transaction_date=date(2026, 3, 1),
                    currency="USD",
                ),
                current_user=get_user(db),
                db=db,
            )
        assert exc.value.status_code == 400

        # 卖 40 以内可以
        create_transaction(
            TransactionCreate(
                broker_account_id=cmb.id,
                symbol="AAPL",
                market="美股",
                transaction_type="SELL",
                quantity=Decimal("40"),
                price=Decimal("15"),
                transaction_date=date(2026, 3, 1),
                currency="USD",
            ),
            current_user=get_user(db),
            db=db,
        )
    finally:
        reset_tables(db)
        db.close()


def test_delete_transfer_with_dependent_sell_rejected():
    """目标账户已有依赖该转仓的卖出时，删除转仓对必须 409（review #55 二轮 P1）。"""
    db = SessionLocal()
    reset_tables(db)
    try:
        cmb = make_account(db, "CMB")
        ibkr = make_account(db, "IBKR")
        add_txn(db, account_id=cmb.id, quantity="100", price="10",
                txn_date=date(2026, 1, 1))
        db.commit()
        recalculate_holdings(db, 1, "AAPL", "美股")
        legs = do_transfer(db, from_id=cmb.id, to_id=ibkr.id, quantity="60",
                           transfer_date=date(2026, 2, 1))
        # 目标账户卖出 50：依赖转入的 60 股
        add_txn(db, account_id=ibkr.id, txn_type="SELL", quantity="50", price="15",
                txn_date=date(2026, 3, 1))
        db.commit()
        recalculate_holdings(db, 1, "AAPL", "美股")

        with pytest.raises(HTTPException) as exc:
            delete_transaction(legs[0].id, current_user=get_user(db), db=db)
        assert exc.value.status_code == 409
        assert "不能删除" in exc.value.detail

        # 转仓对仍在，持仓保持账户级（无 NULL 合并行）
        remaining = db.query(Transaction).filter(
            Transaction.transaction_type.in_(["TRANSFER_OUT", "TRANSFER_IN"])
        ).count()
        assert remaining == 2
        rows = get_rows(db)
        assert all(row.broker_account_id is not None for row in rows)
        by_account = {row.broker_account_id: row for row in rows}
        assert by_account[cmb.id].quantity == Decimal("40")
        assert by_account[ibkr.id].quantity == Decimal("10")
    finally:
        reset_tables(db)
        db.close()


def test_advisory_lock_serializes_same_security_timeline():
    """时间线 advisory lock 机制验证：同键互斥、事务结束释放、不同键不互斥。"""
    from sqlalchemy import create_engine, text

    import os
    engine = create_engine(os.environ["DATABASE_URL"])
    key = "security-timeline:1:AAPL:美股"
    other_key = "security-timeline:1:0700:港股"

    with engine.connect() as conn1, engine.connect() as conn2:
        tx1 = conn1.begin()
        conn1.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})

        # 同键：conn2 拿不到
        got = conn2.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"), {"k": key}
        ).scalar()
        assert got is False
        conn2.rollback()

        # 不同键：conn2 拿得到
        got_other = conn2.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"), {"k": other_key}
        ).scalar()
        assert got_other is True
        conn2.rollback()

        # conn1 事务结束后释放
        tx1.rollback()
        got_after = conn2.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"), {"k": key}
        ).scalar()
        assert got_after is True
        conn2.rollback()
    engine.dispose()


def test_concurrent_transfer_and_sell_never_degrade():
    """两个独立 session 并发：转仓 vs 卖出，任一顺序都恰好一个成功、零静默降级。

    主线程先持有时间线锁把两个操作压在门外，再释放让它们排队执行——
    转仓先行则卖出被账户校验拒绝(400)；卖出先行则转仓因未来超卖被拒(422)。
    """
    import os
    import threading

    from sqlalchemy import create_engine, text

    from app.api.transactions import create_transaction
    from app.schemas.transaction import TransactionCreate
    from app.services.holding_service import replay_account_buckets

    db = SessionLocal()
    reset_tables(db)
    cmb = make_account(db, "CMB")
    ibkr = make_account(db, "IBKR")
    cmb_id, ibkr_id = cmb.id, ibkr.id
    add_txn(db, account_id=cmb_id, quantity="100", price="10",
            txn_date=date(2026, 1, 1))
    db.commit()
    recalculate_holdings(db, 1, "AAPL", "美股")
    db.close()

    results = {}

    def run_transfer():
        session = SessionLocal()
        try:
            create_transfer(
                TransferCreate(
                    symbol="AAPL", market="美股", quantity=Decimal("60"),
                    from_broker_account_id=cmb_id, to_broker_account_id=ibkr_id,
                    transfer_date=date(2026, 2, 1),
                ),
                current_user=session.query(User).filter(User.id == 1).one(),
                db=session,
            )
            results['transfer'] = 'ok'
        except HTTPException as exc:
            results['transfer'] = exc.status_code
        finally:
            session.close()

    def run_sell():
        session = SessionLocal()
        try:
            create_transaction(
                TransactionCreate(
                    broker_account_id=cmb_id, symbol="AAPL", market="美股",
                    transaction_type="SELL", quantity=Decimal("80"),
                    price=Decimal("15"), transaction_date=date(2026, 3, 1),
                    currency="USD",
                ),
                current_user=session.query(User).filter(User.id == 1).one(),
                db=session,
            )
            results['sell'] = 'ok'
        except HTTPException as exc:
            results['sell'] = exc.status_code
        finally:
            session.close()

    # 主线程持锁压住两个操作，确保它们都完成了"启动"再同时竞争
    gate_engine = create_engine(os.environ["DATABASE_URL"])
    gate = gate_engine.connect()
    gate_tx = gate.begin()
    gate.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
        {"k": "security-timeline:1:AAPL:美股"},
    )

    threads = [threading.Thread(target=run_transfer), threading.Thread(target=run_sell)]
    for thread in threads:
        thread.start()
    import time
    time.sleep(0.4)  # 两个操作都已阻塞在锁上
    gate_tx.rollback()
    gate.close()
    gate_engine.dispose()
    for thread in threads:
        thread.join(timeout=10)

    # 恰好一个成功；失败方必须是校验拒绝（400/422），不是静默通过
    outcomes = sorted(str(v) for v in results.values())
    assert 'ok' in results.values(), results
    assert outcomes.count('ok') == 1, results
    failure = [v for v in results.values() if v != 'ok'][0]
    assert failure in (400, 422), results

    # 终态时间线严格自洽：无降级、无 NULL 合并行
    verify = SessionLocal()
    try:
        replay_account_buckets(verify, 1, "AAPL", "美股")  # 不抛即自洽
        rows = get_rows(verify)
        assert all(row.broker_account_id is not None for row in rows)
    finally:
        reset_tables(verify)
        verify.close()


def _gate_on(key):
    """主线程持有 advisory lock 压住并发操作，返回 (释放函数, engine)。"""
    import os

    from sqlalchemy import create_engine, text

    engine = create_engine(os.environ["DATABASE_URL"])
    conn = engine.connect()
    tx = conn.begin()
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})

    def release():
        tx.rollback()
        conn.close()
        engine.dispose()

    return release


def test_concurrent_symbol_move_and_quantity_update_converge():
    """记录锁下 symbol 迁移与数量更新并发：任一顺序收敛到 MSFT×200 且持仓一致。

    review #55 三轮 P1 的精确场景：修复前等待锁的一方用旧键（AAPL）重算，
    造成 transaction=MSFT×200 而 holdings=MSFT×100 的分叉。
    """
    import threading

    from app.api.transactions import update_transaction
    from app.schemas.transaction import TransactionUpdate

    db = SessionLocal()
    reset_tables(db)
    txn = add_txn(db, account_id=None, quantity="100", price="10",
                  txn_date=date(2026, 1, 1))
    db.commit()
    recalculate_holdings(db, 1, "AAPL", "美股")
    txn_id = txn.id
    db.close()

    release = _gate_on(f"transaction-record:{txn_id}")
    errors = []

    def move_symbol():
        session = SessionLocal()
        try:
            update_transaction(txn_id, TransactionUpdate(symbol="MSFT"),
                               current_user=session.query(User).filter(User.id == 1).one(),
                               db=session)
        except Exception as exc:  # noqa: BLE001
            errors.append(("move", exc))
        finally:
            session.close()

    def change_quantity():
        session = SessionLocal()
        try:
            update_transaction(txn_id, TransactionUpdate(quantity=Decimal("200")),
                               current_user=session.query(User).filter(User.id == 1).one(),
                               db=session)
        except Exception as exc:  # noqa: BLE001
            errors.append(("qty", exc))
        finally:
            session.close()

    threads = [threading.Thread(target=move_symbol), threading.Thread(target=change_quantity)]
    for thread in threads:
        thread.start()
    import time
    time.sleep(0.4)
    release()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, errors
    verify = SessionLocal()
    try:
        final = verify.query(Transaction).filter(Transaction.id == txn_id).one()
        assert final.symbol == "MSFT"
        assert final.quantity == Decimal("200")
        # 派生持仓与最终 ledger 一致：MSFT×200，AAPL 无残留
        assert get_rows(verify, symbol="AAPL") == []
        msft_rows = get_rows(verify, symbol="MSFT")
        assert len(msft_rows) == 1
        assert msft_rows[0].quantity == Decimal("200")
    finally:
        reset_tables(verify)
        verify.close()


def test_concurrent_symbol_move_and_delete_leave_no_orphans():
    """symbol 迁移与删除并发：任一顺序都不留孤儿持仓行。"""
    import threading

    from app.api.transactions import update_transaction
    from app.schemas.transaction import TransactionUpdate

    db = SessionLocal()
    reset_tables(db)
    txn = add_txn(db, account_id=None, quantity="100", price="10",
                  txn_date=date(2026, 1, 1))
    db.commit()
    recalculate_holdings(db, 1, "AAPL", "美股")
    txn_id = txn.id
    db.close()

    release = _gate_on(f"transaction-record:{txn_id}")
    outcomes = {}

    def move_symbol():
        session = SessionLocal()
        try:
            update_transaction(txn_id, TransactionUpdate(symbol="MSFT"),
                               current_user=session.query(User).filter(User.id == 1).one(),
                               db=session)
            outcomes['move'] = 'ok'
        except HTTPException as exc:
            outcomes['move'] = exc.status_code
        finally:
            session.close()

    def delete_it():
        session = SessionLocal()
        try:
            delete_transaction(txn_id,
                               current_user=session.query(User).filter(User.id == 1).one(),
                               db=session)
            outcomes['delete'] = 'ok'
        except HTTPException as exc:
            outcomes['delete'] = exc.status_code
        finally:
            session.close()

    threads = [threading.Thread(target=move_symbol), threading.Thread(target=delete_it)]
    for thread in threads:
        thread.start()
    import time
    time.sleep(0.4)
    release()
    for thread in threads:
        thread.join(timeout=10)

    # 删除必成功；迁移要么在删除前完成(ok)、要么锁后重读 404
    assert outcomes.get('delete') == 'ok', outcomes
    assert outcomes.get('move') in ('ok', 404), outcomes

    verify = SessionLocal()
    try:
        assert verify.query(Transaction).filter(Transaction.id == txn_id).first() is None
        assert get_rows(verify, symbol="AAPL") == []
        assert get_rows(verify, symbol="MSFT") == []
    finally:
        reset_tables(verify)
        verify.close()
