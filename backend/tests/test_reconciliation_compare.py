"""对账闭环：as-of 持仓重放、现金推导与快照自动比对。"""

from datetime import date
from decimal import Decimal

from app.api.reconciliation_snapshots import (
    compare_reconciliation_snapshot,
    create_reconciliation_snapshot,
)
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.cash_event import CashEvent
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.security_rule import SecurityRule
from app.models.reconciliation_snapshot import ReconciliationSnapshot
from app.models.transaction import Transaction
from app.schemas.reconciliation_snapshot import (
    ReconciliationPosition,
    ReconciliationSnapshotCreate,
)
from app.services.reconciliation_service import (
    derive_account_cash_asof,
    replay_account_positions_asof,
)
from tests.helpers import add_transaction, get_user, make_account, reset_tables


RESET_MODELS = (
    SecurityRule,
    BrokerFundFlow,
    IbkrActivityFlow,
    ReconciliationSnapshot,
    CashEvent,
    Holding,
    CorporateAction,
    Transaction,
    BrokerAccount,
)


def add_txn(db, *, account_id, txn_type="BUY", quantity="100", price="10",
            fee="0", txn_date=date(2026, 1, 5), symbol="600000", market="A股",
            currency="CNY"):
    return add_transaction(
        db, broker_account_id=account_id, symbol=symbol, name=symbol, market=market,
        transaction_type=txn_type, quantity=Decimal(quantity), price=Decimal(price),
        fee=Decimal(fee), transaction_date=txn_date, currency=currency,
    )


def make_snapshot_via_api(db, account_id, *, snapshot_date=date(2026, 1, 31),
                          positions=(), cash=None):
    payload = ReconciliationSnapshotCreate(
        broker_account_id=account_id,
        snapshot_date=snapshot_date,
        positions=[ReconciliationPosition(**p) for p in positions],
        cash_balances=cash or {},
    )
    return create_reconciliation_snapshot(payload, current_user=get_user(db), db=db)


