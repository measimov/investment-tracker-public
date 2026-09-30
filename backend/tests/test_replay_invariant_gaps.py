"""#270：重放不变量外围的缺口。

- 手工录入 / 标准 CSV 导入的超卖校验此前只数买卖与转仓（第五份重放），期初建仓、拆股、
  送股之后的合法卖出被误报，反向拆股之后的超卖反而放行；
- IBKR 转板推算只数 BUY/SELL；
- 配股成本在持仓重算与 FIFO/曲线之间分叉；
- 比例字段只校验非空，「10/3」静默 no-op；PATCH 绕过创建期校验、只按旧类型判断是否重算。
"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.database import SessionLocal
from app.main import app
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.corporate_action import CorporateActionCreate, CorporateActionUpdate
from app.services.auth_session_service import issue_session
from app.services.holding_service import recalculate_holdings, validate_account_sequence
from app.services.ibkr_activity_importer import calculate_position_before
from app.services.portfolio.semantics import rights_issue_lot
from app.services.statistics.fifo_results import fifo_results_for_user
from tests.helpers import reset_tables

RESET_MODELS = (
    BrokerFundFlow,
    IbkrActivityFlow,
    Holding,
    CorporateAction,
    Transaction,
    BrokerAccount,
)
SYMBOL, MARKET = "600000", "A股"


@pytest.fixture
def db():
    session = SessionLocal()
    reset_tables(session, RESET_MODELS)
    try:
        yield session
    finally:
        reset_tables(session, RESET_MODELS)
        session.close()


def _account(db, name="账户A"):
    account = BrokerAccount(user_id=2, broker="测试券商", account_name=name, base_currency="CNY")
    db.add(account)
    db.flush()
    return account.id


def _buy(db, quantity, account_id=None, day=date(2026, 1, 5), user_id=2):
    db.add(
        Transaction(
            user_id=user_id,
            broker_account_id=account_id,
            symbol=SYMBOL,
            name="浦发银行",
            market=MARKET,
            transaction_type="BUY",
            quantity=Decimal(str(quantity)),
            price=Decimal("10"),
            fee=Decimal("0"),
            transaction_date=day,
            currency="CNY",
        )
    )


def _action(db, user_id=2, account_id=None, day=date(2026, 1, 10), **fields):
    action = CorporateAction(
        user_id=user_id,
        broker_account_id=account_id,
        symbol=SYMBOL,
        name="浦发银行",
        market=MARKET,
        ex_date=day,
        currency="CNY",
        **fields,
    )
    db.add(action)
    db.flush()
    return action


def _sell(quantity, account_id=None, day=date(2026, 2, 1)):
    return SimpleNamespace(
        id=None,
        symbol=SYMBOL,
        market=MARKET,
        transaction_type="SELL",
        quantity=Decimal(str(quantity)),
        transaction_date=day,
        broker_account_id=account_id,
    )


def _validate(db, candidate):
    validate_account_sequence(
        db,
        user_id=2,
        broker_account_id=candidate.broker_account_id,
        symbol=SYMBOL,
        market=MARKET,
        candidates=[candidate],
    )


# --------------------------------------------------------------------------- 超卖校验


def test_sell_after_opening_position_is_allowed(db):
    """#174 场景：转托管转入只建期初仓、不建买入，随后的场内卖出不得被误报超卖。"""
    account_id = _account(db)
    _action(
        db, account_id=account_id, action_type="OPENING_POSITION", adjusted_quantity=Decimal("100")
    )
    db.commit()
    _validate(db, _sell(60, account_id))


def test_opening_position_of_another_account_does_not_cover_this_one(db):
    account_id = _account(db)
    other_id = _account(db, "账户B")
    _action(
        db, account_id=other_id, action_type="OPENING_POSITION", adjusted_quantity=Decimal("100")
    )
    db.commit()
    with pytest.raises(ValueError, match="超过可用数量 0"):
        _validate(db, _sell(60, account_id))


def test_sell_after_split_and_bonus_is_allowed(db):
    _buy(db, 100)
    _action(db, action_type="STOCK_SPLIT", split_ratio="1:2")  # 100 → 200
    _action(
        db, day=date(2026, 1, 20), action_type="STOCK_DIVIDEND", distribution_ratio="10:5"
    )  # 200 → 300
    db.commit()
    _validate(db, _sell(300))
    with pytest.raises(ValueError, match="卖出 301 超过可用数量 300"):
        _validate(db, _sell(301))


def test_oversell_after_reverse_split_is_rejected(db):
    _buy(db, 100)
    _action(db, action_type="REVERSE_SPLIT", split_ratio="10:1")  # 100 → 10
    db.commit()
    with pytest.raises(ValueError, match="卖出 50 超过可用数量 10"):
        _validate(db, _sell(50))


