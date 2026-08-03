"""row_hash 抗回归：三家券商导入器的摘要必须与重构前捕获的黄金值逐字节一致。

黄金摘要在提取 broker_import_common 公共层之前用旧实现捕获；任何改动导致
这里失败，都意味着历史已导入流水的去重键会漂移——绝不允许。
"""

from datetime import date
from decimal import Decimal

from app.services import cmb_fund_flow_importer as cmb
from app.services import eastmoney_statement_importer as em
from app.services import ibkr_activity_importer as ibkr

CMB_VALUES = {
    "broker": "招商证券",
    "trade_date": date(2026, 3, 2),
    "serial_number": "﻿ 100234 ",  # BOM + 空白：走 strip_bom
    "business_name": "证券买入",
    "security_code": "600000.0",  # 尾缀 .0：strip_bom 去除
    "currency": "CNY",
    "trade_price": Decimal("10.500"),  # normalize -> 10.5
    "trade_quantity": Decimal("100"),
    "amount": Decimal("-1051.00"),
    "stamp_tax": Decimal("0"),
    "commission": Decimal("1.05"),
    "other_fee": Decimal("0.00"),
    "contract_number": "C-778",
    "shareholder_code": "A123456789",
}
EM_VALUES = {
    "broker": "东方财富证券",
    "statement_type": "hk_connect",
    "trade_date": date(2026, 4, 15),
    "event_type": "BUY",
    "security_code": "00700",
    "trade_quantity": Decimal("200"),
    "trade_price": Decimal("321.400"),
    "amount": Decimal("-64280.00"),
    "commission": Decimal("32.14"),
    "stamp_tax": Decimal("83.60"),
    "handling_fee": Decimal("3.86"),
    "management_fee": Decimal("0"),
    "settlement_fee": Decimal("1.29"),
    "transfer_fee": Decimal("0"),
    "other_fee": Decimal("0"),
    "settlement_rate": Decimal("0.92150"),
}
EM_LEGACY_VALUES = {
    "broker": "东方财富证券",
    "trade_date": date(2026, 4, 15),
    "business_name": "证券买入",
    "security_code": "600000",
    "security_name": "浦发银行",
    "trade_quantity": Decimal("100"),
    "trade_price": Decimal("10.50"),
    "amount": Decimal("-1051.00"),
    "commission": Decimal("1.05"),
    "stamp_tax": Decimal("0"),
    "transfer_fee": Decimal("0.20"),
    "cash_balance": Decimal("8949.00"),
}
IBKR_VALUES = {
    "broker": "IBKR",
    "trade_date": date(2026, 1, 22),
    "account": "U***67968",
    "description": " APPLE INC ",  # 前后空白：走 strip_text
    "activity_type": "买",
    "raw_symbol": "AAPL",
    "symbol": "AAPL",
    "quantity": Decimal("100"),
    "price": Decimal("10.00"),
    "price_currency": "USD",
    "gross_amount": Decimal("-1000.00"),
    "commission": Decimal("-1.00"),
    "net_amount": Decimal("-1001.00"),
}


def test_cmb_row_hash_matches_pre_refactor_golden():
    assert cmb.calculate_row_hash(CMB_VALUES) == (
        "9f10d633e3f271f4e14affa24734846e3602f123ff84d1ca17cd13d02104201d"
    )
    assert cmb.calculate_row_hash({**CMB_VALUES, "duplicate_occurrence": 2}) == (
        "6f830b65967379cc62118a10dadf03763904653aabfc189e8fcd9d5d7d4603f9"
    )


def test_eastmoney_row_hash_matches_pre_refactor_golden():
    assert em.calculate_row_hash(EM_VALUES) == (
        "625fac32df7a69a97b653a8b94d77df2442332599d50c7c62ab5fec391fb1099"
    )
    assert em.calculate_row_hash({**EM_VALUES, "duplicate_occurrence": 3}) == (
        "6d3dee63af7e8f792d65e44e225362ed32a940d068a1dd0605254c07f15eebf1"
    )
    assert em.calculate_row_hash(EM_LEGACY_VALUES, fields=em.LEGACY_HASH_FIELDS) == (
        "141778f9c4eafdcc7f5800905ecf28f0c6725a01132eb9c4cb26b60d3c11d477"
    )


def test_ibkr_row_hash_matches_pre_refactor_golden():
    assert ibkr.calculate_row_hash(IBKR_VALUES) == (
        "2c1c05194c796e0bb7a8bcef0ad056f33ba6135931723dea9927026c42eef170"
    )
    assert ibkr.calculate_row_hash({**IBKR_VALUES, "duplicate_occurrence": 2}) == (
        "52c9aba5ecb422efb5d1655af33c180540dc8ccffa9f52dc2d54c1e430926643"
    )


def test_normalize_and_strict_decimal_semantics_are_stable():
    assert cmb.normalize_hash_value(Decimal("10.500")) == "10.5"
    assert cmb.normalize_hash_value("﻿600000.0") == "600000"
    assert em.normalize_hash_value(Decimal("0.92150")) == "0.9215"
    assert em.normalize_hash_value(" x ") == "x"
    assert ibkr.normalize_hash_value(date(2026, 1, 22)) == "2026-01-22"
    assert cmb.parse_strict_pdf_decimal("1,234.50") == Decimal("1234.50")
    assert em.parse_strict_decimal("1,234.50") == Decimal("1234.50")
