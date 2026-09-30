"""IBKR 疑似重复守卫：row_hash 之外的第二层，**不改任何 hash 输入**。

生产场景（2026-09-28）：IBKR 账户历史由三种形态导入——Transaction History CSV
（2024-10-09..2026-05-12，成交+股息+预扣税）、trade_history.xlsx（2026-05-13..07-02，
仅成交，Trade ID 并进说明 → 与 CSV 同笔成交 row_hash 不同）、只含现金的 CSV；07-02
之后的成交手工录入；5 笔港股股息刚从分红公告建议入账（HKD、真实除净日/派息日），
而 IBKR 报表按 USD 在到账日记股息 + 另一行外国预扣税。补导 05-13 至今的新 CSV 时，
这些都不得双份入账。
"""

import io
from datetime import date
from decimal import Decimal

import pytest

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.corporate_action import CorporateAction
from app.models.holding import Holding
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.import_batch import ImportBatch
from app.models.transaction import Transaction
from app.services import ibkr_activity_importer as importer
from app.services.ibkr_activity_importer import import_ibkr_activity, preview_ibkr_activity
from tests.helpers import ibkr_csv, reset_tables


RESET_MODELS = (
    BrokerFundFlow,
    IbkrActivityFlow,
    Holding,
    CorporateAction,
    Transaction,
    ImportBatch,
    BrokerAccount,
)