def test_matching_snapshot_is_marked_matched():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = make_account(db)
        add_txn(db, account_id=account.id, quantity="100", price="10", fee="5")
        db.add(CashEvent(
            user_id=1, broker_account_id=account.id, event_type="DEPOSIT",
            amount=Decimal("2000"), currency="CNY", event_date=date(2026, 1, 2),
        ))
        db.commit()

        snapshot = make_snapshot_via_api(
            db, account.id,
            positions=[{"symbol": "600000", "market": "A股", "quantity": Decimal("100")}],
            # 2000 入金 − (100×10+5) = 995
            cash={"CNY": Decimal("995")},
        )
        assert snapshot.status == "MATCHED"
        assert snapshot.compared_at is not None
        assert snapshot.diff_detail["summary"]["matched"] is True
        assert snapshot.diff_detail["positions"][0]["status"] == "MATCH"
        assert snapshot.diff_detail["cash"][0]["status"] == "MATCH"
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_mismatches_are_classified_per_item():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = make_account(db)
        add_txn(db, account_id=account.id, symbol="600000", quantity="100")
        add_txn(db, account_id=account.id, symbol="000001", quantity="50")
        db.commit()

        snapshot = make_snapshot_via_api(
            db, account.id,
            positions=[
                {"symbol": "600000", "market": "A股", "quantity": Decimal("120")},  # 数量差
                {"symbol": "600519", "market": "A股", "quantity": Decimal("10")},   # 系统缺
                # 000001 系统有、快照缺
            ],
            cash={"CNY": Decimal("888")},  # 推导为负（无入金），必不匹配
        )
        assert snapshot.status == "MISMATCHED"
        by_symbol = {d["symbol"]: d for d in snapshot.diff_detail["positions"]}
        assert by_symbol["600000"]["status"] == "QUANTITY_MISMATCH"
        assert by_symbol["600000"]["delta"] == -20.0
        assert by_symbol["600519"]["status"] == "MISSING_IN_SYSTEM"
        assert by_symbol["000001"]["status"] == "MISSING_IN_SNAPSHOT"
        assert snapshot.diff_detail["cash"][0]["status"] == "MISMATCH"
        assert snapshot.diff_detail["summary"]["position_mismatches"] == 3
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_replay_is_as_of_snapshot_date():
    """快照日之后的交易与转仓不得影响比对。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        cmb = make_account(db, "CMB")
        ibkr = make_account(db, "IBKR")
        add_txn(db, account_id=cmb.id, quantity="100", txn_date=date(2026, 1, 5))
        # 快照日之后：又买了 50，并转 30 到 IBKR
        add_txn(db, account_id=cmb.id, quantity="50", txn_date=date(2026, 2, 10))
        out = add_txn(db, account_id=cmb.id, txn_type="TRANSFER_OUT", quantity="30",
                      txn_date=date(2026, 2, 15))
        in_leg = add_txn(db, account_id=ibkr.id, txn_type="TRANSFER_IN", quantity="30",
                         txn_date=date(2026, 2, 15))
        in_leg.linked_transaction_id = out.id
        out.linked_transaction_id = in_leg.id
        db.commit()

        positions, inconsistent = replay_account_positions_asof(
            db, 1, cmb.id, date(2026, 1, 31)
        )
        assert inconsistent == []
        assert positions[("600000", "A股")] == Decimal("100")

        # 转仓生效后的 as-of：CMB 120 / IBKR 30
        positions_later, _ = replay_account_positions_asof(db, 1, cmb.id, date(2026, 2, 28))
        assert positions_later[("600000", "A股")] == Decimal("120")
        ibkr_positions, _ = replay_account_positions_asof(db, 1, ibkr.id, date(2026, 2, 28))
        assert ibkr_positions[("600000", "A股")] == Decimal("30")
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_cash_derivation_covers_events_trades_and_dividends():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = make_account(db)
        db.add(CashEvent(
            user_id=1, broker_account_id=account.id, event_type="DEPOSIT",
            amount=Decimal("10000"), currency="CNY", event_date=date(2026, 1, 2),
        ))
        db.add(CashEvent(
            user_id=1, broker_account_id=account.id, event_type="FEE",
            amount=Decimal("15"), currency="CNY", event_date=date(2026, 1, 3),
        ))
        add_txn(db, account_id=account.id, quantity="100", price="10", fee="5",
                txn_date=date(2026, 1, 5))
        add_txn(db, account_id=account.id, txn_type="SELL", quantity="40", price="12",
                fee="3", txn_date=date(2026, 1, 10))
        db.add(CorporateAction(
            user_id=1, broker_account_id=account.id, symbol="600000", name="600000",
            market="A股", action_type="CASH_DIVIDEND", ex_date=date(2026, 1, 15),
            payment_date=date(2026, 1, 20), total_dividend=Decimal("100"),
            tax_withheld=Decimal("10"), net_dividend=Decimal("90"), currency="CNY",
        ))
        # 快照日之后的现金事件不计
        db.add(CashEvent(
            user_id=1, broker_account_id=account.id, event_type="WITHDRAWAL",
            amount=Decimal("5000"), currency="CNY", event_date=date(2026, 2, 5),
        ))
        db.commit()

        balances = derive_account_cash_asof(db, 1, account.id, date(2026, 1, 31))
        # 10000 − 15 − 1005 + 477 + 90 = 9547
        assert balances["CNY"] == Decimal("9547")
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_replay_inconsistent_security_reported_and_mismatched():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        cmb = make_account(db, "CMB")
        add_txn(db, account_id=cmb.id, quantity="100", txn_date=date(2026, 1, 5))
        # NULL 账户超卖 → 该证券按账户重放矛盾
        add_txn(db, account_id=None, txn_type="SELL", quantity="80",
                txn_date=date(2026, 1, 10))
        db.commit()

        snapshot = make_snapshot_via_api(
            db, cmb.id,
            positions=[{"symbol": "600000", "market": "A股", "quantity": Decimal("100")}],
        )
        assert snapshot.status == "MISMATCHED"
        inconsistent = snapshot.diff_detail["replay_inconsistent"]
        assert len(inconsistent) == 1
        assert inconsistent[0]["symbol"] == "600000"
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_manual_compare_refreshes_after_ledger_change():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = make_account(db)
        db.add(CashEvent(
            user_id=1, broker_account_id=account.id, event_type="DEPOSIT",
            amount=Decimal("2000"), currency="CNY", event_date=date(2026, 1, 2),
        ))
        # 实际买了 100，但只录了 80 —— 快照按真实券商状态录入
        add_txn(db, account_id=account.id, quantity="80")
        db.commit()

        snapshot = make_snapshot_via_api(
            db, account.id,
            positions=[{"symbol": "600000", "market": "A股", "quantity": Decimal("100")}],
            cash={"CNY": Decimal("1000")},  # 2000 − 100×10
        )
        assert snapshot.status == "MISMATCHED"
        assert snapshot.diff_detail["positions"][0]["status"] == "QUANTITY_MISMATCH"
        assert snapshot.diff_detail["cash"][0]["status"] == "MISMATCH"

        # 补录漏掉的 20 股（数量与现金同时归位）后手动重比 → MATCHED
        add_txn(db, account_id=account.id, quantity="20", txn_date=date(2026, 1, 6))
        db.commit()
        refreshed = compare_reconciliation_snapshot(
            snapshot.id, current_user=get_user(db), db=db
        )
        assert refreshed.status == "MATCHED"
        assert refreshed.diff_detail["summary"]["matched"] is True
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_scoped_snapshot_compares_only_its_market():
    """东财分范围快照：stock 只比 A股、hk_connect 只比港股，且不比现金（review #57 P1）。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = make_account(db)
        add_txn(db, account_id=account.id, symbol="600000", market="A股", quantity="100")
        add_txn(db, account_id=account.id, symbol="00700", market="港股", quantity="200",
                currency="HKD")
        db.commit()

        stock_snapshot = ReconciliationSnapshot(
            user_id=1, broker_account_id=account.id, snapshot_date=date(2026, 1, 31),
            statement_scope="stock",
            positions=[{"symbol": "600000", "market": "A股", "quantity": "100"}],
            cash_balances={"CNY": "12345"},  # 范围内现金，不参与比对
        )
        db.add(stock_snapshot)
        db.flush()
        from app.services.reconciliation_service import run_and_store_compare
        run_and_store_compare(db, stock_snapshot)
        assert stock_snapshot.status == "MATCHED"
        assert stock_snapshot.diff_detail["summary"]["cash_compared"] is False
        # 港股持仓不得被算成"快照缺记录"
        symbols = [d["symbol"] for d in stock_snapshot.diff_detail["positions"]]
        assert symbols == ["600000"]

        hk_snapshot = ReconciliationSnapshot(
            user_id=1, broker_account_id=account.id, snapshot_date=date(2026, 1, 31),
            statement_scope="hk_connect",
            positions=[{"symbol": "00700", "market": "港股", "quantity": "200"}],
            cash_balances={"HKD": "999"},
        )
        db.add(hk_snapshot)
        db.flush()
        run_and_store_compare(db, hk_snapshot)
        assert hk_snapshot.status == "MATCHED"
        assert [d["symbol"] for d in hk_snapshot.diff_detail["positions"]] == ["00700"]
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_cash_mismatch_blocks_overall_match():
    """持仓一致但现金未闭合不得整体绿灯（review #57 P1 假绿场景）。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = make_account(db)
        # 只有买入、没有入金：推导现金 -1000
        add_txn(db, account_id=account.id, quantity="100", price="10")
        db.commit()

        snapshot = make_snapshot_via_api(
            db, account.id,
            positions=[{"symbol": "600000", "market": "A股", "quantity": Decimal("100")}],
            cash={},  # 快照未录现金
        )
        assert snapshot.status == "MISMATCHED"
        summary = snapshot.diff_detail["summary"]
        assert summary["position_mismatches"] == 0
        assert summary["cash_mismatches"] == 1
        assert snapshot.diff_detail["cash"][0]["derived_balance"] == -1000.0
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()



def test_excluded_securities_are_ignored_on_both_sides():
    """排除清单（如货币基金 511880）：券商快照有、系统无 → 仍 MATCHED，
    且生效的排除项记入 summary.excluded_symbols 供审计。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = make_account(db)
        add_txn(db, account_id=account.id, symbol="600000", quantity="100")
        db.add(CashEvent(
            user_id=1, broker_account_id=account.id, event_type="DEPOSIT",
            amount=Decimal("1000"), currency="CNY", event_date=date(2026, 1, 2),
        ))
        db.add(SecurityRule(rule_type="EXCLUDE", user_id=1, symbol="511880", market="A股", note="货币基金"))
        db.commit()

        snapshot = make_snapshot_via_api(
            db, account.id,
            positions=[
                {"symbol": "600000", "market": "A股", "quantity": Decimal("100")},
                {"symbol": "511880", "market": "A股", "quantity": Decimal("7000")},
            ],
            cash={"CNY": Decimal("0")},
        )
        assert snapshot.diff_detail["summary"]["excluded_symbols"] == [
            {"symbol": "511880", "market": "A股"}
        ]
        by_symbol = {d["symbol"]: d for d in snapshot.diff_detail["positions"]}
        assert "511880" not in by_symbol
        assert by_symbol["600000"]["status"] == "MATCH"
        # 现金仍照常比对（排除只作用于持仓行）
        assert snapshot.diff_detail["summary"]["position_mismatches"] == 0
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_exclusion_only_matches_exact_symbol_market_key():
    """(symbol, market) 精确匹配：同代码不同市场不受排除影响。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = make_account(db)
        db.add(SecurityRule(rule_type="EXCLUDE", user_id=1, symbol="511880", market="港股"))
        db.commit()

        snapshot = make_snapshot_via_api(
            db, account.id,
            positions=[{"symbol": "511880", "market": "A股", "quantity": Decimal("7000")}],
        )
        assert snapshot.status == "MISMATCHED"
        assert snapshot.diff_detail["summary"]["excluded_symbols"] == []
        assert snapshot.diff_detail["positions"][0]["status"] == "MISSING_IN_SYSTEM"
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_excluded_security_replay_inconsistency_does_not_block_matched():
    """排除标的的重放矛盾同样双侧忽略：不阻塞 MATCHED，且记入 excluded_symbols。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        cmb = make_account(db, "CMB")
        # 正常标的：账户内自洽
        add_txn(db, account_id=cmb.id, symbol="600000", quantity="100")
        # 排除标的：NULL 账户超卖 → 按账户重放矛盾（AccountReplayError 形态）
        add_txn(db, account_id=cmb.id, symbol="511880", quantity="100",
                txn_date=date(2026, 1, 5))
        add_txn(db, account_id=None, symbol="511880", txn_type="SELL", quantity="80",
                txn_date=date(2026, 1, 10))
        db.add(SecurityRule(rule_type="EXCLUDE", user_id=1, symbol="511880", market="A股", note="货币基金"))
        # 现金闭合（两笔买入共 2000），使整体状态只取决于排除语义是否生效
        db.add(CashEvent(
            user_id=1, broker_account_id=cmb.id, event_type="DEPOSIT",
            amount=Decimal("2000"), currency="CNY", event_date=date(2026, 1, 2),
        ))
        db.commit()

        snapshot = make_snapshot_via_api(
            db, cmb.id,
            positions=[{"symbol": "600000", "market": "A股", "quantity": Decimal("100")}],
        )
        assert snapshot.status == "MATCHED"
        assert snapshot.diff_detail["replay_inconsistent"] == []
        assert snapshot.diff_detail["summary"]["replay_inconsistent_count"] == 0
        assert snapshot.diff_detail["summary"]["excluded_symbols"] == [
            {"symbol": "511880", "market": "A股"}
        ]
        assert {d["symbol"] for d in snapshot.diff_detail["positions"]} == {"600000"}
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_settlement_aware_cash_uses_flow_settlement_currency():
    """沪港通 HKD 记账、CNY 结算：推导现金按流水结算净额入 CNY 桶，
    不再产生幻影 HKD 流出；无流水链接的普通交易口径不变。"""
    from datetime import datetime, timezone

    from app.models.broker_fund_flow import BrokerFundFlow
    from app.services.reconciliation_service import derive_account_cash_asof

    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = make_account(db, "CMB")
        # 沪港通买入：HKD 记账（qty 1000 × 10 HKD + fee），CNY 实际结算 -9,300.50
        hk_txn = add_txn(db, account_id=account.id, symbol="00728", market="港股",
                         quantity="1000", price="10", fee="30",
                         txn_date=date(2026, 1, 5), currency="HKD")
        db.add(BrokerFundFlow(
            user_id=1, broker_account_id=account.id, broker="招商证券",
            business_name="证券买入", trade_date=date(2026, 1, 5),
            trade_price=Decimal("10"), trade_quantity=Decimal("1000"),
            amount=Decimal("-9300.50"), currency="CNY",
            settlement_rate=Decimal("0.9271"), transaction_id=hk_txn.id,
            row_hash="s" * 64, created_at=datetime.now(timezone.utc),
        ))
        # 普通 A 股买入：无结算流水，按记账口径 qty×price+fee
        add_txn(db, account_id=account.id, symbol="600000", market="A股",
                quantity="100", price="10", fee="5",
                txn_date=date(2026, 1, 6), currency="CNY")
        db.commit()

        balances = derive_account_cash_asof(db, 1, account.id, date(2026, 1, 31))
        # CNY = 沪港通结算 -9300.50 + 普通买入 -(1000+5)
        assert balances["CNY"] == Decimal("-10305.50")
        assert "HKD" not in balances  # 幻影 HKD 桶消失
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_settlement_cash_eastmoney_uses_gross_minus_detail_fees():
    """东财港股通流水 amount 是无符号 CNY 成交额、费用在明细列：
    BUY 流出 成交额+费，SELL 流入 成交额−费；直接加 amount 属旧错误口径。"""
    from datetime import datetime, timezone

    from app.models.broker_fund_flow import BrokerFundFlow
    from app.services.reconciliation_service import derive_account_cash_asof

    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = make_account(db, "EM")
        buy = add_txn(db, account_id=account.id, symbol="00700", market="港股",
                      quantity="100", price="80", fee="20",
                      txn_date=date(2026, 1, 5), currency="HKD")
        sell = add_txn(db, account_id=account.id, symbol="00700", market="港股",
                       quantity="100", price="85", fee="18",
                       txn_date=date(2026, 1, 20), currency="HKD",
                       txn_type="SELL")
        common = dict(
            user_id=1, broker_account_id=account.id, broker="东方财富证券",
            currency="CNY", settlement_rate=Decimal("0.92"),
            created_at=datetime.now(timezone.utc),
        )
        # BUY：成交额 8000（无符号），费用 22 → CNY 现金 -8022
        db.add(BrokerFundFlow(
            business_name="证券买入", trade_date=date(2026, 1, 5),
            amount=Decimal("8000"), commission=Decimal("15"),
            stamp_tax=Decimal("5"), handling_fee=Decimal("2"),
            transaction_id=buy.id, row_hash="e" * 64, **common,
        ))
        # SELL：成交额 8500，费用 30 → CNY 现金 +8470
        db.add(BrokerFundFlow(
            business_name="证券卖出", trade_date=date(2026, 1, 20),
            amount=Decimal("8500"), commission=Decimal("20"),
            stamp_tax=Decimal("10"),
            transaction_id=sell.id, row_hash="f" * 64, **common,
        ))
        db.commit()

        balances = derive_account_cash_asof(db, 1, account.id, date(2026, 1, 31))
        assert balances["CNY"] == Decimal("448")  # -8022 + 8470
        assert "HKD" not in balances
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_settlement_cash_unknown_broker_falls_back_to_booking_currency():
    """未知券商的带汇率流水不猜口径：回退记账币种 qty×price。"""
    from datetime import datetime, timezone

    from app.models.broker_fund_flow import BrokerFundFlow
    from app.services.reconciliation_service import derive_account_cash_asof

    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = make_account(db, "OTHER")
        txn = add_txn(db, account_id=account.id, symbol="00700", market="港股",
                      quantity="100", price="80", fee="20",
                      txn_date=date(2026, 1, 5), currency="HKD")
        db.add(BrokerFundFlow(
            user_id=1, broker_account_id=account.id, broker="某未来券商",
            business_name="证券买入", trade_date=date(2026, 1, 5),
            amount=Decimal("-8022"), currency="CNY",
            settlement_rate=Decimal("0.92"), transaction_id=txn.id,
            row_hash="g" * 64, created_at=datetime.now(timezone.utc),
        ))
        db.commit()

        balances = derive_account_cash_asof(db, 1, account.id, date(2026, 1, 31))
        assert balances["HKD"] == Decimal("-8020")  # -(100×80+20)，回退口径
        assert "CNY" not in balances
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()
