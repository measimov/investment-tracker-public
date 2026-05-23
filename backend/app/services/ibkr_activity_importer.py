from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models.corporate_action import CorporateAction
from ..models.ibkr_activity_flow import IbkrActivityFlow
from ..models.transaction import Transaction
from ..core.logging import get_app_logger
from ..services.holding_service import recalculate_holdings
from ..services.stock_price_service import (
    to_tushare_a_code,
    to_tushare_hk_code,
    tushare_query,
)


BROKER_NAME = "IBKR"
BASE_CURRENCY_FALLBACK = "USD"
TRADE_TYPES = {"买": "BUY", "卖": "SELL"}
EXERCISE_TYPES = {"行权", "被行权"}
DIVIDEND_TYPE = "股息"
WITHHOLDING_TAX_TYPE = "外国预扣税"
CASH_ACTIVITY_TYPES = {"贷方利息", "借方利息", "存款", "调整"}
FX_ACTIVITY_TYPE = "外汇交易组成部分"
OPTION_MONTHS = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"
SYNTHETIC_RELISTING_MARKER = "synthetic_relisting_transfer"
KNOWN_RELISTINGS = [
    {
        "old_symbol": "01263",
        "old_market": "港股",
        "old_currency": "HKD",
        "new_symbol": "PCT",
        "new_market": "新加坡股",
        "new_currency": "SGD",
        "name": "柏能集团",
    }
]
KNOWN_SECURITY_NAMES = {
    ("01263", "港股"): "柏能集团",
    ("PCT", "新加坡股"): "柏能集团",
}
HASH_FIELDS = [
    "broker",
    "trade_date",
    "account",
    "description",
    "activity_type",
    "raw_symbol",
    "symbol",
    "quantity",
    "price",
    "price_currency",
    "gross_amount",
    "commission",
    "net_amount",
]
HASH_DUPLICATE_OCCURRENCE_FIELD = "duplicate_occurrence"

logger = get_app_logger(__name__)


@dataclass
class ParsedIbkrFlow:
    source_row_number: int
    row_hash: str
    account: Optional[str]
    trade_date: date
    description: Optional[str]
    activity_type: str
    raw_symbol: str
    symbol: Optional[str]
    name: Optional[str]
    market: Optional[str]
    quantity: Optional[Decimal]
    price: Optional[Decimal]
    price_currency: Optional[str]
    base_currency: str
    gross_amount: Optional[Decimal]
    commission: Optional[Decimal]
    net_amount: Optional[Decimal]
    fee_in_price_currency: Optional[Decimal]
    skip_reason: Optional[str] = None

    @property
    def transaction_type(self) -> Optional[str]:
        if self.activity_type in TRADE_TYPES:
            return TRADE_TYPES[self.activity_type]
        if self.activity_type in EXERCISE_TYPES and self.quantity is not None:
            if self.quantity > 0:
                return "BUY"
            if self.quantity < 0:
                return "SELL"
        return None

    @property
    def is_trade(self) -> bool:
        return self.transaction_type is not None and self.skip_reason is None

    @property
    def is_cash_dividend(self) -> bool:
        return (
            self.activity_type == DIVIDEND_TYPE
            and self.symbol is not None
            and self.gross_amount is not None
            and self.gross_amount > 0
            and self.skip_reason is None
        )

    @property
    def is_withholding_tax(self) -> bool:
        return (
            self.activity_type == WITHHOLDING_TAX_TYPE
            and self.symbol is not None
            and self.gross_amount is not None
            and self.gross_amount < 0
            and self.skip_reason is None
        )


def strip_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\ufeff", "").strip()


def parse_decimal(value: Any) -> Optional[Decimal]:
    text = strip_text(value).replace(",", "")
    if not text or text == "-":
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_trade_date(value: Any) -> Optional[date]:
    text = strip_text(value)
    try:
        return date.fromisoformat(text)
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


