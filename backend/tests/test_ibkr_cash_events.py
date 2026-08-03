"""IBKR 现金入账：存款/利息/外汇兑换 → CashEvent，调整只归档（现金闭环件3）。"""

from decimal import Decimal

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models.broker_account import BrokerAccount
from app.models.cash_event import CashEvent
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.import_batch import ImportBatch
from app.models.transaction import Transaction
from app.services import ibkr_activity_importer as importer
from app.services.ibkr_activity_importer import (
    import_ibkr_activity,
    preview_ibkr_activity,
)
from tests.helpers import ibkr_csv
from tests.helpers import make_account as make_shared_account
from tests.helpers import reset_tables


RESET_MODELS = (
    IbkrActivityFlow,
    CashEvent,
    Holding,
    Transaction,
    ImportBatch,
    BrokerAccount,
)


def make_account(db) -> BrokerAccount:
    return make_shared_account(
        db,
        "IBKR",
        account_name="IBKR 测试账户",
        account_number_masked="****7968",
        base_currency="USD",
        commit=True,
    )


CASH_ROWS = (
    # 存款 +50,000 USD
    "Transaction History,Data,2025-01-22,U***67968,电子资金转账,存款,-,-,-,-,"
    "50000.0,-,50000.0",
    # 贷方利息 +5.14（基础货币等值，原币种在说明里）
    "Transaction History,Data,2026-05-05,U***67968,USD 贷方利息- 四月-2026,贷方利息,"
    "-,-,-,-,5.14,-,5.14",
    # 借方利息 -6.56 → FEE
    "Transaction History,Data,2026-04-06,U***67968,USD 借贷费用—— 三月-2026,借方利息,"
    "-,-,-,-,-6.56,-,-6.56",
    # 调整 = FX 折算损益，纸面项：归档不入账
    "Transaction History,Data,2026-05-12,U***67968,FX Translations P&L,调整,-,-,-,-,"
    "-30200.61,-,-30200.61",
    # 外汇兑换：买入 10,000 USD、卖出 78,120.10 HKD，佣金 2 USD
    'Transaction History,Data,2026-02-03,U***67968,"外汇交易基础货币净额: 10,000 '
    'USD.HKD",外汇交易组成部分,USD.HKD,10000.0,7.81201,HKD,-0.5916,-2.0,-0.5916',
)


def import_cash_statement(db, account, filename="ibkr-cash.csv"):
    return import_ibkr_activity(
        db, 1, ibkr_csv(*CASH_ROWS), filename, broker_account_id=account.id
    )


def events_by_type(db):
    result = {}
    for event in db.query(CashEvent).filter(CashEvent.user_id == 1).all():
        result.setdefault(event.event_type, []).append(event)
    return result


