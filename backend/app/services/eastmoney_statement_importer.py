from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pypdf import PdfReader
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models.broker_fund_flow import BrokerFundFlow
from ..models.cash_event import CashEvent
from ..models.corporate_action import CorporateAction
from ..models.reconciliation_snapshot import ReconciliationSnapshot
from ..models.transaction import Transaction
from ..services import broker_import_common
from ..services.broker_import_common import (
    HASH_DUPLICATE_OCCURRENCE_FIELD,
    find_dividend_for_tax,
    normalize_hash_value as normalize_hash_value,  # 测试断言导入器命名空间
    parse_strict_decimal,
    source_error_rows,
    strip_text,
)
from ..services.security_rule_service import get_excluded_symbols
from ..services.holding_service import recalculate_holdings
from ..services.import_batch_service import (
    complete_import_batch,
    fail_import_batch,
    set_import_batch_source_stats,
    start_import_batch,
    validate_import_account,
    validate_source_file_account,
)


BROKER_NAME = "东方财富证券"
SOURCE_TYPE_BY_SCOPE = {
    "stock": "eastmoney_stock_statement_pdf",
    "hk_connect": "eastmoney_hk_connect_statement_pdf",
}
PARSER_NAME = "eastmoney_statement"
# 行为变化必须升版（ImportBatch 按 parser/version 审计入账口径）：
# v6 = 开放基金申购 生成规范 BUY（此前仅归档）
PARSER_VERSION = "7"
STOCK_FLOW_HEADER = [
    "发生日期",
    "买卖类别",
    "证券代码",
    "证券名称",
    "成交数量",
    "成交价格",
    "总发生金额",
    "手续费",
    "印花税",
    "过户费",
    "资金余额",
]
HK_CONNECT_FLOW_HEADER = [
    "发生日期",
    "买卖类别",
    "证券代码",
    "证券名称",
    "成交数量",
    "成交价格",
    "结算汇率",
    "总发生金额",
    "手续费",
    "印花税",
    "交易征费",
    "交易费",
    "系统费用",
    "交收费",
    "其他费用",
]
TRADE_BUSINESS_MAP = {
    "证券买入": "BUY",
    "证券卖出": "SELL",
    "港股通买入": "BUY",
    "港股通卖出": "SELL",
    # 场内基金申购确认（如 161225/161226 白银LOF）：份额×净值与发生金额的
    # 差额即申购费（佣金列），金额恒等式精确成立，校验与买入同构。
    # 不建模会让后续"证券卖出"撞期初持仓守卫（与招商侧同一缺口）。
    "开放基金申购": "BUY",
}
DIVIDEND_BUSINESS_NAME = "红利入账"
DIVIDEND_TAX_BUSINESS_NAME = "股息红利差异扣税"
HK_CONNECT_FEE_BUSINESS_NAME = "港股通组合费"
STOCK_STATEMENT_TYPE = "stock"
HK_CONNECT_STATEMENT_TYPE = "hk_connect"
HASH_FIELDS = [
    "broker",
    "statement_type",
    "trade_date",
    "event_type",
    "security_code",
    "trade_quantity",
    "trade_price",
    "amount",
    "commission",
    "stamp_tax",
    "handling_fee",
    "management_fee",
    "settlement_fee",
    "transfer_fee",
    "other_fee",
    "settlement_rate",
]
LEGACY_HASH_FIELDS = [
    "broker",
    "trade_date",
    "business_name",
    "security_code",
    "security_name",
    "trade_quantity",
    "trade_price",
    "amount",
    "commission",
    "stamp_tax",
    "transfer_fee",
    "cash_balance",
]
AMOUNT_TOLERANCE = Decimal("0.02")
ROW_HASH_NOTE_PATTERN = re.compile(r"\brow_hash=([0-9a-f]{64})\b")
POSITION_REQUIRED_COLUMNS = {"证券代码", "证券名称", "持仓数量"}


@dataclass
class EastmoneyStatementPosition:
    symbol: str
    name: Optional[str]
    market: str
    quantity: Decimal
    currency: str = "CNY"


@dataclass
class EastmoneyStatementContext:
    statement_type: str
    period_start: date
    period_end: date
    positions: List[EastmoneyStatementPosition]
    cash_balances: Dict[str, Decimal]


@dataclass
class ParsedEastmoneyFlow:
    source_row_number: int
    row_hash: str
    legacy_row_hash: str
    security_code: str
    security_name: Optional[str]
    currency: str
    trade_date: date
    trade_price: Decimal
    trade_quantity: Decimal
    amount: Decimal
    cash_balance: Optional[Decimal]
    settlement_rate: Optional[Decimal]
    business_name: str
    stamp_tax: Decimal
    commission: Decimal
    handling_fee: Decimal
    management_fee: Decimal
    settlement_fee: Decimal
    transfer_fee: Decimal
    other_fee: Decimal
    statement_type: str
    skip_reason: Optional[str] = None

    @property
    def transaction_type(self) -> Optional[str]:
        if self.skip_reason:
            return None
        return TRADE_BUSINESS_MAP.get(self.business_name)

    @property
    def is_trade(self) -> bool:
        return self.transaction_type is not None

    @property
    def is_cash_dividend(self) -> bool:
        return (
            self.skip_reason is None
            and self.business_name == DIVIDEND_BUSINESS_NAME
            and bool(self.security_code)
            and self.amount > 0
        )

    @property
    def is_dividend_tax(self) -> bool:
        return (
            self.skip_reason is None
            and self.business_name == DIVIDEND_TAX_BUSINESS_NAME
            and bool(self.security_code)
            and self.amount < 0
        )

    @property
    def is_cash_fee(self) -> bool:
        return (
            self.skip_reason is None
            and self.business_name == HK_CONNECT_FEE_BUSINESS_NAME
            and self.amount > 0
        )

    @property
    def total_fee(self) -> Decimal:
        return (
            self.commission
            + self.stamp_tax
            + self.handling_fee
            + self.management_fee
            + self.settlement_fee
            + self.transfer_fee
            + self.other_fee
        )

    @property
    def normalized_transaction_currency(self) -> str:
        return "HKD" if self.statement_type == HK_CONNECT_STATEMENT_TYPE else self.currency

    @property
    def normalized_transaction_price(self) -> Decimal:
        if self.statement_type != HK_CONNECT_STATEMENT_TYPE:
            return self.trade_price
        if not self.settlement_rate or self.settlement_rate <= 0:
            raise ValueError("港股通交易缺少有效结算汇率")
        if self.trade_quantity <= 0:
            raise ValueError("港股通交易缺少有效成交数量")
        return abs(self.amount) / self.trade_quantity / self.settlement_rate

    @property
    def normalized_transaction_fee(self) -> Decimal:
        if self.statement_type != HK_CONNECT_STATEMENT_TYPE:
            return self.total_fee
        if not self.settlement_rate or self.settlement_rate <= 0:
            raise ValueError("港股通交易缺少有效结算汇率")
        return self.total_fee / self.settlement_rate


