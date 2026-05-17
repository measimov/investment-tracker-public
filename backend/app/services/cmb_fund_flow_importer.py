from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models.broker_fund_flow import BrokerFundFlow
from ..models.corporate_action import CorporateAction
from ..models.transaction import Transaction
from ..services.holding_service import recalculate_holdings


BROKER_NAME = "招商证券"
TRADE_BUSINESS_MAP = {
    "证券买入": "BUY",
    "证券卖出": "SELL",
}
DIVIDEND_BUSINESS_NAMES = {"股息入账", "产品红利发放"}
TAX_BUSINESS_NAME = "股息红利税补缴"
CASH_MANAGEMENT_SYMBOLS = {"880013"}
HASH_FIELDS = [
    "broker",
    "trade_date",
    "serial_number",
    "business_name",
    "security_code",
    "security_name",
    "currency",
    "trade_price",
    "trade_quantity",
    "amount",
    "contract_number",
    "shareholder_code",
]


@dataclass
class ParsedFlow:
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
    remaining_quantity: Optional[Decimal]
    contract_number: Optional[str]
    serial_number: Optional[str]
    business_name: str
    stamp_tax: Decimal
    commission: Decimal
    handling_fee: Decimal
    management_fee: Decimal
    settlement_fee: Decimal
    transfer_fee: Decimal
    other_fee: Decimal
    shareholder_code: Optional[str]
    notes: Optional[str]

    @property
    def transaction_type(self) -> Optional[str]:
        return TRADE_BUSINESS_MAP.get(self.business_name)

    @property
    def is_cash_dividend(self) -> bool:
        return (
            self.business_name in DIVIDEND_BUSINESS_NAMES
            and bool(self.security_code)
            and self.security_code not in CASH_MANAGEMENT_SYMBOLS
            and self.amount > 0
        )

    @property
    def is_dividend_tax(self) -> bool:
        return (
            self.business_name == TAX_BUSINESS_NAME
            and bool(self.security_code)
            and self.amount < 0
        )

    @property
    def total_fee(self) -> Decimal:
        return (
            self.stamp_tax
            + self.commission
            + self.handling_fee
            + self.management_fee
            + self.settlement_fee
            + self.transfer_fee
            + self.other_fee
        )


def strip_bom(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).replace("\ufeff", "").strip()
    if text.endswith(".0") and text.replace(".", "", 1).isdigit():
        text = text[:-2]
    return text


def parse_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    text = strip_bom(value).replace(",", "")
    if not text or text == "---":
        return default
    text = re.sub(r"^(人民币|港币|美元)", "", text)
    try:
        return Decimal(text)
    except InvalidOperation:
        return default


def parse_optional_decimal(value: Any) -> Optional[Decimal]:
    text = strip_bom(value).replace(",", "")
    if not text or text == "---":
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_trade_date(value: Any) -> Optional[date]:
    text = strip_bom(value)
    parsed = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
    if pd.isna(parsed):
        parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def normalize_currency(value: str) -> str:
    mapping = {"人民币": "CNY", "港币": "HKD", "美元": "USD"}
    return mapping.get(value, value or "CNY")


def currency_from_price(value: Any) -> Optional[str]:
    text = strip_bom(value)
    if text.startswith("港币"):
        return "HKD"
    if text.startswith("美元"):
        return "USD"
    if text.startswith("人民币"):
        return "CNY"
    return None


def infer_market(symbol: str, currency: str, shareholder_code: Optional[str]) -> str:
    symbol = symbol.strip()
    shareholder_code = shareholder_code or ""
    if symbol.startswith(("200", "900")):
        return "B股"
    if currency == "HKD" or shareholder_code.startswith("H") or (symbol.isdigit() and len(symbol) == 5):
        return "港股"
    if currency == "USD":
        return "美股"
    if symbol.startswith(("6", "5", "9")):
        return "A股"
    if symbol.startswith(("0", "1", "2", "3")):
        return "A股"
    return "其他"


