from __future__ import annotations

import io
import re
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd
import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pypdf import PdfReader
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..models.broker_account import BrokerAccount
from ..models.broker_fund_flow import BrokerFundFlow
from ..models.cash_event import CashEvent
from ..models.corporate_action import CorporateAction
from ..models.transaction import Transaction
from ..services import broker_import_common
from ..services.broker_import_common import (
    RESULT_SAMPLE_LIMIT,
    archived_row_count,
    base_import_result,
    disambiguated_row_hash,
    iso_date_range,
    ProspectiveTransaction,
    attribute_tax_source,
    find_dividend_for_tax,
    load_unattributed_tax_sources,
    mark_unattributed_tax,
    source_error_rows,
)
from ..services.security_rule_service import (
    get_cash_management_symbols,
    get_cmb_cash_business_map,
    get_excluded_symbols,
)
from ..services.holding_service import (
    UNPERSISTED_SORT_ID,
    load_account_quantity_actions,
    recalculate_holdings,
    replay_account_quantities,
)
from ..services.portfolio.semantics import QUANTITY_ACTION_TYPES
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
# 行为变化必须升版（ImportBatch 按 parser/version 审计入账口径）。
# 变更清单必须与版本号同步维护——半截历史比没有更误导：
#   v7  = 开放基金申购/新股入账 生成规范 BUY（此前仅归档）
#   v8  = 账本特例规则改为表驱动（security_rules），排除标的等不再硬编码（2a27883）
#   v9  = 现金管理标的排除清单接入（ebe4d48）
#   v10 = 现金业务行入账为 CashEvent 并回填归档历史（cc5f7b9）
#   v11 = 未归属红利税行改为可恢复（skip_reason=unattributed_tax）：不再计入
#         判重的"已入账"，补齐股息后重导会在原归档行上就地转正（#132 子项 B）
PARSER_VERSION = "11"
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
# CASH_INFLOW_EVENT_TYPES 是事件类型→方向的语义（非账本特例），保持硬编码；
# 业务名→事件类型映射已表驱动（security_rules CMB_CASH_BUSINESS）。
CASH_INFLOW_EVENT_TYPES = {"DEPOSIT", "INTEREST", "OTHER", "TRANSFER_IN"}
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
PDF_AMOUNT_TOLERANCE = Decimal("0.02")
MANUAL_REVIEW_WARNING_SUFFIX = "manual review required"
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
    # 现金管理产品标记（security_rules CASH_MANAGEMENT 类型 pre-pass 置位）：
    # 该标的的"产品红利发放"按利息入账而非股息。与 excluded 互斥，排除优先。
    is_cash_management_symbol: bool = False
    # 现金业务事件类型（security_rules CMB_CASH_BUSINESS 类型 pre-pass 置位）：
    # None = 业务名不在映射内，行保持归档（fail-open）。
    cash_event_type: Optional[str] = None

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
            and not self.is_cash_management_symbol
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
    def is_cash_business(self) -> bool:
        """现金业务行：映射内业务名、非零金额、方向与事件类型一致、未被排除。

        方向不符的行在 parse 阶段已产出阻断错误；这里再次否决入账资格，
        确保即使错误被上层忽略也只会归档、绝不 abs() 成反向事件。"""
        if self.excluded or self.amount == 0:
            return False
        if self.cash_event_type is None:
            return False
        if self.cash_event_type in CASH_INFLOW_EVENT_TYPES:
            return self.amount > 0
        return self.amount < 0

    @property
    def is_cash_interest(self) -> bool:
        return (
            not self.excluded
            and self.business_name == PRODUCT_DIVIDEND_BUSINESS_NAME
            and self.is_cash_management_symbol
            and self.amount > 0
        )

    @property
    def becomes_transaction(self) -> bool:
        """本行是否会入账成一笔 Transaction（其余形态各自归档或建现金/行动记录）。

        导入循环的分支链与预览的"待入账交易"构造共用这一个判据。**不要**在预览
        里另写一份：#132 的主题正是"同一问题不同答案"，两份映射一旦漂移，预览
        就会重新变成不可信的——而这恰恰是它要修的毛病。
        """
        return (
            not self.is_cash_interest
            and not self.is_cash_business
            and not self.is_cash_dividend
            and not self.is_dividend_tax
            and bool(self.transaction_type)
            and bool(self.security_code)
            and self.trade_quantity != 0
            and self.trade_price > 0
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


def parse_strict_pdf_decimal(value: Any) -> Optional[Decimal]:
    return broker_import_common.parse_strict_decimal(value, strip=strip_bom)


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
    return broker_import_common.normalize_hash_value(value, strip=strip_bom)


def calculate_row_hash(values: Dict[str, Any]) -> str:
    return broker_import_common.calculate_row_hash(values, HASH_FIELDS, strip=strip_bom)


def _normalized_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(value).normalize()


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


def _extract_pdf_flow_rows(
    contents: bytes, *, provenance: Optional[List[Dict[str, Any]]] = None
) -> List[Dict[str, str]]:
    """提取流水明细行。

    provenance 是**只增不改**的诊断出参：传入一个列表就会收到与返回值逐下标
    对齐的行来源（页码、y 坐标、是否走了无节标题回退）。返回值、DataFrame 与
    row_hash 完全不受影响——出错行在 parse_rows 里 `continue` 后零留痕，
    没有这个出参就无法回答"那一行到底来自哪一页、哪个章节"。
    """
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
            for page_index, words in enumerate(page_words):
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
                        if provenance is not None:
                            provenance.append(
                                {
                                    "page_index": page_index,
                                    "top": round(float(anchor["top"]), 1),
                                    "word_count": len(row_words),
                                    "columns_filled": len(row),
                                    "section_fallback": not has_section_titles,
                                }
                            )
    except PDFPasswordIncorrect as exc:
        raise ValueError("PDF is encrypted. Please decrypt it with qpdf before importing.") from exc

    if not extracted_rows:
        raise ValueError("No 招商证券流水明细 rows found in PDF")
    return extracted_rows


def read_cmb_statement_pdf(
    contents: bytes, *, provenance: Optional[List[Dict[str, Any]]] = None
) -> pd.DataFrame:
    rows = _extract_pdf_flow_rows(contents, provenance=provenance)
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
    contents: bytes,
    filename: str,
    *,
    cash_management_symbols: frozenset = frozenset(),
    cash_business_map: Optional[Dict[str, str]] = None,
) -> tuple[List[ParsedFlow], Dict[str, int], int, List[str]]:
    """解析对账单。特例规则（现金管理标的/现金业务映射）由调用方注入，
    保持本函数无 DB 依赖；缺省空规则 = 股息按股息、现金业务只归档。"""
    cash_business_map = cash_business_map or {}
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
            and security_code not in cash_management_symbols
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

        cash_event_type = cash_business_map.get(business_name)
        if cash_event_type and amount != 0:
            # 方向由映射的事件类型承担，符号必须一致（防对账单口径漂移静默入错账）
            expect_inflow = cash_event_type in CASH_INFLOW_EVENT_TYPES
            if expect_inflow and amount < 0:
                errors.append(
                    f"row {row_number}: {business_name} 应为流入但金额为负"
                )
            if not expect_inflow and amount > 0:
                errors.append(
                    f"row {row_number}: {business_name} 应为流出但金额为正"
                )

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

        row_hash = disambiguated_row_hash(
            hash_values, pdf_hash_occurrences, calculate_row_hash
        )

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
                is_cash_management_symbol=security_code in cash_management_symbols,
                cash_event_type=cash_event_type,
            )
        )

    return parsed_rows, business_counts, len(df), errors