def test_import_books_cash_and_fx_rows_as_events(monkeypatch):
    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda s, m: None)
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = make_account(db)
        result = import_cash_statement(db, account)

        assert result["errors"] == []
        # 存款1 + 利息1 + 费用1 + 外汇两腿2 + 外汇佣金1 = 6
        assert result["imported_cash_events"] == 6
        assert result["eligible_cash_event_rows"] == 3
        assert result["eligible_fx_rows"] == 1

        events = events_by_type(db)
        assert [e.amount for e in events["DEPOSIT"]] == [Decimal("50000.0")]
        assert events["DEPOSIT"][0].currency == "USD"
        assert [e.amount for e in events["INTEREST"]] == [Decimal("5.14")]

        # 外汇：基础腿 FX_IN 10,000 USD，对价腿 FX_OUT 78,120.10 HKD，佣金 FEE 2 USD
        fx_in = events["FX_IN"][0]
        fx_out = events["FX_OUT"][0]
        assert (fx_in.amount, fx_in.currency) == (Decimal("10000.0"), "USD")
        assert (fx_out.amount, fx_out.currency) == (Decimal("78120.1"), "HKD")
        fees = {(e.amount, e.currency) for e in events["FEE"]}
        assert fees == {(Decimal("6.56"), "USD"), (Decimal("2.0"), "USD")}

        # 调整行归档但不产生事件
        adjustment = (
            db.query(IbkrActivityFlow)
            .filter(IbkrActivityFlow.activity_type == "调整")
            .one()
        )
        assert adjustment.cash_event_id is None
        assert adjustment.skip_reason == "cash"

        # 链接列齐备：外汇行三链，现金行单链
        fx_flow = (
            db.query(IbkrActivityFlow)
            .filter(IbkrActivityFlow.activity_type == "外汇交易组成部分")
            .one()
        )
        assert fx_flow.cash_event_id == fx_in.id
        assert fx_flow.fx_quote_cash_event_id == fx_out.id
        assert fx_flow.fx_fee_cash_event_id is not None
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_reimport_is_idempotent(monkeypatch):
    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda s, m: None)
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = make_account(db)
        first = import_cash_statement(db, account)
        first_count = db.query(CashEvent).count()

        # 首轮审计口径：4 行可入账（3 现金业务 + 1 外汇），调整行是
        # 设计上有意归档的预期跳过——批次必须 COMPLETED 而非 PARTIAL
        batch1 = db.query(ImportBatch).filter(ImportBatch.id == first["import_batch_id"]).one()
        assert first["booked_source_rows"] == 4
        assert first["expected_archived_rows"] == 1
        assert batch1.status == "COMPLETED"
        assert batch1.imported_count == 4
        assert batch1.duplicate_count == 0
        assert batch1.skipped_count == 0

        result = import_cash_statement(db, account, filename="ibkr-cash-again.csv")

        assert result["imported_cash_events"] == 0
        assert db.query(CashEvent).count() == first_count
        assert (
            db.query(IbkrActivityFlow)
            .filter(IbkrActivityFlow.skip_reason.in_(["cash", "fx"]))
            .count()
            == 5
        )
        # 重导入审计口径：4 行已入账重复，批次仍 COMPLETED
        batch2 = db.query(ImportBatch).filter(ImportBatch.id == result["import_batch_id"]).one()
        assert result["duplicate_rows"] == 4
        assert batch2.status == "COMPLETED"
        assert batch2.imported_count == 0
        assert batch2.duplicate_count == 4
        assert batch2.skipped_count == 0
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_interest_sign_mismatch_archives_without_event(monkeypatch):
    """方向异常（贷方利息为负）不得静默入错账：归档 + warning，不建事件。"""
    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda s, m: None)
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = make_account(db)
        contents = ibkr_csv(
            "Transaction History,Data,2026-05-05,U***67968,USD 贷方利息- 四月-2026,"
            "贷方利息,-,-,-,-,-5.14,-,-5.14",
        )
        result = import_ibkr_activity(
            db, 1, contents, "ibkr-bad-sign.csv", broker_account_id=account.id
        )

        assert result["imported_cash_events"] == 0
        assert db.query(CashEvent).count() == 0
        archived = db.query(IbkrActivityFlow).one()
        assert archived.cash_event_id is None
        assert any("方向" in warning for warning in result.get("warnings", []))
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_imported_ibkr_cash_events_are_read_only_via_api(monkeypatch):
    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda s, m: None)
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = make_account(db)
        import_cash_statement(db, account)
        fx_flow = (
            db.query(IbkrActivityFlow)
            .filter(IbkrActivityFlow.fx_quote_cash_event_id.isnot(None))
            .one()
        )
        quote_event_id = fx_flow.fx_quote_cash_event_id

        from app.core.security import get_password_hash
        from app.models.user import User

        user = db.query(User).filter(User.id == 1).one()
        original_password = user.hashed_password
        user.hashed_password = get_password_hash("ibkr-cash-password")
        db.commit()

        client = TestClient(app)
        login = client.post(
            "/api/auth/login",
            json={"username": user.username, "password": "ibkr-cash-password"},
        )
        assert login.status_code == 200
        csrf = client.cookies.get("investment_csrf")

        listed = client.get("/api/cash-events", params={"limit": 100})
        assert listed.status_code == 200
        by_id = {item["id"]: item for item in listed.json()}
        assert by_id[quote_event_id]["imported"] is True

        deleted = client.delete(
            f"/api/cash-events/{quote_event_id}", headers={"X-CSRF-Token": csrf}
        )
        assert deleted.status_code == 409

        user.hashed_password = original_password
        db.commit()
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_parser_version_tracks_booking_semantics():
    """入账语义检测器：改变了导入产物（入账/归档/判重范围）必须升版并更新
    此断言——v5 现金入账、v6 排除规则表驱动。"""
    assert importer.PARSER_VERSION == "6"