def parse_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    text = strip_text(value).replace(",", "")
    if not text or text == "--":
        return default
    try:
        return Decimal(text)
    except InvalidOperation:
        return default


def parse_optional_decimal(value: Any) -> Optional[Decimal]:
    text = strip_text(value).replace(",", "")
    if not text or text == "--":
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_trade_date(value: Any) -> Optional[date]:
    text = strip_text(value)
    if not re.fullmatch(r"\d{8}", text):
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def calculate_row_hash(values: Dict[str, Any], *, fields: Optional[List[str]] = None) -> str:
    return broker_import_common.calculate_row_hash(values, fields or HASH_FIELDS)


def infer_market(symbol: str) -> Optional[str]:
    symbol = strip_text(symbol)
    if not symbol:
        return None
    if re.fullmatch(r"\d{5}", symbol):
        return "港股"
    if re.fullmatch(r"\d{6}", symbol):
        return "A股"
    return None


def canonical_event_type(business_name: str) -> str:
    if business_name in TRADE_BUSINESS_MAP:
        return TRADE_BUSINESS_MAP[business_name]
    if business_name == DIVIDEND_BUSINESS_NAME:
        return "CASH_DIVIDEND"
    if business_name == DIVIDEND_TAX_BUSINESS_NAME:
        return "DIVIDEND_TAX"
    if business_name == HK_CONNECT_FEE_BUSINESS_NAME:
        return "FEE"
    return f"RAW:{business_name}"


def detect_skip_reason(
    statement_type: str, business_name: str, security_code: str
) -> Optional[str]:
    if business_name in TRADE_BUSINESS_MAP:
        market = infer_market(security_code)
        if not market:
            return "unsupported"
        if statement_type == STOCK_STATEMENT_TYPE and market != "A股":
            return "conflict"
        if statement_type == HK_CONNECT_STATEMENT_TYPE and market != "港股":
            return "conflict"
        return None
    if business_name in {DIVIDEND_BUSINESS_NAME, DIVIDEND_TAX_BUSINESS_NAME}:
        market = infer_market(security_code)
        if not market:
            return "unsupported"
        if statement_type == STOCK_STATEMENT_TYPE and market != "A股":
            return "conflict"
        if statement_type == HK_CONNECT_STATEMENT_TYPE and market != "港股":
            return "conflict"
        return None
    if business_name == HK_CONNECT_FEE_BUSINESS_NAME:
        return None if statement_type == HK_CONNECT_STATEMENT_TYPE else "conflict"
    return "unsupported"


def ensure_pdf_is_readable(contents: bytes) -> None:
    reader = PdfReader(io.BytesIO(contents))
    if reader.is_encrypted:
        raise ValueError("PDF is encrypted. Please decrypt it with qpdf before importing.")


def normalize_header_cell(value: Any) -> str:
    return re.sub(r"\s+", "", strip_text(value))


def parse_statement_date(value: str) -> date:
    normalized = value.replace("/", "-")
    parts = normalized.split("-")
    if len(parts) != 3:
        raise ValueError(f"invalid 东方财富 statement date: {value}")
    try:
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError as exc:
        raise ValueError(f"invalid 东方财富 statement date: {value}") from exc


def statement_type_from_text(text: str) -> str:
    if "港股通股票明细对账单" in text:
        return HK_CONNECT_STATEMENT_TYPE
    if "股票明细对账单" in text:
        return STOCK_STATEMENT_TYPE
    raise ValueError("No supported 东方财富 statement title found in PDF")


def read_eastmoney_statement_context(contents: bytes) -> EastmoneyStatementContext:
    ensure_pdf_is_readable(contents)
    positions: List[EastmoneyStatementPosition] = []
    cash_balances: Dict[str, Decimal] = {}
    page_texts: List[str] = []

    try:
        with pdfplumber.open(io.BytesIO(contents)) as pdf:
            for page in pdf.pages:
                page_texts.append(page.extract_text() or "")
                for table in page.extract_tables():
                    if not table:
                        continue
                    header = [normalize_header_cell(column) for column in table[0]]
                    if not POSITION_REQUIRED_COLUMNS.issubset(set(header)):
                        continue
                    for row in table[1:]:
                        if len(row) != len(header):
                            raise ValueError("unexpected 东方财富 position table column count")
                        values = {
                            key: normalize_header_cell(value) for key, value in zip(header, row)
                        }
                        symbol = strip_text(values.get("证券代码"))
                        market = infer_market(symbol)
                        quantity = parse_strict_decimal(values.get("持仓数量"))
                        if not symbol or not market or quantity is None or quantity < 0:
                            raise ValueError(
                                f"invalid 东方财富 reported position: {symbol or '(blank)'}"
                            )
                        positions.append(
                            EastmoneyStatementPosition(
                                symbol=symbol,
                                name=strip_text(values.get("证券名称")) or None,
                                market=market,
                                quantity=quantity,
                                currency="HKD" if market == "港股" else "CNY",
                            )
                        )
    except PDFPasswordIncorrect as exc:
        raise ValueError("PDF is encrypted. Please decrypt it with qpdf before importing.") from exc

    document_text = "\n".join(page_texts)
    statement_type = statement_type_from_text(document_text)
    period_match = re.search(
        r"查询区间[：:]\s*(\d{4}/\d{2}/\d{2})\s*-\s*(\d{4}/\d{2}/\d{2})",
        document_text,
    )
    if not period_match:
        raise ValueError("No declared query period found in 东方财富 statement")
    period_start = parse_statement_date(period_match.group(1))
    period_end = parse_statement_date(period_match.group(2))
    if period_end < period_start:
        raise ValueError("东方财富 statement query period is reversed")

    cash_match = re.search(
        r"资金余额\(RMB\)[：:]\s*([+-]?(?:\d[\d,]*)(?:\.\d+)?)",
        document_text,
    )
    if cash_match:
        cash_amount = parse_strict_decimal(cash_match.group(1))
        if cash_amount is None:
            raise ValueError("Invalid RMB cash balance in 东方财富 statement")
        cash_balances["CNY"] = cash_amount

    return EastmoneyStatementContext(
        statement_type=statement_type,
        period_start=period_start,
        period_end=period_end,
        positions=positions,
        cash_balances=cash_balances,
    )