def parse_rows_with_warnings(
    contents: bytes,
    filename: str,
    *,
    cash_management_symbols: frozenset = frozenset(),
    cash_business_map: Optional[Dict[str, str]] = None,
) -> tuple[List[ParsedFlow], Dict[str, int], int, List[str], List[str]]:
    """
    Split preserved manual-review rows from errors that make an import unsafe.

    ``parse_rows`` retains its low-level compatibility contract. At the broker
    import boundary, rows explicitly marked for manual review are warnings:
    they can be archived without being guessed into a canonical ledger event.
    Structural parsing and reconciliation failures remain blocking errors.
    """
    parsed_rows, business_counts, total_rows, messages = parse_rows(
        contents,
        filename,
        cash_management_symbols=cash_management_symbols,
        cash_business_map=cash_business_map,
    )
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
        # 未归属税行虽已归档，但**不算已入账**：把它当重复行跳过的话，
        # 补齐股息后重导同一对账单也补不回 tax_withheld（永久失联）。
        BrokerFundFlow.skip_reason.is_(None),
    )
    if broker_account_id is not None:
        query = query.filter(BrokerFundFlow.broker_account_id == broker_account_id)
    rows = query.all()
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
    imported_cash_events: int,
    affected_symbols: int,
    errors: List[str],
    warnings: Optional[List[str]] = None,
) -> Dict[str, Any]:
    trade_rows = [flow for flow in parsed_rows if flow.transaction_type]
    dividend_rows = [flow for flow in parsed_rows if flow.is_cash_dividend]
    tax_rows = [flow for flow in parsed_rows if flow.is_dividend_tax]
    cash_rows = [
        flow for flow in parsed_rows if flow.is_cash_interest or flow.is_cash_business
    ]
    eligible_trade_rows = [
        flow
        for flow in trade_rows
        if flow.security_code and flow.trade_quantity != 0 and flow.trade_price > 0
    ]
    # 招商现状没有同批判重：duplicate 只对库内 hash 判定（勿改用
    # split_new_and_duplicate_rows"顺手统一"，那是行为变更）。
    duplicate_rows = [flow for flow in parsed_rows if flow.row_hash in existing_hashes]
    import_rows = [flow for flow in parsed_rows if flow.row_hash not in existing_hashes]
    parsed_source_rows = {flow.source_row_number for flow in parsed_rows}
    error_rows = source_error_rows(errors, parsed_source_rows)
    excluded_rows = [flow for flow in parsed_rows if flow.excluded]
    # 本批新增（非重复）的排除行：complete_import_batch 用它把"预期跳过"
    # 从 unbooked/error 口径中扣除；审计口径 skipped_excluded_rows 仍含重复行。
    excluded_unbooked_rows = [
        flow for flow in excluded_rows if flow.row_hash not in existing_hashes
    ]
    skipped_invalid_rows = len(trade_rows) - len(eligible_trade_rows) + len(error_rows)
    skipped_non_trade_rows = max(
        0,
        total_rows
        - len(trade_rows)
        - len(dividend_rows)
        - len(tax_rows)
        - len(cash_rows)
        - len(error_rows)
        - len(excluded_rows),
    )

    date_start, date_end = iso_date_range([flow.trade_date for flow in parsed_rows])
    result = base_import_result(
        broker=BROKER_NAME,
        filename=filename,
        total_rows=total_rows,
        eligible_trade_rows=len(eligible_trade_rows),
        eligible_dividend_rows=len(dividend_rows),
        eligible_tax_rows=len(tax_rows),
        eligible_cash_rows=len(cash_rows),
        imported_transactions=imported_transactions,
        imported_corporate_actions=imported_corporate_actions,
        imported_tax_adjustments=imported_tax_adjustments,
        imported_cash_events=imported_cash_events,
        duplicate_rows=len(duplicate_rows),
        skipped_non_trade_rows=skipped_non_trade_rows,
        skipped_invalid_rows=skipped_invalid_rows,
        skipped_excluded_rows=len(excluded_rows),
        excluded_unbooked_rows=len(excluded_unbooked_rows),
        affected_symbols=affected_symbols,
        date_start=date_start,
        date_end=date_end,
        business_counts=business_counts,
        duplicate_samples=[
            flow_to_sample(flow, True) for flow in duplicate_rows[:RESULT_SAMPLE_LIMIT]
        ],
        import_samples=[
            flow_to_sample(flow, False) for flow in import_rows[:RESULT_SAMPLE_LIMIT]
        ],
        errors=errors,
        warnings=warnings,
    )
    result["source_account_masks"] = source_account_masks(parsed_rows)
    return result


