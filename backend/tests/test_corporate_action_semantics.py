"""Issue #47: the four position replays must agree on corporate-action fields.

第四处 = 券商导入器的账户级持仓预检（招商 validate_account_positions_before_commit
与东财 calculate_account_position_quantities）。它们此前各自手写数量分支：
裸读 shares_received（ratio-only 送股加 0 股）、拆股只认 split_ratio 且解析失败
静默吞掉，并且按 broker_account_id 过滤公司行动——而分红同步接受送转建议时
刻意写 broker_account_id=None（送转是比例行动，作用于所有账户桶），于是最常见
的那条路径在预检里根本不可见。后果：招商整批拒绝导入、东财对账 MISMATCHED
整批回滚。这里把预检钉进同一组断言。
"""

from datetime import date
from decimal import Decimal

import pytest

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.transaction import Transaction
from app.schemas.corporate_action import CorporateActionCreate
from app.services.eastmoney_statement_importer import calculate_account_position_quantities
from app.services.holding_service import recalculate_holdings
from app.services.statistics.fifo_results import fifo_results_for_user
from tests.helpers import reset_tables


RESET_MODELS = (
    BrokerFundFlow, IbkrActivityFlow, Holding, CorporateAction, Transaction, BrokerAccount,
)


def _seed_position(db, **action_fields):
    """买入挂在某个券商账户上；公司行动按分红同步的默认形态挂 NULL 账户。"""
    account = BrokerAccount(
        user_id=1, broker="东方财富", account_name="东财", base_currency="CNY",
    )
    db.add(account)
    db.flush()
    db.add(Transaction(
        user_id=1, symbol="600000", name="浦发银行", market="A股",
        transaction_type="BUY", quantity=Decimal("100"), price=Decimal("10"),
        fee=Decimal("0"), transaction_date=date(2026, 1, 1), currency="CNY",
        broker_account_id=account.id,
    ))
    db.add(CorporateAction(
        user_id=1, symbol="600000", name="浦发银行", market="A股",
        ex_date=date(2026, 1, 10), currency="CNY",
        broker_account_id=None, **action_fields,
    ))
    db.commit()
    return account.id


def _replayed_quantities(db, account_id):
    holding = recalculate_holdings(db, 1, "600000", "A股")
    fifo = fifo_results_for_user(db, 1, {("600000", "A股")})[("600000", "A股")]
    fifo_quantity = sum(Decimal(str(b["quantity"])) for b in fifo["buy_queue"])
    precheck = calculate_account_position_quantities(
        db, user_id=1, broker_account_id=account_id, snapshot_date=date(2026, 12, 31),
    )[("600000", "A股")]
    return Decimal(str(holding.quantity)), fifo_quantity, precheck