def read_eastmoney_statement_rows(contents: bytes) -> tuple[List[tuple[int, Dict[str, str]]], int]:
    ensure_pdf_is_readable(contents)
    data_rows: List[tuple[int, Dict[str, str]]] = []
    row_number = 0

    try:
        with pdfplumber.open(io.BytesIO(contents)) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables():
                    if not table:
                        continue
                    header = [normalize_header_cell(column) for column in table[0]]
                    if header == STOCK_FLOW_HEADER:
                        statement_type = STOCK_STATEMENT_TYPE
                        currency = "CNY"
                    elif header == HK_CONNECT_FLOW_HEADER:
                        statement_type = HK_CONNECT_STATEMENT_TYPE
                        currency = "CNY"
                    else:
                        continue
                    for row in table[1:]:
                        row_number += 1
                        if len(row) != len(header):
                            raise ValueError(
                                f"row {row_number}: unexpected 东方财富 table column count"
                            )
                        normalized_row = {
                            key: normalize_header_cell(value) for key, value in zip(header, row)
                        }
                        normalized_row.update(
                            {
                                "_statement_type": statement_type,
                                "_currency": currency,
                            }
                        )
                        data_rows.append((row_number, normalized_row))
    except PDFPasswordIncorrect as exc:
        raise ValueError("PDF is encrypted. Please decrypt it with qpdf before importing.") from exc

    if not data_rows:
        raise ValueError("No 东方财富资金流水明细 table found in PDF")
    return data_rows, row_number


def parse_table_rows(
    data_rows: Iterable[tuple[int, Dict[str, str]]],
) -> tuple[List[ParsedEastmoneyFlow], Dict[str, int], int, List[str]]:
    parsed_rows: List[ParsedEastmoneyFlow] = []
    business_counts: Dict[str, int] = {}
    errors: List[str] = []
    hash_occurrences: Dict[str, int] = {}
    legacy_hash_occurrences: Dict[str, int] = {}
    total_rows = 0

    for row_number, row in data_rows:
        total_rows += 1
        business_name = strip_text(row.get("买卖类别"))
        if not business_name:
            errors.append(f"row {row_number}: missing business type")
            continue
        business_counts[business_name] = business_counts.get(business_name, 0) + 1

        trade_date = parse_trade_date(row.get("发生日期"))
        if trade_date is None:
            errors.append(f"row {row_number}: invalid trade date")
            continue

        statement_type = strip_text(row.get("_statement_type")) or STOCK_STATEMENT_TYPE
        if statement_type not in {STOCK_STATEMENT_TYPE, HK_CONNECT_STATEMENT_TYPE}:
            errors.append(f"row {row_number}: unsupported statement scope")
            continue
        numeric_columns = [
            "成交数量",
            "成交价格",
            "总发生金额",
            "手续费",
            "印花税",
        ]
        if statement_type == HK_CONNECT_STATEMENT_TYPE:
            numeric_columns.extend(
                [
                    "结算汇率",
                    "交易征费",
                    "交易费",
                    "系统费用",
                    "交收费",
                    "其他费用",
                ]
            )
        else:
            numeric_columns.append("过户费")

        strict_values: Dict[str, Decimal] = {}
        invalid_columns = []
        for column in numeric_columns:
            parsed_value = parse_strict_decimal(row.get(column))
            if parsed_value is None:
                invalid_columns.append(column)
            else:
                strict_values[column] = parsed_value
        if invalid_columns:
            errors.append(
                f"row {row_number}: invalid PDF numeric fields: {', '.join(invalid_columns)}"
            )
            continue

        security_code = strip_text(row.get("证券代码"))
        security_name = strip_text(row.get("证券名称")) or None
        trade_quantity = strict_values["成交数量"]
        trade_price = strict_values["成交价格"]
        amount = strict_values["总发生金额"]
        commission = strict_values["手续费"]
        stamp_tax = strict_values["印花税"]
        handling_fee = strict_values.get("交易费", Decimal("0"))
        management_fee = strict_values.get("交易征费", Decimal("0"))
        settlement_fee = strict_values.get("交收费", Decimal("0"))
        transfer_fee = strict_values.get("过户费", Decimal("0"))
        other_fee = strict_values.get("系统费用", Decimal("0")) + strict_values.get(
            "其他费用", Decimal("0")
        )
        settlement_rate = (
            strict_values.get("结算汇率") if statement_type == HK_CONNECT_STATEMENT_TYPE else None
        )
        cash_balance_text = strip_text(row.get("资金余额"))
        cash_balance = parse_optional_decimal(cash_balance_text)
        if (
            statement_type == STOCK_STATEMENT_TYPE
            and cash_balance_text
            and cash_balance_text != "--"
            and cash_balance is None
        ):
            errors.append(f"row {row_number}: invalid PDF cash balance")
            continue
        skip_reason = detect_skip_reason(statement_type, business_name, security_code)

        fee_values = [
            commission,
            stamp_tax,
            handling_fee,
            management_fee,
            settlement_fee,
            transfer_fee,
            other_fee,
        ]
        if any(fee < 0 for fee in fee_values):
            errors.append(f"row {row_number}: PDF fee fields must be non-negative")
            continue

        if business_name in TRADE_BUSINESS_MAP and skip_reason is None:
            if trade_quantity <= 0 or trade_price <= 0:
                skip_reason = "invalid"
                errors.append(f"row {row_number}: invalid trade quantity or price")
            elif statement_type == STOCK_STATEMENT_TYPE:
                gross_amount = trade_quantity * trade_price
                total_fee = sum(fee_values, Decimal("0"))
                transaction_type = TRADE_BUSINESS_MAP[business_name]
                price_text = strip_text(row.get("成交价格"))
                decimal_places = len(price_text.rsplit(".", 1)[1]) if "." in price_text else 0
                gross_rounding_tolerance = trade_quantity * Decimal("0.5").scaleb(-decimal_places)
                expected_amount = (
                    -(gross_amount + total_fee)
                    if transaction_type == "BUY"
                    else gross_amount - total_fee
                )
                if abs(amount - expected_amount) > (gross_rounding_tolerance + AMOUNT_TOLERANCE):
                    errors.append(
                        f"row {row_number}: PDF trade amount does not reconcile with value and fees"
                    )
                    continue
            else:
                if settlement_rate is None or settlement_rate <= 0:
                    errors.append(f"row {row_number}: invalid 港股通 settlement rate")
                    continue
                unit_amount = abs(amount) / trade_quantity
                price_text = strip_text(row.get("成交价格"))
                decimal_places = len(price_text.rsplit(".", 1)[1]) if "." in price_text else 0
                unit_tolerance = Decimal("0.5").scaleb(-decimal_places)
                if abs(unit_amount - trade_price) > unit_tolerance:
                    errors.append(
                        f"row {row_number}: 港股通 trade amount does not reconcile "
                        "with the rounded CNY unit price"
                    )
                    continue
        elif business_name == DIVIDEND_BUSINESS_NAME and amount <= 0:
            skip_reason = "invalid"
            errors.append(f"row {row_number}: dividend amount must be positive")
        elif business_name == DIVIDEND_TAX_BUSINESS_NAME and amount >= 0:
            skip_reason = "invalid"
            errors.append(f"row {row_number}: dividend tax amount must be negative")
        elif business_name == HK_CONNECT_FEE_BUSINESS_NAME and amount <= 0:
            skip_reason = "invalid"
            errors.append(f"row {row_number}: 港股通组合费 amount must be positive")

        if skip_reason == "conflict":
            errors.append(
                f"row {row_number}: {business_name} conflicts with "
                f"{statement_type} statement authority scope"
            )

        hash_values = {
            "broker": BROKER_NAME,
            "statement_type": statement_type,
            "trade_date": trade_date,
            "event_type": canonical_event_type(business_name),
            "business_name": business_name,
            "security_code": security_code,
            "security_name": security_name,
            "trade_quantity": trade_quantity,
            "trade_price": trade_price,
            "amount": amount,
            "commission": commission,
            "stamp_tax": stamp_tax,
            "handling_fee": handling_fee,
            "management_fee": management_fee,
            "settlement_fee": settlement_fee,
            "transfer_fee": transfer_fee,
            "other_fee": other_fee,
            "settlement_rate": settlement_rate,
            "cash_balance": cash_balance,
        }
        base_row_hash = calculate_row_hash(hash_values)
        hash_occurrences[base_row_hash] = hash_occurrences.get(base_row_hash, 0) + 1
        row_hash = base_row_hash
        if hash_occurrences[base_row_hash] > 1:
            hash_values[HASH_DUPLICATE_OCCURRENCE_FIELD] = hash_occurrences[base_row_hash]
            row_hash = calculate_row_hash(hash_values)

        legacy_hash_values = {
            "broker": BROKER_NAME,
            "trade_date": trade_date,
            "business_name": business_name,
            "security_code": security_code,
            "security_name": security_name,
            "trade_quantity": trade_quantity,
            "trade_price": trade_price,
            "amount": amount,
            "commission": commission,
            "stamp_tax": stamp_tax,
            "transfer_fee": transfer_fee,
            "cash_balance": cash_balance,
        }
        legacy_base_hash = calculate_row_hash(legacy_hash_values, fields=LEGACY_HASH_FIELDS)
        legacy_hash_occurrences[legacy_base_hash] = (
            legacy_hash_occurrences.get(legacy_base_hash, 0) + 1
        )
        legacy_row_hash = legacy_base_hash
        if legacy_hash_occurrences[legacy_base_hash] > 1:
            legacy_hash_values[HASH_DUPLICATE_OCCURRENCE_FIELD] = legacy_hash_occurrences[
                legacy_base_hash
            ]
            legacy_row_hash = calculate_row_hash(legacy_hash_values, fields=LEGACY_HASH_FIELDS)

        parsed_rows.append(
            ParsedEastmoneyFlow(
                source_row_number=row_number,
                row_hash=row_hash,
                legacy_row_hash=legacy_row_hash,
                security_code=security_code,
                security_name=security_name,
                currency=strip_text(row.get("_currency")) or "CNY",
                trade_date=trade_date,
                trade_price=trade_price,
                trade_quantity=trade_quantity,
                amount=amount,
                cash_balance=cash_balance,
                settlement_rate=settlement_rate,
                business_name=business_name,
                stamp_tax=stamp_tax,
                commission=commission,
                handling_fee=handling_fee,
                management_fee=management_fee,
                settlement_fee=settlement_fee,
                transfer_fee=transfer_fee,
                other_fee=other_fee,
                statement_type=statement_type,
                skip_reason=skip_reason,
            )
        )

    return parsed_rows, business_counts, total_rows, errors