def normalize_hash_value(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, date):
        return value.isoformat()
    return strip_bom(value)


def calculate_row_hash(values: Dict[str, Any]) -> str:
    payload = "|".join(normalize_hash_value(values.get(field, "")) for field in HASH_FIELDS)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def read_cmb_fund_flow(contents: bytes, filename: str) -> pd.DataFrame:
    suffix = filename.lower().rsplit(".", 1)[-1]
    engine = "xlrd" if suffix == "xls" else "openpyxl"
    df = pd.read_excel(io.BytesIO(contents), engine=engine)
    df.columns = [strip_bom(column) for column in df.columns]
    return df.dropna(how="all")


def parse_rows(contents: bytes, filename: str) -> tuple[List[ParsedFlow], Dict[str, int], int, List[str]]:
    df = read_cmb_fund_flow(contents, filename)
    required_columns = {
        "证券代码",
        "证券名称",
        "币种",
        "成交日期",
        "成交价格",
        "成交数量",
        "发生金额",
        "流水号",
        "业务名称",
    }
    missing_columns = sorted(required_columns - set(df.columns))
    if missing_columns:
        raise ValueError(f"Missing required columns: {', '.join(missing_columns)}")

    parsed_rows: List[ParsedFlow] = []
    errors: List[str] = []
    business_counts: Dict[str, int] = {}

    for index, row in df.iterrows():
        row_number = int(index) + 2
        business_name = strip_bom(row.get("业务名称"))
        if not business_name:
            continue
        business_counts[business_name] = business_counts.get(business_name, 0) + 1

        trade_date = parse_trade_date(row.get("成交日期"))
        if trade_date is None:
            errors.append(f"row {row_number}: invalid trade date")
            continue

        security_code = strip_bom(row.get("证券代码"))
        security_name = strip_bom(row.get("证券名称")) or None
        currency = currency_from_price(row.get("成交价格")) or normalize_currency(strip_bom(row.get("币种")))
        trade_price = parse_decimal(row.get("成交价格"))
        trade_quantity = parse_decimal(row.get("成交数量"))
        amount = parse_decimal(row.get("发生金额"))
        contract_number = strip_bom(row.get("合同编号")) or None
        serial_number = strip_bom(row.get("流水号")) or None
        shareholder_code = strip_bom(row.get("股东代码")) or None

        hash_values = {
            "broker": BROKER_NAME,
            "trade_date": trade_date,
            "serial_number": serial_number,
            "business_name": business_name,
            "security_code": security_code,
            "security_name": security_name,
            "currency": currency,
            "trade_price": trade_price,
            "trade_quantity": trade_quantity,
            "amount": amount,
            "contract_number": contract_number,
            "shareholder_code": shareholder_code,
        }

        parsed_rows.append(
            ParsedFlow(
                source_row_number=row_number,
                row_hash=calculate_row_hash(hash_values),
                security_code=security_code,
                security_name=security_name,
                currency=currency,
                trade_date=trade_date,
                trade_price=trade_price,
                trade_quantity=trade_quantity,
                amount=amount,
                cash_balance=parse_optional_decimal(row.get("资金余额")),
                remaining_quantity=parse_optional_decimal(row.get("剩余数量")),
                contract_number=contract_number,
                serial_number=serial_number,
                business_name=business_name,
                stamp_tax=parse_decimal(row.get("印花税")),
                commission=parse_decimal(row.get("佣金")),
                handling_fee=parse_decimal(row.get("经手费")),
                management_fee=parse_decimal(row.get("证管费")),
                settlement_fee=parse_decimal(row.get("结算费")),
                transfer_fee=parse_decimal(row.get("过户费")),
                other_fee=parse_decimal(row.get("其他费用")),
                shareholder_code=shareholder_code,
                notes=strip_bom(row.get("备注")) or None,
            )
        )

    return parsed_rows, business_counts, len(df), errors


