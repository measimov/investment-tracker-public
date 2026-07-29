from __future__ import annotations

import hashlib
import io
import re
from bisect import bisect_right
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Deque, Dict, Iterable, List, Optional

import pandas as pd
import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pypdf import PdfReader
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models.broker_account import BrokerAccount
from ..models.broker_fund_flow import BrokerFundFlow
from ..models.cash_event import CashEvent
from ..models.corporate_action import CorporateAction
from ..models.transaction import Transaction
from ..services.excluded_security_service import get_excluded_symbols
from ..services.holding_service import recalculate_holdings
from ..services.import_batch_service import (
    complete_import_batch,
    fail_import_batch,
    set_import_batch_source_stats,
    start_import_batch,
    validate_import_account,
    validate_source_file_account,
)


BROKER_NAME = "招商证券"
SOURCE_TYPE = "cmb_statement_pdf"
PARSER_NAME = "cmb_statement"
# 行为变化必须升版（ImportBatch 按 parser/version 审计入账口径）：
# v7 = 开放基金申购/新股入账 生成规范 BUY（此前仅归档）
PARSER_VERSION = "8"
TRADE_BUSINESS_MAP = {
    "证券买入": "BUY",
    "证券卖出": "SELL",
    # 场内基金申购确认：份额按金额/净值折算（数量×价格与成交金额有舍入差，
    # 在既有容差内），发生金额 = -(成交金额+费用) 精确成立，校验与买入同构。
    # 不建模会让后续"证券卖出"撞持仓预检（如 161225 白银LOF）。
    "开放基金申购": "BUY",
}
# 新股/新债中签入账：零现金行（成交金额与发生金额均为 0，缴款在别的业务里），
# 发行价披露在价格列。视同买入建仓，但跳过买卖行的金额对账校验。
ALLOTMENT_BUSINESS_NAMES = {"新股入账"}
DIVIDEND_BUSINESS_NAMES = {"股息入账", "产品红利发放"}
PRODUCT_DIVIDEND_BUSINESS_NAME = "产品红利发放"
TAX_BUSINESS_NAME = "股息红利税补缴"
CASH_MANAGEMENT_SYMBOLS = {"880013"}
# 沪/深港通：价格列是 HKD，金额与费用列是 CNY 结算。结算汇率不披露，
# 由行内推导（成交金额CNY ÷ (数量 × HKD价格)），并做合理区间校验。
HK_CONNECT_MARKET_NAMES = {"沪港通", "深港通"}
HK_CONNECT_SETTLEMENT_RATE_MIN = Decimal("0.5")
HK_CONNECT_SETTLEMENT_RATE_MAX = Decimal("1.5")
# 流水明细之后的章节：它们的表格行会被逐词提取误认成流水行（配号信息的
# 数字会落进"币种"等列），按节标题划界排除。
PDF_FLOW_SECTION_TITLE = "流水明细"
PDF_FLOW_TERMINATOR_TITLES = {"未回业务流水明细", "配号信息"}
ACCOUNT_MASK_TAIL_LENGTH = 4
HASH_FIELDS = [
    "broker",
    "trade_date",
    "serial_number",
    "business_name",
    "security_code",
    "currency",
    "trade_price",
    "trade_quantity",
    "amount",
    "stamp_tax",
    "commission",
    "other_fee",
    "contract_number",
    "shareholder_code",
]
HASH_DUPLICATE_OCCURRENCE_FIELD = "duplicate_occurrence"
PDF_FLOW_COLUMNS = [
    "发生日期",
    "市场",
    "币种",
    "银行代码",
    "证券账号",
    "证券代码",
    "证券名称",
    "业务标志",
    "发生数量",
    "成交均价",
    "成交金额",
    "佣金",
    "印花税",
    "其他费",
    "变动金额",
    "资金余额",
    "证券余额",
]
PDF_REQUIRED_HEADER_COLUMNS = set(PDF_FLOW_COLUMNS)
PDF_NUMERIC_COLUMNS = [
    "成交数量",
    "成交价格",
    "PDF成交金额",
    "佣金",
    "印花税",
    "其他费用",
    "发生金额",
    "资金余额",
    "剩余数量",
]
STRICT_DECIMAL_PATTERN = re.compile(r"^[+-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)$")
PDF_AMOUNT_TOLERANCE = Decimal("0.02")
SOURCE_ROW_ERROR_PATTERN = re.compile(r"^row (\d+):")
MANUAL_REVIEW_WARNING_SUFFIX = "manual review required"
LEGACY_EXCEL_STATEMENT_TYPE = "cmb_fund_flow_excel"
LEGACY_EXCEL_SUFFIXES = (".xls", ".xlsx")
ROW_HASH_NOTE_PATTERN = re.compile(r"row_hash=([0-9a-f]{64})")


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
    market_text: str = ""
    settlement_rate: Optional[Decimal] = None
    # 排除清单命中标记：置位后所有入账语义（交易/股息/税/利息）失效，行只归档。
    excluded: bool = False

    @property
    def transaction_type(self) -> Optional[str]:
        if self.excluded:
            return None
        if self.business_name in ALLOTMENT_BUSINESS_NAMES:
            return "BUY"
        return TRADE_BUSINESS_MAP.get(self.business_name)

    @property
    def is_hk_connect(self) -> bool:
        return self.market_text in HK_CONNECT_MARKET_NAMES

    @property
    def effective_currency(self) -> str:
        """沪/深港通交易以 HKD 记账（价格列即 HKD），其余按披露币种。"""
        return "HKD" if self.is_hk_connect else self.currency

    @property
    def effective_fee(self) -> Decimal:
        """港股通费用列为 CNY，需按推导的结算汇率换回 HKD。"""
        if self.is_hk_connect and self.settlement_rate and self.settlement_rate > 0:
            return (self.total_fee / self.settlement_rate).quantize(Decimal("0.00000001"))
        return self.total_fee

    @property
    def is_cash_dividend(self) -> bool:
        return (
            not self.excluded
            and self.business_name in DIVIDEND_BUSINESS_NAMES
            and bool(self.security_code)
            and self.security_code not in CASH_MANAGEMENT_SYMBOLS
            and self.amount > 0
        )

    @property
    def is_dividend_tax(self) -> bool:
        return (
            not self.excluded
            and self.business_name == TAX_BUSINESS_NAME
            and bool(self.security_code)
            and self.amount < 0
        )

    @property
    def is_cash_interest(self) -> bool:
        return (
            not self.excluded
            and self.business_name == PRODUCT_DIVIDEND_BUSINESS_NAME
            and self.security_code in CASH_MANAGEMENT_SYMBOLS
            and self.amount > 0
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


@dataclass
class LegacyFlowMatch:
    source_flow: BrokerFundFlow
    transaction: Optional[Transaction] = None
    corporate_action: Optional[CorporateAction] = None


@dataclass
class ExactClaimResult:
    row_hashes: set[str]
    imported_cash_events: int = 0


def strip_bom(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).replace("\ufeff", "").strip()
    if text.endswith(".0") and text.replace(".", "", 1).isdigit():
        text = text[:-2]
    return text


def parse_strict_pdf_decimal(value: Any) -> Optional[Decimal]:
    text = strip_bom(value)
    if not text or not STRICT_DECIMAL_PATTERN.fullmatch(text):
        return None
    try:
        return Decimal(text.replace(",", ""))
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


def _normalized_account_code(value: Optional[str]) -> str:
    return re.sub(r"[^A-Z0-9]", "", strip_bom(value).upper())


def source_account_masks(parsed_rows: Iterable[ParsedFlow]) -> List[str]:
    masks = set()
    for flow in parsed_rows:
        account_code = _normalized_account_code(flow.shareholder_code)
        if not account_code:
            continue
        tail = account_code[-ACCOUNT_MASK_TAIL_LENGTH:]
        masks.add(f"****{tail}")
    return sorted(masks)


def validate_statement_account_masks(
    account: BrokerAccount,
    parsed_rows: Iterable[ParsedFlow],
) -> List[str]:
    masks = source_account_masks(parsed_rows)
    if not masks:
        return masks

    configured_tokens = re.findall(
        r"[A-Z0-9]+",
        strip_bom(account.account_number_masked).upper(),
    )
    missing = [
        mask
        for mask in masks
        if not any(token.endswith(mask[4:]) for token in configured_tokens)
    ]
    if missing:
        configured = account.account_number_masked or "未设置"
        raise ValueError(
            "招商证券账户掩码未覆盖对账单全部证券账户尾号；"
            f"缺少={', '.join(missing)}；当前账户掩码={configured}"
        )
    return masks


def infer_market(symbol: str, currency: str, shareholder_code: Optional[str]) -> str:
    symbol = symbol.strip()
    shareholder_code = shareholder_code or ""
    if symbol.startswith(("200", "900")):
        return "B股"
    if (
        currency == "HKD"
        or shareholder_code.startswith("H")
        or (symbol.isdigit() and len(symbol) == 5)
    ):
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
    fields = HASH_FIELDS
    if values.get(HASH_DUPLICATE_OCCURRENCE_FIELD):
        fields = HASH_FIELDS + [HASH_DUPLICATE_OCCURRENCE_FIELD]
    payload = "|".join(normalize_hash_value(values.get(field, "")) for field in fields)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalized_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(value).normalize()


def _event_kind(
    business_name: str,
    *,
    security_code: str,
    amount: Decimal,
) -> Optional[str]:
    transaction_type = TRADE_BUSINESS_MAP.get(business_name)
    if transaction_type:
        return transaction_type
    if (
        business_name == PRODUCT_DIVIDEND_BUSINESS_NAME
        and security_code in CASH_MANAGEMENT_SYMBOLS
        and amount > 0
    ):
        return "INTEREST"
    if (
        business_name in DIVIDEND_BUSINESS_NAMES
        and security_code
        and security_code not in CASH_MANAGEMENT_SYMBOLS
        and amount > 0
    ):
        return "CASH_DIVIDEND"
    if business_name == TAX_BUSINESS_NAME and security_code and amount < 0:
        return "DIVIDEND_TAX"
    return None


def _economic_key(
    *,
    business_name: str,
    trade_date: date,
    security_code: str,
    currency: str,
    trade_quantity: Decimal,
    trade_price: Decimal,
    amount: Decimal,
    total_fee: Decimal,
    shareholder_code: Optional[str],
) -> tuple[Any, ...]:
    """Cross-format identity for one economic event, independent of file-only IDs."""
    kind = _event_kind(
        business_name,
        security_code=security_code,
        amount=_normalized_decimal(amount),
    )
    account_identity = strip_bom(shareholder_code).upper()
    common = (
        account_identity,
        kind,
        trade_date,
        strip_bom(security_code),
        strip_bom(currency).upper(),
    )
    if kind in {"BUY", "SELL"}:
        return common + (
            abs(_normalized_decimal(trade_quantity)),
            _normalized_decimal(trade_price),
            _normalized_decimal(amount),
            _normalized_decimal(total_fee),
        )
    return common + (_normalized_decimal(amount),)


def _conflict_key(
    *,
    business_name: str,
    trade_date: date,
    security_code: str,
    currency: str,
    trade_quantity: Decimal,
    amount: Decimal,
    shareholder_code: Optional[str],
) -> tuple[Any, ...]:
    """Coarser key used only to stop near-matching Excel/PDF rows from double counting."""
    kind = _event_kind(
        business_name,
        security_code=security_code,
        amount=_normalized_decimal(amount),
    )
    common = (
        strip_bom(shareholder_code).upper(),
        kind,
        trade_date,
        strip_bom(security_code),
        strip_bom(currency).upper(),
    )
    if kind in {"BUY", "SELL"}:
        return common + (abs(_normalized_decimal(trade_quantity)),)
    return common


def _parsed_flow_economic_key(flow: ParsedFlow) -> tuple[Any, ...]:
    return _economic_key(
        business_name=flow.business_name,
        trade_date=flow.trade_date,
        security_code=flow.security_code,
        currency=flow.currency,
        trade_quantity=flow.trade_quantity,
        trade_price=flow.trade_price,
        amount=flow.amount,
        total_fee=flow.total_fee,
        shareholder_code=flow.shareholder_code,
    )


def _parsed_flow_conflict_key(flow: ParsedFlow) -> tuple[Any, ...]:
    return _conflict_key(
        business_name=flow.business_name,
        trade_date=flow.trade_date,
        security_code=flow.security_code,
        currency=flow.currency,
        trade_quantity=flow.trade_quantity,
        amount=flow.amount,
        shareholder_code=flow.shareholder_code,
    )


def _stored_flow_economic_key(flow: BrokerFundFlow) -> tuple[Any, ...]:
    return _economic_key(
        business_name=flow.business_name,
        trade_date=flow.trade_date,
        security_code=flow.security_code or "",
        currency=flow.currency,
        trade_quantity=_normalized_decimal(flow.trade_quantity),
        trade_price=_normalized_decimal(flow.trade_price),
        amount=_normalized_decimal(flow.amount),
        total_fee=_stored_flow_total_fee(flow),
        shareholder_code=flow.shareholder_code,
    )


def _stored_flow_total_fee(flow: BrokerFundFlow) -> Decimal:
    return sum(
        (
            _normalized_decimal(getattr(flow, field))
            for field in (
                "stamp_tax",
                "commission",
                "handling_fee",
                "management_fee",
                "settlement_fee",
                "transfer_fee",
                "other_fee",
            )
        ),
        Decimal("0"),
    )


def _transaction_matches_legacy_source(
    transaction: Transaction,
    flow: BrokerFundFlow,
) -> bool:
    return (
        transaction.transaction_type == TRADE_BUSINESS_MAP.get(flow.business_name)
        and strip_bom(transaction.symbol) == strip_bom(flow.security_code)
        and transaction.transaction_date == flow.trade_date
        and abs(_normalized_decimal(transaction.quantity))
        == abs(_normalized_decimal(flow.trade_quantity))
        and _normalized_decimal(transaction.price) == _normalized_decimal(flow.trade_price)
        and strip_bom(transaction.currency).upper() == strip_bom(flow.currency).upper()
        and _normalized_decimal(transaction.fee) == _stored_flow_total_fee(flow)
        and strip_bom(transaction.market)
        == infer_market(
            strip_bom(flow.security_code),
            strip_bom(flow.currency).upper(),
            flow.shareholder_code,
        )
    )


def _cash_event_matches_source(
    cash_event: CashEvent,
    flow: BrokerFundFlow,
) -> bool:
    return (
        cash_event.event_type == "INTEREST"
        and cash_event.event_date == flow.trade_date
        and strip_bom(cash_event.currency).upper() == strip_bom(flow.currency).upper()
        and _normalized_decimal(cash_event.amount) == _normalized_decimal(flow.amount)
    )


def _corporate_action_matches_legacy_source(
    action: CorporateAction,
    flow: BrokerFundFlow,
) -> bool:
    if (
        strip_bom(action.symbol) != strip_bom(flow.security_code)
        or strip_bom(action.currency).upper() != strip_bom(flow.currency).upper()
        or strip_bom(action.market)
        != infer_market(
            strip_bom(flow.security_code),
            strip_bom(flow.currency).upper(),
            flow.shareholder_code,
        )
    ):
        return False
    if flow.business_name in DIVIDEND_BUSINESS_NAMES:
        return action.ex_date == flow.trade_date and _normalized_decimal(
            action.total_dividend
        ) == _normalized_decimal(flow.amount)
    if flow.business_name == TAX_BUSINESS_NAME:
        return action.ex_date <= flow.trade_date and _normalized_decimal(
            action.tax_withheld
        ) >= abs(_normalized_decimal(flow.amount))
    return False


def _corporate_action_aggregate_matches_legacy_sources(
    action: CorporateAction,
    flows: List[BrokerFundFlow],
) -> bool:
    dividend_flows = [
        flow
        for flow in flows
        if _event_kind(
            flow.business_name,
            security_code=flow.security_code or "",
            amount=_normalized_decimal(flow.amount),
        )
        == "CASH_DIVIDEND"
    ]
    tax_total = sum(
        (
            abs(_normalized_decimal(flow.amount))
            for flow in flows
            if _event_kind(
                flow.business_name,
                security_code=flow.security_code or "",
                amount=_normalized_decimal(flow.amount),
            )
            == "DIVIDEND_TAX"
        ),
        Decimal("0"),
    )
    if len(dividend_flows) != 1 or action.total_dividend is None:
        return False
    dividend_total = _normalized_decimal(dividend_flows[0].amount)
    if _normalized_decimal(action.total_dividend) != dividend_total:
        return False
    if _normalized_decimal(action.tax_withheld) != tax_total:
        return False
    if action.net_dividend is None:
        return False
    expected_net = max(
        Decimal("0"),
        _normalized_decimal(action.total_dividend) - _normalized_decimal(action.tax_withheld),
    )
    return _normalized_decimal(action.net_dividend) == expected_net


def _stored_flow_conflict_key(flow: BrokerFundFlow) -> tuple[Any, ...]:
    return _conflict_key(
        business_name=flow.business_name,
        trade_date=flow.trade_date,
        security_code=flow.security_code or "",
        currency=flow.currency,
        trade_quantity=_normalized_decimal(flow.trade_quantity),
        amount=_normalized_decimal(flow.amount),
        shareholder_code=flow.shareholder_code,
    )


def validate_cmb_statement_filename(filename: str) -> None:
    if not filename.lower().endswith(".pdf"):
        raise ValueError("招商证券对账单 must be a PDF file")


def ensure_pdf_is_readable(contents: bytes) -> None:
    reader = PdfReader(io.BytesIO(contents))
    if reader.is_encrypted:
        raise ValueError("PDF is encrypted. Please decrypt it with qpdf before importing.")


def _find_pdf_column_boundaries(
    page_words: List[List[Dict[str, Any]]],
) -> List[float]:
    best_headers: Dict[str, float] = {}
    for words in page_words:
        anchors = [word for word in words if strip_bom(word.get("text")) == "发生日期"]
        for anchor in anchors:
            header_positions = {
                strip_bom(word.get("text")): float(word["x0"])
                for word in words
                if abs(float(word["top"]) - float(anchor["top"])) < 2.2
                and strip_bom(word.get("text")) in PDF_REQUIRED_HEADER_COLUMNS
            }
            if len(header_positions) > len(best_headers):
                best_headers = header_positions
            if PDF_REQUIRED_HEADER_COLUMNS <= set(header_positions):
                starts = [header_positions[column] for column in PDF_FLOW_COLUMNS]
                return [(left + right) / 2 for left, right in zip(starts, starts[1:])]

    missing_headers = sorted(PDF_REQUIRED_HEADER_COLUMNS - set(best_headers))
    raise ValueError(
        f"No 招商证券流水明细 table found in PDF (missing headers: {', '.join(missing_headers)})"
    )


def _extract_pdf_flow_rows(contents: bytes) -> List[Dict[str, str]]:
    ensure_pdf_is_readable(contents)
    extracted_rows: List[Dict[str, str]] = []

    try:
        with pdfplumber.open(io.BytesIO(contents)) as pdf:
            page_words = [
                page.extract_words(
                    x_tolerance=1,
                    y_tolerance=2,
                    keep_blank_chars=False,
                )
                for page in pdf.pages
            ]
            boundaries = _find_pdf_column_boundaries(page_words)

            # 只取"流水明细"节内的行：普通对账单还有"未回业务流水明细"和
            # "配号信息"两个表，其行同样以 8 位日期开头，逐词提取会把配号/
            # 股东账号误落进"币种"等列产生幽灵行。这些节可能跨页且续页无任何
            # 标题，因此按页序维护"是否在流水明细节内"的状态：遇到节标题进入，
            # 遇到终止标题退出，状态跨页传递。
            # 旧电子对账单版式可能不含节标题：全文档都找不到"流水明细"时
            # 退回旧行为（所有日期锚点有效），保证向后兼容。
            has_section_titles = any(
                strip_bom(word.get("text")) == PDF_FLOW_SECTION_TITLE
                for words in page_words
                for word in words
            )
            in_flow_section = not has_section_titles
            for words in page_words:
                events = (
                    sorted(
                        (
                            float(word["top"]),
                            strip_bom(word.get("text")) == PDF_FLOW_SECTION_TITLE,
                        )
                        for word in words
                        if strip_bom(word.get("text")) == PDF_FLOW_SECTION_TITLE
                        or strip_bom(word.get("text")) in PDF_FLOW_TERMINATOR_TITLES
                    )
                    if has_section_titles
                    else []
                )

                def _anchor_in_flow(anchor_top: float) -> bool:
                    state = in_flow_section
                    for event_top, is_enter in events:
                        if event_top >= anchor_top:
                            break
                        state = is_enter
                    return state

                anchors = sorted(
                    (
                        word
                        for word in words
                        if float(word["x0"]) < boundaries[0]
                        and re.fullmatch(r"\d{8}", strip_bom(word.get("text")))
                        and _anchor_in_flow(float(word["top"]))
                    ),
                    key=lambda word: float(word["top"]),
                )
                for _, is_enter in events:
                    in_flow_section = is_enter
                for anchor in anchors:
                    row_words = sorted(
                        (
                            word
                            for word in words
                            if abs(float(word["top"]) - float(anchor["top"])) < 2.2
                        ),
                        key=lambda word: float(word["x0"]),
                    )
                    row: Dict[str, str] = {}
                    for word in row_words:
                        column_index = bisect_right(boundaries, float(word["x0"]))
                        column = PDF_FLOW_COLUMNS[column_index]
                        text = strip_bom(word.get("text"))
                        if text:
                            row[column] = f"{row.get(column, '')}{text}"
                    if row.get("发生日期"):
                        extracted_rows.append(row)
    except PDFPasswordIncorrect as exc:
        raise ValueError("PDF is encrypted. Please decrypt it with qpdf before importing.") from exc

    if not extracted_rows:
        raise ValueError("No 招商证券流水明细 rows found in PDF")
    return extracted_rows


def read_cmb_statement_pdf(contents: bytes) -> pd.DataFrame:
    rows = _extract_pdf_flow_rows(contents)
    normalized_rows = [
        {
            "市场": row.get("市场", ""),
            "证券代码": row.get("证券代码", ""),
            "证券名称": row.get("证券名称", ""),
            "币种": row.get("币种", ""),
            "成交日期": row.get("发生日期", ""),
            "成交价格": row.get("成交均价", ""),
            "成交数量": row.get("发生数量", ""),
            "PDF成交金额": row.get("成交金额", ""),
            "发生金额": row.get("变动金额", ""),
            "流水号": "",
            "业务名称": row.get("业务标志", ""),
            "资金余额": row.get("资金余额", ""),
            "剩余数量": row.get("证券余额", ""),
            "佣金": row.get("佣金", ""),
            "印花税": row.get("印花税", ""),
            "其他费用": row.get("其他费", ""),
            "股东代码": row.get("证券账号", ""),
        }
        for row in rows
    ]
    return pd.DataFrame(normalized_rows)


def read_cmb_fund_flow(contents: bytes, filename: str) -> pd.DataFrame:
    validate_cmb_statement_filename(filename)
    return read_cmb_statement_pdf(contents)


def parse_rows(
    contents: bytes, filename: str
) -> tuple[List[ParsedFlow], Dict[str, int], int, List[str]]:
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
    pdf_hash_occurrences: Dict[str, int] = {}

    for index, row in df.iterrows():
        row_number = int(index) + 2
        business_name = strip_bom(row.get("业务名称"))
        if not business_name:
            business_name = "__MISSING__"
            errors.append(f"row {row_number}: missing business name; manual review required")
        business_counts[business_name] = business_counts.get(business_name, 0) + 1

        trade_date = parse_trade_date(row.get("成交日期"))
        if trade_date is None:
            errors.append(f"row {row_number}: invalid trade date")
            continue

        strict_pdf_values: Dict[str, Decimal] = {}
        invalid_pdf_columns = []
        for column in PDF_NUMERIC_COLUMNS:
            parsed_value = parse_strict_pdf_decimal(row.get(column))
            if parsed_value is None:
                invalid_pdf_columns.append(column)
            else:
                strict_pdf_values[column] = parsed_value
        if invalid_pdf_columns:
            errors.append(
                f"row {row_number}: invalid PDF numeric fields: {', '.join(invalid_pdf_columns)}"
            )
            continue

        security_code = strip_bom(row.get("证券代码"))
        security_name = strip_bom(row.get("证券名称")) or None
        currency = normalize_currency(strip_bom(row.get("币种")))
        trade_price = strict_pdf_values["成交价格"]
        trade_quantity = strict_pdf_values["成交数量"]
        amount = strict_pdf_values["发生金额"]
        stamp_tax = strict_pdf_values["印花税"]
        commission = strict_pdf_values["佣金"]
        other_fee = strict_pdf_values["其他费用"]
        if any(fee < 0 for fee in (stamp_tax, commission, other_fee)):
            errors.append(f"row {row_number}: PDF fee fields must be non-negative")
            continue

        market_text = strip_bom(row.get("市场")) or ""
        is_hk_connect = market_text in HK_CONNECT_MARKET_NAMES
        settlement_rate: Optional[Decimal] = None

        if business_name in TRADE_BUSINESS_MAP:
            pdf_trade_amount = strict_pdf_values["PDF成交金额"]
            if pdf_trade_amount < 0:
                errors.append(f"row {row_number}: PDF trade value must be non-negative")
                continue
            pdf_total_fee = stamp_tax + commission + other_fee
            price_text = strip_bom(row.get("成交价格"))
            decimal_places = len(price_text.rsplit(".", 1)[1]) if "." in price_text else 0
            gross_rounding_tolerance = (
                abs(trade_quantity) * Decimal("0.5").scaleb(-decimal_places)
            )
            if TRADE_BUSINESS_MAP[business_name] == "BUY":
                expected_amount = -(pdf_trade_amount + pdf_total_fee)
                valid_quantity_sign = trade_quantity > 0
            else:
                expected_amount = pdf_trade_amount - pdf_total_fee
                valid_quantity_sign = trade_quantity < 0
            if not valid_quantity_sign:
                errors.append(
                    f"row {row_number}: PDF trade quantity sign does not match {business_name}"
                )
                continue
            if is_hk_connect:
                # 沪/深港通：价格列是 HKD，成交金额与费用列是 CNY 结算，
                # 数量×均价 与 成交金额 天然不等。改为推导结算汇率并校验其
                # 落在合理区间（推导值同时供落库换算费用/审计）。
                hk_gross = abs(trade_quantity) * trade_price
                if hk_gross <= 0:
                    errors.append(
                        f"row {row_number}: 港股通 trade has invalid quantity or price"
                    )
                    continue
                settlement_rate = (pdf_trade_amount / hk_gross).quantize(
                    Decimal("0.00000001")
                )
                if not (
                    HK_CONNECT_SETTLEMENT_RATE_MIN
                    <= settlement_rate
                    <= HK_CONNECT_SETTLEMENT_RATE_MAX
                ):
                    errors.append(
                        f"row {row_number}: 港股通 derived settlement rate "
                        f"{settlement_rate} outside plausible range"
                    )
                    continue
            elif abs(
                pdf_trade_amount - (abs(trade_quantity) * trade_price)
            ) > gross_rounding_tolerance + PDF_AMOUNT_TOLERANCE:
                errors.append(
                    f"row {row_number}: PDF trade value does not reconcile "
                    "with quantity and displayed average price"
                )
                continue
            if abs(amount - expected_amount) > PDF_AMOUNT_TOLERANCE:
                errors.append(
                    f"row {row_number}: PDF trade amount does not reconcile with value and fees"
                )
                continue

        if business_name in ALLOTMENT_BUSINESS_NAMES:
            # 新股/新债中签入账的零现金前提必须显式成立，否则阻塞：
            # 正份额、正发行价、成交金额/发生金额/全部费用均为 0。
            # 不做这些断言的话，负数量会在落库时被 abs() 静默洗成正持仓，
            # 带现金的异常行也会被当成零成本建仓。
            pdf_trade_amount = strict_pdf_values["PDF成交金额"]
            allotment_fees = stamp_tax + commission + other_fee
            if not security_code:
                errors.append(
                    f"row {row_number}: allotment missing security code"
                )
                continue
            if trade_quantity <= 0 or trade_price <= 0:
                errors.append(
                    f"row {row_number}: allotment quantity and issue price must be positive"
                )
                continue
            if pdf_trade_amount != 0 or amount != 0 or allotment_fees != 0:
                errors.append(
                    f"row {row_number}: allotment must be a zero-cash row "
                    "(trade value, amount and fees all zero)"
                )
                continue

        if (
            business_name in DIVIDEND_BUSINESS_NAMES
            and security_code not in CASH_MANAGEMENT_SYMBOLS
        ):
            if not security_code:
                errors.append(
                    f"row {row_number}: dividend missing security code; manual review required"
                )
            if amount <= 0:
                errors.append(f"row {row_number}: dividend amount must be positive")

        if business_name == TAX_BUSINESS_NAME and not security_code:
            errors.append(
                f"row {row_number}: dividend tax missing security code; manual review required"
            )
        if business_name == TAX_BUSINESS_NAME and amount >= 0:
            errors.append(f"row {row_number}: dividend tax amount must be negative")

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
            "stamp_tax": stamp_tax,
            "commission": commission,
            "other_fee": other_fee,
            "contract_number": contract_number,
            "shareholder_code": shareholder_code,
        }

        base_row_hash = calculate_row_hash(hash_values)
        row_hash = base_row_hash
        pdf_hash_occurrences[base_row_hash] = pdf_hash_occurrences.get(base_row_hash, 0) + 1
        if pdf_hash_occurrences[base_row_hash] > 1:
            hash_values[HASH_DUPLICATE_OCCURRENCE_FIELD] = pdf_hash_occurrences[base_row_hash]
            row_hash = calculate_row_hash(hash_values)

        parsed_rows.append(
            ParsedFlow(
                source_row_number=row_number,
                row_hash=row_hash,
                security_code=security_code,
                security_name=security_name,
                currency=currency,
                trade_date=trade_date,
                trade_price=trade_price,
                trade_quantity=trade_quantity,
                amount=amount,
                cash_balance=strict_pdf_values["资金余额"],
                remaining_quantity=strict_pdf_values["剩余数量"],
                contract_number=contract_number,
                serial_number=serial_number,
                business_name=business_name,
                stamp_tax=stamp_tax,
                commission=commission,
                handling_fee=Decimal("0"),
                management_fee=Decimal("0"),
                settlement_fee=Decimal("0"),
                transfer_fee=Decimal("0"),
                other_fee=other_fee,
                shareholder_code=shareholder_code,
                notes=strip_bom(row.get("备注")) or None,
                market_text=market_text,
                settlement_rate=settlement_rate,
            )
        )

    return parsed_rows, business_counts, len(df), errors