# ---------------------------------------------------------------------------
# 脱敏诊断报告
#
# 为什么需要：出错行在 parse_rows 里 `continue`，既不产出 ParsedFlow 也不落
# broker_fund_flows，报错消息又只带行号——除了拿到那份 PDF 重跑，没有任何归因
# 手段。而报障者不可能把对账单发出来（里面是全部持仓与金额）。
#
# 本函数是**纯只读的第二遍分析**：重新解析一次，不参与入账、不改任何解析口径，
# 因此不动 PARSER_VERSION、不碰 row_hash。
# ---------------------------------------------------------------------------

DIAGNOSTIC_PATTERN_COLUMNS = [
    "成交价格",
    "成交数量",
    "PDF成交金额",
    "发生金额",
    "佣金",
    "印花税",
    "其他费用",
    "资金余额",
    "剩余数量",
]
DIAGNOSTIC_PATTERN_TOP_N = 5
#: 费用三列——`fees_all_zero` 的判定必须以它们**全部**解析成功为前提
DIAGNOSTIC_FEE_COLUMNS = ("佣金", "印花税", "其他费用")


#: 标签列一旦发生列错位，挤进来的就是这些列的值——它们不能原样回传。
#: 用规范化后的列名（PDF 的「证券账号」在 read_cmb_statement_pdf 里落成「股东代码」）
DIAGNOSTIC_SENSITIVE_COLUMNS = ("证券名称", "证券代码", "股东代码")


