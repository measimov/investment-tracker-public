"""导入结果契约：三家 builder 的键集合被两头钉死。

上界——builder 产出的每个键都必须被响应模型承载。历史事故：IBKR 现金入账
加了 eligible_cash_event_rows / eligible_fx_rows，但 BrokerImportResult 没
跟上，FastAPI response_model 把它们静默丢弃，前端拿不到；且可入账现金行被
算进"现金类跳过"。

下界——结算契约 `BATCH_SETTLEMENT_KEYS`：complete_import_batch 按键直取的
每个键，三家结果都必须在场（base_import_result 骨架给少产的券商落显式
默认 0）。此前三家键集合各不相同，批次结算只能靠 .get 默认值逐键调和。
"""

from datetime import date
from decimal import Decimal

import pytest

from app.schemas.broker_import import BrokerImportResult
from app.services import cmb_fund_flow_importer
from app.services import eastmoney_statement_importer
from app.services import ibkr_activity_importer as importer
from app.services.import_batch_service import BATCH_SETTLEMENT_KEYS


def _flow(row_hash, activity_type, **overrides):
    defaults = dict(
        source_row_number=1,
        row_hash=row_hash,
        account="U***0001",
        trade_date=date(2026, 1, 5),
        description=None,
        activity_type=activity_type,
        raw_symbol="",
        symbol=None,
        name=None,
        market=None,
        quantity=None,
        price=None,
        price_currency=None,
        base_currency="USD",
        gross_amount=None,
        commission=None,
        net_amount=None,
        fee_in_price_currency=None,
        skip_reason="cash",
    )
    defaults.update(overrides)
    return importer.ParsedIbkrFlow(**defaults)


def _representative_rows():
    return [
        # 可入账现金业务：存款
        _flow("a" * 64, "存款", net_amount=Decimal("1000"), gross_amount=Decimal("1000")),
        # 设计上只归档的调整（纸面损益）
        _flow("b" * 64, "调整", net_amount=Decimal("-5"), gross_amount=Decimal("-5")),
        # 可入账外汇兑换（两腿）
        _flow(
            "c" * 64,
            "外汇交易组成部分",
            raw_symbol="USD.HKD",
            quantity=Decimal("100"),
            price=Decimal("7.8"),
            price_currency="HKD",
            skip_reason="fx",
        ),
        # 货币对异常的外汇行：归档不入账
        _flow(
            "d" * 64,
            "外汇交易组成部分",
            raw_symbol="USD.HKD",
            quantity=Decimal("100"),
            price=Decimal("7.8"),
            price_currency="JPY",
            skip_reason="fx",
        ),
    ]


def _build_result(rows):
    return importer.build_import_result(
        filename="contract.csv",
        total_rows=len(rows),
        parsed_rows=rows,
        business_counts={},
        existing_hashes=set(),
        booked_source_hashes=set(),
        imported_transactions=0,
        imported_corporate_actions=0,
        imported_tax_adjustments=0,
        affected_symbols=0,
        errors=[],
    )


def _build_cmb_result():
    return cmb_fund_flow_importer.build_import_result(
        filename="contract.pdf",
        total_rows=0,
        parsed_rows=[],
        business_counts={},
        existing_hashes=set(),
        imported_transactions=0,
        imported_corporate_actions=0,
        imported_tax_adjustments=0,
        imported_cash_events=0,
        affected_symbols=0,
        errors=[],
    )


def _build_eastmoney_result():
    context = eastmoney_statement_importer.EastmoneyStatementContext(
        statement_type=eastmoney_statement_importer.STOCK_STATEMENT_TYPE,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        positions=[],
        cash_balances={},
    )
    return eastmoney_statement_importer.build_import_result(
        filename="contract.pdf",
        total_rows=0,
        parsed_rows=[],
        context=context,
        business_counts={},
        existing_hashes=set(),
        imported_transactions=0,
        imported_corporate_actions=0,
        imported_tax_adjustments=0,
        imported_cash_events=0,
        affected_symbols=0,
        errors=[],
    )


# 键集合是 dict 字面量的静态形状，空输入即可覆盖；数值语义由各家端到端测试守。
RESULT_BUILDERS = {
    "cmb": _build_cmb_result,
    "eastmoney": _build_eastmoney_result,
    "ibkr": lambda: _build_result(_representative_rows()),
}


@pytest.mark.parametrize("broker", sorted(RESULT_BUILDERS))
def test_result_keys_are_all_carried_by_response_model(broker):
    result = RESULT_BUILDERS[broker]()
    schema_fields = set(BrokerImportResult.model_fields)
    dropped = set(result) - schema_fields
    assert not dropped, f"BrokerImportResult 会静默丢弃这些键: {sorted(dropped)}"


@pytest.mark.parametrize("broker", sorted(RESULT_BUILDERS))
def test_result_defaults_suspected_duplicates_to_zero(broker):
    """疑似重复守卫（#190）暂只有招商接线，其余两家的骨架默认 0 / 空列表。"""
    result = RESULT_BUILDERS[broker]()
    assert result["suspected_duplicate_rows"] == 0
    assert result["suspected_duplicate_samples"] == []


@pytest.mark.parametrize("broker", sorted(RESULT_BUILDERS))
def test_result_defaults_opening_position_rows_to_zero(broker):
    """期初建仓（#174）暂只有招商接线，其余两家的骨架默认 0。"""
    result = RESULT_BUILDERS[broker]()
    assert result["eligible_opening_position_rows"] == 0