def parse_rows_with_warnings(
    contents: bytes,
    filename: str,
) -> tuple[List[ParsedFlow], Dict[str, int], int, List[str], List[str]]:
    """
    Split preserved manual-review rows from errors that make an import unsafe.

    ``parse_rows`` retains its low-level compatibility contract. At the broker
    import boundary, rows explicitly marked for manual review are warnings:
    they can be archived without being guessed into a canonical ledger event.
    Structural parsing and reconciliation failures remain blocking errors.
    """
    parsed_rows, business_counts, total_rows, messages = parse_rows(contents, filename)
    warnings = [
        message
        for message in messages
        if message.endswith(MANUAL_REVIEW_WARNING_SUFFIX)
    ]
    errors = [
        message
        for message in messages
        if not message.endswith(MANUAL_REVIEW_WARNING_SUFFIX)
    ]
    return parsed_rows, business_counts, total_rows, errors, warnings


def flow_to_sample(flow: ParsedFlow, duplicate: bool) -> Dict[str, Any]:
    market = infer_market(flow.security_code, flow.currency, flow.shareholder_code)
    mapped_type = flow.transaction_type or (
        "CASH_DIVIDEND"
        if flow.is_cash_dividend
        else "DIVIDEND_TAX"
        if flow.is_dividend_tax
        else "INTEREST"
        if flow.is_cash_interest
        else ""
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
    hashes: Iterable[str],
    broker_account_id: Optional[int] = None,
) -> set[str]:
    hash_list = list(hashes)
    if not hash_list:
        return set()
    query = db.query(BrokerFundFlow.row_hash).filter(
        BrokerFundFlow.user_id == user_id,
        BrokerFundFlow.row_hash.in_(hash_list),
    )
    if broker_account_id is not None:
        query = query.filter(BrokerFundFlow.broker_account_id == broker_account_id)
    rows = query.all()
    return {row[0] for row in rows}