def _diagnostic_decimal(value: Any) -> Optional[Decimal]:
    return parse_strict_pdf_decimal(value)


def _diagnostic_label(value: Any, record: Dict[str, Any]) -> str:
    """标签列（市场/币种/业务名称）的回传形式。

    正常情况下这些是券商词表，原样透出正是排查所需。但**列错位恰恰是本次要
    排查的假设之一**——边界一偏，证券名称或证券账号就会落进"市场"列，再被
    词表原样抄进报告。

    所以：值若与本行某个敏感列相同，只报「哪一列溢出来了」。这比抄出值本身
    更有诊断价值（直接点名错位方向），且不泄露持仓与账号。其余一律过
    `digit_class`，让账号形状（`Addddddddd`）可见而数字不可见。
    """
    text = strip_bom(value)
    if not text:
        return ""
    for column in DIAGNOSTIC_SENSITIVE_COLUMNS:
        if text == strip_bom(record.get(column)):
            return f"<spilled:{column}>"
    return broker_import_common.digit_class(text, strip=strip_bom)


def _diagnostic_error_row(
    *,
    row_number: int,
    message: str,
    row: Optional[Dict[str, Any]],
    provenance: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "row_number": row_number,
        "message": message,
        "row_found": row is not None,
    }
    if provenance:
        record.update(
            {
                "page_index": provenance.get("page_index"),
                "top": provenance.get("top"),
                "word_count": provenance.get("word_count"),
                "columns_filled": provenance.get("columns_filled"),
                "section_fallback": provenance.get("section_fallback"),
            }
        )
    if row is None:
        return record

    market_text = strip_bom(row.get("市场"))
    security_code = strip_bom(row.get("证券代码"))
    record.update(
        {
            # 标签类保留词表可读性，但列错位挤进来的敏感值只报"哪一列溢出"
            "market": _diagnostic_label(row.get("市场"), row),
            "currency": _diagnostic_label(row.get("币种"), row),
            "business": _diagnostic_label(row.get("业务名称"), row),
            "shareholder_code_masked": broker_import_common.mask_code(
                strip_bom(row.get("股东代码")), keep=2
            ),
            "security_code_masked": broker_import_common.mask_code(security_code, keep=2),
            "security_code_length": len(security_code),
            "security_name_length": len(strip_bom(row.get("证券名称"))),
            "raw_patterns": {
                column: broker_import_common.digit_class(row.get(column), strip=strip_bom)
                for column in DIAGNOSTIC_PATTERN_COLUMNS
            },
            # main 上的现行判据——用来直接证实/证伪"港股通写法没认出来"这一假设
            "is_hk_connect_by_current_rule": market_text in HK_CONNECT_MARKET_NAMES,
        }
    )

    price = _diagnostic_decimal(row.get("成交价格"))
    quantity = _diagnostic_decimal(row.get("成交数量"))
    trade_amount = _diagnostic_decimal(row.get("PDF成交金额"))
    amount = _diagnostic_decimal(row.get("发生金额"))
    fees = [_diagnostic_decimal(row.get(column)) for column in DIAGNOSTIC_FEE_COLUMNS]
    unparsed_fee_columns = [
        column for column, fee in zip(DIAGNOSTIC_FEE_COLUMNS, fees) if fee is None
    ]
    price_text = strip_bom(row.get("成交价格"))
    decimal_places = len(price_text.rsplit(".", 1)[1]) if "." in price_text else 0

    gross = abs(quantity) * price if (quantity is not None and price is not None) else None
    tolerance = (
        abs(quantity) * Decimal("0.5").scaleb(-decimal_places) + PDF_AMOUNT_TOLERANCE
        if quantity is not None
        else None
    )
    deviation = (
        abs(trade_amount - gross) if (trade_amount is not None and gross is not None) else None
    )
    record.update(
        {
            "price_decimal_places": decimal_places,
            "quantity_sign": None if quantity is None else int(quantity.compare(Decimal(0))),
            "quantity_magnitude": broker_import_common.magnitude(
                None if quantity is None else abs(quantity)
            ),
            "trade_amount_magnitude": broker_import_common.magnitude(trade_amount),
            "amount_magnitude": broker_import_common.magnitude(amount),
            # 三态，不能塌缩成布尔："费用未知"与"费用全为零"是两个完全不同的
            # 结论。生成诊断的典型场景之一恰恰是数值列解析失败（那一行正是因此
            # 才进的 errors），此时把 None 过滤掉会让三项全失败变成 all([])==True，
            # 一项失败、其余为 0 也报 True——维护者据此正好排除掉"费用列错位/
            # 格式异常"这条线索，而那可能就是根因。
            "fees_all_zero": None if unparsed_fee_columns else all(fee == 0 for fee in fees),
            "unparsed_fee_columns": unparsed_fee_columns,
            # 这一个比值就能分流根因：≈0.9 是港股通结算汇率、略大于 1 是债券
            # 应计利息、数量级离谱是逆回购/列错位、None 是价格或数量为 0
            "trade_amount_over_gross": broker_import_common.safe_ratio(trade_amount, gross),
            "deviation_over_tolerance": broker_import_common.safe_ratio(deviation, tolerance),
        }
    )
    return record