def test_fx_price_currency_mismatch_archives_without_events(monkeypatch):
    """货币对代码与 Price Currency 列不一致（列漂移）：宁可归档报警，
    绝不把现金记进错误币种——断言零 CashEvent。"""
    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda s, m: None)
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = make_account(db)
        contents = ibkr_csv(
            'Transaction History,Data,2026-02-03,U***67968,"外汇交易基础货币净额: 10,000 '
            'USD.HKD",外汇交易组成部分,USD.HKD,10000.0,7.81201,JPY,-0.59,-2.0,-0.59',
        )
        result = import_ibkr_activity(
            db, 1, contents, "ibkr-fx-drift.csv", broker_account_id=account.id
        )

        assert result["imported_cash_events"] == 0
        assert result["eligible_fx_rows"] == 0
        assert db.query(CashEvent).count() == 0
        archived = db.query(IbkrActivityFlow).one()
        assert archived.cash_event_id is None
        assert archived.fx_quote_cash_event_id is None
        assert any("不一致" in warning for warning in result.get("warnings", []))
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_preview_after_import_reports_cash_rows_as_duplicates(monkeypatch):
    """预览与正式导入共用归档判重：首导后重预览必须把已入账的
    现金/外汇行报为 duplicate，而不是再次显示为待导入。"""
    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda s, m: None)
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = make_account(db)
        import_cash_statement(db, account)

        preview = preview_ibkr_activity(
            db, 1, ibkr_csv(*CASH_ROWS), "ibkr-cash.csv", broker_account_id=account.id
        )

        assert preview["duplicate_rows"] == 4
        assert preview["booked_source_rows"] == 4
        assert preview["import_samples"] == []
        assert len(preview["duplicate_samples"]) == 4
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_preview_warns_on_fx_currency_drift_without_booking(monkeypatch):
    """币种冲突在预览阶段就要报警且不计入 booked，与正式导入同口径。"""
    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda s, m: None)
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        account = make_account(db)
        contents = ibkr_csv(
            'Transaction History,Data,2026-02-03,U***67968,"外汇交易基础货币净额: 10,000 '
            'USD.HKD",外汇交易组成部分,USD.HKD,10000.0,7.81201,JPY,-0.59,-2.0,-0.59',
        )

        preview = preview_ibkr_activity(
            db, 1, contents, "ibkr-fx-drift.csv", broker_account_id=account.id
        )

        assert any("不一致" in warning for warning in preview.get("warnings", []))
        assert preview["booked_source_rows"] == 0
        assert preview["eligible_fx_rows"] == 0
        assert db.query(CashEvent).count() == 0
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()


def test_preview_blocks_cash_rows_archived_under_other_account(monkeypatch):
    """他账户已归档的现金行：预览与导入同样阻断，不静默跳过。"""
    import pytest

    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda s, m: None)
    db = SessionLocal()
    reset_tables(db, RESET_MODELS)
    try:
        first = make_account(db)
        import_cash_statement(db, first)
        other = BrokerAccount(
            user_id=1,
            broker="IBKR",
            account_name="IBKR 第二账户",
            account_number_masked="****7968",
            base_currency="USD",
        )
        db.add(other)
        db.commit()
        db.refresh(other)

        # 用行子集构造不同的文件内容，绕过文件级判重，命中行级归档判重
        with pytest.raises(ValueError, match="已归属其他券商账户"):
            preview_ibkr_activity(
                db,
                1,
                ibkr_csv(*CASH_ROWS[:2]),
                "ibkr-cash-subset.csv",
                broker_account_id=other.id,
            )
    finally:
        reset_tables(db, RESET_MODELS)
        db.close()