def _all_sources_for_action(
    db: Session,
    *,
    user_id: int,
    action: CorporateAction,
) -> Optional[List[BrokerFundFlow]]:
    note_hashes = set(ROW_HASH_NOTE_PATTERN.findall(action.notes or ""))
    query = db.query(BrokerFundFlow).filter(
        BrokerFundFlow.user_id == user_id,
        BrokerFundFlow.broker == BROKER_NAME,
    )
    if note_hashes:
        query = query.filter(
            (BrokerFundFlow.corporate_action_id == action.id)
            | BrokerFundFlow.row_hash.in_(note_hashes)
        )
    else:
        query = query.filter(BrokerFundFlow.corporate_action_id == action.id)
    sources = query.order_by(BrokerFundFlow.id).all()

    sources_by_hash: Dict[str, List[BrokerFundFlow]] = defaultdict(list)
    for source in sources:
        sources_by_hash[source.row_hash].append(source)
    if any(len(sources_by_hash[row_hash]) != 1 for row_hash in note_hashes):
        return None

    return [
        source
        for source in sources
        if _event_kind(
            source.business_name,
            security_code=source.security_code or "",
            amount=_normalized_decimal(source.amount),
        )
        in {"CASH_DIVIDEND", "DIVIDEND_TAX"}
    ]