def flow_to_sample(flow: ParsedFlow, duplicate: bool) -> Dict[str, Any]:
    market = infer_market(flow.security_code, flow.currency, flow.shareholder_code)
    mapped_type = flow.transaction_type or ("CASH_DIVIDEND" if flow.is_cash_dividend else "DIVIDEND_TAX" if flow.is_dividend_tax else "")
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
    parsed_rows: List[ParsedFlow],
    business_counts: Dict[str, int],
    existing_hashes: set[str],
    imported_transactions: int,
    imported_corporate_actions: int,
    imported_tax_adjustments: int,
    affected_symbols: int,
    errors: List[str],
) -> Dict[str, Any]:
    trade_rows = [flow for flow in parsed_rows if flow.transaction_type]
    dividend_rows = [flow for flow in parsed_rows if flow.is_cash_dividend]
    tax_rows = [flow for flow in parsed_rows if flow.is_dividend_tax]
    eligible_trade_rows = [
        flow
        for flow in trade_rows
        if flow.security_code and flow.trade_quantity != 0 and flow.trade_price > 0
    ]
    eligible_rows = eligible_trade_rows + dividend_rows + tax_rows
    duplicate_rows = [flow for flow in eligible_rows if flow.row_hash in existing_hashes]
    import_rows = [flow for flow in eligible_rows if flow.row_hash not in existing_hashes]
    dates = [flow.trade_date for flow in parsed_rows]

    return {
        "broker": BROKER_NAME,
        "filename": filename,
        "total_rows": total_rows,
        "eligible_trade_rows": len(eligible_trade_rows),
        "eligible_dividend_rows": len(dividend_rows),
        "eligible_tax_rows": len(tax_rows),
        "imported_transactions": imported_transactions,
        "imported_corporate_actions": imported_corporate_actions,
        "imported_tax_adjustments": imported_tax_adjustments,
        "duplicate_rows": len(duplicate_rows),
        "skipped_non_trade_rows": total_rows - len(trade_rows) - len(dividend_rows) - len(tax_rows),
        "skipped_invalid_rows": len(trade_rows) - len(eligible_trade_rows) + len(errors),
        "affected_symbols": affected_symbols,
        "date_start": min(dates).isoformat() if dates else None,
        "date_end": max(dates).isoformat() if dates else None,
        "business_counts": business_counts,
        "duplicate_samples": [flow_to_sample(flow, True) for flow in duplicate_rows[:10]],
        "import_samples": [flow_to_sample(flow, False) for flow in import_rows[:10]],
        "errors": errors[:50],
    }


def preview_cmb_fund_flow(db: Session, user_id: int, contents: bytes, filename: str) -> Dict[str, Any]:
    parsed_rows, business_counts, total_rows, errors = parse_rows(contents, filename)
    existing_hashes = get_existing_hashes(
        db,
        user_id,
        [
            flow.row_hash
            for flow in parsed_rows
            if flow.transaction_type or flow.is_cash_dividend or flow.is_dividend_tax
        ],
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
    flow: ParsedFlow,
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
        remaining_quantity=flow.remaining_quantity,
        contract_number=flow.contract_number,
        serial_number=flow.serial_number,
        business_name=flow.business_name,
        stamp_tax=flow.stamp_tax,
        commission=flow.commission,
        handling_fee=flow.handling_fee,
        management_fee=flow.management_fee,
        settlement_fee=flow.settlement_fee,
        transfer_fee=flow.transfer_fee,
        other_fee=flow.other_fee,
        shareholder_code=flow.shareholder_code,
        notes=flow.notes,
    )