ACCOUNT = "U***00001"
# 01024 买 200 @ 41.36 HKD（金额列为基础货币 USD 等值）
CSV_KUAISHOU_BUY = (
    f"Transaction History,Data,2026-06-30,{ACCOUNT},KUAISHOU TECHNOLOGY,买,1024,"
    "200,41.36,HKD,-1055.10,-2.30,-1057.40"
)
CSV_NVDA_BUY = (
    f"Transaction History,Data,2026-06-30,{ACCOUNT},NVIDIA CORP,买,NVDA,"
    "10,120.5,USD,-1205.00,-1.00,-1206.00"
)
CSV_CNOOC_DIVIDEND = (
    f"Transaction History,Data,2026-07-03,{ACCOUNT},"
    "883(CNE1000002L3) 现金红利 HKD 0.75 每股 (普通股息),股息,883,-,-,-,95.50,0,95.50"
)
CSV_CNOOC_TAX = (
    f"Transaction History,Data,2026-07-03,{ACCOUNT},"
    "883(CNE1000002L3) 现金红利 HKD 0.75 每股 - CN 税,外国预扣税,883,-,-,-,-9.55,0,-9.55"
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(importer, "lookup_tushare_security_name", lambda symbol, market: None)
    monkeypatch.setattr(importer, "_resolved_name_cache", {})


@pytest.fixture
def db():
    session = SessionLocal()
    reset_tables(session, RESET_MODELS)
    try:
        yield session
    finally:
        reset_tables(session, RESET_MODELS)
        session.close()


@pytest.fixture
def account(db):
    row = BrokerAccount(
        user_id=1,
        broker="IBKR",
        account_name="IBKR 疑似重复账户",
        account_number_masked="****0001",
        base_currency="USD",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def ibkr_xlsx(*rows: dict) -> bytes:
    import pandas as pd

    defaults = {
        "Date (HKT)": "2026-06-30 14:31",
        "Symbol": "1024",
        "Name": "KUAISHOU TECHNOLOGY",
        "Type": "STK",
        "Ccy": "HKD",
        "Side": "BUY",
        "Qty": 200,
        "Price": 41.36,
        "Net Amount": 8272.0,
        "Commission": 18.0,
        "Trade ID": "t-0001",
    }
    frame = pd.DataFrame([{**defaults, **row} for row in rows])
    buffer = io.BytesIO()
    frame.to_excel(buffer, sheet_name="All Trades", index=False)
    return buffer.getvalue()


def _import(db, account, contents, filename="ibkr.csv", **kwargs):
    return import_ibkr_activity(db, 1, contents, filename, broker_account_id=account.id, **kwargs)


def _preview(db, account, contents, filename="ibkr.csv", **kwargs):
    return preview_ibkr_activity(db, 1, contents, filename, broker_account_id=account.id, **kwargs)


def _manual_trade(
    db,
    account,
    *,
    symbol="01024",
    market="港股",
    price="41.36",
    quantity="200",
    transaction_type="BUY",
    day=date(2026, 6, 30),
    currency="HKD",
    broker_account_id="same",
):
    txn = Transaction(
        user_id=1,
        broker_account_id=account.id if broker_account_id == "same" else broker_account_id,
        symbol=symbol,
        name="手工",
        market=market,
        transaction_type=transaction_type,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fee=Decimal("0"),
        transaction_date=day,
        currency=currency,
        notes="手工录入",
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)
    return txn


def _suggestion_dividend(
    db, account, *, ex_date=date(2026, 6, 10), payment_date=date(2026, 7, 3), symbol="00883"
):
    action = CorporateAction(
        user_id=1,
        broker_account_id=account.id,
        symbol=symbol,
        name="中国海洋石油",
        market="港股",
        action_type="CASH_DIVIDEND",
        ex_date=ex_date,
        payment_date=payment_date,
        dividend_per_share=Decimal("0.75"),
        total_dividend=Decimal("750"),
        tax_withheld=Decimal("0"),
        net_dividend=Decimal("750"),
        currency="HKD",
        notes="来自分红公告建议 #12（hkexnews-dividend）",
    )
    db.add(action)
    db.commit()
    db.refresh(action)
    return action


# --------------------------------------------------------------------------- 成交


def test_xlsx_imported_trade_then_csv_same_trade_is_held(db, account):
    """xlsx 把 Trade ID 并进说明 → CSV 同笔成交 hash 不同；二级键命中即扣住。"""
    first = _import(db, account, ibkr_xlsx({"Trade ID": "t-1"}), "trade_history.xlsx")
    assert first["imported_transactions"] == 1
    xlsx_txn = db.query(Transaction).one()

    contents = ibkr_csv(CSV_KUAISHOU_BUY)
    preview = _preview(db, account, contents)
    assert preview["errors"] == []
    assert preview["suspected_duplicate_rows"] == 1
    sample = preview["suspected_duplicate_samples"][0]
    assert sample["match_kind"] == "transaction"
    assert sample["existing_id"] == xlsx_txn.id
    assert sample["existing_date"] == "2026-06-30"
    assert "trade_history.xlsx" in sample["existing_source"]
    assert sample["existing_source_filename"] == "trade_history.xlsx"
    assert sample["previously_held"] is False
    assert "同日同向同数量" in sample["reason"]

    result = _import(db, account, contents)
    assert result["suspected_duplicate_rows"] == 1
    assert result["imported_transactions"] == 0
    assert result["duplicate_rows"] == 0
    assert result["batch_status"] == "PARTIAL"
    assert db.query(Transaction).count() == 1, "同笔成交不得双份入账"
    held = db.query(IbkrActivityFlow).filter_by(skip_reason="suspected_duplicate").one()
    assert held.transaction_id is None and held.corporate_action_id is None
    assert f"transaction id={xlsx_txn.id}" in held.notes
    assert "manual confirmation required" in held.notes
    batch = db.get(ImportBatch, result["import_batch_id"])
    assert "疑似" in batch.error_message and batch.skipped_count == 1
    # 预览与导入同一结论
    assert [s["row_hash"] for s in result["suspected_duplicate_samples"]] == [sample["row_hash"]]


def test_manual_transaction_then_csv_is_held(db, account):
    """手工录入（无任何流水链接）也是已入账事实；价格精度不同（41.4 vs 41.36）仍命中。"""
    manual = _manual_trade(db, account, price="41.4")
    result = _import(db, account, ibkr_csv(CSV_KUAISHOU_BUY))

    assert result["suspected_duplicate_rows"] == 1
    assert result["imported_transactions"] == 0
    sample = result["suspected_duplicate_samples"][0]
    assert sample["existing_id"] == manual.id
    assert sample["existing_source"] == "手工录入"
    assert sample["existing_price"] == "41.4" and sample["price"] == "41.36"
    assert db.query(Transaction).count() == 1


def test_manual_transaction_in_unassigned_bucket_is_held(db, account):
    """未指定账户（NULL）的手工交易同样计入——与分红判重的账户口径一致。"""
    _manual_trade(db, account, broker_account_id=None)
    result = _import(db, account, ibkr_csv(CSV_KUAISHOU_BUY))
    assert result["suspected_duplicate_rows"] == 1
    assert db.query(Transaction).count() == 1


def test_us_trade_one_day_apart_is_held_but_hk_is_not(db, account):
    """xlsx 日期是香港时间：美股夜盘成交会落到次日，±1 天内仍视为同一笔；港股要求同日。"""
    _manual_trade(
        db,
        account,
        symbol="NVDA",
        market="美股",
        price="120.5",
        quantity="10",
        day=date(2026, 7, 1),
        currency="USD",
    )
    _manual_trade(db, account, day=date(2026, 7, 1))
    result = _import(db, account, ibkr_csv(CSV_NVDA_BUY, CSV_KUAISHOU_BUY))

    assert result["suspected_duplicate_rows"] == 1
    sample = result["suspected_duplicate_samples"][0]
    assert sample["symbol"] == "NVDA" and sample["existing_date"] == "2026-07-01"
    assert "日期相邻" in sample["reason"]
    assert result["imported_transactions"] == 1  # 港股 06-30 vs 07-01：不同日，照常入账


def test_two_genuine_same_day_fills_with_one_existing_holds_only_one(db, account):
    """计数语义：库里 1 笔、本文件同键 2 笔 → 扣 1 笔、入 1 笔。"""
    _manual_trade(db, account)
    result = _import(db, account, ibkr_csv(CSV_KUAISHOU_BUY, CSV_KUAISHOU_BUY))

    assert result["suspected_duplicate_rows"] == 1
    assert result["imported_transactions"] == 1
    assert db.query(Transaction).count() == 2
    assert db.query(IbkrActivityFlow).filter_by(skip_reason="suspected_duplicate").count() == 1


def test_same_day_same_quantity_far_price_is_not_held_but_warned(db, account):
    """同日同量但价格差超过 1%：不是同一笔成交，照常入账；只提示人工核对。"""
    _manual_trade(db, account, price="45")
    result = _import(db, account, ibkr_csv(CSV_KUAISHOU_BUY))

    assert result["suspected_duplicate_rows"] == 0
    assert result["imported_transactions"] == 1
    assert any("同日同向" in warning for warning in result["warnings"])


def test_closest_price_pairs_first(db, account):
    """同键两笔既有、一笔新行：与价格最接近的那笔配对（样本里可见）。"""
    _manual_trade(db, account, price="41.60")
    near = _manual_trade(db, account, price="41.37")
    result = _import(db, account, ibkr_csv(CSV_KUAISHOU_BUY))
    assert result["suspected_duplicate_samples"][0]["existing_id"] == near.id


def test_hash_duplicate_does_not_consume_budget(db, account):
    """同一份 CSV 重导：hash 命中的行是普通重复，它链接的交易不再拿来扣住别的行。"""
    contents = ibkr_csv(CSV_KUAISHOU_BUY)
    first = _import(db, account, contents)
    assert first["imported_transactions"] == 1

    second = _import(db, account, contents)
    assert second["duplicate_rows"] == 1
    assert second["suspected_duplicate_rows"] == 0
    assert second["batch_status"] == "COMPLETED"


# --------------------------------------------------------------------------- 股息


def test_dividend_booked_from_suggestion_holds_csv_dividend_and_tax(db, account):
    """分红公告建议入账（HKD、除净日 06-10、派息日 07-03）后，IBKR USD 股息与预扣税都扣住。"""
    action = _suggestion_dividend(db, account)
    contents = ibkr_csv(CSV_CNOOC_DIVIDEND, CSV_CNOOC_TAX)

    preview = _preview(db, account, contents)
    # 税行随股息扣住：预览不得出现「找不到唯一股息」的阻断错误
    assert preview["errors"] == []
    assert preview["suspected_duplicate_rows"] == 2
    kinds = {s["transaction_type"]: s for s in preview["suspected_duplicate_samples"]}
    assert set(kinds) == {"CASH_DIVIDEND", "DIVIDEND_TAX"}
    dividend_sample = kinds["CASH_DIVIDEND"]
    assert dividend_sample["match_kind"] == "corporate_action"
    assert dividend_sample["existing_id"] == action.id
    assert dividend_sample["existing_currency"] == "HKD"
    assert dividend_sample["currency"] == "USD"
    assert dividend_sample["existing_date"] == "2026-07-03"
    assert dividend_sample["existing_source"].startswith("来自分红公告建议 #12")
    assert "随同日疑似重复股息" in kinds["DIVIDEND_TAX"]["reason"]

    result = _import(db, account, contents)
    assert result["errors"] == []
    assert result["suspected_duplicate_rows"] == 2
    assert result["imported_corporate_actions"] == 0
    assert result["imported_tax_adjustments"] == 0
    assert result["batch_status"] == "PARTIAL"
    assert db.query(CorporateAction).count() == 1
    db.refresh(action)
    assert action.tax_withheld == Decimal("0"), "被扣住的税行不得记到建议入账的股息上"
    held = db.query(IbkrActivityFlow).filter_by(skip_reason="suspected_duplicate").all()
    assert len(held) == 2
    assert all(row.corporate_action_id is None for row in held)


def test_standalone_tax_near_suggestion_dividend_is_held(db, account):
    """本文件只有预扣税行（股息在上一份导出里没有）：日期窗口命中已入账股息 → 扣住，不报错。"""
    _suggestion_dividend(db, account)
    result = _import(db, account, ibkr_csv(CSV_CNOOC_TAX))
    assert result["errors"] == []
    assert result["suspected_duplicate_rows"] == 1
    assert "预扣税对应的股息疑似已入账" in result["suspected_duplicate_samples"][0]["reason"]


def test_dividend_outside_window_is_booked(db, account):
    """同标的但派息日相隔一年：不是同一笔股息，照常入账（税行归属到新股息）。"""
    _suggestion_dividend(db, account, ex_date=date(2025, 6, 10), payment_date=date(2025, 7, 3))
    result = _import(db, account, ibkr_csv(CSV_CNOOC_DIVIDEND, CSV_CNOOC_TAX))
    assert result["errors"] == []
    assert result["suspected_duplicate_rows"] == 0
    assert result["imported_corporate_actions"] == 1
    assert result["imported_tax_adjustments"] == 1


def test_ibkr_linked_dividend_uses_tight_window(db, account):
    """已由 IBKR 导入的股息按到账日记账：一个月前的那笔（月度派息）不能顶替本月这笔。"""
    previous_month = ibkr_csv(
        f"Transaction History,Data,2026-06-05,{ACCOUNT},"
        "883(CNE1000002L3) 现金红利 HKD 0.70 每股 (普通股息),股息,883,-,-,-,90.00,0,90.00"
    )
    assert _import(db, account, previous_month)["imported_corporate_actions"] == 1

    result = _import(db, account, ibkr_csv(CSV_CNOOC_DIVIDEND))
    assert result["suspected_duplicate_rows"] == 0
    assert result["imported_corporate_actions"] == 1


# --------------------------------------------------------------------------- 确认与重导


def test_reimport_counts_archived_suspected_rows_as_duplicates(db, account):
    """#189 同款：已归档的疑似行重导按重复计，不再插第二条同 hash 行，也不报孤儿记录。"""
    _manual_trade(db, account)
    _suggestion_dividend(db, account)
    contents = ibkr_csv(CSV_KUAISHOU_BUY, CSV_CNOOC_DIVIDEND, CSV_CNOOC_TAX)
    first = _import(db, account, contents)
    assert first["suspected_duplicate_rows"] == 3

    preview = _preview(db, account, contents)
    assert preview["errors"] == []
    assert preview["suspected_duplicate_rows"] == 0
    assert preview["duplicate_rows"] == 3
    assert {s["previously_held"] for s in preview["suspected_duplicate_samples"]} == {True}
    assert len(preview["suspected_duplicate_samples"]) == 3
    assert all(
        "suspected duplicate of" in s["reason"] for s in preview["suspected_duplicate_samples"]
    )

    second = _import(db, account, contents)
    assert second["duplicate_rows"] == 3
    assert second["suspected_duplicate_rows"] == 0
    assert second["imported_transactions"] == 0
    assert second["batch_status"] == "COMPLETED"
    assert db.query(IbkrActivityFlow).count() == 3
    assert db.query(Transaction).count() == 1
    assert db.query(CorporateAction).count() == 1


def test_confirmed_trade_is_booked_in_place(db, account):
    """用户确认为真实的另一笔成交：带 confirm 清单重导，在原归档行上转正。"""
    _manual_trade(db, account)
    contents = ibkr_csv(CSV_KUAISHOU_BUY)
    first = _import(db, account, contents)
    row_hash = first["suspected_duplicate_samples"][0]["row_hash"]
    held = db.query(IbkrActivityFlow).filter_by(row_hash=row_hash).one()
    held_id = held.id

    preview = _preview(db, account, contents, confirmed_row_hashes=frozenset({row_hash}))
    assert preview["suspected_duplicate_rows"] == 0
    assert preview["suspected_duplicate_samples"] == []
    assert preview["duplicate_rows"] == 0

    result = _import(db, account, contents, confirmed_row_hashes=frozenset({row_hash}))
    assert result["imported_transactions"] == 1
    assert result["batch_status"] == "COMPLETED"
    assert db.query(Transaction).count() == 2
    booked = db.query(IbkrActivityFlow).filter_by(row_hash=row_hash).one()
    assert booked.id == held_id, "确认转正必须在原归档行上进行"
    assert booked.skip_reason is None and booked.transaction_id is not None
    assert "confirmed as a distinct trade" in booked.notes

    # 再导一次（不带确认）：已入账的普通重复
    again = _import(db, account, contents)
    assert again["duplicate_rows"] == 1 and again["imported_transactions"] == 0


def test_confirmed_dividend_books_with_its_tax(db, account):
    """确认股息 → 新建 USD 股息，同日预扣税一并在原归档行上转正（税行不必逐条勾选）。"""
    suggestion = _suggestion_dividend(db, account)
    contents = ibkr_csv(CSV_CNOOC_DIVIDEND, CSV_CNOOC_TAX)
    first = _import(db, account, contents)
    dividend_hash = next(
        s["row_hash"]
        for s in first["suspected_duplicate_samples"]
        if s["transaction_type"] == "CASH_DIVIDEND"
    )
    held_ids = {row.row_hash: row.id for row in db.query(IbkrActivityFlow).all()}

    result = _import(db, account, contents, confirmed_row_hashes=frozenset({dividend_hash}))
    assert result["errors"] == []
    assert result["imported_corporate_actions"] == 1
    assert result["imported_tax_adjustments"] == 1
    assert result["batch_status"] == "COMPLETED"

    new_action = db.query(CorporateAction).filter(CorporateAction.id != suggestion.id).one()
    assert new_action.currency == "USD"
    assert new_action.total_dividend == Decimal("95.50")
    assert new_action.tax_withheld == Decimal("9.55")
    rows = db.query(IbkrActivityFlow).all()
    assert {row.row_hash: row.id for row in rows} == held_ids, "不得插新行"
    assert all(row.corporate_action_id == new_action.id for row in rows)
    assert all(row.skip_reason is None for row in rows)
    db.refresh(suggestion)
    assert suggestion.tax_withheld == Decimal("0")

    # 再导一次（不带确认）：转正后的股息+税是普通 hash 重复，校验链接一致、不报孤儿
    again = _import(db, account, contents)
    assert again["errors"] == []
    assert again["duplicate_rows"] == 2 and again["suspected_duplicate_rows"] == 0
    db.refresh(new_action)
    assert new_action.tax_withheld == Decimal("9.55")


def test_confirm_list_must_belong_to_file(db, account):
    with pytest.raises(ValueError, match="不存在的流水"):
        _preview(
            db, account, ibkr_csv(CSV_KUAISHOU_BUY), confirmed_row_hashes=frozenset({"0" * 64})
        )


def test_unrelated_rows_are_unaffected(db, account):
    """守卫只作用于命中的行：其他标的成交/股息照常入账，且与守卫前结果一致。"""
    _manual_trade(db, account)
    _suggestion_dividend(db, account)
    contents = ibkr_csv(
        CSV_KUAISHOU_BUY,
        CSV_NVDA_BUY,
        f"Transaction History,Data,2026-07-10,{ACCOUNT},"
        "AAPL(US0378331005) 现金红利 USD 0.26 每股 (普通股息),股息,AAPL,-,-,-,2.60,0,2.60",
    )
    result = _import(db, account, contents)
    assert result["suspected_duplicate_rows"] == 1
    assert result["imported_transactions"] == 1
    assert result["imported_corporate_actions"] == 1
    assert {txn.symbol for txn in db.query(Transaction).all()} == {"01024", "NVDA"}
    assert {a.symbol for a in db.query(CorporateAction).all()} == {"00883", "AAPL"}


# --------------------------------------------------------------------------- PR #253 评审回归

CSV_CNOOC_SPECIAL = (
    f"Transaction History,Data,2026-07-03,{ACCOUNT},"
    "883(CNE1000002L3) 现金红利 HKD 0.15 每股 (特别股息),股息,883,-,-,-,19.10,0,19.10"
)
CSV_CNOOC_SPECIAL_TAX = (
    f"Transaction History,Data,2026-07-03,{ACCOUNT},"
    "883(CNE1000002L3) 现金红利 HKD 0.15 每股 - CN 税,外国预扣税,883,-,-,-,-1.91,0,-1.91"
)


def test_unattributed_tax_reimported_after_suggestion_dividend_reuses_archived_row(db, account):
    """P2：未归属税行 → 后补跨币种公告股息 → 重导：原地改为疑似，不插第二条同 hash 行。"""
    # 账本里还没有可归属的股息：税行报出并归档为「未归属税行」（评审复现的起点）
    _import(db, account, ibkr_csv(CSV_CNOOC_TAX))
    (archived,) = db.query(IbkrActivityFlow).all()
    assert archived.skip_reason == "unattributed_tax"

    _suggestion_dividend(db, account)
    contents = ibkr_csv(CSV_CNOOC_TAX)
    preview = _preview(db, account, contents)
    assert preview["errors"] == [] and preview["suspected_duplicate_rows"] == 1

    result = _import(db, account, contents)
    assert result["errors"] == []
    assert result["suspected_duplicate_rows"] == 1
    rows = db.query(IbkrActivityFlow).all()
    assert len(rows) == 1 and rows[0].id == archived.id
    assert rows[0].skip_reason == "suspected_duplicate"
    assert rows[0].corporate_action_id is None


def test_mixed_same_day_dividends_pair_taxes_by_per_share_description(db, account):
    """P1：同日两笔股息一笔被扣、一笔入账 → 税按「币种 每股金额」配对，不把被扣那笔的税
    累加到入账的那笔上（评审复现：应扣 1.91，此前扣了 11.46）。"""
    action = _suggestion_dividend(db, account)
    contents = ibkr_csv(CSV_CNOOC_DIVIDEND, CSV_CNOOC_TAX, CSV_CNOOC_SPECIAL, CSV_CNOOC_SPECIAL_TAX)
    result = _import(db, account, contents)
    assert result["errors"] == []
    held = sorted(
        (row.activity_type, "HKD 0.75 每股" in row.description)
        for row in db.query(IbkrActivityFlow).filter_by(skip_reason="suspected_duplicate")
    )
    assert held == [("外国预扣税", True), ("股息", True)]
    booked = db.query(CorporateAction).filter(CorporateAction.id != action.id).one()
    assert (booked.total_dividend, booked.tax_withheld, booked.net_dividend) == (
        Decimal("19.10"),
        Decimal("1.91"),
        Decimal("17.19"),
    )
    db.refresh(action)
    assert action.tax_withheld == Decimal("0")


def test_mixed_same_day_unpairable_tax_is_held_not_misattributed(db, account):
    """描述里没有每股金额、配不上：混合日的税行一律扣住待确认，入账的股息不带错税。"""
    action = _suggestion_dividend(db, account)
    strip = lambda row: row.replace(" 每股", "")  # noqa: E731
    contents = ibkr_csv(
        CSV_CNOOC_DIVIDEND, strip(CSV_CNOOC_TAX), CSV_CNOOC_SPECIAL, strip(CSV_CNOOC_SPECIAL_TAX)
    )
    result = _import(db, account, contents)
    assert result["errors"] == []
    reasons = [s["reason"] for s in result["suspected_duplicate_samples"]]
    assert sum("无法从描述确定" in r for r in reasons) == 2
    booked = db.query(CorporateAction).filter(CorporateAction.id != action.id).one()
    assert booked.total_dividend == Decimal("19.10") and booked.tax_withheld == Decimal("0")


def test_suspected_sample_amount_uses_base_currency(db, account):
    """P2：港股成交价是 HKD，发生金额是基础货币 USD——样本不得把 -1055.10 标成 HKD。"""
    _import(db, account, ibkr_xlsx({"Trade ID": "t-1"}), "trade_history.xlsx")
    preview = _preview(db, account, ibkr_csv(CSV_KUAISHOU_BUY))
    (sample,) = preview["suspected_duplicate_samples"]
    assert (sample["amount"], sample["currency"]) == ("-1055.10", "USD")
    assert (sample["price"], sample["price_currency"]) == ("41.36", "HKD")


def test_taxes_imported_later_follow_previously_held_dividend(db, account):
    """PR #253 复审 P1：先导两笔同日股息（普通被扣、特别入账），再导含两条税的完整文件。
    上次扣住未确认的普通股息已排出匹配池，税行仍须按它「未入账」处理：普通股息的税扣住，
    特别股息只记自己的 1.91（此前两条都记到特别股息上：11.46 / 7.64）。"""
    action = _suggestion_dividend(db, account)
    first = _import(db, account, ibkr_csv(CSV_CNOOC_DIVIDEND, CSV_CNOOC_SPECIAL))
    assert first["errors"] == [] and first["suspected_duplicate_rows"] == 1
    special = db.query(CorporateAction).filter(CorporateAction.id != action.id).one()
    assert special.total_dividend == Decimal("19.10") and special.tax_withheld == Decimal("0")

    contents = ibkr_csv(CSV_CNOOC_DIVIDEND, CSV_CNOOC_TAX, CSV_CNOOC_SPECIAL, CSV_CNOOC_SPECIAL_TAX)
    preview = _preview(db, account, contents)
    assert preview["errors"] == []
    second = _import(db, account, contents)
    assert second["errors"] == []
    db.refresh(special)
    assert (special.tax_withheld, special.net_dividend) == (Decimal("1.91"), Decimal("17.19"))
    held_tax = (
        db.query(IbkrActivityFlow)
        .filter_by(skip_reason="suspected_duplicate", activity_type="外国预扣税")
        .one()
    )
    assert "HKD 0.75 每股" in held_tax.description
    assert "上次导入已归档为疑似" in held_tax.notes
    db.refresh(action)
    assert action.tax_withheld == Decimal("0")

    # 之后确认普通股息：它连同上次扣住的税一起转正，税不会再落到特别股息上
    held_dividend = (
        db.query(IbkrActivityFlow)
        .filter_by(skip_reason="suspected_duplicate", activity_type="股息")
        .one()
    )
    third = _import(db, account, contents, confirmed_row_hashes=frozenset({held_dividend.row_hash}))
    assert third["errors"] == []
    db.refresh(special)
    assert special.tax_withheld == Decimal("1.91")
    regular = (
        db.query(CorporateAction).filter(CorporateAction.id.notin_([action.id, special.id])).one()
    )
    assert (regular.total_dividend, regular.tax_withheld) == (Decimal("95.50"), Decimal("9.55"))


def test_same_day_dividends_each_get_their_own_tax_on_fresh_import(db, account):
    """同日两笔股息各带预扣税（无任何既有记录）：按每股描述各归各的，不再整条报「找不到唯一股息」。"""
    contents = ibkr_csv(CSV_CNOOC_DIVIDEND, CSV_CNOOC_TAX, CSV_CNOOC_SPECIAL, CSV_CNOOC_SPECIAL_TAX)
    assert _preview(db, account, contents)["errors"] == []
    result = _import(db, account, contents)
    assert result["errors"] == [] and result["imported_corporate_actions"] == 2
    booked = sorted(
        (a.total_dividend, a.tax_withheld, a.net_dividend) for a in db.query(CorporateAction)
    )
    assert booked == [
        (Decimal("19.10"), Decimal("1.91"), Decimal("17.19")),
        (Decimal("95.50"), Decimal("9.55"), Decimal("85.95")),
    ]


def test_tax_only_followup_file_respects_archived_held_dividend(db, account):
    """PR #253 复审 P1：后续文件不再包含股息行、只有两条税。库里已扣留、未确认的普通股息
    仍须参与配对——普通股息的税扣住，特别股息只记 1.91（此前 11.46 / 7.64、疑似数 0）。"""
    action = _suggestion_dividend(db, account)
    first = _import(db, account, ibkr_csv(CSV_CNOOC_DIVIDEND, CSV_CNOOC_SPECIAL))
    assert first["errors"] == [] and first["suspected_duplicate_rows"] == 1
    special = db.query(CorporateAction).filter(CorporateAction.id != action.id).one()

    taxes_only = ibkr_csv(CSV_CNOOC_TAX, CSV_CNOOC_SPECIAL_TAX)
    preview = _preview(db, account, taxes_only)
    assert preview["errors"] == [] and preview["suspected_duplicate_rows"] == 1
    result = _import(db, account, taxes_only)
    assert result["errors"] == []
    assert result["suspected_duplicate_rows"] == 1
    db.refresh(special)
    assert (special.tax_withheld, special.net_dividend) == (Decimal("1.91"), Decimal("17.19"))
    held_tax = (
        db.query(IbkrActivityFlow)
        .filter_by(skip_reason="suspected_duplicate", activity_type="外国预扣税")
        .one()
    )
    assert "HKD 0.75 每股" in held_tax.description
    db.refresh(action)
    assert action.tax_withheld == Decimal("0")


def test_tax_only_followup_unpairable_is_held_not_attributed_to_the_only_booked(db, account):
    """同上但税行描述没有每股金额：库里有扣留股息又配不上 → 扣住待确认，
    不因「唯一可入账候选恰好是特别股息」就归给它。"""
    _suggestion_dividend(db, account)
    _import(db, account, ibkr_csv(CSV_CNOOC_DIVIDEND, CSV_CNOOC_SPECIAL))
    strip = lambda row: row.replace(" 每股", "")  # noqa: E731
    result = _import(db, account, ibkr_csv(strip(CSV_CNOOC_TAX)))
    assert result["errors"] == [] and result["suspected_duplicate_rows"] == 1
    assert "无法从描述确定" in result["suspected_duplicate_samples"][0]["reason"]
    assert all(a.tax_withheld == Decimal("0") for a in db.query(CorporateAction))


def test_confirmed_tax_is_not_attributed_to_a_dividend_with_other_per_share(db, account):
    """人工确认一条税行、而它的股息仍被扣留：唯一的入账候选是另一笔（每股 0.15）股息，
    描述不符 → 不归属，原归档行改记未归属税行并报出。"""
    _suggestion_dividend(db, account)
    _import(db, account, ibkr_csv(CSV_CNOOC_DIVIDEND, CSV_CNOOC_SPECIAL))
    taxes_only = ibkr_csv(CSV_CNOOC_TAX)
    _import(db, account, taxes_only)
    held_tax = (
        db.query(IbkrActivityFlow)
        .filter_by(skip_reason="suspected_duplicate", activity_type="外国预扣税")
        .one()
    )
    result = _import(db, account, taxes_only, confirmed_row_hashes=frozenset({held_tax.row_hash}))
    assert any("found 0" in error for error in result["errors"])
    assert all(a.tax_withheld == Decimal("0") for a in db.query(CorporateAction))
    db.refresh(held_tax)
    assert held_tax.skip_reason == "unattributed_tax"