def claim_unassigned_exact_pdf_sources(
    db: Session,
    *,
    user_id: int,
    broker_account_id: int,
    parsed_rows: Iterable[ParsedFlow],
) -> ExactClaimResult:
    """
    Assign exact PDF source rows created before broker accounts existed.

    Linked economic records must still agree with the immutable source row.
    Otherwise the import stops instead of silently creating a second record.
    """
    rows = list(parsed_rows)
    hashes = [flow.row_hash for flow in rows]
    if not hashes:
        return ExactClaimResult(row_hashes=set())
    sources = (
        db.query(BrokerFundFlow)
        .filter(
            BrokerFundFlow.user_id == user_id,
            BrokerFundFlow.broker == BROKER_NAME,
            BrokerFundFlow.broker_account_id.is_(None),
            BrokerFundFlow.row_hash.in_(hashes),
            (
                (BrokerFundFlow.statement_type == SOURCE_TYPE)
                | BrokerFundFlow.source_filename.ilike("%.pdf")
            ),
        )
        .all()
    )
    sources_by_hash = {source.row_hash: source for source in sources}
    claimed: set[str] = set()
    imported_cash_events = 0
    claimed_actions: Dict[int, CorporateAction] = {}

    for flow in rows:
        source = sources_by_hash.get(flow.row_hash)
        if source is None:
            continue

        if flow.transaction_type:
            transaction = db.get(Transaction, source.transaction_id) if source.transaction_id else None
            if transaction is None or not _transaction_matches_legacy_source(transaction, source):
                raise ValueError(
                    "招商证券旧 PDF 来源行对应交易缺失或已修改；为避免重复记账，本次未导入"
                )
            if transaction.user_id != user_id or transaction.broker_account_id not in (
                None,
                broker_account_id,
            ):
                raise ValueError("招商证券旧 PDF 来源行对应交易已归属其他账户；本次未导入")
            transaction.broker_account_id = broker_account_id

        action = (
            db.get(CorporateAction, source.corporate_action_id)
            if source.corporate_action_id
            else None
        )
        if (flow.is_cash_dividend or flow.is_dividend_tax) and action is None:
            candidates = (
                db.query(CorporateAction)
                .filter(
                    CorporateAction.user_id == user_id,
                    CorporateAction.notes.contains(source.row_hash),
                )
                .all()
            )
            if len(candidates) == 1:
                action = candidates[0]
        if flow.is_cash_dividend or flow.is_dividend_tax:
            if action is None or not _corporate_action_matches_legacy_source(action, source):
                raise ValueError(
                    "招商证券旧 PDF 来源行对应公司行动缺失或已修改；"
                    "为避免重复记账，本次未导入"
                )
            if action.user_id != user_id or action.broker_account_id not in (
                None,
                broker_account_id,
            ):
                raise ValueError("招商证券旧 PDF 来源行对应公司行动已归属其他账户；本次未导入")
            action.broker_account_id = broker_account_id
            source.corporate_action_id = action.id
            claimed_actions[action.id] = action

        if flow.is_cash_interest:
            cash_event = db.get(CashEvent, source.cash_event_id) if source.cash_event_id else None
            if cash_event is not None:
                if (
                    not _cash_event_matches_source(cash_event, source)
                    or cash_event.user_id != user_id
                    or cash_event.broker_account_id not in (None, broker_account_id)
                ):
                    raise ValueError(
                        "招商证券旧 PDF 来源行对应现金收益缺失、已修改或已归属其他账户；"
                        "本次未导入"
                    )
                cash_event.broker_account_id = broker_account_id
            else:
                cash_event = CashEvent(
                    user_id=user_id,
                    broker_account_id=broker_account_id,
                    event_type="INTEREST",
                    amount=flow.amount,
                    currency=flow.currency,
                    event_date=flow.trade_date,
                    notes=(
                        f"{BROKER_NAME}对账单天添利产品红利; "
                        f"row={flow.source_row_number}; row_hash={flow.row_hash}"
                    ),
                )
                db.add(cash_event)
                db.flush()
                source.cash_event_id = cash_event.id
                imported_cash_events += 1

        source.broker_account_id = broker_account_id
        source.statement_type = SOURCE_TYPE
        claimed.add(flow.row_hash)

    db.flush()
    for action in claimed_actions.values():
        action_sources = _all_sources_for_action(
            db,
            user_id=user_id,
            action=action,
        )
        if action_sources is None or not _corporate_action_aggregate_matches_legacy_sources(
            action,
            action_sources,
        ):
            raise ValueError(
                "招商证券旧 PDF 来源行对应公司行动的全部股息、税费或净额聚合不一致；"
                "为避免承接漂移记录，本次未导入"
            )

    return ExactClaimResult(
        row_hashes=claimed,
        imported_cash_events=imported_cash_events,
    )