def build_cmb_diagnostics(
    contents: bytes,
    filename: str,
    *,
    errors: List[str],
    warnings: Optional[List[str]] = None,
    parsed_rows: Optional[List[ParsedFlow]] = None,
) -> Dict[str, Any]:
    """脱敏诊断报告：足以定位版式/口径问题，且不含金额、数量、价格与证券名称。"""
    provenance: List[Dict[str, Any]] = []
    df = read_cmb_statement_pdf(contents, provenance=provenance)
    records = df.to_dict("records")
    warnings = warnings or []

    reader = PdfReader(io.BytesIO(contents))
    metadata = reader.metadata or {}
    with pdfplumber.open(io.BytesIO(contents)) as pdf:
        page_words = [
            page.extract_words(x_tolerance=1, y_tolerance=2, keep_blank_chars=False)
            for page in pdf.pages
        ]

    header_page: Optional[int] = None
    header_columns: List[str] = []
    for page_index, words in enumerate(page_words):
        found = sorted(
            {
                strip_bom(word.get("text"))
                for word in words
                if strip_bom(word.get("text")) in PDF_REQUIRED_HEADER_COLUMNS
            }
        )
        if len(found) > len(header_columns):
            header_page, header_columns = page_index, found

    section_titles: Dict[str, int] = {}
    for words in page_words:
        for word in words:
            text = strip_bom(word.get("text"))
            if text == PDF_FLOW_SECTION_TITLE or text in PDF_FLOW_TERMINATOR_TITLES:
                section_titles[text] = section_titles.get(text, 0) + 1

    try:
        boundaries = [round(value, 1) for value in _find_pdf_column_boundaries(page_words)]
    except ValueError:
        boundaries = []

    parsed_source_rows = {flow.source_row_number for flow in parsed_rows or []}
    error_row_numbers = source_error_rows(errors, parsed_source_rows)
    trade_rows = [flow for flow in parsed_rows or [] if flow.transaction_type]
    eligible_trade_rows = [
        flow
        for flow in trade_rows
        if flow.security_code and flow.trade_quantity != 0 and flow.trade_price > 0
    ]

    error_rows: List[Dict[str, Any]] = []
    for message in errors:
        match = broker_import_common.SOURCE_ROW_ERROR_PATTERN.match(message)
        if not match:
            continue
        row_number = int(match.group(1))
        index = row_number - 2  # parse_rows 的 row_number = dataframe 下标 + 2
        error_rows.append(
            _diagnostic_error_row(
                row_number=row_number,
                message=message,
                row=records[index] if 0 <= index < len(records) else None,
                provenance=provenance[index] if 0 <= index < len(provenance) else None,
            )
        )

    labels = ["市场", "币种", "业务名称"]
    return {
        "file_fingerprint": {
            "parser_name": PARSER_NAME,
            "parser_version": PARSER_VERSION,
            "app_version": settings.app_version,
            "build_sha": settings.build_sha,
            # 文件名与 PDF 元数据只回传结构与分类，绝不回传原文：文件名可能含
            # 真实姓名或账户备注，`/Creator` 常形如「Microsoft Word - 张三.docx」
            # 或带本机用户名。指纹是不可逆摘要，用于同一来源在多份报告间对照。
            "filename_extension": broker_import_common.safe_extension(
                filename, allowed=(".pdf",)
            ),
            "filename_digit_shape": broker_import_common.digit_run_shape(filename),
            "filename_fingerprint": broker_import_common.text_fingerprint(filename),
            "page_count": len(page_words),
            "pdf_producer_class": broker_import_common.classify_generator(
                metadata.get("/Producer")
            ),
            "pdf_producer_fingerprint": broker_import_common.text_fingerprint(
                metadata.get("/Producer")
            ),
            "pdf_creator_class": broker_import_common.classify_generator(
                metadata.get("/Creator")
            ),
            "pdf_creator_fingerprint": broker_import_common.text_fingerprint(
                metadata.get("/Creator")
            ),
            "header_found_page": header_page,
            "header_columns_found": header_columns,
            "header_columns_missing": sorted(PDF_REQUIRED_HEADER_COLUMNS - set(header_columns)),
            "column_boundaries": boundaries,
            # False = 走了"全文档无节标题"的回退分支，未回业务流水/配号信息
            # 的行会被当成正常流水收进来
            "has_section_titles": bool(section_titles.get(PDF_FLOW_SECTION_TITLE)),
            "section_titles_seen": section_titles,
            "extracted_rows": len(records),
            "provenance_rows": len(provenance),
        },
        "vocabulary": {
            "known_hk_connect_names": sorted(HK_CONNECT_MARKET_NAMES),
            "known_trade_businesses": sorted(TRADE_BUSINESS_MAP),
            "market_currency_business": broker_import_common.label_histogram(
                [
                    {column: _diagnostic_label(record.get(column), record) for column in labels}
                    for record in records
                ],
                labels,
                strip=strip_bom,
            ),
            "column_patterns": {
                column: broker_import_common.label_histogram(
                    [
                        {
                            column: broker_import_common.digit_class(
                                record.get(column), strip=strip_bom
                            )
                        }
                        for record in records
                    ],
                    [column],
                    strip=strip_bom,
                )[:DIAGNOSTIC_PATTERN_TOP_N]
                for column in DIAGNOSTIC_PATTERN_COLUMNS
            },
        },
        "error_rows": error_rows,
        "counts": {
            "errors_total": len(errors),
            "warnings_total": len(warnings),
            "error_source_rows": len(error_row_numbers),
            # 「无效跳过」的两个分量——报障者界面上那个数字到底怎么来的
            "ineligible_trade_rows": len(trade_rows) - len(eligible_trade_rows),
            "skipped_invalid_rows": len(trade_rows)
            - len(eligible_trade_rows)
            + len(error_row_numbers),
        },
    }