def parse_rows(
    contents: bytes, filename: str
) -> tuple[List[ParsedEastmoneyFlow], Dict[str, int], int, List[str]]:
    data_rows, _ = read_eastmoney_statement_rows(contents)
    return parse_table_rows(data_rows)


def eligible_rows(parsed_rows: List[ParsedEastmoneyFlow]) -> List[ParsedEastmoneyFlow]:
    return [
        flow
        for flow in parsed_rows
        if flow.is_trade or flow.is_cash_dividend or flow.is_dividend_tax or flow.is_cash_fee
    ]


def flow_to_sample(flow: ParsedEastmoneyFlow, duplicate: bool) -> Dict[str, Any]:
    market = infer_market(flow.security_code) or ""
    mapped_type = flow.transaction_type or (
        "CASH_DIVIDEND"
        if flow.is_cash_dividend
        else "DIVIDEND_TAX"
        if flow.is_dividend_tax
        else "FEE"
        if flow.is_cash_fee
        else flow.skip_reason or ""
    )
    return {
        "row_number": flow.source_row_number,
        "symbol": flow.security_code,
        "name": flow.security_name,
        "market": market,
        "transaction_type": mapped_type,
        "trade_date": flow.trade_date.isoformat(),
        "quantity": str(abs(flow.trade_quantity)),
        "price": str(flow.trade_price),
        "fee": str(flow.total_fee),
        "row_hash": flow.row_hash,
        "duplicate": duplicate,
    }


def get_existing_hashes(
    db: Session,
    user_id: int,
    flows: Iterable[ParsedEastmoneyFlow],
    broker_account_id: Optional[int] = None,
) -> set[str]:
    flow_list = list(flows)
    hash_list = list(
        {row_hash for flow in flow_list for row_hash in (flow.row_hash, flow.legacy_row_hash)}
    )
    if not hash_list:
        return set()
    query = db.query(BrokerFundFlow.row_hash).filter(
        BrokerFundFlow.user_id == user_id,
        BrokerFundFlow.row_hash.in_(hash_list),
    )
    if broker_account_id is not None:
        query = query.filter(BrokerFundFlow.broker_account_id == broker_account_id)
    stored_hashes = {row[0] for row in query.all()}
    return {
        flow.row_hash
        for flow in flow_list
        if flow.row_hash in stored_hashes or flow.legacy_row_hash in stored_hashes
    }