def _is_legacy_excel_flow(flow: BrokerFundFlow) -> bool:
    filename = (flow.source_filename or "").lower()
    return filename.endswith(LEGACY_EXCEL_SUFFIXES) and flow.statement_type in (
        None,
        LEGACY_EXCEL_STATEMENT_TYPE,
    )


def plan_legacy_excel_matches(
    db: Session,
    *,
    user_id: int,
    broker_account_id: int,
    parsed_rows: List[ParsedFlow],
    existing_hashes: set[str],
) -> Dict[str, LegacyFlowMatch]:
    """
    Match old Excel-backed business records to PDF rows as a multiset.

    The source-row hash remains format-specific. This bridge only reuses an
    existing business record when the securities account and economic facts
    agree exactly. Near matches are rejected before any account assignment.
    """
    pdf_rows = [
        flow
        for flow in parsed_rows
        if (
            (
                flow.transaction_type
                and flow.security_code
                and flow.trade_quantity != 0
                and flow.trade_price > 0
            )
            or flow.is_cash_dividend
            or flow.is_dividend_tax
        )
    ]
    if not pdf_rows:
        return {}

    shareholder_codes = {
        strip_bom(flow.shareholder_code).upper()
        for flow in pdf_rows
        if strip_bom(flow.shareholder_code)
    }
    if not shareholder_codes:
        return {}

    period_start = min(flow.trade_date for flow in pdf_rows)
    period_end = max(flow.trade_date for flow in pdf_rows)
    stored_rows = (
        db.query(BrokerFundFlow)
        .filter(
            BrokerFundFlow.user_id == user_id,
            BrokerFundFlow.broker == BROKER_NAME,
            BrokerFundFlow.trade_date >= period_start,
            BrokerFundFlow.trade_date <= period_end,
            (
                BrokerFundFlow.broker_account_id.is_(None)
                | (BrokerFundFlow.broker_account_id == broker_account_id)
            ),
        )
        .order_by(BrokerFundFlow.id)
        .all()
    )
    legacy_rows = [
        flow
        for flow in stored_rows
        if _is_legacy_excel_flow(flow)
        and strip_bom(flow.shareholder_code).upper() in shareholder_codes
        and _event_kind(
            flow.business_name,
            security_code=flow.security_code or "",
            amount=_normalized_decimal(flow.amount),
        )
    ]
    if not legacy_rows:
        return {}

    existing_sources_by_hash: Dict[str, List[BrokerFundFlow]] = defaultdict(list)
    if existing_hashes:
        existing_source_rows = (
            db.query(BrokerFundFlow)
            .filter(
                BrokerFundFlow.user_id == user_id,
                BrokerFundFlow.broker_account_id == broker_account_id,
                BrokerFundFlow.row_hash.in_(existing_hashes),
            )
            .order_by(BrokerFundFlow.id)
            .all()
        )
        for source in existing_source_rows:
            existing_sources_by_hash[source.row_hash].append(source)

    transaction_ids = {
        flow.transaction_id
        for flow in legacy_rows
        if flow.transaction_id is not None and flow.business_name in TRADE_BUSINESS_MAP
    }
    transactions = {
        transaction.id: transaction
        for transaction in (
            db.query(Transaction)
            .filter(
                Transaction.id.in_(transaction_ids),
                Transaction.user_id == user_id,
            )
            .all()
            if transaction_ids
            else []
        )
    }

    action_hashes = {
        flow.row_hash
        for flow in legacy_rows
        if flow.business_name in DIVIDEND_BUSINESS_NAMES or flow.business_name == TAX_BUSINESS_NAME
    }
    action_hashes.update(
        flow.row_hash
        for flow in pdf_rows
        if flow.row_hash in existing_hashes and (flow.is_cash_dividend or flow.is_dividend_tax)
    )
    actions_by_hash: Dict[str, CorporateAction] = {}
    ambiguous_action_hashes: set[str] = set()
    if action_hashes:
        actions = (
            db.query(CorporateAction)
            .filter(
                CorporateAction.user_id == user_id,
                CorporateAction.action_type == "CASH_DIVIDEND",
            )
            .order_by(CorporateAction.id)
            .all()
        )
        for action in actions:
            for row_hash in ROW_HASH_NOTE_PATTERN.findall(action.notes or ""):
                if row_hash in action_hashes:
                    existing_action = actions_by_hash.get(row_hash)
                    if existing_action is not None and existing_action.id != action.id:
                        ambiguous_action_hashes.add(row_hash)
                    actions_by_hash[row_hash] = action
    if ambiguous_action_hashes:
        raise ValueError("招商证券来源哈希关联到多条公司行动，无法安全承接旧 Excel；本次未导入")

    valid_legacy_matches: List[LegacyFlowMatch] = []
    account_conflicts = 0
    business_record_conflicts = 0
    for flow in legacy_rows:
        if flow.business_name in TRADE_BUSINESS_MAP:
            transaction = transactions.get(flow.transaction_id)
            if transaction is None:
                continue
            if transaction.broker_account_id not in (None, broker_account_id):
                account_conflicts += 1
                continue
            if not _transaction_matches_legacy_source(transaction, flow):
                business_record_conflicts += 1
                continue
            valid_legacy_matches.append(LegacyFlowMatch(source_flow=flow, transaction=transaction))
            continue
        action = actions_by_hash.get(flow.row_hash)
        if action is None:
            continue
        if action.broker_account_id not in (None, broker_account_id):
            account_conflicts += 1
            continue
        if not _corporate_action_matches_legacy_source(action, flow):
            business_record_conflicts += 1
            continue
        valid_legacy_matches.append(LegacyFlowMatch(source_flow=flow, corporate_action=action))

    if account_conflicts:
        raise ValueError(
            "招商证券旧 Excel 来源行与 "
            f"{account_conflicts} 条已归属其他账户的业务记录冲突；本次未导入"
        )
    if business_record_conflicts:
        raise ValueError(
            "招商证券旧 Excel 来源行与 "
            f"{business_record_conflicts} 条已修改的交易或公司行动不一致；"
            "为避免复用漂移记录，本次未导入"
        )

    corporate_actions: Dict[int, CorporateAction] = {}
    for match in valid_legacy_matches:
        if match.corporate_action is None:
            continue
        corporate_actions[match.corporate_action.id] = match.corporate_action

    action_source_hashes = {
        action_id: set(ROW_HASH_NOTE_PATTERN.findall(action.notes or ""))
        for action_id, action in corporate_actions.items()
    }
    all_action_source_hashes = {
        row_hash for hashes in action_source_hashes.values() for row_hash in hashes
    }
    legacy_action_sources_by_hash: Dict[str, List[BrokerFundFlow]] = defaultdict(list)
    if all_action_source_hashes:
        all_action_sources = (
            db.query(BrokerFundFlow)
            .filter(
                BrokerFundFlow.user_id == user_id,
                BrokerFundFlow.broker == BROKER_NAME,
                BrokerFundFlow.row_hash.in_(all_action_source_hashes),
            )
            .order_by(BrokerFundFlow.id)
            .all()
        )
        for source in all_action_sources:
            if _is_legacy_excel_flow(source) and (
                source.business_name in DIVIDEND_BUSINESS_NAMES
                or source.business_name == TAX_BUSINESS_NAME
            ):
                legacy_action_sources_by_hash[source.row_hash].append(source)

    aggregate_conflicts = 0
    aggregate_account_conflicts = 0
    for action_id, action in corporate_actions.items():
        action_sources: List[BrokerFundFlow] = []
        ambiguous_sources = False
        for row_hash in action_source_hashes[action_id]:
            sources = legacy_action_sources_by_hash.get(row_hash, [])
            if len(sources) > 1:
                ambiguous_sources = True
                break
            action_sources.extend(sources)
        if ambiguous_sources or not action_sources:
            aggregate_conflicts += 1
            continue
        if any(
            source.broker_account_id not in (None, broker_account_id) for source in action_sources
        ):
            aggregate_account_conflicts += 1
            continue
        if not all(
            _corporate_action_matches_legacy_source(action, source) for source in action_sources
        ) or not _corporate_action_aggregate_matches_legacy_sources(action, action_sources):
            aggregate_conflicts += 1

    if aggregate_account_conflicts:
        raise ValueError(
            "招商证券旧 Excel 来源行与 "
            f"{aggregate_account_conflicts} 条已归属其他账户的公司行动来源冲突；"
            "本次未导入"
        )
    if aggregate_conflicts:
        raise ValueError(
            "招商证券旧 Excel 来源行与 "
            f"{aggregate_conflicts} 条已修改的公司行动税费或净额不一致；"
            "为避免复用漂移记录，本次未导入"
        )

    if not valid_legacy_matches:
        return {}

    pdf_by_conflict: Dict[tuple[Any, ...], List[ParsedFlow]] = defaultdict(list)
    legacy_by_conflict: Dict[tuple[Any, ...], List[LegacyFlowMatch]] = defaultdict(list)
    for flow in pdf_rows:
        pdf_by_conflict[_parsed_flow_conflict_key(flow)].append(flow)
    for match in valid_legacy_matches:
        legacy_by_conflict[_stored_flow_conflict_key(match.source_flow)].append(match)

    conflict_groups = 0
    for key in pdf_by_conflict.keys() & legacy_by_conflict.keys():
        pdf_counts = Counter(_parsed_flow_economic_key(flow) for flow in pdf_by_conflict[key])
        legacy_counts = Counter(
            _stored_flow_economic_key(match.source_flow) for match in legacy_by_conflict[key]
        )
        if sum((legacy_counts - pdf_counts).values()):
            conflict_groups += 1
    if conflict_groups:
        raise ValueError(
            "招商证券 PDF 与旧 Excel 在 "
            f"{conflict_groups} 个重叠事件组的数量、价格、金额或手续费不一致；"
            "为避免重复记账，本次未导入，请先核对来源记录"
        )

    legacy_by_economic_key: Dict[tuple[Any, ...], Deque[LegacyFlowMatch]] = defaultdict(deque)
    for match in valid_legacy_matches:
        legacy_by_economic_key[_stored_flow_economic_key(match.source_flow)].append(match)

    planned_matches: Dict[str, LegacyFlowMatch] = {}
    existing_business_conflicts = 0
    for flow in pdf_rows:
        candidates = legacy_by_economic_key.get(_parsed_flow_economic_key(flow))
        if candidates:
            match = candidates.popleft()
            if flow.row_hash in existing_hashes:
                existing_sources = existing_sources_by_hash.get(flow.row_hash, [])
                existing_source = existing_sources[0] if len(existing_sources) == 1 else None
                same_business_record = (
                    existing_source is not None
                    and existing_source.broker == BROKER_NAME
                    and existing_source.statement_type == SOURCE_TYPE
                    and (
                        (
                            match.transaction is not None
                            and existing_source.transaction_id == match.transaction.id
                        )
                        or (
                            match.corporate_action is not None
                            and actions_by_hash.get(flow.row_hash) is not None
                            and actions_by_hash[flow.row_hash].id == match.corporate_action.id
                        )
                    )
                )
                if not same_business_record:
                    existing_business_conflicts += 1
                continue
            planned_matches[flow.row_hash] = match
    if existing_business_conflicts:
        raise ValueError(
            "招商证券数据库中同时存在 "
            f"{existing_business_conflicts} 条 PDF 来源和未承接的旧 Excel 业务记录；"
            "无法证明它们共用同一业务记录，本次未导入"
        )
    unmatched_legacy_rows = sum(len(candidates) for candidates in legacy_by_economic_key.values())
    if unmatched_legacy_rows:
        raise ValueError(
            "招商证券旧 Excel 在本份 PDF 覆盖区间内有 "
            f"{unmatched_legacy_rows} 条有效业务记录未找到一一对应的对账单事件；"
            "为避免保留重复或缺失记录，本次未导入"
        )
    return planned_matches


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
    imported_cash_events: int,
    affected_symbols: int,
    errors: List[str],
    warnings: Optional[List[str]] = None,
    migrated_legacy_rows: int = 0,
    migrated_unassigned_rows: int = 0,
) -> Dict[str, Any]:
    trade_rows = [flow for flow in parsed_rows if flow.transaction_type]
    dividend_rows = [flow for flow in parsed_rows if flow.is_cash_dividend]
    tax_rows = [flow for flow in parsed_rows if flow.is_dividend_tax]
    cash_rows = [flow for flow in parsed_rows if flow.is_cash_interest]
    eligible_trade_rows = [
        flow
        for flow in trade_rows
        if flow.security_code and flow.trade_quantity != 0 and flow.trade_price > 0
    ]
    duplicate_rows = [flow for flow in parsed_rows if flow.row_hash in existing_hashes]
    import_rows = [flow for flow in parsed_rows if flow.row_hash not in existing_hashes]
    dates = [flow.trade_date for flow in parsed_rows]
    parsed_source_rows = {flow.source_row_number for flow in parsed_rows}
    source_error_rows = {
        int(match.group(1))
        for error in errors
        if (match := SOURCE_ROW_ERROR_PATTERN.match(error))
        and int(match.group(1)) not in parsed_source_rows
    }
    excluded_rows = [flow for flow in parsed_rows if flow.excluded]
    # 本批新增（非重复）的排除行：complete_import_batch 用它把"预期跳过"
    # 从 unbooked/error 口径中扣除；审计口径 skipped_excluded_rows 仍含重复行。
    excluded_unbooked_rows = [
        flow for flow in excluded_rows if flow.row_hash not in existing_hashes
    ]
    skipped_invalid_rows = len(trade_rows) - len(eligible_trade_rows) + len(source_error_rows)
    skipped_non_trade_rows = max(
        0,
        total_rows
        - len(trade_rows)
        - len(dividend_rows)
        - len(tax_rows)
        - len(cash_rows)
        - len(source_error_rows)
        - len(excluded_rows),
    )

    return {
        "broker": BROKER_NAME,
        "filename": filename,
        "total_rows": total_rows,
        "eligible_trade_rows": len(eligible_trade_rows),
        "eligible_dividend_rows": len(dividend_rows),
        "eligible_tax_rows": len(tax_rows),
        "eligible_cash_rows": len(cash_rows),
        "imported_transactions": imported_transactions,
        "imported_corporate_actions": imported_corporate_actions,
        "imported_tax_adjustments": imported_tax_adjustments,
        "imported_cash_events": imported_cash_events,
        "migrated_legacy_rows": migrated_legacy_rows,
        "migrated_unassigned_rows": migrated_unassigned_rows,
        "duplicate_rows": len(duplicate_rows),
        "skipped_non_trade_rows": skipped_non_trade_rows,
        "skipped_invalid_rows": skipped_invalid_rows,
        "skipped_excluded_rows": len(excluded_rows),
        "excluded_unbooked_rows": len(excluded_unbooked_rows),
        "affected_symbols": affected_symbols,
        "date_start": min(dates).isoformat() if dates else None,
        "date_end": max(dates).isoformat() if dates else None,
        "business_counts": business_counts,
        "source_account_masks": source_account_masks(parsed_rows),
        "duplicate_samples": [flow_to_sample(flow, True) for flow in duplicate_rows[:10]],
        "import_samples": [flow_to_sample(flow, False) for flow in import_rows[:10]],
        "warnings": (warnings or [])[:50],
        "errors": errors[:50],
    }


