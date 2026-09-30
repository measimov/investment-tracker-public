"""手工入口的市场校验与代码归一（#278）：schema 层直接断言，不经数据库。"""

from datetime import date

import pytest
from pydantic import ValidationError

from app.core.markets import MANUAL_MARKETS
from app.schemas.corporate_action import CorporateActionCreate, CorporateActionUpdate
from app.schemas.security_rule import SecurityRuleCreate
from app.schemas.transaction import TransactionCreate, TransactionUpdate, TransferCreate
from app.services.stock_price_service import Market


def _txn(**overrides):
    base = dict(
        symbol="700",
        market="港股",
        transaction_type="BUY",
        quantity=1,
        price=1,
        transaction_date=date(2026, 9, 1),
        currency="HKD",
    )
    return {**base, **overrides}


def test_market_enum_matches_manual_markets():
    assert tuple(m.value for m in Market) == MANUAL_MARKETS


@pytest.mark.parametrize(
    "build",
    [
        lambda m: TransactionCreate(**_txn(market=m)),
        lambda m: TransactionUpdate(market=m),
        lambda m: TransferCreate(
            symbol="700", market=m, quantity=1, transfer_date=date(2026, 9, 1)
        ),
        lambda m: CorporateActionCreate(
            symbol="700",
            market=m,
            action_type="CASH_DIVIDEND",
            ex_date=date(2026, 9, 1),
            total_dividend=1,
        ),
        lambda m: CorporateActionUpdate(market=m),
    ],
)
def test_manual_entries_reject_unknown_markets(build):
    with pytest.raises(ValidationError, match="market 必须是"):
        build("HK")
    with pytest.raises(ValidationError, match="market 必须是"):
        build("场外开基")  # 导入器专用市场不接受手工录入


def test_partial_updates_without_market_still_pass():
    assert TransactionUpdate(notes="x").market is None
    assert CorporateActionUpdate(notes="x").market is None


def test_transfer_symbol_is_normalized():
    transfer = TransferCreate(
        symbol=" 700 ", market="港股", quantity=1, transfer_date=date(2026, 9, 1)
    )
    assert transfer.symbol == "00700"


def test_rule_symbols_are_normalized_except_cmb_business_names():
    exclude = SecurityRuleCreate(rule_type="EXCLUDE", symbol="700", market="港股")
    assert exclude.symbol == "00700"
    name = SecurityRuleCreate(
        rule_type="NAME_OVERRIDE", symbol="aapl", market="美股", payload={"name": "Apple"}
    )
    assert name.symbol == "AAPL"
    relisting = SecurityRuleCreate(
        rule_type="RELISTING",
        symbol="PCT",
        market="新加坡股",
        payload={
            "new_symbol": "700",
            "new_market": "港股",
            "new_currency": "hkd",
            "old_currency": "sgd",
        },
    )
    assert relisting.payload["new_symbol"] == "00700"
    cmb = SecurityRuleCreate(
        rule_type="CMB_CASH_BUSINESS", symbol="银行转存", payload={"event_type": "DEPOSIT"}
    )
    assert cmb.symbol == "银行转存"
