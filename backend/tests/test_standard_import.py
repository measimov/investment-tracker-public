from datetime import date
from decimal import Decimal

import pandas as pd

from app.services.standard_import import (
    import_standard_corporate_actions_dataframe,
    import_standard_transactions_dataframe,
)
from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.transaction import Transaction
from tests.helpers import reset_tables


RESET_MODELS = (BrokerFundFlow, IbkrActivityFlow, Holding, CorporateAction, Transaction)


def standard_df(*rows):
    return pd.DataFrame(rows)


def test_standard_import_recalculates_holdings():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        result = import_standard_transactions_dataframe(
            db,
            1,
            standard_df(
                {
                    "symbol": "600000",
                    "name": "浦发银行",
                    "market": "A股",
                    "transaction_type": "BUY",
                    "quantity": 100,
                    "price": 10,
                    "fee": 1,
                    "transaction_date": "2026-01-01",
                    "currency": "CNY",
                }
            ),
        )

        holding = db.query(Holding).filter_by(user_id=1, symbol="600000", market="A股").one()

        assert result["count"] == 1
        assert db.query(Transaction).count() == 1
        assert holding.quantity == Decimal("100.00000000")
        assert holding.total_cost == Decimal("1001.00000000")
    finally:
        db.close()


def test_standard_import_rolls_back_when_recalculation_fails():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        try:
            import_standard_transactions_dataframe(
                db,
                1,
                standard_df(
                    {
                        "symbol": "600000",
                        "market": "A股",
                        "transaction_type": "SELL",
                        "quantity": 100,
                        "price": 10,
                        "transaction_date": date(2026, 1, 1),
                    }
                ),
            )
        except ValueError:
            pass

        assert db.query(Transaction).count() == 0
        assert db.query(Holding).count() == 0
    finally:
        db.close()


def test_standard_corporate_action_import_recalculates_holdings():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        import_standard_transactions_dataframe(
            db,
            1,
            standard_df(
                {
                    "symbol": "00700",
                    "name": "腾讯控股",
                    "market": "港股",
                    "transaction_type": "BUY",
                    "quantity": 100,
                    "price": 300,
                    "fee": 10,
                    "transaction_date": "2026-01-01",
                    "currency": "HKD",
                }
            ),
        )

        result = import_standard_corporate_actions_dataframe(
            db,
            1,
            standard_df(
                {
                    "symbol": "00700",
                    "name": "腾讯控股",
                    "market": "港股",
                    "action_type": "CASH_DIVIDEND",
                    "ex_date": "2026-01-10",
                    "payment_date": "2026-01-10",
                    "total_dividend": "300",
                    "tax_withheld": "30",
                    "net_dividend": "270",
                    "currency": "HKD",
                },
                {
                    "symbol": "00700",
                    "name": "腾讯控股",
                    "market": "港股",
                    "action_type": "STOCK_SPLIT",
                    "ex_date": "2026-01-15",
                    "split_ratio": "1:2",
                    "currency": "HKD",
                },
            ),
        )

        holding = db.query(Holding).filter_by(user_id=1, symbol="00700", market="港股").one()
        dividend = db.query(CorporateAction).filter_by(
            user_id=1,
            symbol="00700",
            action_type="CASH_DIVIDEND",
        ).one()

        assert result == {
            "message": "Successfully imported 2 corporate actions",
            "count": 2,
            "affected_symbols": 1,
        }
        assert db.query(CorporateAction).count() == 2
        assert holding.quantity == Decimal("200.00000000")
        assert holding.total_cost == Decimal("30010.00000000")
        assert dividend.net_dividend == Decimal("270.00000000")
    finally:
        db.close()


def test_standard_corporate_action_import_rolls_back_on_invalid_row():
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        try:
            import_standard_corporate_actions_dataframe(
                db,
                1,
                standard_df(
                    {
                        "symbol": "00700",
                        "market": "港股",
                        "action_type": "CASH_DIVIDEND",
                        "ex_date": "2026-01-10",
                        "total_dividend": "300",
                        "net_dividend": "300",
                    },
                    {
                        "symbol": "00700",
                        "market": "港股",
                        "action_type": "UNSUPPORTED",
                        "ex_date": "2026-01-11",
                    },
                ),
            )
        except ValueError:
            pass

        assert db.query(CorporateAction).count() == 0
    finally:
        db.close()