def apply_exclusions(parsed_rows: List[ParsedFlow], excluded_symbols) -> None:
    """排除清单标记：命中标的的行只归档不入账（预览与正式导入共用）。"""
    if not excluded_symbols:
        return
    for flow in parsed_rows:
        if flow.security_code and flow.security_code in excluded_symbols:
            flow.excluded = True


def preview_cmb_fund_flow(
    db: Session,
    user_id: int,
    contents: bytes,
    filename: str,
    broker_account_id: Optional[int] = None,
) -> Dict[str, Any]:
    if broker_account_id is None:
        raise ValueError("broker_account_id is required for 招商证券 preview")
    account = validate_import_account(
        db,
        user_id=user_id,
        broker_account_id=broker_account_id,
        broker=BROKER_NAME,
    )
    validate_source_file_account(
        db,
        user_id=user_id,
        broker_account_id=broker_account_id,
        broker=BROKER_NAME,
        contents=contents,
    )
    parsed_rows, business_counts, total_rows, errors, warnings = parse_rows_with_warnings(
        contents,
        filename,
    )
    apply_exclusions(parsed_rows, get_excluded_symbols(db, user_id))
    if account is None:
        raise ValueError("broker_account_id is required for 招商证券 preview")
    validate_statement_account_masks(account, parsed_rows)
    savepoint = db.begin_nested()
    try:
        exact_claim = claim_unassigned_exact_pdf_sources(
            db,
            user_id=user_id,
            broker_account_id=broker_account_id,
            parsed_rows=parsed_rows,
        )
        existing_hashes = get_existing_hashes(
            db,
            user_id,
            [flow.row_hash for flow in parsed_rows],
            broker_account_id=broker_account_id,
        )
        existing_hashes.update(exact_claim.row_hashes)
        legacy_matches = plan_legacy_excel_matches(
            db,
            user_id=user_id,
            broker_account_id=broker_account_id,
            parsed_rows=parsed_rows,
            existing_hashes=existing_hashes,
        )
        duplicate_hashes = set(existing_hashes) | set(legacy_matches)
    finally:
        if savepoint.is_active:
            savepoint.rollback()
        db.expire_all()

    return build_import_result(
        filename=filename,
        total_rows=total_rows,
        parsed_rows=parsed_rows,
        business_counts=business_counts,
        existing_hashes=duplicate_hashes,
        imported_transactions=0,
        imported_corporate_actions=0,
        imported_tax_adjustments=0,
        imported_cash_events=0,
        affected_symbols=0,
        errors=errors,
        warnings=warnings,
        migrated_legacy_rows=len(legacy_matches),
        migrated_unassigned_rows=len(exact_claim.row_hashes),
    )