@pytest.mark.parametrize("broker", sorted(RESULT_BUILDERS))
def test_result_covers_batch_settlement_keys(broker):
    """结算契约：complete_import_batch 直取的键三家都必须在场。

    缺键在新口径下是 KeyError 而不是静默按 0 计——这正是想要的失败方式，
    但契约测试要在进结算之前就把缺口指出来。
    """
    result = RESULT_BUILDERS[broker]()
    missing = BATCH_SETTLEMENT_KEYS - set(result)
    assert not missing, f"{broker} 结果缺结算契约键: {sorted(missing)}"


def test_preview_counts_separate_bookable_from_skipped():
    result = _build_result(_representative_rows())
    payload = BrokerImportResult.model_validate(result).model_dump()

    # 可入账行分列，不再计入"跳过"
    assert payload["eligible_cash_event_rows"] == 1
    assert payload["eligible_fx_rows"] == 1
    assert payload["skipped_cash_rows"] == 1  # 仅调整
    assert payload["skipped_fx_rows"] == 1  # 仅币种异常行
    assert payload["expected_archived_rows"] == 1


# --------------------------------------------------------------------------- #279 批次生命周期


def test_cmb_archived_disposition_dispatch_table():
    """招商重导的归档行去向查表；未登记的 skip_reason 一律按重复计（不插第二条同 hash 行）。"""
    from types import SimpleNamespace

    from app.services import cmb_fund_flow_importer as cmb
    from app.services.broker_import_common import (
        SUSPECTED_DUPLICATE,
        UNATTRIBUTED_TAX,
        UNBOOKED_CUSTODY_OUT,
        UNBOOKED_OPENING_POSITION,
    )

    def flow(**kw):
        base = dict(row_hash="h1", is_dividend_tax=False, is_opening_position=False)
        return SimpleNamespace(**{**base, **kw})

    d = cmb.archived_disposition
    assert d(flow(), None, set()) == cmb.ARCHIVED_BOOKED
    assert d(flow(), UNBOOKED_CUSTODY_OUT, set()) == cmb.ARCHIVED_DUPLICATE
    assert d(flow(), UNATTRIBUTED_TAX, set()) == cmb.ARCHIVED_DUPLICATE
    assert d(flow(is_dividend_tax=True), UNATTRIBUTED_TAX, set()) == cmb.ARCHIVED_REVIVE
    assert d(flow(), SUSPECTED_DUPLICATE, set()) == cmb.ARCHIVED_DUPLICATE
    assert d(flow(), SUSPECTED_DUPLICATE, {"h1"}) == cmb.ARCHIVED_REVIVE
    assert d(flow(), UNBOOKED_OPENING_POSITION, set()) == cmb.ARCHIVED_DUPLICATE
    assert d(flow(is_opening_position=True), UNBOOKED_OPENING_POSITION, set()) == (
        cmb.ARCHIVED_REVIVE
    )
    assert d(flow(), "some_future_marker", {"h1"}) == cmb.ARCHIVED_DUPLICATE


def test_archive_and_link_revives_or_creates_and_booked_rows_count_both():
    from types import SimpleNamespace

    from app.services.broker_import_common import archive_and_link

    added = []
    db = SimpleNamespace(add=added.append)
    held = {"old": SimpleNamespace(name="preserved")}
    revived = set()
    kept = archive_and_link(
        db,
        held,
        "old",
        revive=lambda row: (setattr(row, "linked", 7), row)[1],
        create=lambda: SimpleNamespace(name="new"),
        revived=revived,
    )
    fresh = archive_and_link(
        db,
        held,
        "fresh",
        revive=lambda row: row,
        create=lambda: SimpleNamespace(name="new"),
        revived=revived,
    )
    assert (kept.name, kept.linked, fresh.name) == ("preserved", 7, "new")
    assert held == {} and revived == {"old"} and added == [kept, fresh]


def test_fail_broker_import_counts_only_what_is_committed(monkeypatch):
    """已提交后才失败：按库里实际状态重数；未提交：计数清零（fail_import_batch 自己回滚）。"""
    from app.services import broker_import_common as common

    calls = []
    monkeypatch.setattr(
        common, "fail_import_batch", lambda db, batch_id, exc, **kw: calls.append(kw)
    )
    monkeypatch.setattr(common, "archived_row_count", lambda db, model, batch_id: 5)
    monkeypatch.setattr(
        common, "booked_source_rows", lambda db, model, batch_id, revived: 3 + len(set(revived))
    )

    class FakeDb:
        rolled_back = False

        def rollback(self):
            self.rolled_back = True

    db = FakeDb()
    common.fail_broker_import(
        db,
        1,
        RuntimeError("x"),
        model=object,
        records_committed=True,
        row_count=10,
        duplicate_count=2,
        revived_hashes={"a"},
    )
    assert db.rolled_back
    assert calls[-1] == dict(
        records_committed=True, row_count=10, imported_count=4, duplicate_count=2, archived_count=5
    )
    common.fail_broker_import(
        FakeDb(),
        1,
        RuntimeError("x"),
        model=object,
        records_committed=False,
        row_count=10,
        duplicate_count=2,
    )
    assert calls[-1]["imported_count"] == 0 and calls[-1]["archived_count"] == 0