def apply_exclusions(parsed_rows: List[ParsedFlow], excluded_symbols) -> None:
    """排除清单标记：命中标的的行只归档不入账（预览与正式导入共用）。"""
    if not excluded_symbols:
        return
    for flow in parsed_rows:
        if flow.security_code and flow.security_code in excluded_symbols:
            flow.excluded = True


def reject_unassigned_legacy_sources(db: Session, user_id: int) -> None:
    broker_import_common.reject_unassigned_legacy_sources(db, user_id, BROKER_NAME)

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
        cash_management_symbols=frozenset(get_cash_management_symbols(db, user_id)),
        cash_business_map=get_cmb_cash_business_map(db, user_id),
    )
    apply_exclusions(parsed_rows, get_excluded_symbols(db, user_id))
    reject_unassigned_legacy_sources(db, user_id)
    if account is None:
        raise ValueError("broker_account_id is required for 招商证券 preview")
    validate_statement_account_masks(account, parsed_rows)
    duplicate_hashes = get_existing_hashes(
        db,
        user_id,
        [flow.row_hash for flow in parsed_rows],
        broker_account_id=broker_account_id,
    )

    result = build_import_result(
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
    )
    # 整批一票否决的持仓预检必须在预览里也跑一遍（#132）：否则用户拿到干净
    # 预览、正式导入却被整批拒绝。校验是纯内存重放，把本批还没落库的交易用
    # 替身补进去即可，预览仍是只读端点。失败写进 errors——前端据此禁用导入
    # 按钮并红条展示，与它对解析错误的处置一致。
    try:
        validate_account_positions_before_commit(
            db,
            user_id=user_id,
            broker_account_id=broker_account_id,
            extra_transactions=prospective_transactions(
                parsed_rows, duplicate_hashes, broker_account_id=broker_account_id
            ),
        )
    except ValueError as exc:
        # 持仓预检失败不是行级解析错误（没有 `row N:` 前缀），但它同样阻塞导入，
        # 必须并进 errors 全集——否则诊断报告里的 counts 会和界面上的条数打架。
        errors = [*errors, str(exc)]
        result["errors"] = [*result.get("errors", []), str(exc)]
        result["errors_total"] = result.get("errors_total", 0) + 1

    # 有问题才产出诊断（正常导入零开销）。诊断是排查辅助，绝不能把预览本身
    # 弄挂——任何异常都降级成一行说明，用户该拿到的预览结果照常返回。
    if result.get("errors") or result.get("warnings"):
        try:
            result["diagnostics"] = build_cmb_diagnostics(
                contents,
                filename,
                errors=errors,
                warnings=warnings,
                parsed_rows=parsed_rows,
            )
        except Exception as exc:  # noqa: BLE001 - 诊断失败不得影响预览
            result["diagnostics"] = {"diagnostics_error": type(exc).__name__}
    return result


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