def test_standard_import_attributes_broker_account():
    """标准导入带 broker_account_id：交易/公司行动/重算的持仓都落到该账户桶。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    account = BrokerAccount(user_id=1, broker="HSBC", account_name="hsbc-probe")
    db.add(account)
    db.commit()
    db.refresh(account)
    try:
        import_standard_transactions_dataframe(
            db,
            1,
            standard_df(
                {
                    "symbol": "00700",
                    "name": "腾讯控股",
                    "market": "港股",
                    "transaction_type": "BUY",
                    "quantity": 100,
                    "price": 300,
                    "fee": 30,
                    "transaction_date": "2026-01-05",
                    "currency": "HKD",
                }
            ),
            broker_account_id=account.id,
        )
        import_standard_corporate_actions_dataframe(
            db,
            1,
            standard_df(
                {
                    "symbol": "00700",
                    "market": "港股",
                    "action_type": "CASH_DIVIDEND",
                    "ex_date": "2026-02-01",
                    "total_dividend": "160",
                    "net_dividend": "160",
                    "currency": "HKD",
                }
            ),
            broker_account_id=account.id,
        )

        txn = db.query(Transaction).one()
        action = db.query(CorporateAction).one()
        holding = db.query(Holding).filter_by(user_id=1, symbol="00700", market="港股").one()

        assert txn.broker_account_id == account.id
        assert action.broker_account_id == account.id
        assert holding.broker_account_id == account.id
    finally:
        reset_tables(db, RESET_MODELS)
        db.query(BrokerAccount).filter_by(id=account.id).delete()
        db.commit()
        db.close()


def test_standard_import_without_account_lands_in_null_bucket():
    """不带 broker_account_id 时行为不变：落 NULL 账户桶。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        import_standard_transactions_dataframe(
            db,
            1,
            standard_df(
                {
                    "symbol": "600000",
                    "name": "浦发银行",
                    "market": "A股",
                    "transaction_type": "BUY",
                    "quantity": 100,
                    "price": 10,
                    "fee": 1,
                    "transaction_date": "2026-01-01",
                    "currency": "CNY",
                }
            ),
        )
        txn = db.query(Transaction).one()
        holding = db.query(Holding).one()
        assert txn.broker_account_id is None
        assert holding.broker_account_id is None
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


# ---------------------------------------------------------------------------
# issue #130：标准导入此前直接建 ORM 行，绕开 TransactionCreate 的全部约束、
# validate_no_oversell 与时间线锁。一份 CSV 可以写入伪造转仓、负数量、负价格。
# 对照：同文件的公司行动导入早就先过 CorporateActionCreate 了。
# ---------------------------------------------------------------------------

import pytest  # noqa: E402


def _row(**overrides):
    row = {
        "symbol": "600000",
        "name": "浦发银行",
        "market": "A股",
        "transaction_type": "BUY",
        "quantity": 100,
        "price": 10,
        "fee": 1,
        "transaction_date": "2026-01-01",
        "currency": "CNY",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    "overrides,expect",
    [
        # 伪造转仓：转仓只能经 POST /transactions/transfer 成对创建，
        # 单腿写入会让账户桶数量凭空变化
        ({"transaction_type": "TRANSFER_OUT"}, "transaction_type"),
        ({"transaction_type": "TRANSFER_IN"}, "transaction_type"),
        ({"transaction_type": "buy"}, "transaction_type"),  # 大小写也不放过
        ({"quantity": -100}, "quantity"),
        ({"quantity": 0}, "quantity"),
        ({"price": -10}, "price"),
        ({"price": 0}, "price"),
        ({"fee": -1}, "fee"),
        ({"symbol": "X" * 21}, "symbol"),        # max_length=20
        ({"market": "M" * 21}, "market"),        # max_length=20
        ({"currency": "C" * 11}, "currency"),    # max_length=10
    ],
)
def test_standard_import_rejects_invalid_rows(overrides, expect):
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        with pytest.raises(ValueError) as excinfo:
            import_standard_transactions_dataframe(db, 1, standard_df(_row(**overrides)))

        message = str(excinfo.value)
        assert expect in message, f"错误信息应指出违规字段，实际：{message}"
        assert "第 1 行" in message, f"错误信息应带行号，实际：{message}"
        # 整批拒绝，不得留下半条记录
        assert db.query(Transaction).count() == 0
        assert db.query(Holding).count() == 0
    finally:
        db.close()


