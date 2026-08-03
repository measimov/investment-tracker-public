"""CMB 现金业务入账与存量回填（现金闭环件1）。"""

from datetime import date
from decimal import Decimal


from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.cash_event import CashEvent
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.import_batch import ImportBatch
from app.models.security_rule import SecurityRule
from app.models.transaction import Transaction
from app.services import cmb_fund_flow_importer as importer
from app.services.cmb_fund_flow_importer import (
    import_cmb_fund_flow,
)
from tests.test_cmb_fund_flow_importer import parsed_flow
from tests.helpers import make_account as make_shared_account
from tests.helpers import reset_tables, seed_security_rule


RESET_MODELS = (
    SecurityRule,
    BrokerFundFlow,
    Holding,
    CashEvent,
    CorporateAction,
    Transaction,
    ImportBatch,
    BrokerAccount,
)


# 表驱动后 parse_rows 会按注入的业务映射写 flow.cash_event_type；
# 本文件绕过 parse 直接构造 flow，故用与迁移种子一致的最小映射自行标注
_TEST_BUSINESS_MAP = {
    "银行转存": "DEPOSIT",
    "港股通组合费收取": "FEE",
    "质押回购拆出": "TRANSFER_OUT",
    "拆出质押购回": "TRANSFER_IN",
}


def _cash_flow(row_number, row_hash, business_name, amount, currency="CNY"):
    flow = parsed_flow(
        row_number=row_number, row_hash=row_hash, business_name=business_name,
        trade_date=date(2026, 3, 2), quantity="0", price="0", amount=amount,
    )
    flow.security_code = ""
    flow.currency = currency
    flow.cash_event_type = _TEST_BUSINESS_MAP.get(business_name)
    return flow


def _make_account(db):
    return make_shared_account(
        db, "招商证券", account_name="招商测试账户",
        base_currency="CNY", account_number_masked="****A123", commit=True,
    )


def test_import_books_cash_business_rows(monkeypatch):
    """银行转存→DEPOSIT、组合费→FEE、回购拆出→TRANSFER_OUT：建 CashEvent 并链接。"""
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = _make_account(db)
        flows = [
            _cash_flow(2, "a" * 64, "银行转存", "100000"),
            _cash_flow(3, "b" * 64, "港股通组合费收取", "-0.06"),
            _cash_flow(4, "c" * 64, "质押回购拆出", "-100000"),
            _cash_flow(5, "d" * 64, "拆出质押购回", "100005.5"),
        ]
        monkeypatch.setattr(
            importer, "parse_rows",
            lambda contents, filename, **kwargs: (flows, {"银行转存": 1}, 4, []),
        )
        result = import_cmb_fund_flow(db, 1, b"%PDF", "cmb.pdf", broker_account_id=account.id)

        assert result["imported_cash_events"] == 4
        assert result["eligible_cash_rows"] == 4
        assert result["batch_status"] == "COMPLETED"

        events = {e.event_type: e for e in db.query(CashEvent).all()}
        assert events["DEPOSIT"].amount == Decimal("100000.00000000")
        assert events["FEE"].amount == Decimal("0.06000000")  # 方向由类型承担，金额恒正
        assert events["TRANSFER_OUT"].amount == Decimal("100000.00000000")
        assert events["TRANSFER_IN"].amount == Decimal("100005.50000000")
        flows_db = db.query(BrokerFundFlow).all()
        assert all(f.cash_event_id is not None for f in flows_db)
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_sign_mismatch_row_is_never_booked(monkeypatch):
    """检视意见回归（真实 parser → import 链路）：方向不符的现金业务行
    在 parse 产出错误，且导入侧绝不生成/链接 CashEvent（只归档）。"""
    import pandas as pd

    from tests.test_cmb_fund_flow_importer import pdf_dataframe_row

    dataframe = pd.DataFrame([
        pdf_dataframe_row(
            证券代码="", 证券名称="", 业务名称="银行转存",
            成交价格="0.00", 成交数量="0.00", PDF成交金额="0.00",
            发生金额="-100.00",  # 转存应为流入，却为负
            佣金="0.00", 其他费用="0.00", 股东代码="8888A123",
        ),
        pdf_dataframe_row(
            证券代码="", 证券名称="", 业务名称="银行转存",
            成交价格="0.00", 成交数量="0.00", PDF成交金额="0.00",
            发生金额="200.00", 流水号="OK1",
            佣金="0.00", 其他费用="0.00", 股东代码="8888A123",
        ),
    ])
    monkeypatch.setattr(importer, "read_cmb_fund_flow", lambda contents, filename: dataframe)

    # parse 阶段：产出阻断错误
    rows, counts, total, errors, warnings = importer.parse_rows_with_warnings(
        b"%PDF",
        "statement.pdf",
        cash_business_map=_TEST_BUSINESS_MAP,
    )
    assert any("应为流入但金额为负" in e for e in errors)
    bad = next(f for f in rows if f.amount < 0)
    good = next(f for f in rows if f.amount > 0)
    assert bad.is_cash_business is False  # 入账资格被方向校验否决
    assert good.is_cash_business is True

    # import 阶段（真实入口）：方向不符行只归档，正确行正常入账
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = _make_account(db)
        seed_security_rule(
            db, 1, "CMB_CASH_BUSINESS", "银行转存", payload={"event_type": "DEPOSIT"}
        )
        result = import_cmb_fund_flow(db, 1, b"%PDF", "statement.pdf",
                                      broker_account_id=account.id)
        assert result["imported_cash_events"] == 1
        events = db.query(CashEvent).all()
        assert len(events) == 1
        assert events[0].event_type == "DEPOSIT"
        assert events[0].amount == Decimal("200.00000000")
        flows = {f.amount: f for f in db.query(BrokerFundFlow).all()}
        assert flows[Decimal("-100.00000000")].cash_event_id is None  # 只归档
        assert flows[Decimal("200.00000000")].cash_event_id is not None
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()

