"""组合快照端点：合并数据形状、价格新鲜度标记与对账状态出口。"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.cash_event import CashEvent
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.reconciliation_snapshot import ReconciliationSnapshot
from app.models.transaction import Transaction
from app.services.holding_service import recalculate_holdings
from app.services.reconciliation_service import run_and_store_compare
from app.services.statistics import build_portfolio_snapshot
from tests.helpers import make_account, reset_tables


RESET_MODELS = (
    BrokerFundFlow,
    IbkrActivityFlow,
    ReconciliationSnapshot,
    CashEvent,
    Holding,
    CorporateAction,
    Transaction,
    BrokerAccount,
)


def test_portfolio_snapshot_bundles_dashboard_data():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = BrokerAccount(user_id=1, broker="CMB", account_name="CMB", base_currency="CNY")
        db.add(account)
        db.flush()

        for symbol, quantity, price, txn_date in [
            ("600000", "100", "10", date(2026, 1, 5)),
            ("PCT", "50", "5", date(2026, 1, 8)),
        ]:
            db.add(Transaction(
                user_id=1, broker_account_id=account.id, symbol=symbol, name=symbol,
                market="A股" if symbol == "600000" else "新加坡股",
                transaction_type="BUY", quantity=Decimal(quantity),
                price=Decimal(price), fee=Decimal("0"),
                transaction_date=txn_date, currency="CNY",
            ))
        db.commit()
        recalculate_holdings(db, 1, "600000", "A股")
        recalculate_holdings(db, 1, "PCT", "新加坡股")

        rows = db.query(Holding).filter(Holding.user_id == 1).all()
        now = datetime.now(timezone.utc)
        for row in rows:
            if row.symbol == "600000":
                row.current_price = Decimal("11")
                row.price_updated_at = now  # 新鲜
            else:
                row.current_price = Decimal("6")
                row.price_updated_at = now - timedelta(days=40)  # 陈价（PCT 手动维护场景）
        db.commit()

        snapshot_row = ReconciliationSnapshot(
            user_id=1, broker_account_id=account.id, snapshot_date=date(2026, 1, 31),
            positions=[
                {"symbol": "600000", "market": "A股", "quantity": "100"},
                {"symbol": "PCT", "market": "新加坡股", "quantity": "50"},
            ],
            cash_balances={},
        )
        db.add(snapshot_row)
        db.flush()
        run_and_store_compare(db, snapshot_row)

        snapshot = build_portfolio_snapshot(db, 1)

        # 表现区块完整携带（含估算口径标记）
        assert snapshot["performance"]["account_return"]["calculation_status"] == "exact"
        assert snapshot["performance"]["current_performance"]["current_market_value_cny"] > 0

        # 价格新鲜度：600000 新鲜、PCT 陈价并进入警告
        freshness = snapshot["prices"]["freshness"]
        assert freshness["600000:A股"]["stale"] is False
        assert freshness["PCT:新加坡股"]["stale"] is True
        assert snapshot["prices"]["stale_keys"] == ["PCT:新加坡股"]
        assert any("PCT" in warning for warning in snapshot["data_quality"]["warnings"])

        # 对账状态出口
        account_info = snapshot["accounts"][0]
        assert account_info["latest_reconciliation"] is not None
        assert account_info["latest_reconciliation"]["status"] in ("MATCHED", "MISMATCHED")

        # 近期交易与市场分布
        assert len(snapshot["recent_transactions"]) == 2
        assert {m["market"] for m in snapshot["markets"]} == {"A股", "新加坡股"}
        assert snapshot["base_currency"] == "CNY"
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def _buy(db, account_id, *, symbol="600000", market="A股", quantity="100"):
    db.add(Transaction(
        user_id=1, broker_account_id=account_id, symbol=symbol, name=symbol,
        market=market, transaction_type="BUY", quantity=Decimal(quantity),
        price=Decimal("10"), fee=Decimal("0"),
        transaction_date=date(2026, 1, 5), currency="CNY",
    ))


def test_price_and_timestamp_selected_atomically_regardless_of_row_order():
    """多账户行价格/时间不一致时：取更新最晚的 (价格, 时间) 对，与插入顺序无关。"""
    from app.services.statistics import resolve_server_prices

    for reverse in (False, True):
        db = SessionLocal()
        reset_tables(db, RESET_MODELS)
        try:
            a = make_account(db, "A")
            b = make_account(db, "B")
            _buy(db, a.id)
            _buy(db, b.id)
            db.commit()
            recalculate_holdings(db, 1, "600000", "A股")

            rows = db.query(Holding).filter(Holding.user_id == 1).order_by(Holding.id).all()
            assert len(rows) == 2
            if reverse:
                rows = list(reversed(rows))
            now = datetime.now(timezone.utc)
            # 旧价 9（30 天前） vs 新价 12（现在）——两种插入顺序都必须选 12/now
            rows[0].current_price = Decimal("9")
            rows[0].price_updated_at = now - timedelta(days=30)
            rows[1].current_price = Decimal("12")
            rows[1].price_updated_at = now
            db.commit()

            prices, sources, freshness = resolve_server_prices(db, 1)
            key = "600000:A股"
            assert prices[key] == 12.0, f"reverse={reverse}: 必须取更新更晚的价格"
            assert freshness[key]["stale"] is False
            assert freshness[key]["price_as_of"] == now.isoformat()
        finally:
            reset_tables(db, RESET_MODELS)
            db.close()


def test_account_badge_aggregates_all_scopes_on_latest_date():
    """同日一红一绿的分范围快照：整体必须红，且与创建顺序无关（review #59 P1）。"""
    for first_status_matched in (False, True):
        db = SessionLocal()
        reset_tables(db, RESET_MODELS)
        try:
            account = make_account(db, "东财")
            # stock 范围有差异（快照持仓 100 但账本没有）；hk_connect 范围一致（都为空）
            scoped = [
                ("stock", [{"symbol": "600000", "market": "A股", "quantity": "100"}]),
                ("hk_connect", []),
            ]
            if first_status_matched:
                scoped = list(reversed(scoped))
            for scope, positions in scoped:
                row = ReconciliationSnapshot(
                    user_id=1, broker_account_id=account.id,
                    snapshot_date=date(2026, 1, 31), statement_scope=scope,
                    positions=positions, cash_balances={},
                )
                db.add(row)
                db.flush()
                run_and_store_compare(db, row)

            snapshot = build_portfolio_snapshot(db, 1)
            badge = snapshot["accounts"][0]["latest_reconciliation"]
            assert badge["status"] == "MISMATCHED", (
                f"order={'green-first' if first_status_matched else 'red-first'}: "
                "任一 scope 有差异整体必须红"
            )
            assert badge["all_scoped"] is True
            statuses = {s["statement_scope"]: s["status"] for s in badge["scopes"]}
            assert statuses["stock"] == "MISMATCHED"
            assert statuses["hk_connect"] == "MATCHED"
        finally:
            reset_tables(db, RESET_MODELS)
            db.close()