def test_standard_import_reports_offending_row_number():
    """多行时行号必须指向真正出错的那一行。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        with pytest.raises(ValueError) as excinfo:
            import_standard_transactions_dataframe(
                db, 1,
                standard_df(_row(), _row(), _row(quantity=-5)),
            )

        assert "第 3 行" in str(excinfo.value)
        assert db.query(Transaction).count() == 0
    finally:
        db.close()


def test_standard_import_rejects_oversell_within_the_batch():
    """同一批次内先卖后买（净超卖）必须整批拒绝。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        with pytest.raises(ValueError) as excinfo:
            import_standard_transactions_dataframe(
                db, 1,
                standard_df(
                    _row(transaction_type="BUY", quantity=100, transaction_date="2026-01-01"),
                    _row(transaction_type="SELL", quantity=150, transaction_date="2026-02-01"),
                ),
            )

        assert "超卖" in str(excinfo.value)
        assert db.query(Transaction).count() == 0
    finally:
        db.close()


def test_standard_import_rejects_oversell_against_existing_rows():
    """超卖判定要把库内既有交易算进去，不能只看本批次。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        import_standard_transactions_dataframe(
            db, 1, standard_df(_row(quantity=100, transaction_date="2026-01-01"))
        )

        with pytest.raises(ValueError) as excinfo:
            import_standard_transactions_dataframe(
                db, 1,
                standard_df(_row(transaction_type="SELL", quantity=150,
                                 transaction_date="2026-03-01")),
            )

        assert "超卖" in str(excinfo.value)
        assert db.query(Transaction).count() == 1  # 只剩第一批那条
    finally:
        db.close()


def test_standard_import_allows_valid_sell_within_position():
    """合法的买入后卖出必须照常通过——守住正常路径不被校验误伤。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        result = import_standard_transactions_dataframe(
            db, 1,
            standard_df(
                _row(transaction_type="BUY", quantity=100, transaction_date="2026-01-01"),
                _row(transaction_type="SELL", quantity=60, transaction_date="2026-02-01"),
            ),
        )

        assert result["count"] == 2
        holding = db.query(Holding).filter_by(user_id=1, symbol="600000", market="A股").one()
        assert holding.quantity == Decimal("40.00000000")
    finally:
        db.close()


def test_standard_import_keeps_decimal_precision():
    """数量/价格走 Decimal，不再经 float 中转。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        import_standard_transactions_dataframe(
            db, 1,
            standard_df(_row(quantity="1.00000001", price="8931992295.31575055", fee=0)),
        )

        txn = db.query(Transaction).one()
        assert txn.quantity == Decimal("1.00000001")
        assert txn.price == Decimal("8931992295.31575055")
    finally:
        db.close()


@pytest.mark.parametrize(
    "overrides,field",
    [
        ({"symbol": ""}, "symbol"),
        ({"symbol": "   "}, "symbol"),      # 纯空白经 normalize 后也是空
        ({"market": ""}, "market"),
        ({"market": "  "}, "market"),
        ({"currency": ""}, "currency"),
        ({"currency": " "}, "currency"),
    ],
)
def test_standard_import_rejects_blank_required_fields(overrides, field):
    """空/纯空白的必填字段不得入库（复审 P2）。

    只有 max_length 时它们全部能通过，空标的会让后续持仓重放与统计挂在一个
    无意义的键上。
    """
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        with pytest.raises(ValueError) as excinfo:
            import_standard_transactions_dataframe(db, 1, standard_df(_row(**overrides)))

        message = str(excinfo.value)
        assert field in message, f"应指出是哪个字段，实际：{message}"
        assert "第 1 行" in message
        assert db.query(Transaction).count() == 0
        assert db.query(Holding).count() == 0
    finally:
        db.close()


def test_standard_import_strips_surrounding_whitespace():
    """带首尾空白的合法值应被 strip 后入库，而不是原样保留。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        import_standard_transactions_dataframe(
            db, 1, standard_df(_row(market="  A股  ", currency=" CNY "))
        )

        txn = db.query(Transaction).one()
        assert txn.market == "A股"
        assert txn.currency == "CNY"
    finally:
        db.close()
