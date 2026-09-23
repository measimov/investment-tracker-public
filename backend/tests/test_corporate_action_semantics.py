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


# ---------------------------------------------------------------------------
# #174 期初建仓（OPENING_POSITION）：账户级绝对数量 + 可选成本，四处重放同源
# ---------------------------------------------------------------------------


def _seed_opening(db, *, on_account: bool, **action_fields):
    """空账本上直接建期初仓（成本已知/未知），可选再买 100 股验证平均成本合并。"""
    account = BrokerAccount(
        user_id=1, broker="东方财富", account_name="东财", base_currency="CNY",
    )
    db.add(account)
    db.flush()
    db.add(CorporateAction(
        user_id=1, symbol="600000", name="浦发银行", market="A股",
        action_type="OPENING_POSITION", ex_date=date(2026, 1, 5), currency="CNY",
        broker_account_id=account.id if on_account else None, adjusted_quantity=Decimal("50"),
        **action_fields,
    ))
    db.commit()
    return account.id


@pytest.mark.parametrize(
    "action_fields,expected_total_cost,expected_unknown",
    [
        ({}, Decimal("0"), Decimal("50")),  # 成本未知：0 成本入账、未知份额 50
        ({"adjusted_cost_per_share": Decimal("8")}, Decimal("400"), Decimal("0")),
        ({"cost_basis_adjustment": Decimal("450")}, Decimal("450"), Decimal("0")),
        # 两者都给：总成本优先（校验层要求一致，这里直接落库验证优先级）
        (
            {"adjusted_cost_per_share": Decimal("9"), "cost_basis_adjustment": Decimal("450")},
            Decimal("450"), Decimal("0"),
        ),
    ],
)
def test_opening_position_replays_agree_across_holding_fifo_and_precheck(
    action_fields, expected_total_cost, expected_unknown
):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account_id = _seed_opening(db, on_account=True, **action_fields)
        holding = recalculate_holdings(db, 1, "600000", "A股")
        assert holding.broker_account_id == account_id  # 落在行动自己的账户桶
        assert Decimal(str(holding.quantity)) == Decimal("50")
        assert Decimal(str(holding.total_cost)) == expected_total_cost
        assert Decimal(str(holding.unknown_cost_quantity)) == expected_unknown
        fifo = fifo_results_for_user(db, 1, {("600000", "A股")})[("600000", "A股")]
        lots = fifo["buy_queue"]
        assert sum(Decimal(str(b["quantity"])) for b in lots) == Decimal("50")
        assert all(b["cost_known"] is (expected_unknown == 0) for b in lots)
        precheck = calculate_account_position_quantities(
            db, user_id=1, broker_account_id=account_id, snapshot_date=date(2026, 12, 31),
        )[("600000", "A股")]
        assert precheck == Decimal("50")
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_null_account_opening_position_opens_the_unassigned_bucket_only():
    """NULL 账户的期初仓开在未指定账户桶，**不**让某个账户的预检借它放行卖出。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account_id = _seed_opening(db, on_account=False, adjusted_cost_per_share=Decimal("8"))
        holding = recalculate_holdings(db, 1, "600000", "A股")
        assert holding.broker_account_id is None and Decimal(str(holding.quantity)) == Decimal("50")
        precheck = calculate_account_position_quantities(
            db, user_id=1, broker_account_id=account_id, snapshot_date=date(2026, 12, 31),
        )
        assert precheck.get(("600000", "A股"), Decimal("0")) == Decimal("0")
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_selling_unknown_cost_lots_marks_realized_pnl_as_estimated():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account_id = _seed_opening(db, on_account=True)
        db.add(Transaction(
            user_id=1, symbol="600000", name="浦发银行", market="A股",
            transaction_type="SELL", quantity=Decimal("20"), price=Decimal("12"),
            fee=Decimal("0"), transaction_date=date(2026, 2, 1), currency="CNY",
            broker_account_id=account_id,
        ))
        db.commit()
        holding = recalculate_holdings(db, 1, "600000", "A股")
        assert Decimal(str(holding.quantity)) == Decimal("30")
        assert Decimal(str(holding.unknown_cost_quantity)) == Decimal("30")
        fifo = fifo_results_for_user(db, 1, {("600000", "A股")})[("600000", "A股")]
        assert fifo["estimated_cost_trade_count"] == 1
        assert fifo["closed_trades"][0]["cost_estimated"] is True
        assert fifo["closed_trades"][0]["realized_pnl"] == pytest.approx(240.0)  # 成本按 0
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_schema_requires_quantity_and_consistent_costs_for_opening_position():
    base = {"symbol": "600000", "market": "A股", "action_type": "OPENING_POSITION", "ex_date": "2026-01-05"}
    with pytest.raises(ValueError, match="adjusted_quantity"):
        CorporateActionCreate(**base)
    with pytest.raises(ValueError, match="不一致"):
        CorporateActionCreate(**base, adjusted_quantity=Decimal("50"),
                              adjusted_cost_per_share=Decimal("8"), cost_basis_adjustment=Decimal("100"))
    ok = CorporateActionCreate(**base, adjusted_quantity=Decimal("50"))
    assert ok.adjusted_cost_per_share is None and ok.cost_basis_adjustment is None
