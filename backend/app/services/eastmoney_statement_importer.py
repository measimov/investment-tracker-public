from __future__ import annotations

import hashlib
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
from ..models.corporate_action import CorporateAction
from ..models.transaction import Transaction
from ..services.holding_service import recalculate_holdings


BROKER_NAME = "东方财富证券"
FLOW_HEADER = [
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
TRADE_BUSINESS_MAP = {
    "证券买入": "BUY",
    "证券卖出": "SELL",
}
DIVIDEND_BUSINESS_NAME = "红利入账"
DIVIDEND_TAX_BUSINESS_NAME = "股息红利差异扣税"
FUND_BUSINESS_NAMES = {"开放基金申购"}
HASH_FIELDS = [
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
HASH_DUPLICATE_OCCURRENCE_FIELD = "duplicate_occurrence"


@dataclass
class ParsedEastmoneyFlow:
    source_row_number: int
    row_hash: str
    security_code: str
    security_name: Optional[str]
    currency: str
    trade_date: date
    trade_price: Decimal
    trade_quantity: Decimal
    amount: Decimal
    cash_balance: Optional[Decimal]
    business_name: str
    stamp_tax: Decimal
    commission: Decimal
    transfer_fee: Decimal
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
    def total_fee(self) -> Decimal:
        return self.commission + self.stamp_tax + self.transfer_fee


def strip_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\ufeff", "").strip()


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


def normalize_hash_value(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, date):
        return value.isoformat()
    return strip_text(value)


def calculate_row_hash(values: Dict[str, Any]) -> str:
    fields = HASH_FIELDS
    if values.get(HASH_DUPLICATE_OCCURRENCE_FIELD):
        fields = HASH_FIELDS + [HASH_DUPLICATE_OCCURRENCE_FIELD]
    payload = "|".join(normalize_hash_value(values.get(field, "")) for field in fields)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_fund_symbol(symbol: str) -> bool:
    symbol = strip_text(symbol)
    return symbol.startswith(("15", "16", "18", "50", "51", "52", "56", "58"))


def infer_market(symbol: str) -> Optional[str]:
    symbol = strip_text(symbol)
    if not symbol:
        return None
    if is_fund_symbol(symbol):
        return None
    if re.fullmatch(r"\d{5}", symbol):
        return "港股"
    if symbol.startswith(("6", "0", "3")):
        return "A股"
    return None


def detect_skip_reason(business_name: str, security_code: str) -> Optional[str]:
    if business_name in FUND_BUSINESS_NAMES or is_fund_symbol(security_code):
        return "fund"
    if business_name in TRADE_BUSINESS_MAP:
        if not security_code or not infer_market(security_code):
            return "unsupported"
        return None
    if business_name in {DIVIDEND_BUSINESS_NAME, DIVIDEND_TAX_BUSINESS_NAME}:
        if not security_code or not infer_market(security_code):
            return "unsupported"
        return None
    return "unsupported"


def ensure_pdf_is_readable(contents: bytes) -> None:
    reader = PdfReader(io.BytesIO(contents))
    if reader.is_encrypted:
        raise ValueError("PDF is encrypted. Please decrypt it with qpdf before importing.")


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
                    header = [strip_text(column) for column in table[0]]
                    if header != FLOW_HEADER:
                        continue
                    for row in table[1:]:
                        row_number += 1
                        if len(row) != len(FLOW_HEADER):
                            continue
                        data_rows.append(
                            (row_number, {key: strip_text(value) for key, value in zip(FLOW_HEADER, row)})
                        )
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
    total_rows = 0

    for row_number, row in data_rows:
        total_rows += 1
        business_name = strip_text(row.get("买卖类别"))
        if not business_name:
            continue
        business_counts[business_name] = business_counts.get(business_name, 0) + 1

        trade_date = parse_trade_date(row.get("发生日期"))
        if trade_date is None:
            errors.append(f"row {row_number}: invalid trade date")
            continue

        security_code = strip_text(row.get("证券代码"))
        security_name = strip_text(row.get("证券名称")) or None
        trade_quantity = parse_decimal(row.get("成交数量"))
        trade_price = parse_decimal(row.get("成交价格"))
        amount = parse_decimal(row.get("总发生金额"))
        commission = parse_decimal(row.get("手续费"))
        stamp_tax = parse_decimal(row.get("印花税"))
        transfer_fee = parse_decimal(row.get("过户费"))
        cash_balance = parse_optional_decimal(row.get("资金余额"))
        skip_reason = detect_skip_reason(business_name, security_code)

        if business_name in TRADE_BUSINESS_MAP and skip_reason is None:
            if trade_quantity == 0 or trade_price <= 0:
                skip_reason = "invalid"

        hash_values = {
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
        base_row_hash = calculate_row_hash(hash_values)
        hash_occurrences[base_row_hash] = hash_occurrences.get(base_row_hash, 0) + 1
        row_hash = base_row_hash
        if hash_occurrences[base_row_hash] > 1:
            hash_values[HASH_DUPLICATE_OCCURRENCE_FIELD] = hash_occurrences[base_row_hash]
            row_hash = calculate_row_hash(hash_values)

        parsed_rows.append(
            ParsedEastmoneyFlow(
                source_row_number=row_number,
                row_hash=row_hash,
                security_code=security_code,
                security_name=security_name,
                currency="CNY",
                trade_date=trade_date,
                trade_price=trade_price,
                trade_quantity=trade_quantity,
                amount=amount,
                cash_balance=cash_balance,
                business_name=business_name,
                stamp_tax=stamp_tax,
                commission=commission,
                transfer_fee=transfer_fee,
                skip_reason=skip_reason,
            )
        )

    return parsed_rows, business_counts, total_rows, errors


def parse_rows(contents: bytes, filename: str) -> tuple[List[ParsedEastmoneyFlow], Dict[str, int], int, List[str]]:
    data_rows, _ = read_eastmoney_statement_rows(contents)
    return parse_table_rows(data_rows)


def eligible_rows(parsed_rows: List[ParsedEastmoneyFlow]) -> List[ParsedEastmoneyFlow]:
    return [
        flow
        for flow in parsed_rows
        if flow.is_trade or flow.is_cash_dividend or flow.is_dividend_tax
    ]


def flow_to_sample(flow: ParsedEastmoneyFlow, duplicate: bool) -> Dict[str, Any]:
    market = infer_market(flow.security_code) or ""
    mapped_type = flow.transaction_type or (
        "CASH_DIVIDEND" if flow.is_cash_dividend else "DIVIDEND_TAX" if flow.is_dividend_tax else flow.skip_reason or ""
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


def get_existing_hashes(db: Session, user_id: int, hashes: Iterable[str]) -> set[str]:
    hash_list = list(hashes)
    if not hash_list:
        return set()
    rows = db.query(BrokerFundFlow.row_hash).filter(
        BrokerFundFlow.user_id == user_id,
        BrokerFundFlow.row_hash.in_(hash_list),
    ).all()
    return {row[0] for row in rows}


def build_import_result(
    *,
    filename: str,
    total_rows: int,
    parsed_rows: List[ParsedEastmoneyFlow],
    business_counts: Dict[str, int],
    existing_hashes: set[str],
    imported_transactions: int,
    imported_corporate_actions: int,
    imported_tax_adjustments: int,
    affected_symbols: int,
    errors: List[str],
) -> Dict[str, Any]:
    rows = eligible_rows(parsed_rows)
    trade_rows = [flow for flow in rows if flow.is_trade]
    dividend_rows = [flow for flow in rows if flow.is_cash_dividend]
    tax_rows = [flow for flow in rows if flow.is_dividend_tax]

    seen_hashes: set[str] = set()
    duplicate_rows = []
    import_rows = []
    for flow in rows:
        if flow.row_hash in existing_hashes or flow.row_hash in seen_hashes:
            duplicate_rows.append(flow)
        else:
            import_rows.append(flow)
            seen_hashes.add(flow.row_hash)

    dates = [flow.trade_date for flow in parsed_rows]
    skip_counts = {
        "fund": len([flow for flow in parsed_rows if flow.skip_reason == "fund"]),
        "unsupported": len([flow for flow in parsed_rows if flow.skip_reason == "unsupported"]),
        "invalid": len([flow for flow in parsed_rows if flow.skip_reason == "invalid"]),
    }

    return {
        "broker": BROKER_NAME,
        "filename": filename,
        "total_rows": total_rows,
        "eligible_trade_rows": len(trade_rows),
        "eligible_dividend_rows": len(dividend_rows),
        "eligible_tax_rows": len(tax_rows),
        "imported_transactions": imported_transactions,
        "imported_corporate_actions": imported_corporate_actions,
        "imported_tax_adjustments": imported_tax_adjustments,
        "duplicate_rows": len(duplicate_rows),
        "skipped_non_trade_rows": total_rows - len(trade_rows) - len(dividend_rows) - len(tax_rows),
        "skipped_invalid_rows": skip_counts["invalid"] + len(errors),
        "skipped_option_rows": 0,
        "skipped_fx_rows": 0,
        "skipped_cash_rows": skip_counts["fund"],
        "skipped_unsupported_rows": skip_counts["unsupported"],
        "affected_symbols": affected_symbols,
        "date_start": min(dates).isoformat() if dates else None,
        "date_end": max(dates).isoformat() if dates else None,
        "business_counts": business_counts,
        "duplicate_samples": [flow_to_sample(flow, True) for flow in duplicate_rows[:10]],
        "import_samples": [flow_to_sample(flow, False) for flow in import_rows[:10]],
        "errors": errors[:50],
    }


def preview_eastmoney_statement(
    db: Session, user_id: int, contents: bytes, filename: str
) -> Dict[str, Any]:
    parsed_rows, business_counts, total_rows, errors = parse_rows(contents, filename)
    existing_hashes = get_existing_hashes(
        db, user_id, [flow.row_hash for flow in eligible_rows(parsed_rows)]
    )
    return build_import_result(
        filename=filename,
        total_rows=total_rows,
        parsed_rows=parsed_rows,
        business_counts=business_counts,
        existing_hashes=existing_hashes,
        imported_transactions=0,
        imported_corporate_actions=0,
        imported_tax_adjustments=0,
        affected_symbols=0,
        errors=errors,
    )


def create_broker_fund_flow(
    *,
    user_id: int,
    filename: str,
    flow: ParsedEastmoneyFlow,
    transaction_id: Optional[int] = None,
) -> BrokerFundFlow:
    return BrokerFundFlow(
        user_id=user_id,
        transaction_id=transaction_id,
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
        business_name=flow.business_name,
        stamp_tax=flow.stamp_tax,
        commission=flow.commission,
        transfer_fee=flow.transfer_fee,
    )


def find_dividend_for_tax(
    db: Session, user_id: int, flow: ParsedEastmoneyFlow, market: str
) -> Optional[CorporateAction]:
    candidates = db.query(CorporateAction).filter(
        CorporateAction.user_id == user_id,
        CorporateAction.symbol == flow.security_code,
        CorporateAction.market == market,
        CorporateAction.action_type == "CASH_DIVIDEND",
        CorporateAction.currency == flow.currency,
        CorporateAction.ex_date <= flow.trade_date,
    ).order_by(CorporateAction.ex_date.desc(), CorporateAction.id.desc()).limit(5).all()
    return candidates[0] if candidates else None


def import_eastmoney_statement(
    db: Session, user_id: int, contents: bytes, filename: str
) -> Dict[str, Any]:
    parsed_rows, business_counts, total_rows, errors = parse_rows(contents, filename)
    existing_hashes = get_existing_hashes(
        db, user_id, [flow.row_hash for flow in eligible_rows(parsed_rows)]
    )
    duplicate_hashes = set(existing_hashes)

    imported_transactions = 0
    imported_corporate_actions = 0
    imported_tax_adjustments = 0
    affected_symbols: set[tuple[str, str]] = set()
    pending_tax_flows: List[ParsedEastmoneyFlow] = []

    for flow in parsed_rows:
        if not flow.is_trade and not flow.is_cash_dividend and not flow.is_dividend_tax:
            continue
        if flow.row_hash in existing_hashes:
            continue

        market = infer_market(flow.security_code)
        if not market:
            continue

        if flow.is_cash_dividend:
            action = CorporateAction(
                user_id=user_id,
                symbol=flow.security_code,
                name=flow.security_name,
                market=market,
                action_type="CASH_DIVIDEND",
                ex_date=flow.trade_date,
                payment_date=flow.trade_date,
                dividend_per_share=flow.trade_price if flow.trade_price > 0 else None,
                total_dividend=flow.amount,
                tax_withheld=Decimal("0"),
                net_dividend=flow.amount,
                currency=flow.currency,
                notes=(
                    f"{BROKER_NAME}股票明细对账单; row={flow.source_row_number}; "
                    f"业务={flow.business_name}; row_hash={flow.row_hash}"
                ),
            )
            db.add(action)
            db.flush()
            db.add(create_broker_fund_flow(user_id=user_id, filename=filename, flow=flow))
            existing_hashes.add(flow.row_hash)
            imported_corporate_actions += 1
            continue

        if flow.is_dividend_tax:
            action = find_dividend_for_tax(db, user_id, flow, market)
            if not action:
                pending_tax_flows.append(flow)
                continue
            tax_amount = abs(flow.amount)
            action.tax_withheld = (action.tax_withheld or Decimal("0")) + tax_amount
            if action.total_dividend is not None:
                action.net_dividend = max(Decimal("0"), action.total_dividend - action.tax_withheld)
            action.notes = (
                f"{action.notes or ''}; {BROKER_NAME}红利税 row={flow.source_row_number}; "
                f"row_hash={flow.row_hash}"
            ).strip("; ")
            db.add(create_broker_fund_flow(user_id=user_id, filename=filename, flow=flow))
            existing_hashes.add(flow.row_hash)
            imported_tax_adjustments += 1
            continue

        transaction_type = flow.transaction_type
        if not transaction_type:
            continue

        transaction = Transaction(
            user_id=user_id,
            symbol=flow.security_code,
            name=flow.security_name,
            market=market,
            transaction_type=transaction_type,
            quantity=abs(flow.trade_quantity),
            price=flow.trade_price,
            fee=flow.total_fee,
            transaction_date=flow.trade_date,
            currency=flow.currency,
            notes=(
                f"{BROKER_NAME}股票明细对账单; row={flow.source_row_number}; "
                f"业务={flow.business_name}; 金额={flow.amount}"
            ),
        )
        db.add(transaction)
        db.flush()
        db.add(
            create_broker_fund_flow(
                user_id=user_id, filename=filename, flow=flow, transaction_id=transaction.id
            )
        )
        existing_hashes.add(flow.row_hash)
        affected_symbols.add((flow.security_code, market))
        imported_transactions += 1

    for flow in pending_tax_flows:
        if flow.row_hash in existing_hashes:
            continue
        market = infer_market(flow.security_code)
        if not market:
            continue
        action = find_dividend_for_tax(db, user_id, flow, market)
        if not action:
            continue
        tax_amount = abs(flow.amount)
        action.tax_withheld = (action.tax_withheld or Decimal("0")) + tax_amount
        if action.total_dividend is not None:
            action.net_dividend = max(Decimal("0"), action.total_dividend - action.tax_withheld)
        action.notes = (
            f"{action.notes or ''}; {BROKER_NAME}红利税 row={flow.source_row_number}; "
            f"row_hash={flow.row_hash}"
        ).strip("; ")
        db.add(create_broker_fund_flow(user_id=user_id, filename=filename, flow=flow))
        existing_hashes.add(flow.row_hash)
        imported_tax_adjustments += 1

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ValueError("Duplicate 东方财富 statement flow detected during import")

    recalculated_symbols = 0
    for symbol, market in affected_symbols:
        try:
            recalculate_holdings(db, user_id, symbol, market)
            recalculated_symbols += 1
        except ValueError as exc:
            errors.append(f"{symbol} {market}: {exc}")

    return build_import_result(
        filename=filename,
        total_rows=total_rows,
        parsed_rows=parsed_rows,
        business_counts=business_counts,
        existing_hashes=duplicate_hashes,
        imported_transactions=imported_transactions,
        imported_corporate_actions=imported_corporate_actions,
        imported_tax_adjustments=imported_tax_adjustments,
        affected_symbols=recalculated_symbols,
        errors=errors,
    )