def create_broker_fund_flow(
    *,
    user_id: int,
    broker_account_id: int,
    filename: str,
    flow: ParsedFlow,
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
        remaining_quantity=flow.remaining_quantity,
        statement_type=SOURCE_TYPE,
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
        settlement_rate=flow.settlement_rate,
        shareholder_code=flow.shareholder_code,
        notes=flow.notes,
    )


def find_dividend_for_tax(
    db: Session,
    user_id: int,
    flow: ParsedFlow,
    market: str,
    broker_account_id: Optional[int] = None,
) -> Optional[CorporateAction]:
    query = db.query(CorporateAction).filter(
        CorporateAction.user_id == user_id,
        CorporateAction.symbol == flow.security_code,
        CorporateAction.market == market,
        CorporateAction.action_type == "CASH_DIVIDEND",
        CorporateAction.currency == flow.currency,
        CorporateAction.ex_date <= flow.trade_date,
        CorporateAction.broker_account_id == broker_account_id,
    )
    candidates = query.order_by(
        CorporateAction.ex_date.desc(),
        CorporateAction.id.desc(),
    ).all()
    return candidates[0] if len(candidates) == 1 else None


def _source_sequence_value(value: Any) -> tuple[int, Any]:
    text = strip_bom(value)
    if not text:
        return (2, "")
    try:
        return (0, int(text))
    except ValueError:
        return (1, text)