def test_manual_sell_api_counts_the_split():
    """端到端：拆股之后通过 API 卖出拆股前不存在的数量。修复前是 400 超卖。"""
    session = SessionLocal()
    reset_tables(session, RESET_MODELS)
    try:
        _buy(session, 100)
        _action(session, action_type="STOCK_SPLIT", split_ratio="1:2")
        session.commit()
        recalculate_holdings(session, 2, SYMBOL, MARKET)
        user = session.query(User).filter(User.id == 2).one()
        token, _ = issue_session(session, user)
        headers = {"Authorization": f"Bearer {token}"}

        import anyio

        async def post_sell():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                return await client.post(
                    "/api/transactions",
                    headers=headers,
                    json={
                        "symbol": SYMBOL,
                        "name": "浦发银行",
                        "market": MARKET,
                        "transaction_type": "SELL",
                        "quantity": "150",
                        "price": "6",
                        "fee": "0",
                        "transaction_date": "2026-02-01",
                        "currency": "CNY",
                    },
                )

        response = anyio.run(post_sell)
        assert response.status_code == 201, response.text
        session.expire_all()
        holding = session.query(Holding).filter(Holding.user_id == 2).one()
        assert holding.quantity == Decimal("50")
    finally:
        reset_tables(session, RESET_MODELS)
        session.close()


def test_standard_import_counts_opening_position(db):
    from app.services.standard_import import _validate_import_batch_sequence

    account_id = _account(db)
    _action(
        db, account_id=account_id, action_type="OPENING_POSITION", adjusted_quantity=Decimal("100")
    )
    db.commit()
    row = {
        "symbol": SYMBOL,
        "market": MARKET,
        "transaction_type": "SELL",
        "quantity": Decimal("100"),
        "transaction_date": date(2026, 2, 1),
        "broker_account_id": account_id,
    }
    _validate_import_batch_sequence(db, 2, [row], account_id)
    with pytest.raises(ValueError, match="导入后出现超卖"):
        _validate_import_batch_sequence(db, 2, [{**row, "quantity": Decimal("101")}], account_id)


# --------------------------------------------------------------------------- 配股成本


def test_rights_issue_lot_amount_first_and_double_field_guard():
    base = {
        "action_type": "RIGHTS_ISSUE",
        "subscription_quantity": Decimal("30"),
        "subscription_price": Decimal("5"),
    }
    assert rights_issue_lot(SimpleNamespace(**base, subscription_amount=None)) == (
        Decimal("30"),
        Decimal("150"),
    )
    assert rights_issue_lot(SimpleNamespace(**base, subscription_amount=Decimal("160"))) == (
        Decimal("30"),
        Decimal("160"),
    )
    assert (
        rights_issue_lot(
            SimpleNamespace(
                **{**base, "subscription_price": None}, subscription_amount=Decimal("160")
            )
        )
        is None
    )
    assert rights_issue_lot(SimpleNamespace(action_type="STOCK_SPLIT")) is None


def test_rights_issue_cost_agrees_between_holding_and_fifo(db):
    """录了认购金额（含费用）时，持仓均价与 FIFO 剩余成本同口径：100×10 + 160 = 1160。"""
    _buy(db, 100, user_id=1)
    _action(
        db,
        user_id=1,
        action_type="RIGHTS_ISSUE",
        subscription_quantity=Decimal("30"),
        subscription_price=Decimal("5"),
        subscription_amount=Decimal("160"),
    )
    db.commit()
    holding = recalculate_holdings(db, 1, SYMBOL, MARKET)
    fifo = fifo_results_for_user(db, 1, {(SYMBOL, MARKET)})[(SYMBOL, MARKET)]
    assert holding.quantity == Decimal("130")
    assert Decimal(str(holding.total_cost)) == Decimal("1160")
    assert sum(Decimal(str(lot["total_cost"])) for lot in fifo["buy_queue"]) == Decimal("1160")


# --------------------------------------------------------------------------- schema


def _create(**fields):
    return CorporateActionCreate(symbol=SYMBOL, market=MARKET, ex_date=date(2026, 1, 10), **fields)


def test_full_width_colon_ratio_is_normalized():
    assert (
        _create(action_type="STOCK_DIVIDEND", distribution_ratio="10：3").distribution_ratio
        == "10:3"
    )
    assert _create(action_type="STOCK_SPLIT", split_ratio=" 1 : 2 ").split_ratio == "1:2"
    assert CorporateActionUpdate(split_ratio="1：2").split_ratio == "1:2"


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"action_type": "STOCK_DIVIDEND", "distribution_ratio": "10/3"}, "无法解析"),
        ({"action_type": "STOCK_SPLIT", "split_ratio": "abc"}, "无法解析"),
        ({"action_type": "RIGHTS_ISSUE", "subscription_quantity": Decimal("30")}, "认购价"),
    ],
)
def test_unusable_quantity_fields_are_rejected(fields, message):
    with pytest.raises(ValidationError, match=message):
        _create(**fields)


def test_update_rejects_negative_cost_basis():
    with pytest.raises(ValidationError):
        CorporateActionUpdate(cost_basis_adjustment=Decimal("-1"))


# --------------------------------------------------------------------------- PATCH