@pytest.mark.parametrize(
    "action_fields,expected",
    [
        # Bonus with only distribution_ratio (10 songs 3): 100 -> 130
        ({"action_type": "STOCK_DIVIDEND", "distribution_ratio": "10:3"}, Decimal("130")),
        # Bonus with only shares_received: 100 + 30 -> 130
        ({"action_type": "STOCK_DIVIDEND", "shares_received": Decimal("30")}, Decimal("130")),
        # Both provided: distribution_ratio wins (10:2 -> 120, not +30)
        (
            {
                "action_type": "STOCK_DIVIDEND",
                "distribution_ratio": "10:2",
                "shares_received": Decimal("30"),
            },
            Decimal("120"),
        ),
        # Split with only split_ratio 1:2 -> 200
        ({"action_type": "STOCK_SPLIT", "split_ratio": "1:2"}, Decimal("200")),
        # Split with only new_shares -> 200
        ({"action_type": "STOCK_SPLIT", "new_shares": Decimal("200")}, Decimal("200")),
        # Reverse split 10:1 -> 10
        ({"action_type": "REVERSE_SPLIT", "split_ratio": "10:1"}, Decimal("10")),
    ],
)
def test_holding_and_fifo_replays_agree(action_fields, expected):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account_id = _seed_position(db, **action_fields)
        holding_qty, fifo_qty, precheck_qty = _replayed_quantities(db, account_id)
        assert holding_qty == expected
        assert fifo_qty == expected
        assert precheck_qty == expected, (
            "导入器账户预检与内核重放分叉——ratio-only 送股/new_shares 拆股会让"
            "招商整批拒绝导入、东财对账 MISMATCHED 整批回滚"
        )
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_precheck_ignores_rights_issue_without_price():
    """配股缺认购价：内核三处都不计入数量，预检必须一致（原来会多算）。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account_id = _seed_position(
            db, action_type="RIGHTS_ISSUE", subscription_quantity=Decimal("100"),
        )
        holding_qty, fifo_qty, precheck_qty = _replayed_quantities(db, account_id)
        assert holding_qty == Decimal("100")
        assert fifo_qty == Decimal("100")
        assert precheck_qty == Decimal("100")
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_precheck_counts_transfer_in_as_position():
    """转入腿是本账户获得该数量的唯一记录，预检必须计入。

    修复前：招商侧 Transaction 落进 `elif event.action_type` 分支直接
    AttributeError 崩掉整个导入；东财侧被静默忽略，computed 少一整笔。
    """
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        source = BrokerAccount(
            user_id=1, broker="招商证券", account_name="招商", base_currency="CNY",
        )
        target = BrokerAccount(
            user_id=1, broker="东方财富", account_name="东财", base_currency="CNY",
        )
        db.add_all([source, target])
        db.flush()
        db.add(Transaction(
            user_id=1, symbol="600000", name="浦发银行", market="A股",
            transaction_type="BUY", quantity=Decimal("100"), price=Decimal("10"),
            fee=Decimal("0"), transaction_date=date(2026, 1, 1), currency="CNY",
            broker_account_id=source.id,
        ))
        out_leg = Transaction(
            user_id=1, symbol="600000", name="浦发银行", market="A股",
            transaction_type="TRANSFER_OUT", quantity=Decimal("100"), price=Decimal("10"),
            fee=Decimal("0"), transaction_date=date(2026, 2, 1), currency="CNY",
            broker_account_id=source.id,
        )
        in_leg = Transaction(
            user_id=1, symbol="600000", name="浦发银行", market="A股",
            transaction_type="TRANSFER_IN", quantity=Decimal("100"), price=Decimal("10"),
            fee=Decimal("0"), transaction_date=date(2026, 2, 1), currency="CNY",
            broker_account_id=target.id,
        )
        db.add_all([out_leg, in_leg])
        db.flush()
        out_leg.linked_transaction_id = in_leg.id
        in_leg.linked_transaction_id = out_leg.id
        db.commit()

        quantities = calculate_account_position_quantities(
            db, user_id=1, broker_account_id=target.id, snapshot_date=date(2026, 12, 31),
        )
        assert quantities[("600000", "A股")] == Decimal("100")

        # 转出账户归零，不是负数
        source_quantities = calculate_account_position_quantities(
            db, user_id=1, broker_account_id=source.id, snapshot_date=date(2026, 12, 31),
        )
        assert source_quantities[("600000", "A股")] == Decimal("0")
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_ttwr_curve_position_matches_holding_for_ratio_only_bonus():
    """A distribution_ratio-only bonus must not trigger a terminal mismatch."""
    from app.services.statistics import calculate_performance_analytics

    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        _seed_position(db, action_type="STOCK_DIVIDEND", distribution_ratio="10:3")
        recalculate_holdings(db, 1, "600000", "A股")

        analytics = calculate_performance_analytics(db, 1, {"600000": 10})

        assert analytics["data_quality"]["terminal_position_mismatches"] == []
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_schema_requires_a_quantity_field():
    base = dict(
        symbol="600000", market="A股", ex_date=date(2026, 1, 10), currency="CNY"
    )
    with pytest.raises(ValueError):
        CorporateActionCreate(action_type="STOCK_DIVIDEND", **base)
    with pytest.raises(ValueError):
        CorporateActionCreate(action_type="STOCK_SPLIT", **base)
    # Valid when one usable field is present.
    CorporateActionCreate(
        action_type="STOCK_DIVIDEND", distribution_ratio="10:3", **base
    )
    CorporateActionCreate(action_type="STOCK_SPLIT", new_shares=Decimal("200"), **base)