def _decimal_value(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def _transaction_matches_flow(
    transaction: Transaction,
    flow: ParsedEastmoneyFlow,
) -> bool:
    return (
        transaction.symbol == flow.security_code
        and transaction.market == infer_market(flow.security_code)
        and transaction.transaction_type == flow.transaction_type
        and _decimal_value(transaction.quantity) == abs(flow.trade_quantity)
        and _decimal_value(transaction.price) == flow.normalized_transaction_price
        and _decimal_value(transaction.fee) == flow.normalized_transaction_fee
        and transaction.transaction_date == flow.trade_date
        and transaction.currency == flow.normalized_transaction_currency
    )


def _corporate_action_matches_flow(
    action: CorporateAction,
    flow: ParsedEastmoneyFlow,
) -> bool:
    if (
        action.symbol != flow.security_code
        or action.market != infer_market(flow.security_code)
        or action.action_type != "CASH_DIVIDEND"
        or action.currency != flow.currency
    ):
        return False
    if flow.is_cash_dividend:
        return (
            action.ex_date == flow.trade_date
            and _decimal_value(action.total_dividend) == flow.amount
        )
    if flow.is_dividend_tax:
        return action.ex_date <= flow.trade_date
    return False


def _cash_event_matches_flow(
    cash_event: CashEvent,
    flow: ParsedEastmoneyFlow,
) -> bool:
    return (
        cash_event.event_type == "FEE"
        and cash_event.event_date == flow.trade_date
        and cash_event.currency == flow.currency
        and _decimal_value(cash_event.amount) == abs(flow.amount)
    )


def _validate_corporate_action_source_aggregate(
    db: Session,
    *,
    user_id: int,
    broker_account_id: int,
    action: CorporateAction,
    selected_sources: Iterable[BrokerFundFlow],
) -> None:
    selected = {source.id: source for source in selected_sources}
    action_notes = action.notes or ""
    candidate_sources = (
        db.query(BrokerFundFlow)
        .filter(
            BrokerFundFlow.user_id == user_id,
            BrokerFundFlow.broker == BROKER_NAME,
            BrokerFundFlow.business_name.in_([DIVIDEND_BUSINESS_NAME, DIVIDEND_TAX_BUSINESS_NAME]),
        )
        .all()
    )
    note_hashes = ROW_HASH_NOTE_PATTERN.findall(action_notes)
    if len(note_hashes) != len(set(note_hashes)):
        raise ValueError("东方财富旧公司行动 notes 含重复 row_hash 引用；本次未导入")
    sources_by_hash: Dict[str, List[BrokerFundFlow]] = {}
    for source in candidate_sources:
        sources_by_hash.setdefault(source.row_hash, []).append(source)
    for row_hash in note_hashes:
        if len(sources_by_hash.get(row_hash, [])) != 1:
            raise ValueError(
                "东方财富旧公司行动 notes 引用的 row_hash 来源缺失或不唯一；本次未导入"
            )

    action_sources = {
        source.id: source
        for source in candidate_sources
        if source.corporate_action_id == action.id
        or source.id in selected
        or source.row_hash in action_notes
    }

    dividend_total = Decimal("0")
    tax_total = Decimal("0")
    dividend_count = 0
    for source in action_sources.values():
        if source.broker_account_id not in (None, broker_account_id):
            raise ValueError("东方财富旧公司行动来源横跨不同账户；本次未导入")
        if (
            source.security_code != action.symbol
            or infer_market(source.security_code or "") != action.market
            or source.currency != action.currency
        ):
            raise ValueError("东方财富旧公司行动来源与公司行动标的或币种不一致；本次未导入")
        amount = _decimal_value(source.amount)
        if source.business_name == DIVIDEND_BUSINESS_NAME:
            if amount <= 0:
                raise ValueError("东方财富旧股息来源金额无效；本次未导入")
            if source.trade_date != action.ex_date:
                raise ValueError("东方财富旧股息来源日期与公司行动除息日不一致；本次未导入")
            dividend_total += amount
            dividend_count += 1
        elif source.business_name == DIVIDEND_TAX_BUSINESS_NAME:
            if amount >= 0:
                raise ValueError("东方财富旧红利税来源金额无效；本次未导入")
            if source.trade_date < action.ex_date:
                raise ValueError("东方财富旧红利税来源日期早于公司行动除息日；本次未导入")
            tax_total += abs(amount)

    expected_net = dividend_total - tax_total
    if (
        dividend_count != 1
        or _decimal_value(action.total_dividend) != dividend_total
        or _decimal_value(action.tax_withheld) != tax_total
        or _decimal_value(action.net_dividend) != expected_net
    ):
        raise ValueError(
            "东方财富旧公司行动与全部股息/红利税来源聚合不一致；为避免重复记账，本次未导入"
        )


def build_import_result(
    *,
    filename: str,
    total_rows: int,
    parsed_rows: List[ParsedEastmoneyFlow],
    context: EastmoneyStatementContext,
    business_counts: Dict[str, int],
    existing_hashes: set[str],
    imported_transactions: int,
    imported_corporate_actions: int,
    imported_tax_adjustments: int,
    imported_cash_events: int,
    affected_symbols: int,
    errors: List[str],
) -> Dict[str, Any]:
    rows = eligible_rows(parsed_rows)
    trade_rows = [flow for flow in rows if flow.is_trade]
    dividend_rows = [flow for flow in rows if flow.is_cash_dividend]
    tax_rows = [flow for flow in rows if flow.is_dividend_tax]
    cash_rows = [flow for flow in rows if flow.is_cash_fee]

    eligible_row_hashes = {flow.row_hash for flow in rows}
    seen_hashes: set[str] = set()
    duplicate_rows: List[ParsedEastmoneyFlow] = []
    import_rows: List[ParsedEastmoneyFlow] = []
    new_source_rows: List[ParsedEastmoneyFlow] = []
    for flow in parsed_rows:
        if flow.row_hash in existing_hashes or flow.row_hash in seen_hashes:
            duplicate_rows.append(flow)
        else:
            new_source_rows.append(flow)
            if flow.row_hash in eligible_row_hashes:
                import_rows.append(flow)
            seen_hashes.add(flow.row_hash)

    dates = [flow.trade_date for flow in parsed_rows]
    parsed_source_rows = {flow.source_row_number for flow in parsed_rows}
    error_rows = source_error_rows(errors, parsed_source_rows)
    skip_counts = {
        "unsupported": len([flow for flow in new_source_rows if flow.skip_reason == "unsupported"]),
        "invalid": len([flow for flow in new_source_rows if flow.skip_reason == "invalid"]),
        "conflict": len([flow for flow in new_source_rows if flow.skip_reason == "conflict"]),
        "excluded": len([flow for flow in new_source_rows if flow.skip_reason == "excluded"]),
    }
    skipped_non_trade_rows = len(
        [
            flow
            for flow in new_source_rows
            if not (
                flow.is_trade or flow.is_cash_dividend or flow.is_dividend_tax or flow.is_cash_fee
            )
            # 排除行单独计入 skipped_excluded_rows，不混入非交易口径
            # （否则批次结算的 unresolved 仍会把预期跳过判成 PARTIAL）
            and flow.skip_reason != "excluded"
        ]
    )

    return {
        "broker": BROKER_NAME,
        "filename": filename,
        "total_rows": total_rows,
        "eligible_trade_rows": len(trade_rows),
        "eligible_dividend_rows": len(dividend_rows),
        "eligible_tax_rows": len(tax_rows),
        "eligible_cash_rows": len(cash_rows),
        "imported_transactions": imported_transactions,
        "imported_corporate_actions": imported_corporate_actions,
        "imported_tax_adjustments": imported_tax_adjustments,
        "imported_cash_events": imported_cash_events,
        "duplicate_rows": len(duplicate_rows),
        "skipped_non_trade_rows": skipped_non_trade_rows,
        "skipped_invalid_rows": skip_counts["invalid"] + len(error_rows),
        "skipped_option_rows": 0,
        "skipped_fx_rows": 0,
        "skipped_cash_rows": 0,
        "skipped_unsupported_rows": skip_counts["unsupported"],
        "skipped_conflict_rows": skip_counts["conflict"],
        "skipped_excluded_rows": skip_counts["excluded"],
        "excluded_unbooked_rows": skip_counts["excluded"],
        "affected_symbols": affected_symbols,
        "date_start": context.period_start.isoformat(),
        "date_end": context.period_end.isoformat(),
        "event_date_start": min(dates).isoformat() if dates else None,
        "event_date_end": max(dates).isoformat() if dates else None,
        "statement_scope": context.statement_type,
        "reported_position_count": len(scoped_statement_positions(context)),
        "business_counts": business_counts,
        "duplicate_samples": [flow_to_sample(flow, True) for flow in duplicate_rows[:10]],
        "import_samples": [flow_to_sample(flow, False) for flow in import_rows[:10]],
        "errors": errors[:50],
    }


def apply_exclusions(parsed_rows: List[ParsedEastmoneyFlow], excluded_symbols) -> None:
    """排除清单标记：命中标的的行置 skip_reason="excluded"。

    skip_reason 统一门控 is_trade / is_cash_dividend / is_dividend_tax /
    is_cash_fee，因此一遍标记即可让排除标的全链路只归档不入账。
    """
    if not excluded_symbols:
        return
    for flow in parsed_rows:
        if (
            flow.skip_reason is None
            and flow.security_code
            and flow.security_code in excluded_symbols
        ):
            flow.skip_reason = "excluded"


def reject_unassigned_legacy_sources(db: Session, user_id: int) -> None:
    """领养路径已退役：NULL 账户历史来源必须显式拒绝，绝不静默双记。

    账户级判重按 (user, broker_account, row_hash) 进行，看不见 NULL 桶的
    旧来源；库约束又允许同一 hash 在 NULL 桶与已分配账户各存一份——若放行，
    重新导入会给同一笔流水再建一份 canonical 记录。重建后的正常数据不存在
    这类行；从旧备份恢复的库必须先人工迁移（含旧 Excel 等异构 hash 来源，
    故按存在性整体拒绝，不做逐 hash 匹配）。
    """
    unassigned = (
        db.query(BrokerFundFlow.id)
        .filter(
            BrokerFundFlow.user_id == user_id,
            BrokerFundFlow.broker == BROKER_NAME,
            BrokerFundFlow.broker_account_id.is_(None),
        )
        .count()
    )
    if unassigned:
        raise ValueError(
            f"存在 {unassigned} 条未分配账户的{BROKER_NAME}历史来源（领养路径已退役）。"
            "请先人工迁移或清理这些 NULL 账户流水后再导入，否则会重复入账"
        )


def preview_eastmoney_statement(
    db: Session,
    user_id: int,
    contents: bytes,
    filename: str,
    broker_account_id: Optional[int] = None,
) -> Dict[str, Any]:
    if broker_account_id is None:
        raise ValueError("请选择东方财富券商账户后再预览")
    validate_import_account(
        db,
        user_id=user_id,
        broker_account_id=broker_account_id,
        broker=BROKER_NAME,
    )
    reject_unassigned_legacy_sources(db, user_id)
    validate_source_file_account(
        db,
        user_id=user_id,
        broker_account_id=broker_account_id,
        broker=BROKER_NAME,
        contents=contents,
    )
    parsed_rows, business_counts, total_rows, errors = parse_rows(contents, filename)
    apply_exclusions(parsed_rows, get_excluded_symbols(db, user_id))
    context = read_eastmoney_statement_context(contents)
    if any(flow.statement_type != context.statement_type for flow in parsed_rows):
        raise ValueError("东方财富 statement title and flow table scope do not match")
    existing_hashes = get_existing_hashes(
        db,
        user_id,
        parsed_rows,
        broker_account_id=broker_account_id,
    )
    return build_import_result(
        filename=filename,
        total_rows=total_rows,
        parsed_rows=parsed_rows,
        context=context,
        business_counts=business_counts,
        existing_hashes=existing_hashes,
        imported_transactions=0,
        imported_corporate_actions=0,
        imported_tax_adjustments=0,
        imported_cash_events=0,
        affected_symbols=0,
        errors=errors,
    )


def create_broker_fund_flow(
    *,
    user_id: int,
    broker_account_id: int,
    filename: str,
    flow: ParsedEastmoneyFlow,
    import_batch_id: Optional[int] = None,
    transaction_id: Optional[int] = None,
    corporate_action_id: Optional[int] = None,
    cash_event_id: Optional[int] = None,
) -> BrokerFundFlow:
    return BrokerFundFlow(
        user_id=user_id,
        broker_account_id=broker_account_id,
        import_batch_id=import_batch_id,
        transaction_id=transaction_id,
        corporate_action_id=corporate_action_id,
        cash_event_id=cash_event_id,
        broker=BROKER_NAME,
        row_hash=flow.row_hash,
        source_filename=filename,
        source_row_number=flow.source_row_number,
        security_code=flow.security_code,
        security_name=flow.security_name,
        currency=flow.currency,
        trade_date=flow.trade_date,
        trade_price=flow.trade_price,
        trade_quantity=flow.trade_quantity,
        amount=flow.amount,
        cash_balance=flow.cash_balance,
        settlement_rate=flow.settlement_rate,
        statement_type=flow.statement_type,
        business_name=flow.business_name,
        stamp_tax=flow.stamp_tax,
        commission=flow.commission,
        handling_fee=flow.handling_fee,
        management_fee=flow.management_fee,
        settlement_fee=flow.settlement_fee,
        transfer_fee=flow.transfer_fee,
        other_fee=flow.other_fee,
        notes=(
            f"scope={flow.statement_type}; "
            f"canonical_event={canonical_event_type(flow.business_name)}"
        ),
    )


def statement_market(statement_type: str) -> str:
    if statement_type == STOCK_STATEMENT_TYPE:
        return "A股"
    if statement_type == HK_CONNECT_STATEMENT_TYPE:
        return "港股"
    raise ValueError(f"Unsupported 东方财富 statement scope: {statement_type}")


def scoped_statement_positions(
    context: EastmoneyStatementContext,
) -> List[EastmoneyStatementPosition]:
    market = statement_market(context.statement_type)
    return [position for position in context.positions if position.market == market]


def calculate_account_position_quantities(
    db: Session,
    *,
    user_id: int,
    broker_account_id: int,
    snapshot_date: date,
    raise_on_oversell: bool = False,
) -> Dict[tuple[str, str], Decimal]:
    events: List[tuple[date, int, int, int, str, str, Any]] = []
    transactions = (
        db.query(Transaction)
        .filter(
            Transaction.user_id == user_id,
            Transaction.broker_account_id == broker_account_id,
            Transaction.transaction_date <= snapshot_date,
        )
        .all()
    )
    for transaction in transactions:
        events.append(
            (
                transaction.transaction_date,
                1,
                0 if transaction.transaction_type == "BUY" else 1,
                transaction.id or 0,
                transaction.symbol,
                transaction.market,
                transaction,
            )
        )

    quantity_actions = (
        db.query(CorporateAction)
        .filter(
            CorporateAction.user_id == user_id,
            CorporateAction.broker_account_id == broker_account_id,
            CorporateAction.ex_date <= snapshot_date,
            CorporateAction.action_type.in_(
                [
                    "STOCK_DIVIDEND",
                    "BONUS_ISSUE",
                    "RIGHTS_ISSUE",
                    "STOCK_SPLIT",
                    "REVERSE_SPLIT",
                ]
            ),
        )
        .all()
    )
    for action in quantity_actions:
        events.append(
            (
                action.ex_date,
                0,
                0,
                action.id or 0,
                action.symbol,
                action.market,
                action,
            )
        )

    quantities: Dict[tuple[str, str], Decimal] = {}
    for _, event_priority, _, _, symbol, market, event in sorted(events):
        key = (symbol, market)
        quantity = quantities.get(key, Decimal("0"))
        if event_priority == 1:
            event_quantity = Decimal(str(event.quantity))
            if event.transaction_type == "BUY":
                quantity += event_quantity
            elif event.transaction_type == "SELL":
                if raise_on_oversell and event_quantity > quantity:
                    raise ValueError(
                        "东方财富账户缺少期初持仓："
                        f"{event.symbol} ({event.market}) 在 {event.transaction_date} "
                        f"卖出 {event_quantity}，当时账户内仅有 {quantity}"
                    )
                quantity -= event_quantity
        elif event.action_type in {"STOCK_DIVIDEND", "BONUS_ISSUE"}:
            quantity += Decimal(str(event.shares_received or 0))
        elif event.action_type == "RIGHTS_ISSUE":
            quantity += Decimal(str(event.subscription_quantity or 0))
        elif event.action_type in {"STOCK_SPLIT", "REVERSE_SPLIT"} and event.split_ratio:
            try:
                old_shares, new_shares = event.split_ratio.split(":")
                quantity *= Decimal(new_shares) / Decimal(old_shares)
            except (InvalidOperation, ValueError, ZeroDivisionError):
                pass
        quantities[key] = quantity
    return quantities


def validate_account_position_history(
    db: Session,
    *,
    user_id: int,
    broker_account_id: int,
    through_date: date,
) -> None:
    calculate_account_position_quantities(
        db,
        user_id=user_id,
        broker_account_id=broker_account_id,
        snapshot_date=through_date,
        raise_on_oversell=True,
    )


def create_reconciliation_snapshot(
    db: Session,
    *,
    user_id: int,
    broker_account_id: int,
    import_batch_id: int,
    filename: str,
    context: EastmoneyStatementContext,
) -> ReconciliationSnapshot:
    computed = calculate_account_position_quantities(
        db,
        user_id=user_id,
        broker_account_id=broker_account_id,
        snapshot_date=context.period_end,
    )
    scope_market = statement_market(context.statement_type)
    scoped_positions = scoped_statement_positions(context)
    reported = {
        (position.symbol, position.market): position.quantity for position in scoped_positions
    }
    compared_keys = set(reported) | {
        key for key, quantity in computed.items() if key[1] == scope_market and quantity != 0
    }
    differences = [
        {
            "symbol": symbol,
            "market": market,
            "reported": str(reported.get((symbol, market), Decimal("0"))),
            "computed": str(computed.get((symbol, market), Decimal("0"))),
        }
        for symbol, market in sorted(compared_keys)
        if reported.get((symbol, market), Decimal("0"))
        != computed.get((symbol, market), Decimal("0"))
    ]
    status = "MATCHED" if not differences else "MISMATCHED"
    positions = [
        {
            "symbol": position.symbol,
            "name": position.name,
            "market": position.market,
            "quantity": str(position.quantity),
            "currency": position.currency,
        }
        for position in sorted(scoped_positions, key=lambda item: (item.market, item.symbol))
    ]
    cash_balances = {
        currency: str(amount) for currency, amount in sorted(context.cash_balances.items())
    }

    notes = (
        f"batch_id={import_batch_id}; scope={context.statement_type}; "
        "position quantities reconciled against account-scoped transactions; "
        "cash balance is a broker assertion only."
    )
    if differences:
        notes += f" Quantity differences: {differences[:20]}"
    snapshot = ReconciliationSnapshot(
        user_id=user_id,
        broker_account_id=broker_account_id,
        import_batch_id=import_batch_id,
        statement_scope=context.statement_type,
        snapshot_date=context.period_end,
        status=status,
        source_filename=filename,
        cash_balances=cash_balances,
        positions=positions,
        notes=notes,
    )
    db.add(snapshot)
    db.flush()
    # 统一走对账比对口径（覆盖上面初设的 status），与手工快照同一套 diff。
    from .reconciliation_service import run_and_store_compare

    run_and_store_compare(db, snapshot, commit=False)
    return snapshot


def import_eastmoney_statement(
    db: Session,
    user_id: int,
    contents: bytes,
    filename: str,
    broker_account_id: Optional[int] = None,
) -> Dict[str, Any]:
    if broker_account_id is None:
        raise ValueError("请选择东方财富券商账户后再正式导入")

    batch = start_import_batch(
        db,
        user_id=user_id,
        broker_account_id=broker_account_id,
        broker=BROKER_NAME,
        source_type="eastmoney_statement_pdf",
        filename=filename,
        contents=contents,
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
    )
    batch_id = batch.id
    total_rows = 0
    imported_source_rows = 0
    imported_transactions = 0
    imported_corporate_actions = 0
    imported_tax_adjustments = 0
    imported_cash_events = 0
    records_committed = False

    try:
        reject_unassigned_legacy_sources(db, user_id)
        parsed_rows, business_counts, total_rows, errors = parse_rows(contents, filename)
        apply_exclusions(parsed_rows, get_excluded_symbols(db, user_id))
        context = read_eastmoney_statement_context(contents)
        if any(flow.statement_type != context.statement_type for flow in parsed_rows):
            raise ValueError("东方财富 statement title and flow table scope do not match")
        batch.source_type = SOURCE_TYPE_BY_SCOPE[context.statement_type]
        set_import_batch_source_stats(
            batch,
            row_count=total_rows,
            period_start=context.period_start,
            period_end=context.period_end,
        )
        db.commit()
        db.refresh(batch)
        existing_hashes = get_existing_hashes(
            db,
            user_id,
            parsed_rows,
            broker_account_id=broker_account_id,
        )
        duplicate_hashes = set(existing_hashes)

        affected_symbols: set[tuple[str, str]] = set()
        transaction_ids: Dict[str, int] = {}
        corporate_action_ids: Dict[str, int] = {}
        cash_event_ids: Dict[str, int] = {}
        new_rows = [flow for flow in parsed_rows if flow.row_hash not in existing_hashes]

        for flow in new_rows:
            if flow.is_cash_dividend:
                market = infer_market(flow.security_code)
                if not market:
                    continue
                action = CorporateAction(
                    user_id=user_id,
                    broker_account_id=broker_account_id,
                    import_batch_id=batch_id,
                    symbol=flow.security_code,
                    name=flow.security_name,
                    market=market,
                    action_type="CASH_DIVIDEND",
                    ex_date=flow.trade_date,
                    payment_date=flow.trade_date,
                    dividend_per_share=(flow.trade_price if flow.trade_price > 0 else None),
                    total_dividend=flow.amount,
                    tax_withheld=Decimal("0"),
                    net_dividend=flow.amount,
                    currency=flow.currency,
                    notes=(
                        f"{BROKER_NAME}对账单; scope={flow.statement_type}; "
                        f"row={flow.source_row_number}; 业务={flow.business_name}; "
                        f"row_hash={flow.row_hash}"
                    ),
                )
                db.add(action)
                db.flush()
                corporate_action_ids[flow.row_hash] = action.id
                imported_corporate_actions += 1

        for flow in new_rows:
            if flow.is_trade:
                market = infer_market(flow.security_code)
                if not market:
                    continue
                transaction = Transaction(
                    user_id=user_id,
                    broker_account_id=broker_account_id,
                    import_batch_id=batch_id,
                    symbol=flow.security_code,
                    name=flow.security_name,
                    market=market,
                    transaction_type=flow.transaction_type,
                    quantity=abs(flow.trade_quantity),
                    price=flow.normalized_transaction_price,
                    fee=flow.normalized_transaction_fee,
                    transaction_date=flow.trade_date,
                    currency=flow.normalized_transaction_currency,
                    notes=(
                        f"{BROKER_NAME}对账单; scope={flow.statement_type}; "
                        f"row={flow.source_row_number}; 业务={flow.business_name}; "
                        f"source_cny_price={flow.trade_price}; "
                        f"source_cny_amount={flow.amount}; "
                        f"settlement_rate={flow.settlement_rate or ''}"
                    ),
                )
                db.add(transaction)
                db.flush()
                transaction_ids[flow.row_hash] = transaction.id
                affected_symbols.add((flow.security_code, market))
                imported_transactions += 1
            elif flow.is_cash_fee:
                cash_event = CashEvent(
                    user_id=user_id,
                    broker_account_id=broker_account_id,
                    event_type="FEE",
                    amount=abs(flow.amount),
                    currency=flow.currency,
                    event_date=flow.trade_date,
                    notes=(
                        f"{BROKER_NAME}港股通组合费; source={filename}; "
                        f"row={flow.source_row_number}; row_hash={flow.row_hash}"
                    ),
                )
                db.add(cash_event)
                db.flush()
                cash_event_ids[flow.row_hash] = cash_event.id
                imported_cash_events += 1

        for flow in new_rows:
            if not flow.is_dividend_tax:
                continue
            market = infer_market(flow.security_code)
            if not market:
                continue
            action = find_dividend_for_tax(
                db,
                user_id,
                flow,
                market,
                broker_account_id=broker_account_id,
            )
            if action:
                tax_amount = abs(flow.amount)
                action.tax_withheld = (action.tax_withheld or Decimal("0")) + tax_amount
                if action.total_dividend is not None:
                    action.net_dividend = max(
                        Decimal("0"), action.total_dividend - action.tax_withheld
                    )
                action.notes = (
                    f"{action.notes or ''}; {BROKER_NAME}红利税 "
                    f"row={flow.source_row_number}; row_hash={flow.row_hash}"
                ).strip("; ")
                corporate_action_ids[flow.row_hash] = action.id
                imported_tax_adjustments += 1
            else:
                errors.append(
                    f"row {flow.source_row_number}: no account-scoped dividend "
                    f"found for tax on {flow.security_code}"
                )

        for flow in new_rows:
            db.add(
                create_broker_fund_flow(
                    user_id=user_id,
                    broker_account_id=broker_account_id,
                    filename=filename,
                    flow=flow,
                    import_batch_id=batch_id,
                    transaction_id=transaction_ids.get(flow.row_hash),
                    corporate_action_id=corporate_action_ids.get(flow.row_hash),
                    cash_event_id=cash_event_ids.get(flow.row_hash),
                )
            )

        db.flush()
        validate_account_position_history(
            db,
            user_id=user_id,
            broker_account_id=broker_account_id,
            through_date=context.period_end,
        )
        snapshot = create_reconciliation_snapshot(
            db,
            user_id=user_id,
            broker_account_id=broker_account_id,
            import_batch_id=batch_id,
            filename=filename,
            context=context,
        )
        if snapshot.status != "MATCHED":
            raise ValueError(
                f"东方财富对账单持仓与账户交易记录不一致；整批未导入。 {snapshot.notes}"
            )

        recalculated_symbols = 0
        for symbol, market in affected_symbols:
            recalculate_holdings(db, user_id, symbol, market, commit=False)
            recalculated_symbols += 1

        try:
            db.commit()
        except IntegrityError as exc:
            raise ValueError("Duplicate 东方财富 statement flow detected during import") from exc
        records_committed = True

        result = build_import_result(
            filename=filename,
            total_rows=total_rows,
            parsed_rows=parsed_rows,
            context=context,
            business_counts=business_counts,
            existing_hashes=duplicate_hashes,
            imported_transactions=imported_transactions,
            imported_corporate_actions=imported_corporate_actions,
            imported_tax_adjustments=imported_tax_adjustments,
            imported_cash_events=imported_cash_events,
            affected_symbols=recalculated_symbols,
            errors=errors,
        )
        imported_source_rows = (
            db.query(BrokerFundFlow).filter(BrokerFundFlow.import_batch_id == batch_id).count()
        )
        result["archived_source_rows"] = imported_source_rows
        canonical_imported_count = (
            imported_transactions
            + imported_corporate_actions
            + imported_tax_adjustments
            + imported_cash_events
        )
        completed_batch = complete_import_batch(
            db,
            batch_id,
            result=result,
            imported_count=canonical_imported_count,
            archived_count=imported_source_rows,
        )
        result.update(
            {
                "import_batch_id": completed_batch.id,
                "broker_account_id": completed_batch.broker_account_id,
                "batch_status": completed_batch.status,
                "reconciliation_snapshot_id": snapshot.id,
                "reconciliation_status": snapshot.status,
            }
        )
        return result
    except Exception as exc:
        if records_committed:
            db.rollback()
            try:
                imported_source_rows = (
                    db.query(BrokerFundFlow)
                    .filter(BrokerFundFlow.import_batch_id == batch_id)
                    .count()
                )
            except Exception:
                imported_source_rows = 0
        canonical_imported_count = (
            imported_transactions
            + imported_corporate_actions
            + imported_tax_adjustments
            + imported_cash_events
            if records_committed
            else 0
        )
        fail_import_batch(
            db,
            batch_id,
            exc,
            records_committed=records_committed,
            row_count=total_rows,
            imported_count=canonical_imported_count,
            archived_count=imported_source_rows if records_committed else 0,
        )
        raise