def _source_sequence_value(value: Any) -> tuple[int, Any]:
    text = strip_bom(value)
    if not text:
        return (2, "")
    try:
        return (0, int(text))
    except ValueError:
        return (1, text)


def prospective_transactions(
    parsed_rows: List[ParsedFlow],
    duplicate_hashes: set[str],
    *,
    broker_account_id: Optional[int] = None,
) -> List[ProspectiveTransaction]:
    """本批**还没落库**、正式导入会入账成交易的行（预览专用）。

    入账判据走 `flow.becomes_transaction`——与导入循环同一个谓词，不另写映射。
    """
    return [
        ProspectiveTransaction(
            symbol=flow.security_code,
            market=infer_market(flow.security_code, flow.currency, flow.shareholder_code),
            transaction_type=flow.transaction_type,
            quantity=abs(flow.trade_quantity),
            transaction_date=flow.trade_date,
            price=flow.trade_price,
            fee=flow.effective_fee,
            currency=flow.effective_currency,
            name=flow.security_name,
            broker_account_id=broker_account_id,
            serial_number=flow.serial_number,
            contract_number=flow.contract_number,
            source_row_number=flow.source_row_number or 0,
        )
        for flow in parsed_rows
        if flow.row_hash not in duplicate_hashes and flow.becomes_transaction
    ]