def find_dividend_for_tax(db: Session, user_id: int, flow: ParsedFlow, market: str) -> Optional[CorporateAction]:
    candidates = db.query(CorporateAction).filter(
        CorporateAction.user_id == user_id,
        CorporateAction.symbol == flow.security_code,
        CorporateAction.market == market,
        CorporateAction.action_type == "CASH_DIVIDEND",
        CorporateAction.currency == flow.currency,
        CorporateAction.ex_date <= flow.trade_date,
    ).order_by(CorporateAction.ex_date.desc(), CorporateAction.id.desc()).limit(5).all()
    return candidates[0] if candidates else None


def import_cmb_fund_flow(db: Session, user_id: int, contents: bytes, filename: str) -> Dict[str, Any]:
    parsed_rows, business_counts, total_rows, errors = parse_rows(contents, filename)
    existing_hashes = get_existing_hashes(
        db,
        user_id,
        [
            flow.row_hash
            for flow in parsed_rows
            if flow.transaction_type or flow.is_cash_dividend or flow.is_dividend_tax
        ],
    )
    duplicate_hashes = set(existing_hashes)

    imported_count = 0
    imported_corporate_actions = 0
    imported_tax_adjustments = 0
    affected_symbols: set[tuple[str, str]] = set()

    for flow in parsed_rows:
        if not flow.transaction_type and not flow.is_cash_dividend and not flow.is_dividend_tax:
            continue
        if not flow.security_code:
            continue
        if flow.row_hash in existing_hashes:
            continue

        market = infer_market(flow.security_code, flow.currency, flow.shareholder_code)

        if flow.is_cash_dividend:
            action = CorporateAction(
                user_id=user_id,
                symbol=flow.security_code,
                name=flow.security_name,
                market=market,
                action_type="CASH_DIVIDEND",
                ex_date=flow.trade_date,
                payment_date=flow.trade_date,
                total_dividend=flow.amount,
                tax_withheld=Decimal("0"),
                net_dividend=flow.amount,
                currency=flow.currency,
                notes=(
                    f"{BROKER_NAME}资金流水; 流水号={flow.serial_number or ''}; "
                    f"合同编号={flow.contract_number or ''}; 业务={flow.business_name}; "
                    f"row_hash={flow.row_hash}"
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
                continue
            tax_amount = abs(flow.amount)
            action.tax_withheld = (action.tax_withheld or Decimal("0")) + tax_amount
            if action.total_dividend is not None:
                action.net_dividend = max(Decimal("0"), action.total_dividend - action.tax_withheld)
            action.notes = (
                f"{action.notes or ''}; {BROKER_NAME}红利税补缴 "
                f"流水号={flow.serial_number or ''}; row_hash={flow.row_hash}"
            ).strip("; ")
            db.add(create_broker_fund_flow(user_id=user_id, filename=filename, flow=flow))
            existing_hashes.add(flow.row_hash)
            imported_tax_adjustments += 1
            continue

        if flow.trade_quantity == 0 or flow.trade_price <= 0:
            continue

        transaction = Transaction(
            user_id=user_id,
            symbol=flow.security_code,
            name=flow.security_name,
            market=market,
            transaction_type=flow.transaction_type,
            quantity=abs(flow.trade_quantity),
            price=flow.trade_price,
            fee=flow.total_fee,
            transaction_date=flow.trade_date,
            currency=flow.currency,
            notes=(
                f"{BROKER_NAME}资金流水; 流水号={flow.serial_number or ''}; "
                f"合同编号={flow.contract_number or ''}; 业务={flow.business_name}"
            ),
        )
        db.add(transaction)
        db.flush()

        db.add(create_broker_fund_flow(user_id=user_id, filename=filename, flow=flow, transaction_id=transaction.id))
        existing_hashes.add(flow.row_hash)
        affected_symbols.add((flow.security_code, market))
        imported_count += 1

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ValueError("Duplicate broker fund flow detected during import")

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
        imported_transactions=imported_count,
        imported_corporate_actions=imported_corporate_actions,
        imported_tax_adjustments=imported_tax_adjustments,
        affected_symbols=recalculated_symbols,
        errors=errors,
    )
