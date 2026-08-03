"""导入结果响应契约：builder 产出的每个键都必须被响应模型承载。

历史事故：IBKR 现金入账加了 eligible_cash_event_rows / eligible_fx_rows，
但 BrokerImportResult 没跟上，FastAPI response_model 把它们静默丢弃，
前端拿不到；且可入账现金行被算进"现金类跳过"。本文件双向钉死：
键集合子集关系 + 预览语义（可入账 ≠ 跳过）。
"""

from datetime import date
from decimal import Decimal

from app.schemas.broker_import import BrokerImportResult
from app.services import ibkr_activity_importer as importer


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


def test_ibkr_result_keys_are_all_carried_by_response_model():
    result = _build_result(_representative_rows())
    schema_fields = set(BrokerImportResult.model_fields)
    dropped = set(result) - schema_fields
    assert not dropped, f"BrokerImportResult 会静默丢弃这些键: {sorted(dropped)}"


def test_preview_counts_separate_bookable_from_skipped():
    result = _build_result(_representative_rows())
    payload = BrokerImportResult.model_validate(result).model_dump()

    # 可入账行分列，不再计入"跳过"
    assert payload["eligible_cash_event_rows"] == 1
    assert payload["eligible_fx_rows"] == 1
    assert payload["skipped_cash_rows"] == 1  # 仅调整
    assert payload["skipped_fx_rows"] == 1  # 仅币种异常行
    assert payload["expected_archived_rows"] == 1