def validate_account_positions_before_commit(
    db: Session,
    *,
    user_id: int,
    broker_account_id: int,
    extra_transactions: Sequence[Any] = (),
) -> None:
    """Reject an account ledger that requires an unrecorded opening position.

    extra_transactions：尚未落库的待入账交易（预览通道用）。导入通道在 flush
    之后调用，此时交易已在 DB 查询范围内，故为空；预览不写库，用替身补上。
    """
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

    keys = {(txn.symbol, txn.market) for txn in transactions}
    keys |= {(txn.symbol, txn.market) for txn in extra_transactions}
    owned_actions = (
        db.query(CorporateAction)
        .filter(
            CorporateAction.user_id == user_id,
            CorporateAction.broker_account_id == broker_account_id,
            CorporateAction.action_type.in_(QUANTITY_ACTION_TYPES),
        )
        .all()
    )
    keys |= {(action.symbol, action.market) for action in owned_actions}
    quantity_actions = load_account_quantity_actions(
        db, user_id=user_id, broker_account_id=broker_account_id, keys=keys,
    )

    # 同日次序对齐内核 _TYPE_SORT_ORDER：先买入、再转仓、最后卖出。
    # 原来是 `1 if BUY else 2`，转仓被并进卖出档，同日「转入后立刻卖出」会误报。
    type_rank = {"BUY": 1, "TRANSFER_IN": 2, "TRANSFER_OUT": 2}

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
                action,
            )
        )
    for transaction in transactions:
        source = sources_by_transaction_id.get(transaction.id)
        events.append(
            (
                transaction.transaction_date,
                type_rank.get(transaction.transaction_type, 3),
                _source_sequence_value(source.serial_number if source else None),
                _source_sequence_value(source.contract_number if source else None),
                source.source_row_number if source and source.source_row_number else 0,
                transaction.id or 0,
                transaction,
            )
        )
    # 待入账交易走同一套排序键（流水号/合同号/行号本就来自对账单原行）。
    # id 位必须是排在持久化 id **之后**的哨兵：招商 PDF 常常没有流水号与合同
    # 编号，行号又按每份对账单从头计数，跨文件同日同行号的整键碰撞并非不可能；
    # 一旦碰撞，用 0 会把替身排到既有交易之前，而正式导入拿到真 id 后排在之后
    # ——同日多笔卖出时预览与导入会指向不同的首笔超卖、报出不同余量。
    for prospective in extra_transactions:
        events.append(
            (
                prospective.transaction_date,
                type_rank.get(prospective.transaction_type, 3),
                _source_sequence_value(prospective.serial_number),
                _source_sequence_value(prospective.contract_number),
                prospective.source_row_number or 0,
                UNPERSISTED_SORT_ID,
                prospective,
            )
        )

    def _reject(event, available: Decimal, needed: Decimal) -> None:
        verb = "转出" if event.transaction_type == "TRANSFER_OUT" else "卖出"
        raise ValueError(
            "招商证券账户持仓预检失败："
            f"{event.symbol} {event.market} 在 {event.transaction_date} "
            f"{verb} {format(needed, 'f')}，"
            f"但账户内可用数量仅 {format(available, 'f')}；"
            "缺少期初持仓或证券转入记录，整批未导入"
        )

    # 数量语义统一走 holding_service（内部经 portfolio/semantics），不再本地手写：
    # 原实现裸读 shares_received（ratio-only 送股加 0 股）、拆股只认 split_ratio
    # 且解析失败静默吞掉，且对 TRANSFER_* 会落到 `event.action_type` 分支
    # ——Transaction 无该列，直接 AttributeError 崩掉整个导入。
    replay_account_quantities(
        [event[-1] for event in sorted(events, key=lambda item: item[:-1])],
        on_oversell=_reject,
    )


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
            cash_management_symbols=frozenset(get_cash_management_symbols(db, user_id)),
            cash_business_map=get_cmb_cash_business_map(db, user_id),
        )
        apply_exclusions(parsed_rows, get_excluded_symbols(db, user_id))
        reject_unassigned_legacy_sources(db, user_id)
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
        existing_hashes = get_existing_hashes(
            db,
            user_id,
            [flow.row_hash for flow in parsed_rows],
            broker_account_id=broker_account_id,
        )
        duplicate_hashes = set(existing_hashes)
        # 上次未归属的税行：本批若补齐了股息就在原行上转正（不建新行）
        unattributed_tax_sources = load_unattributed_tax_sources(
            db,
            BrokerFundFlow,
            user_id=user_id,
            hashes=[flow.row_hash for flow in parsed_rows],
            broker_account_id=broker_account_id,
        )

        imported_cash_events = 0
        affected_symbols: set[tuple[str, str]] = set()

        for flow in parsed_rows:
            if flow.row_hash in existing_hashes:
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

            if flow.is_cash_business:
                event_type = flow.cash_event_type
                cash_event = CashEvent(
                    user_id=user_id,
                    broker_account_id=broker_account_id,
                    event_type=event_type,
                    amount=abs(flow.amount),
                    currency=flow.currency,
                    event_date=flow.trade_date,
                    notes=(
                        f"{BROKER_NAME}对账单{flow.business_name}; "
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
                    # 已保留过就不重复建行（row_hash 唯一约束）；标记为未归属，
                    # 补齐股息后重导会被 get_existing_hashes 放行并在此转正。
                    if flow.row_hash not in unattributed_tax_sources:
                        db.add(
                            mark_unattributed_tax(
                                create_broker_fund_flow(
                                    user_id=user_id,
                                    broker_account_id=broker_account_id,
                                    filename=filename,
                                    flow=flow,
                                    import_batch_id=batch_id,
                                ),
                                "preserved without canonical action: "
                                "expected exactly one account-scoped dividend",
                            )
                        )
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
                preserved = unattributed_tax_sources.pop(flow.row_hash, None)
                if preserved is not None:
                    # 上次未归属的那一行就地转正，不插新行
                    db.add(attribute_tax_source(preserved, action.id))
                else:
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

            # 走到这里前面四个 is_* 分支都已 continue，故这里等价于原来那四个
            # 字段条件；用同一个谓词是为了让预览的"待入账交易"不可能与导入分叉。
            if not flow.becomes_transaction:
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

        # 持仓重算在同一事务内完成再 commit（与东财同口径）：先 commit 再重算的话，
        # 重算失败会留下"交易已落库、holdings 停在旧值"的半套数据，而批次只标 PARTIAL。
        recalculated_symbols = 0
        for symbol, market in affected_symbols:
            recalculate_holdings(db, user_id, symbol, market, commit=False)
            recalculated_symbols += 1

        try:
            db.commit()
        except IntegrityError as exc:
            raise ValueError("Duplicate broker fund flow detected during import") from exc
        records_committed = True

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
            imported_source_rows = archived_row_count(db, BrokerFundFlow, batch_id)
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