def validate_account_positions_before_commit(
    db: Session,
    *,
    user_id: int,
    broker_account_id: int,
) -> None:
    """Reject an account ledger that requires an unrecorded opening position."""
    transactions = (
        db.query(Transaction)
        .filter(
            Transaction.user_id == user_id,
            Transaction.broker_account_id == broker_account_id,
        )
        .all()
    )
    transaction_ids = [transaction.id for transaction in transactions if transaction.id]
    sources_by_transaction_id: Dict[int, BrokerFundFlow] = {}
    if transaction_ids:
        sources = (
            db.query(BrokerFundFlow)
            .filter(
                BrokerFundFlow.user_id == user_id,
                BrokerFundFlow.transaction_id.in_(transaction_ids),
            )
            .order_by(BrokerFundFlow.id)
            .all()
        )
        for source in sources:
            sources_by_transaction_id.setdefault(source.transaction_id, source)

    quantity_actions = (
        db.query(CorporateAction)
        .filter(
            CorporateAction.user_id == user_id,
            CorporateAction.broker_account_id == broker_account_id,
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

    events: List[tuple[Any, ...]] = []
    for action in quantity_actions:
        events.append(
            (
                action.ex_date,
                0,
                (0, 0),
                (0, 0),
                0,
                action.id or 0,
                "ACTION",
                action,
            )
        )
    for transaction in transactions:
        source = sources_by_transaction_id.get(transaction.id)
        events.append(
            (
                transaction.transaction_date,
                1 if transaction.transaction_type == "BUY" else 2,
                _source_sequence_value(source.serial_number if source else None),
                _source_sequence_value(source.contract_number if source else None),
                source.source_row_number if source and source.source_row_number else 0,
                transaction.id or 0,
                transaction.transaction_type,
                transaction,
            )
        )

    positions: Dict[tuple[str, str], Decimal] = {}
    for _, _, _, _, _, _, event_type, event in sorted(events):
        key = (event.symbol, event.market)
        quantity = positions.get(key, Decimal("0"))
        if event_type == "BUY":
            quantity += abs(_normalized_decimal(event.quantity))
        elif event_type == "SELL":
            sell_quantity = abs(_normalized_decimal(event.quantity))
            if sell_quantity > quantity:
                raise ValueError(
                    "招商证券账户持仓预检失败："
                    f"{event.symbol} {event.market} 在 {event.transaction_date} "
                    f"卖出 {format(sell_quantity, 'f')}，"
                    f"但账户内可用数量仅 {format(quantity, 'f')}；"
                    "缺少期初持仓或证券转入记录，整批未导入"
                )
            quantity -= sell_quantity
        elif event.action_type in {"STOCK_DIVIDEND", "BONUS_ISSUE"}:
            quantity += _normalized_decimal(event.shares_received)
        elif event.action_type == "RIGHTS_ISSUE":
            quantity += _normalized_decimal(event.subscription_quantity)
        elif event.action_type in {"STOCK_SPLIT", "REVERSE_SPLIT"} and event.split_ratio:
            try:
                old_shares, new_shares = event.split_ratio.split(":")
                quantity *= Decimal(new_shares) / Decimal(old_shares)
            except (InvalidOperation, ValueError, ZeroDivisionError):
                pass
        positions[key] = quantity


def import_cmb_fund_flow(
    db: Session,
    user_id: int,
    contents: bytes,
    filename: str,
    broker_account_id: Optional[int] = None,
) -> Dict[str, Any]:
    validate_cmb_statement_filename(filename)
    if broker_account_id is None:
        raise ValueError("broker_account_id is required for formal 招商证券 imports")

    batch = start_import_batch(
        db,
        user_id=user_id,
        broker_account_id=broker_account_id,
        broker=BROKER_NAME,
        source_type=SOURCE_TYPE,
        filename=filename,
        contents=contents,
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
    )
    batch_id = batch.id
    total_rows = 0
    imported_source_rows = 0
    records_committed = False
    imported_count = 0
    imported_corporate_actions = 0
    imported_tax_adjustments = 0
    imported_cash_events = 0

    try:
        parsed_rows, business_counts, total_rows, errors, warnings = parse_rows_with_warnings(
            contents,
            filename,
        )
        apply_exclusions(parsed_rows, get_excluded_symbols(db, user_id))
        account = validate_import_account(
            db,
            user_id=user_id,
            broker_account_id=broker_account_id,
            broker=BROKER_NAME,
        )
        if account is None:
            raise ValueError("broker_account_id is required for formal 招商证券 imports")
        validate_statement_account_masks(account, parsed_rows)
        dates = [flow.trade_date for flow in parsed_rows]
        set_import_batch_source_stats(
            batch,
            row_count=total_rows,
            period_start=min(dates) if dates else None,
            period_end=max(dates) if dates else None,
        )
        exact_claim = claim_unassigned_exact_pdf_sources(
            db,
            user_id=user_id,
            broker_account_id=broker_account_id,
            parsed_rows=parsed_rows,
        )
        existing_hashes = get_existing_hashes(
            db,
            user_id,
            [flow.row_hash for flow in parsed_rows],
            broker_account_id=broker_account_id,
        )
        existing_hashes.update(exact_claim.row_hashes)
        duplicate_hashes = set(existing_hashes)
        legacy_matches = plan_legacy_excel_matches(
            db,
            user_id=user_id,
            broker_account_id=broker_account_id,
            parsed_rows=parsed_rows,
            existing_hashes=existing_hashes,
        )

        imported_cash_events = exact_claim.imported_cash_events
        migrated_legacy_rows = 0
        affected_symbols: set[tuple[str, str]] = set()

        for flow in parsed_rows:
            if flow.row_hash in existing_hashes:
                continue

            legacy_match = legacy_matches.get(flow.row_hash)
            if legacy_match is not None:
                legacy_source = legacy_match.source_flow
                legacy_source.broker_account_id = broker_account_id
                legacy_source.statement_type = LEGACY_EXCEL_STATEMENT_TYPE
                if legacy_match.transaction is not None:
                    legacy_match.transaction.broker_account_id = broker_account_id
                if legacy_match.corporate_action is not None:
                    legacy_match.corporate_action.broker_account_id = broker_account_id
                    legacy_source.corporate_action_id = legacy_match.corporate_action.id
                    action_hashes = set(
                        ROW_HASH_NOTE_PATTERN.findall(legacy_match.corporate_action.notes or "")
                    )
                    if flow.row_hash not in action_hashes:
                        legacy_match.corporate_action.notes = (
                            f"{legacy_match.corporate_action.notes or ''}; row_hash={flow.row_hash}"
                        ).strip("; ")

                if legacy_source.row_hash != flow.row_hash:
                    db.add(
                        create_broker_fund_flow(
                            user_id=user_id,
                            broker_account_id=broker_account_id,
                            filename=filename,
                            flow=flow,
                            import_batch_id=batch_id,
                            transaction_id=(
                                legacy_match.transaction.id
                                if legacy_match.transaction is not None
                                else None
                            ),
                            corporate_action_id=(
                                legacy_match.corporate_action.id
                                if legacy_match.corporate_action is not None
                                else None
                            ),
                        )
                    )
                existing_hashes.add(flow.row_hash)
                duplicate_hashes.add(flow.row_hash)
                migrated_legacy_rows += 1
                continue

            market = infer_market(flow.security_code, flow.currency, flow.shareholder_code)

            if flow.is_cash_interest:
                cash_event = CashEvent(
                    user_id=user_id,
                    broker_account_id=broker_account_id,
                    event_type="INTEREST",
                    amount=flow.amount,
                    currency=flow.currency,
                    event_date=flow.trade_date,
                    notes=(
                        f"{BROKER_NAME}对账单天添利产品红利; "
                        f"row={flow.source_row_number}; row_hash={flow.row_hash}"
                    ),
                )
                db.add(cash_event)
                db.flush()
                db.add(
                    create_broker_fund_flow(
                        user_id=user_id,
                        broker_account_id=broker_account_id,
                        filename=filename,
                        flow=flow,
                        import_batch_id=batch_id,
                        cash_event_id=cash_event.id,
                    )
                )
                existing_hashes.add(flow.row_hash)
                imported_cash_events += 1
                continue

            if flow.is_cash_dividend:
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
                    total_dividend=flow.amount,
                    tax_withheld=Decimal("0"),
                    net_dividend=flow.amount,
                    currency=flow.currency,
                    notes=(
                        f"{BROKER_NAME}对账单; 流水号={flow.serial_number or ''}; "
                        f"合同编号={flow.contract_number or ''}; 业务={flow.business_name}; "
                        f"row_hash={flow.row_hash}"
                    ),
                )
                db.add(action)
                db.flush()
                db.add(
                    create_broker_fund_flow(
                        user_id=user_id,
                        broker_account_id=broker_account_id,
                        filename=filename,
                        flow=flow,
                        import_batch_id=batch_id,
                        corporate_action_id=action.id,
                    )
                )
                existing_hashes.add(flow.row_hash)
                imported_corporate_actions += 1
                continue

            if flow.is_dividend_tax:
                action = find_dividend_for_tax(
                    db,
                    user_id,
                    flow,
                    market,
                    broker_account_id=broker_account_id,
                )
                if not action:
                    warnings.append(
                        f"row {flow.source_row_number}: expected exactly one account-scoped "
                        f"dividend for tax on {flow.security_code}; source preserved unlinked"
                    )
                    db.add(
                        create_broker_fund_flow(
                            user_id=user_id,
                            broker_account_id=broker_account_id,
                            filename=filename,
                            flow=flow,
                            import_batch_id=batch_id,
                        )
                    )
                    existing_hashes.add(flow.row_hash)
                    continue
                tax_amount = abs(flow.amount)
                action.tax_withheld = (action.tax_withheld or Decimal("0")) + tax_amount
                if action.total_dividend is not None:
                    action.net_dividend = max(
                        Decimal("0"), action.total_dividend - action.tax_withheld
                    )
                action.notes = (
                    f"{action.notes or ''}; {BROKER_NAME}红利税补缴 "
                    f"流水号={flow.serial_number or ''}; row_hash={flow.row_hash}"
                ).strip("; ")
                db.add(
                    create_broker_fund_flow(
                        user_id=user_id,
                        broker_account_id=broker_account_id,
                        filename=filename,
                        flow=flow,
                        import_batch_id=batch_id,
                        corporate_action_id=action.id,
                    )
                )
                existing_hashes.add(flow.row_hash)
                imported_tax_adjustments += 1
                continue

            if (
                not flow.transaction_type
                or not flow.security_code
                or flow.trade_quantity == 0
                or flow.trade_price <= 0
            ):
                db.add(
                    create_broker_fund_flow(
                        user_id=user_id,
                        broker_account_id=broker_account_id,
                        filename=filename,
                        flow=flow,
                        import_batch_id=batch_id,
                    )
                )
                existing_hashes.add(flow.row_hash)
                continue

            transaction_notes = (
                f"{BROKER_NAME}对账单; 流水号={flow.serial_number or ''}; "
                f"合同编号={flow.contract_number or ''}; 业务={flow.business_name}"
            )
            if flow.is_hk_connect and flow.settlement_rate:
                # 港股通以 HKD 记账；CNY 结算金额与推导汇率留在 notes 供审计
                transaction_notes += (
                    f"; {flow.market_text}; 结算币种=CNY; "
                    f"结算金额={flow.amount}; 推导结算汇率={flow.settlement_rate}"
                )
            transaction = Transaction(
                user_id=user_id,
                broker_account_id=broker_account_id,
                import_batch_id=batch_id,
                symbol=flow.security_code,
                name=flow.security_name,
                market=market,
                transaction_type=flow.transaction_type,
                quantity=abs(flow.trade_quantity),
                price=flow.trade_price,
                fee=flow.effective_fee,
                transaction_date=flow.trade_date,
                currency=flow.effective_currency,
                notes=transaction_notes,
            )
            db.add(transaction)
            db.flush()

            db.add(
                create_broker_fund_flow(
                    user_id=user_id,
                    broker_account_id=broker_account_id,
                    filename=filename,
                    flow=flow,
                    import_batch_id=batch_id,
                    transaction_id=transaction.id,
                )
            )
            existing_hashes.add(flow.row_hash)
            affected_symbols.add((flow.security_code, market))
            imported_count += 1

        db.flush()
        validate_account_positions_before_commit(
            db,
            user_id=user_id,
            broker_account_id=broker_account_id,
        )

        try:
            db.commit()
        except IntegrityError as exc:
            raise ValueError("Duplicate broker fund flow detected during import") from exc
        records_committed = True

        recalculated_symbols = 0
        for symbol, market in affected_symbols:
            try:
                recalculate_holdings(db, user_id, symbol, market)
                recalculated_symbols += 1
            except ValueError as exc:
                errors.append(f"{symbol} {market}: {exc}")

        result = build_import_result(
            filename=filename,
            total_rows=total_rows,
            parsed_rows=parsed_rows,
            business_counts=business_counts,
            existing_hashes=duplicate_hashes,
            imported_transactions=imported_count,
            imported_corporate_actions=imported_corporate_actions,
            imported_tax_adjustments=imported_tax_adjustments,
            imported_cash_events=imported_cash_events,
            affected_symbols=recalculated_symbols,
            errors=errors,
            warnings=warnings,
            migrated_legacy_rows=migrated_legacy_rows,
            migrated_unassigned_rows=len(exact_claim.row_hashes),
        )
        imported_source_rows = (
            db.query(BrokerFundFlow).filter(BrokerFundFlow.import_batch_id == batch_id).count()
        )
        result["archived_source_rows"] = imported_source_rows
        completed_batch = complete_import_batch(
            db,
            batch_id,
            result=result,
            imported_count=(
                imported_count
                + imported_corporate_actions
                + imported_tax_adjustments
                + imported_cash_events
            ),
            archived_count=imported_source_rows,
        )
        result.update(
            {
                "import_batch_id": completed_batch.id,
                "broker_account_id": completed_batch.broker_account_id,
                "batch_status": completed_batch.status,
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
        fail_import_batch(
            db,
            batch_id,
            exc,
            records_committed=records_committed,
            row_count=total_rows,
            imported_count=(
                imported_count
                + imported_corporate_actions
                + imported_tax_adjustments
                + imported_cash_events
                if records_committed
                else 0
            ),
            archived_count=imported_source_rows if records_committed else 0,
        )
        raise