def detect_encoding(contents: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            contents.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return "utf-8-sig"


def read_ibkr_transaction_history(
    contents: bytes,
) -> tuple[List[tuple[int, Dict[str, str]]], str, int, List[str]]:
    text = contents.decode(detect_encoding(contents))
    reader = csv.reader(io.StringIO(text))
    header: Optional[List[str]] = None
    data_rows: List[tuple[int, Dict[str, str]]] = []
    total_rows = 0
    base_currency = BASE_CURRENCY_FALLBACK
    errors: List[str] = []

    for row_number, row in enumerate(reader, start=1):
        if not row:
            continue
        if len(row) >= 4 and row[0] == "总结" and row[1] == "Data" and row[2] == "基础货币":
            base_currency = strip_text(row[3]) or BASE_CURRENCY_FALLBACK
        if len(row) >= 2 and row[0] == "Transaction History":
            if row[1] == "Header":
                header = [strip_text(col) for col in row[2:]]
            elif row[1] == "Data":
                total_rows += 1
                if header is None:
                    errors.append(f"row {row_number}: Transaction History data before header")
                    continue
                values = row[2:]
                data_rows.append((row_number, dict(zip(header, values))))

    required = {
        "日期",
        "账户",
        "说明",
        "交易类型",
        "代码",
        "数量",
        "价格",
        "Price Currency",
        "总额",
        "佣金",
        "净额",
    }
    if header is None:
        raise ValueError("Missing Transaction History section")
    missing = sorted(required - set(header))
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")
    return data_rows, base_currency, total_rows, errors


def is_option_symbol(raw_symbol: str, description: Optional[str]) -> bool:
    raw_symbol = strip_text(raw_symbol).upper()
    description = strip_text(description).upper()
    compact = raw_symbol.replace(" ", "")
    combined = f"{raw_symbol} {description}"

    # US OCC option symbol, e.g. PYPL  260417P00040000.
    if re.search(r"[A-Z]{1,6}\d{6}[CP]\d{8}$", compact):
        return True

    # IBKR HK options commonly use underlying + DDMMMYY + strike + C/P in the
    # description, e.g. 883 29JAN26 20 P.
    if re.search(
        rf"\b\d{{1,5}}\s+\d{{2}}(?:{OPTION_MONTHS})\d{{2}}\s+\d+(?:\.\d+)?\s+[CP]\b", combined
    ):
        return True

    # IBKR may also put the contract alias in the Code column, e.g.
    # CNC JAN26 20 P or POP APR26 155 P.
    if re.search(
        rf"\b[A-Z]{{1,5}}\s+(?:{OPTION_MONTHS})\d{{2}}\s+\d+(?:\.\d+)?\s+[CP]\b", combined
    ):
        return True

    # Generic US/HK description format, e.g. FXE 20MAR26 107 P.
    if re.search(
        rf"\b[A-Z0-9]{{1,6}}\s+\d{{2}}(?:{OPTION_MONTHS})\d{{2}}\s+\d+(?:\.\d+)?\s+[CP]\b", combined
    ):
        return True
    return False


def normalize_symbol(raw_symbol: str, price_currency: Optional[str]) -> Optional[str]:
    raw_symbol = raw_symbol.strip()
    if not raw_symbol or raw_symbol == "-":
        return None
    if price_currency == "HKD" and raw_symbol.isdigit():
        return raw_symbol.zfill(5)
    return raw_symbol


def infer_market(
    raw_symbol: str, symbol: Optional[str], price_currency: Optional[str]
) -> Optional[str]:
    if not symbol:
        return None
    if price_currency == "HKD" and raw_symbol.isdigit():
        return "港股"
    if price_currency == "USD" and re.fullmatch(r"[A-Z.]+", symbol):
        return "美股"
    if price_currency == "SGD":
        return "新加坡股"
    return None


def dividend_symbol_and_market(raw_symbol: str) -> tuple[Optional[str], Optional[str]]:
    if raw_symbol.isdigit():
        return raw_symbol.zfill(5), "港股"
    if raw_symbol and raw_symbol != "-":
        return raw_symbol, "美股"
    return None, None


def trade_fee_in_price_currency(
    *,
    quantity: Optional[Decimal],
    price: Optional[Decimal],
    gross_amount: Optional[Decimal],
    net_amount: Optional[Decimal],
    commission: Optional[Decimal],
    price_currency: Optional[str],
    base_currency: str,
) -> Decimal:
    cost_base = Decimal("0")
    if gross_amount is not None and net_amount is not None:
        cost_base = abs(net_amount - gross_amount)
    elif commission is not None:
        cost_base = abs(commission)

    if cost_base == 0:
        return Decimal("0")
    if price_currency == base_currency:
        return cost_base
    if not quantity or not price or not gross_amount:
        return abs(commission or Decimal("0"))
    trade_value = abs(quantity * price)
    gross_abs = abs(gross_amount)
    if trade_value == 0 or gross_abs == 0:
        return abs(commission or Decimal("0"))
    return cost_base * trade_value / gross_abs


def parse_dividend_currency(description: Optional[str]) -> Optional[str]:
    match = re.search(r"现金红利\s+([A-Z]{3})\s+", description or "")
    return match.group(1) if match else None


def lookup_tushare_security_name(symbol: str, market: Optional[str]) -> Optional[str]:
    """Resolve a display name from Tushare instead of trusting IBKR descriptions."""
    if not symbol or not market:
        return None
    if (symbol, market) in KNOWN_SECURITY_NAMES:
        return KNOWN_SECURITY_NAMES[(symbol, market)]

    try:
        if market in {"A股", "B股"}:
            df = tushare_query(
                "stock_basic",
                ts_code=to_tushare_a_code(symbol),
                fields="ts_code,name",
            )
        elif market == "港股":
            df = tushare_query(
                "hk_basic",
                ts_code=to_tushare_hk_code(symbol),
                fields="ts_code,name,fullname",
            )
        elif market == "美股":
            df = tushare_query(
                "us_basic",
                ts_code=str(symbol or "").strip().upper(),
                fields="ts_code,name",
            )
        else:
            return None
    except Exception as exc:
        logger.warning("Tushare name lookup failed for %s %s: %s", symbol, market, str(exc)[:200])
        return KNOWN_SECURITY_NAMES.get((symbol, market))

    if df is None or df.empty:
        return None

    row = df.iloc[0]
    for column in ("name", "fullname"):
        value = strip_text(row.get(column))
        if value:
            return value
    return KNOWN_SECURITY_NAMES.get((symbol, market))


def enrich_security_names(parsed_rows: List[ParsedIbkrFlow]) -> None:
    name_cache: Dict[tuple[str, str], Optional[str]] = {}
    targets = {
        (flow.symbol, flow.market)
        for flow in parsed_rows
        if (flow.is_trade or flow.is_cash_dividend or flow.is_withholding_tax)
        and flow.symbol
        and flow.market
    }

    for symbol, market in sorted(targets):
        name_cache[(symbol, market)] = lookup_tushare_security_name(
            symbol, market
        ) or KNOWN_SECURITY_NAMES.get((symbol, market))

    for flow in parsed_rows:
        if flow.symbol and flow.market:
            flow.name = name_cache.get((flow.symbol, flow.market))


def apply_exercise_import_policy(parsed_rows: List[ParsedIbkrFlow]) -> None:
    """Import option exercise/assignment only when it preserves long-only holdings."""
    quantities: Dict[tuple[str, str], Decimal] = {}
    sorted_rows = sorted(
        parsed_rows,
        key=lambda flow: (
            flow.trade_date,
            flow.source_row_number,
        ),
    )

    for flow in sorted_rows:
        if flow.skip_reason is not None or not flow.symbol or not flow.market:
            continue
        transaction_type = flow.transaction_type
        if not transaction_type or flow.quantity is None:
            continue

        key = (flow.symbol, flow.market)
        current_quantity = quantities.get(key, Decimal("0"))
        flow_quantity = abs(flow.quantity)

        if flow.activity_type in EXERCISE_TYPES and transaction_type == "SELL":
            if flow_quantity > current_quantity:
                flow.skip_reason = "option"
                continue

        if transaction_type == "BUY":
            quantities[key] = current_quantity + flow_quantity
        elif transaction_type == "SELL":
            quantities[key] = max(Decimal("0"), current_quantity - flow_quantity)


def parse_rows(
    contents: bytes, filename: str
) -> tuple[List[ParsedIbkrFlow], Dict[str, int], int, List[str]]:
    data_rows, base_currency, total_rows, errors = read_ibkr_transaction_history(contents)
    parsed_rows: List[ParsedIbkrFlow] = []
    business_counts: Dict[str, int] = {}
    hash_occurrences: Dict[str, int] = {}

    for row_number, row in data_rows:
        activity_type = strip_text(row.get("交易类型"))
        if not activity_type:
            continue
        business_counts[activity_type] = business_counts.get(activity_type, 0) + 1

        trade_date = parse_trade_date(row.get("日期"))
        if trade_date is None:
            errors.append(f"row {row_number}: invalid trade date")
            continue

        account = strip_text(row.get("账户")) or None
        description = strip_text(row.get("说明")) or None
        raw_symbol = strip_text(row.get("代码"))
        quantity = parse_decimal(row.get("数量"))
        price = parse_decimal(row.get("价格"))
        price_currency = strip_text(row.get("Price Currency")) or None
        if price_currency == "-":
            price_currency = None
        gross_amount = parse_decimal(row.get("总额"))
        commission = parse_decimal(row.get("佣金"))
        net_amount = parse_decimal(row.get("净额"))

        symbol = normalize_symbol(raw_symbol, price_currency)
        market = infer_market(raw_symbol, symbol, price_currency)
        skip_reason = None

        if activity_type in {DIVIDEND_TYPE, WITHHOLDING_TAX_TYPE}:
            symbol, market = dividend_symbol_and_market(raw_symbol)
            price_currency = base_currency
        elif activity_type == FX_ACTIVITY_TYPE:
            skip_reason = "fx"
        elif activity_type in CASH_ACTIVITY_TYPES:
            skip_reason = "cash"
        elif activity_type == "公司行动":
            skip_reason = "unsupported"
        elif activity_type in EXERCISE_TYPES:
            if not symbol or not market:
                skip_reason = "unsupported"
            elif quantity is None or quantity == 0 or price is None or price <= 0:
                skip_reason = "invalid"
        elif activity_type in TRADE_TYPES:
            if is_option_symbol(raw_symbol, description):
                skip_reason = "option"
            elif not symbol or not market:
                skip_reason = "unsupported"
            elif quantity is None or quantity == 0 or price is None or price <= 0:
                skip_reason = "invalid"
        else:
            skip_reason = "unsupported"

        fee_in_price_currency = Decimal("0")
        if activity_type in set(TRADE_TYPES) | EXERCISE_TYPES:
            fee_in_price_currency = trade_fee_in_price_currency(
                quantity=quantity,
                price=price,
                gross_amount=gross_amount,
                net_amount=net_amount,
                commission=commission,
                price_currency=price_currency,
                base_currency=base_currency,
            )

        hash_values = {
            "broker": BROKER_NAME,
            "trade_date": trade_date,
            "account": account,
            "description": description,
            "activity_type": activity_type,
            "raw_symbol": raw_symbol,
            "symbol": symbol,
            "quantity": quantity,
            "price": price,
            "price_currency": price_currency,
            "gross_amount": gross_amount,
            "commission": commission,
            "net_amount": net_amount,
        }

        base_row_hash = calculate_row_hash(hash_values)
        hash_occurrences[base_row_hash] = hash_occurrences.get(base_row_hash, 0) + 1
        row_hash = base_row_hash
        if hash_occurrences[base_row_hash] > 1:
            hash_values[HASH_DUPLICATE_OCCURRENCE_FIELD] = hash_occurrences[base_row_hash]
            row_hash = calculate_row_hash(hash_values)

        parsed_rows.append(
            ParsedIbkrFlow(
                source_row_number=row_number,
                row_hash=row_hash,
                account=account,
                trade_date=trade_date,
                description=description,
                activity_type=activity_type,
                raw_symbol=raw_symbol,
                symbol=symbol,
                name=None,
                market=market,
                quantity=quantity,
                price=price,
                price_currency=price_currency,
                base_currency=base_currency,
                gross_amount=gross_amount,
                commission=commission,
                net_amount=net_amount,
                fee_in_price_currency=fee_in_price_currency,
                skip_reason=skip_reason,
            )
        )

    apply_exercise_import_policy(parsed_rows)
    enrich_security_names(parsed_rows)
    return parsed_rows, business_counts, total_rows, errors


def flow_to_sample(flow: ParsedIbkrFlow, duplicate: bool) -> Dict[str, Any]:
    mapped_type = flow.transaction_type or (
        "CASH_DIVIDEND"
        if flow.is_cash_dividend
        else "DIVIDEND_TAX"
        if flow.is_withholding_tax
        else flow.skip_reason or ""
    )
    return {
        "row_number": flow.source_row_number,
        "symbol": flow.symbol or flow.raw_symbol,
        "name": flow.name,
        "market": flow.market or "",
        "transaction_type": mapped_type,
        "trade_date": flow.trade_date.isoformat(),
        "quantity": str(abs(flow.quantity)) if flow.quantity is not None else "0",
        "price": str(flow.price or "0"),
        "fee": str(flow.fee_in_price_currency or "0"),
        "row_hash": flow.row_hash,
        "duplicate": duplicate,
    }


def get_existing_hashes(db: Session, user_id: int, hashes: Iterable[str]) -> set[str]:
    hash_list = list(hashes)
    if not hash_list:
        return set()
    rows = (
        db.query(IbkrActivityFlow.row_hash)
        .filter(
            IbkrActivityFlow.user_id == user_id,
            IbkrActivityFlow.row_hash.in_(hash_list),
        )
        .all()
    )
    return {row[0] for row in rows}


def eligible_rows(parsed_rows: List[ParsedIbkrFlow]) -> List[ParsedIbkrFlow]:
    return [
        flow
        for flow in parsed_rows
        if flow.is_trade or flow.is_cash_dividend or flow.is_withholding_tax
    ]


def build_import_result(
    *,
    filename: str,
    total_rows: int,
    parsed_rows: List[ParsedIbkrFlow],
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
    tax_rows = [flow for flow in rows if flow.is_withholding_tax]
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
        "option": len([flow for flow in parsed_rows if flow.skip_reason == "option"]),
        "fx": len([flow for flow in parsed_rows if flow.skip_reason == "fx"]),
        "cash": len([flow for flow in parsed_rows if flow.skip_reason == "cash"]),
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
        "skipped_option_rows": skip_counts["option"],
        "skipped_fx_rows": skip_counts["fx"],
        "skipped_cash_rows": skip_counts["cash"],
        "skipped_unsupported_rows": skip_counts["unsupported"],
        "affected_symbols": affected_symbols,
        "date_start": min(dates).isoformat() if dates else None,
        "date_end": max(dates).isoformat() if dates else None,
        "business_counts": business_counts,
        "duplicate_samples": [flow_to_sample(flow, True) for flow in duplicate_rows[:10]],
        "import_samples": [flow_to_sample(flow, False) for flow in import_rows[:10]],
        "errors": errors[:50],
    }


def preview_ibkr_activity(
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


def create_ibkr_activity_flow(
    *,
    user_id: int,
    filename: str,
    flow: ParsedIbkrFlow,
    transaction_id: Optional[int] = None,
    corporate_action_id: Optional[int] = None,
) -> IbkrActivityFlow:
    return IbkrActivityFlow(
        user_id=user_id,
        transaction_id=transaction_id,
        corporate_action_id=corporate_action_id,
        broker=BROKER_NAME,
        row_hash=flow.row_hash,
        source_filename=filename,
        source_row_number=flow.source_row_number,
        account=flow.account,
        trade_date=flow.trade_date,
        description=flow.description,
        activity_type=flow.activity_type,
        raw_symbol=flow.raw_symbol,
        symbol=flow.symbol,
        name=flow.name,
        market=flow.market,
        quantity=flow.quantity,
        price=flow.price,
        price_currency=flow.price_currency,
        base_currency=flow.base_currency,
        gross_amount=flow.gross_amount,
        commission=flow.commission,
        net_amount=flow.net_amount,
        fee_in_price_currency=flow.fee_in_price_currency,
        skip_reason=flow.skip_reason,
    )


def find_dividend_for_tax(
    db: Session, user_id: int, flow: ParsedIbkrFlow
) -> Optional[CorporateAction]:
    candidates = (
        db.query(CorporateAction)
        .filter(
            CorporateAction.user_id == user_id,
            CorporateAction.symbol == flow.symbol,
            CorporateAction.market == flow.market,
            CorporateAction.action_type == "CASH_DIVIDEND",
            CorporateAction.currency == flow.base_currency,
            CorporateAction.ex_date <= flow.trade_date,
        )
        .order_by(CorporateAction.ex_date.desc(), CorporateAction.id.desc())
        .limit(5)
        .all()
    )
    return candidates[0] if candidates else None


def calculate_position_before(
    db: Session, user_id: int, symbol: str, market: str, before_date: date
) -> tuple[Decimal, Decimal]:
    transactions = (
        db.query(Transaction)
        .filter(
            Transaction.user_id == user_id,
            Transaction.symbol == symbol,
            Transaction.market == market,
            Transaction.transaction_date < before_date,
        )
        .order_by(Transaction.transaction_date, Transaction.id)
        .all()
    )

    quantity = Decimal("0")
    avg_cost = Decimal("0")
    total_cost = Decimal("0")
    for txn in transactions:
        txn_quantity = Decimal(str(txn.quantity))
        if txn.transaction_type == "BUY":
            total_cost += txn_quantity * Decimal(str(txn.price)) + Decimal(str(txn.fee or 0))
            quantity += txn_quantity
            avg_cost = total_cost / quantity if quantity > 0 else Decimal("0")
        elif txn.transaction_type == "SELL":
            if txn_quantity >= quantity:
                quantity = Decimal("0")
                avg_cost = Decimal("0")
                total_cost = Decimal("0")
            else:
                quantity -= txn_quantity
                total_cost = quantity * avg_cost
    return quantity, avg_cost


def estimate_new_currency_cost_per_share(
    parsed_rows: List[ParsedIbkrFlow],
    *,
    old_symbol: str,
    old_market: str,
    new_symbol: str,
    new_market: str,
) -> Optional[Decimal]:
    old_base_cost = Decimal("0")
    old_quantity = Decimal("0")
    for flow in parsed_rows:
        if (
            flow.symbol == old_symbol
            and flow.market == old_market
            and flow.transaction_type == "BUY"
            and flow.quantity is not None
        ):
            old_quantity += abs(flow.quantity)
            if flow.net_amount is not None:
                old_base_cost += abs(flow.net_amount)

    if old_base_cost <= 0 or old_quantity <= 0:
        return None

    for flow in sorted(parsed_rows, key=lambda item: item.trade_date):
        if (
            flow.symbol == new_symbol
            and flow.market == new_market
            and flow.gross_amount is not None
            and flow.quantity is not None
            and flow.price is not None
        ):
            trade_value = abs(flow.quantity * flow.price)
            gross_base = abs(flow.gross_amount)
            if trade_value > 0 and gross_base > 0:
                new_currency_per_base = trade_value / gross_base
                return old_base_cost * new_currency_per_base / old_quantity
    return None


def apply_known_relisting_transfers(
    db: Session,
    user_id: int,
    parsed_rows: List[ParsedIbkrFlow],
    affected_symbols: set[tuple[str, str]],
) -> int:
    created = 0
    for relisting in KNOWN_RELISTINGS:
        old_symbol = relisting["old_symbol"]
        old_market = relisting["old_market"]
        new_symbol = relisting["new_symbol"]
        new_market = relisting["new_market"]

        new_trade_dates = [
            flow.trade_date
            for flow in parsed_rows
            if flow.symbol == new_symbol and flow.market == new_market and flow.is_trade
        ]
        if not new_trade_dates:
            continue

        if (
            db.query(Transaction)
            .filter(
                Transaction.user_id == user_id,
                Transaction.notes.like(f"%{SYNTHETIC_RELISTING_MARKER}%"),
                Transaction.notes.like(f"%{old_symbol}->{new_symbol}%"),
            )
            .first()
        ):
            continue

        first_new_trade_date = min(new_trade_dates)
        transfer_date = first_new_trade_date - timedelta(days=1)
        quantity, old_avg_cost = calculate_position_before(
            db, user_id, old_symbol, old_market, first_new_trade_date
        )
        if quantity <= 0:
            continue

        new_avg_cost = estimate_new_currency_cost_per_share(
            parsed_rows,
            old_symbol=old_symbol,
            old_market=old_market,
            new_symbol=new_symbol,
            new_market=new_market,
        )
        if new_avg_cost is None:
            new_avg_cost = old_avg_cost

        name = relisting["name"]
        note = (
            f"{BROKER_NAME} Activity Statement; {SYNTHETIC_RELISTING_MARKER}; "
            f"{old_symbol}->{new_symbol}; transfer_date={transfer_date}"
        )
        db.add(
            Transaction(
                user_id=user_id,
                symbol=old_symbol,
                name=name,
                market=old_market,
                transaction_type="SELL",
                quantity=quantity,
                price=old_avg_cost,
                fee=Decimal("0"),
                transaction_date=transfer_date,
                currency=relisting["old_currency"],
                notes=note,
            )
        )
        db.add(
            Transaction(
                user_id=user_id,
                symbol=new_symbol,
                name=name,
                market=new_market,
                transaction_type="BUY",
                quantity=quantity,
                price=new_avg_cost,
                fee=Decimal("0"),
                transaction_date=transfer_date,
                currency=relisting["new_currency"],
                notes=note,
            )
        )
        affected_symbols.add((old_symbol, old_market))
        affected_symbols.add((new_symbol, new_market))
        created += 2

    return created


def apply_withholding_tax(
    db: Session,
    user_id: int,
    filename: str,
    flow: ParsedIbkrFlow,
    action: CorporateAction,
) -> int:
    tax_amount = abs(flow.gross_amount or Decimal("0"))
    action.tax_withheld = (action.tax_withheld or Decimal("0")) + tax_amount
    if action.total_dividend is not None:
        action.net_dividend = max(Decimal("0"), action.total_dividend - action.tax_withheld)
    action.notes = (
        f"{action.notes or ''}; {BROKER_NAME} withholding tax row={flow.source_row_number}; "
        f"row_hash={flow.row_hash}"
    ).strip("; ")
    db.add(
        create_ibkr_activity_flow(
            user_id=user_id,
            filename=filename,
            flow=flow,
            corporate_action_id=action.id,
        )
    )
    return 1


def import_ibkr_activity(
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
    imported_transfer_transactions = 0
    affected_symbols: set[tuple[str, str]] = set()
    pending_tax_flows: List[ParsedIbkrFlow] = []

    for flow in parsed_rows:
        if not flow.is_trade and not flow.is_cash_dividend and not flow.is_withholding_tax:
            continue
        if flow.row_hash in existing_hashes:
            continue

        if flow.is_cash_dividend:
            action = CorporateAction(
                user_id=user_id,
                symbol=flow.symbol,
                name=flow.name,
                market=flow.market,
                action_type="CASH_DIVIDEND",
                ex_date=flow.trade_date,
                payment_date=flow.trade_date,
                total_dividend=flow.gross_amount,
                tax_withheld=Decimal("0"),
                net_dividend=flow.gross_amount,
                currency=flow.base_currency,
                notes=(
                    f"{BROKER_NAME} Activity Statement; "
                    f"row={flow.source_row_number}; "
                    f"raw_symbol={flow.raw_symbol}; "
                    f"dividend_currency={parse_dividend_currency(flow.description) or ''}; "
                    f"row_hash={flow.row_hash}"
                ),
            )
            db.add(action)
            db.flush()
            db.add(
                create_ibkr_activity_flow(
                    user_id=user_id, filename=filename, flow=flow, corporate_action_id=action.id
                )
            )
            existing_hashes.add(flow.row_hash)
            imported_corporate_actions += 1
            continue

        if flow.is_withholding_tax:
            action = find_dividend_for_tax(db, user_id, flow)
            if not action:
                pending_tax_flows.append(flow)
                continue
            imported_tax_adjustments += apply_withholding_tax(db, user_id, filename, flow, action)
            existing_hashes.add(flow.row_hash)
            continue

        transaction_type = flow.transaction_type
        if (
            not transaction_type
            or not flow.symbol
            or not flow.market
            or flow.quantity is None
            or flow.price is None
        ):
            continue
        transaction = Transaction(
            user_id=user_id,
            symbol=flow.symbol,
            name=flow.name,
            market=flow.market,
            transaction_type=transaction_type,
            quantity=abs(flow.quantity),
            price=flow.price,
            fee=flow.fee_in_price_currency or Decimal("0"),
            transaction_date=flow.trade_date,
            currency=flow.price_currency or flow.base_currency,
            notes=(
                f"{BROKER_NAME} Activity Statement; "
                f"account={flow.account or ''}; "
                f"row={flow.source_row_number}; "
                f"type={flow.activity_type}; "
                f"raw_symbol={flow.raw_symbol}; "
                f"gross={flow.gross_amount}; "
                f"net={flow.net_amount}"
            ),
        )
        db.add(transaction)
        db.flush()
        db.add(
            create_ibkr_activity_flow(
                user_id=user_id, filename=filename, flow=flow, transaction_id=transaction.id
            )
        )
        existing_hashes.add(flow.row_hash)
        affected_symbols.add((flow.symbol, flow.market))
        imported_transactions += 1

    for flow in pending_tax_flows:
        if flow.row_hash in existing_hashes:
            continue
        action = find_dividend_for_tax(db, user_id, flow)
        if not action:
            continue
        imported_tax_adjustments += apply_withholding_tax(db, user_id, filename, flow, action)
        existing_hashes.add(flow.row_hash)

    db.flush()
    imported_transfer_transactions = apply_known_relisting_transfers(
        db, user_id, parsed_rows, affected_symbols
    )
    imported_transactions += imported_transfer_transactions

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ValueError("Duplicate IBKR activity flow detected during import")

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