def _put(action_id, payload):
    session = SessionLocal()
    try:
        user = session.query(User).filter(User.id == 2).one()
        token, _ = issue_session(session, user)
    finally:
        session.close()

    import anyio

    async def call():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.put(
                f"/api/corporate-actions/{action_id}",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )

    return anyio.run(call)


def test_changing_cash_dividend_into_split_recalculates_holdings(db):
    _buy(db, 100)
    action = _action(db, action_type="CASH_DIVIDEND", total_dividend=Decimal("50"))
    db.commit()
    recalculate_holdings(db, 2, SYMBOL, MARKET)

    response = _put(action.id, {"action_type": "STOCK_SPLIT", "split_ratio": "1:2"})
    assert response.status_code == 200, response.text
    db.expire_all()
    assert db.query(Holding).filter(Holding.user_id == 2).one().quantity == Decimal("200")


def test_patch_cannot_clear_the_only_quantity_field(db):
    _buy(db, 100)
    action = _action(db, action_type="STOCK_SPLIT", split_ratio="1:2")
    db.commit()
    response = _put(action.id, {"split_ratio": None})
    assert response.status_code == 422
    assert "拆股/合股必须提供" in response.text


# --------------------------------------------------------------------------- IBKR 转板


def test_ibkr_position_before_relisting_counts_split(db):
    account_id = _account(db)
    _buy(db, 100, account_id=account_id)
    _action(db, action_type="STOCK_SPLIT", split_ratio="1:2")
    db.commit()
    quantity, avg_cost = calculate_position_before(
        db, 2, SYMBOL, MARKET, date(2026, 3, 1), broker_account_id=account_id
    )
    assert quantity == Decimal("200")
    assert avg_cost == Decimal("5")


def test_ibkr_position_before_relisting_does_not_borrow_other_buckets(db):
    """PR #296 评审：按账户重放不成立时不再取合并桶（其他账户 + 未指定账户的合计），
    而是返回 0（不合成转板）并把原因写进告警。"""
    account_id = _account(db)
    _buy(db, 3)  # 未指定账户桶
    _buy(db, 7, account_id=account_id)
    # 未指定账户、只填送股数的送股：两个桶都有持仓时无法归属 → AccountReplayError
    _action(db, action_type="STOCK_DIVIDEND", shares_received=Decimal("5"))
    db.commit()
    warnings = []
    quantity, avg_cost = calculate_position_before(
        db,
        2,
        SYMBOL,
        MARKET,
        date(2026, 3, 1),
        broker_account_id=account_id,
        warnings=warnings,
    )
    assert (quantity, avg_cost) == (Decimal("0"), Decimal("0"))
    assert len(warnings) == 1 and "未自动合成转板交易" in warnings[0]


@pytest.mark.parametrize("field, value", [("split_ratio", 2), ("distribution_ratio", 1.5)])
def test_non_string_ratio_is_a_validation_error_not_500(field, value):
    action_type = "STOCK_SPLIT" if field == "split_ratio" else "STOCK_DIVIDEND"
    with pytest.raises(ValidationError):
        CorporateActionCreate(
            symbol=SYMBOL,
            market=MARKET,
            action_type=action_type,
            ex_date=date(2026, 1, 10),
            **{field: value},
        )


@pytest.mark.parametrize(
    "field, value",
    [
        ("split_ratio", "1:0"),
        ("split_ratio", "1:-2"),
        ("distribution_ratio", "10:-3"),
    ],
)
def test_ratio_second_term_must_be_positive(field, value):
    action_type = "STOCK_SPLIT" if field == "split_ratio" else "STOCK_DIVIDEND"
    with pytest.raises(ValidationError, match="不为正"):
        CorporateActionCreate(
            symbol=SYMBOL,
            market=MARKET,
            action_type=action_type,
            ex_date=date(2026, 1, 10),
            **{field: value},
        )


# --------------------------------------------------------------------------- 合入前扫描脚本


def test_scan_script_reports_oversold_buckets_and_unusable_actions(db):
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "scripts" / "scan_account_sequences.py"
    spec = importlib.util.spec_from_file_location("scan_account_sequences", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    _buy(db, 100)
    _action(db, action_type="REVERSE_SPLIT", split_ratio="10:1")  # 100 → 10
    db.add(
        Transaction(
            user_id=2,
            symbol=SYMBOL,
            name="浦发银行",
            market=MARKET,
            transaction_type="SELL",
            quantity=Decimal("50"),
            price=Decimal("10"),
            fee=Decimal("0"),
            transaction_date=date(2026, 2, 1),
            currency="CNY",
        )
    )
    _action(db, day=date(2026, 3, 1), action_type="STOCK_DIVIDEND", distribution_ratio="10/3")
    db.commit()

    bucket_count, oversold, invalid = module.scan(user_id=2)
    assert bucket_count == 1
    assert [(row[2], row[3]) for row in oversold] == [(SYMBOL, MARKET)]
    assert "超过可用数量 10" in oversold[0][4]
    assert [action.distribution_ratio for action, _ in invalid] == ["10/3"]
