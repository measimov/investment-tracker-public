from __future__ import annotations

import csv
import io
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models.broker_account import BrokerAccount
from ..models.cash_event import CashEvent
from ..models.corporate_action import CorporateAction
from ..models.ibkr_activity_flow import IbkrActivityFlow
from ..models.transaction import Transaction
from ..core.logging import get_app_logger
from ..services import broker_import_common
from ..services.broker_import_common import (
    HASH_DUPLICATE_OCCURRENCE_FIELD,
    normalize_hash_value as normalize_hash_value,  # 测试断言导入器命名空间
    strip_text,
)
from ..services.holding_service import recalculate_holdings
from ..services.security_rule_service import (
    get_excluded_symbols,
    get_name_overrides,
    get_relistings,
)
from ..services.import_batch_service import (
    complete_import_batch,
    fail_import_batch,
    set_import_batch_source_stats,
    start_import_batch,
    validate_import_account,
    validate_source_file_account,
)
from ..services.stock_price_service import (
    to_tushare_a_code,
    to_tushare_hk_code,
    tushare_query_once,
)


BROKER_NAME = "IBKR"
SOURCE_TYPE = "ibkr_activity_csv"
SOURCE_TYPE_XLSX = "ibkr_trade_history_xlsx"
PARSER_NAME = "ibkr_activity"
# 入账语义变化必须升版（审计批次可区分）：v6 = 排除规则表驱动，
# 命中标的只归档不入账且不进 eligible 判重
PARSER_VERSION = "6"
# trade_history.xlsx（reporting API 自制导出，规范格式）的 All Trades 表。
# 只含成交（STK/OPT/CASH），不含股息与预扣税 —— 股息仍需其他来源。
XLSX_TRADE_SHEET = "All Trades"
XLSX_REQUIRED_COLUMNS = [
    "Date (HKT)",
    "Symbol",
    "Name",
    "Type",
    "Ccy",
    "Side",
    "Qty",
    "Price",
    "Net Amount",
    "Commission",
    "Trade ID",
]
XLSX_SIDE_MAP = {"BUY": "买", "SELL": "卖"}
XLSX_OPTION_ASSET_TYPE = "OPT"
XLSX_FX_ASSET_TYPE = "CASH"
BASE_CURRENCY_FALLBACK = "USD"
TRADE_TYPES = {"买": "BUY", "卖": "SELL"}
EXERCISE_TYPES = {"行权", "被行权"}
DIVIDEND_TYPE = "股息"
WITHHOLDING_TAX_TYPE = "外国预扣税"
CASH_ACTIVITY_TYPES = {"贷方利息", "借方利息", "存款", "调整"}
FX_ACTIVITY_TYPE = "外汇交易组成部分"
# 可入账为 CashEvent 的现金业务；"调整" 是 FX 折算损益等纸面项，只归档不入账。
# Transaction History 报表的金额列一律为基础货币（USD）等值，原币种在说明里
# （如 "HKD 贷方利息"）——按基础货币入账并在备注保留原说明。
IBKR_CASH_EVENT_TYPES = {"存款", "贷方利息", "借方利息"}
OPTION_MONTHS = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"
SYNTHETIC_RELISTING_MARKER = "synthetic_relisting_transfer"
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

    @property
    def cash_amount(self) -> Optional[Decimal]:
        if self.net_amount is not None:
            return self.net_amount
        return self.gross_amount

    @property
    def cash_event_type(self) -> Optional[str]:
        """现金业务行的 CashEvent 类型；调整（纸面损益）与方向异常返回 None。"""
        if self.activity_type not in IBKR_CASH_EVENT_TYPES:
            return None
        amount = self.cash_amount
        if amount is None or amount == 0:
            return None
        if self.activity_type == "存款":
            return "DEPOSIT" if amount > 0 else "WITHDRAWAL"
        if self.activity_type == "贷方利息":
            return "INTEREST" if amount > 0 else None
        return "FEE" if amount < 0 else None  # 借方利息

    @property
    def is_cash_business(self) -> bool:
        return self.skip_reason == "cash" and self.cash_event_type is not None

    @property
    def fx_legs(self) -> Optional[tuple[tuple[str, Decimal], tuple[str, Decimal]]]:
        """外汇兑换的两条现金腿 ((币种, 带符号金额), ...)。

        数量列是货币对基础腿的带符号净额（说明"外汇交易基础货币净额"），
        对价腿 = -数量×价格；总额/净额列只是折算损益，不是现金流。
        佣金另行（IBKR 现汇佣金以账户基础货币收取，见 fx 入账处）。
        """
        if self.skip_reason != "fx" or self.quantity is None or self.quantity == 0:
            return None
        if self.price is None or self.price <= 0:
            return None
        parts = strip_text(self.raw_symbol).upper().split(".")
        if len(parts) != 2 or not all(len(p) == 3 and p.isalpha() for p in parts):
            return None
        # 腿币种以货币对代码为准；Price Currency 列与对价币不一致说明列漂移，
        # 宁可归档报警也不把现金记进错误币种
        if self.price_currency and self.price_currency.upper() != parts[1]:
            return None
        return (
            (parts[0], self.quantity),
            (parts[1], -(self.quantity * self.price)),
        )


@dataclass
class ExistingSourceResolution:
    booked_hashes: set[str] = field(default_factory=set)
    duplicate_hashes: set[str] = field(default_factory=set)
    unresolved_tax_sources: Dict[str, IbkrActivityFlow] = field(default_factory=dict)


def _account_parts(value: Optional[str]) -> tuple[str, str, bool]:
    """Return fixed prefix/suffix and whether the value is explicitly masked."""
    text = strip_text(value).upper().replace("尾号", "")
    text = re.sub(r"[\s_-]+", "", text)
    masked = bool(re.search(r"[*X•·]+", text))
    if masked:
        parts = re.split(r"[*X•·]+", text)
        prefix = re.sub(r"[^A-Z0-9]", "", parts[0])
        suffix = re.sub(r"[^A-Z0-9]", "", parts[-1])
        return prefix, suffix, True
    normalized = re.sub(r"[^A-Z0-9]", "", text)
    return normalized, normalized, False


def account_identifier_matches(statement_account: Optional[str], configured_mask: str) -> bool:
    """
    Match an IBKR statement account to an exact identifier or a masked tail.

    IBKR commonly emits values such as ``U***00001`` while users may store
    ``U***00001``, ``****0001`` or ``尾号0001``. A tail must contain at least
    four fixed characters; two unmasked full identifiers must match exactly.
    """
    statement = strip_text(statement_account)
    configured = strip_text(configured_mask)
    if not statement or not configured:
        return False

    statement_prefix, statement_suffix, statement_masked = _account_parts(statement)
    configured_prefix, configured_suffix, configured_masked = _account_parts(configured)
    if not statement_suffix or not configured_suffix:
        return False

    if not statement_masked and not configured_masked:
        if statement_prefix == configured_prefix:
            return True
        # A short configured identifier is an explicitly entered account tail.
        return len(configured_suffix) >= 4 and len(configured_suffix) <= 6 and statement_suffix.endswith(
            configured_suffix
        )

    if statement_masked and not configured_masked:
        if 4 <= len(configured_suffix) <= 6 and statement_suffix.endswith(
            configured_suffix
        ):
            return True
        return (
            len(statement_suffix) >= 4
            and configured_suffix.startswith(statement_prefix)
            and configured_suffix.endswith(statement_suffix)
        )
    if configured_masked and not statement_masked:
        return (
            len(configured_suffix) >= 4
            and statement_suffix.startswith(configured_prefix)
            and statement_suffix.endswith(configured_suffix)
        )

    shared_suffix = (
        statement_suffix.endswith(configured_suffix)
        if len(statement_suffix) >= len(configured_suffix)
        else configured_suffix.endswith(statement_suffix)
    )
    if not shared_suffix or min(len(statement_suffix), len(configured_suffix)) < 4:
        return False

    if statement_prefix and configured_prefix:
        return statement_prefix == configured_prefix
    return True


def validate_statement_accounts(
    parsed_rows: List[ParsedIbkrFlow],
    broker_account: BrokerAccount,
    *,
    allow_missing_accounts: bool = False,
) -> List[str]:
    """校验报表行的账户标识与所选账户掩码匹配（防导错账户）。

    trade_history.xlsx（reporting API 导出）不含账户列，无法逐行交叉校验：
    该路径以 allow_missing_accounts=True 调用，全部行都无标识时返回空列表，
    由调用方在结果里附警告；只要有任何一行带了标识，仍照常严格校验。
    """
    configured = strip_text(broker_account.account_number_masked)
    if not configured:
        raise ValueError(
            "所选 IBKR 账户缺少账户掩码或尾号；请先在账户资料中填写后再导入"
        )

    configured_masks = [
        value
        for value in re.split(r"[/,，;；、|\\n]+", configured)
        if strip_text(value)
    ]
    source_accounts = sorted(
        {strip_text(flow.account) for flow in parsed_rows if strip_text(flow.account)}
    )
    if allow_missing_accounts and not source_accounts:
        return []
    missing_account_rows = [
        flow.source_row_number for flow in parsed_rows if not strip_text(flow.account)
    ]
    if missing_account_rows:
        rows = ", ".join(str(row) for row in missing_account_rows[:10])
        raise ValueError(f"IBKR CSV 存在缺少账户标识的交易历史行：{rows}")
    if not source_accounts:
        raise ValueError("IBKR CSV 的交易历史没有可验证的账户标识")

    mismatched = [
        account
        for account in source_accounts
        if not any(
            account_identifier_matches(account, configured_mask)
            for configured_mask in configured_masks
        )
    ]
    if mismatched:
        raise ValueError(
            "IBKR CSV 账户与所选券商账户不匹配："
            f"CSV={', '.join(mismatched)}；所选账户={configured}"
        )
    return source_accounts


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


def calculate_row_hash(values: Dict[str, Any]) -> str:
    return broker_import_common.calculate_row_hash(values, HASH_FIELDS)


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
    """Resolve a display name from Tushare instead of trusting IBKR descriptions.

    Single attempt, no retry: an empty result is a definitive "not in Tushare"
    (SGX symbols, delisted codes), and a token/network failure will fail for the
    whole batch anyway — resolve_security_names' circuit breaker handles that.
    """
    if not symbol or not market:
        return None

    if market in {"A股", "B股"}:
        df = tushare_query_once(
            "stock_basic",
            ts_code=to_tushare_a_code(symbol),
            fields="ts_code,name",
        )
    elif market == "港股":
        df = tushare_query_once(
            "hk_basic",
            ts_code=to_tushare_hk_code(symbol),
            fields="ts_code,name,fullname",
        )
    elif market == "美股":
        df = tushare_query_once(
            "us_basic",
            ts_code=str(symbol or "").strip().upper(),
            fields="ts_code,name",
        )
    else:
        return None

    if df is None or df.empty:
        return None

    row = df.iloc[0]
    for column in ("name", "fullname"):
        value = strip_text(row.get(column))
        if value:
            return value
    return None


# 成功查到的名称按进程生命周期缓存：预览→导入两次调用只查一次外网。
# 查不到/查失败不缓存，下批次可再试（单次尝试代价已很低）。
_resolved_name_cache: Dict[tuple[str, str], str] = {}
NAME_LOOKUP_WORKERS = 4
NAME_LOOKUP_MAX_CONSECUTIVE_FAILURES = 3


def resolve_security_names(
    targets: List[tuple[str, str]],
    *,
    name_overrides: Optional[Dict[tuple[str, str], str]] = None,
) -> Dict[tuple[str, str], Optional[str]]:
    """Batch name resolution: cached → concurrent single-attempt lookups.

    连续失败达到阈值即熔断（token 缺失/网络故障时批内所有查询都会失败，
    无谓等待正是"预览卡死 2 分钟"的根因），剩余标的直接降级为已知名称表。
    """
    name_overrides = name_overrides or {}
    results: Dict[tuple[str, str], Optional[str]] = {}
    pending: List[tuple[str, str]] = []
    for key in targets:
        if key in name_overrides:
            results[key] = name_overrides[key]
        elif key in _resolved_name_cache:
            results[key] = _resolved_name_cache[key]
        else:
            pending.append(key)
    if not pending:
        return results

    failure_lock = Lock()
    consecutive_failures = 0

    def worker(key: tuple[str, str]) -> Optional[str]:
        nonlocal consecutive_failures
        with failure_lock:
            if consecutive_failures >= NAME_LOOKUP_MAX_CONSECUTIVE_FAILURES:
                return None
        try:
            name = lookup_tushare_security_name(*key)
        except Exception as exc:
            with failure_lock:
                consecutive_failures += 1
                tripped = consecutive_failures == NAME_LOOKUP_MAX_CONSECUTIVE_FAILURES
            logger.warning(
                "Tushare name lookup failed for %s %s: %s", key[0], key[1], str(exc)[:200]
            )
            if tripped:
                logger.warning(
                    "Tushare 查名连续失败 %s 次，本批剩余标的跳过外网查询",
                    NAME_LOOKUP_MAX_CONSECUTIVE_FAILURES,
                )
            return None
        with failure_lock:
            consecutive_failures = 0
        if name:
            _resolved_name_cache[key] = name
        return name

    with ThreadPoolExecutor(max_workers=min(NAME_LOOKUP_WORKERS, len(pending))) as pool:
        for key, name in zip(pending, pool.map(worker, pending)):
            results[key] = name
    return results


def enrich_security_names(
    parsed_rows: List[ParsedIbkrFlow],
    *,
    name_overrides: Optional[Dict[tuple[str, str], str]] = None,
) -> None:
    targets = sorted(
        {
            (flow.symbol, flow.market)
            for flow in parsed_rows
            if (flow.is_trade or flow.is_cash_dividend or flow.is_withholding_tax)
            and flow.symbol
            and flow.market
        }
    )
    name_cache = resolve_security_names(targets, name_overrides=name_overrides)

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


def read_ibkr_trade_history_xlsx(
    contents: bytes,
) -> tuple[List[tuple[int, Dict[str, str]]], str, int, List[str]]:
    """把 trade_history.xlsx 的 All Trades 表适配成 CSV reader 的同形行。

    行 dict 的键与 read_ibkr_transaction_history 一致，parse_rows 无需分叉：
    - Side BUY/SELL → 交易类型 买/卖；Type=CASH（外汇兑换）→ 外汇交易组成部分
    - Type 原样放进"资产类别"（parse_rows 据此把 OPT 判为期权跳过归档）
    - Trade ID 拼进"说明"，进 row_hash，成为跨上传去重的稳定标识
    - Net Amount 即无符号成交额（数量×价格），费用另列 Commission；
      重建 总额/净额 的 CSV 符号约定（买为负、卖为正），
      使 trade_fee_in_price_currency 推出的费用恰为 Commission（成交币种）。
    """
    try:
        frame = pd.read_excel(
            io.BytesIO(contents), sheet_name=XLSX_TRADE_SHEET, dtype=object
        )
    except ValueError as exc:
        raise ValueError(f"Missing {XLSX_TRADE_SHEET} sheet in IBKR xlsx") from exc

    missing = sorted(set(XLSX_REQUIRED_COLUMNS) - set(map(str, frame.columns)))
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")

    def _text(value: Any) -> str:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return ""
        return str(value).strip()

    data_rows: List[tuple[int, Dict[str, str]]] = []
    errors: List[str] = []
    for index, row in frame.iterrows():
        row_number = int(index) + 2  # 表头占第 1 行
        raw_date = row.get("Date (HKT)")
        try:
            trade_date = pd.to_datetime(raw_date).date().isoformat()
        except (ValueError, TypeError):
            errors.append(f"row {row_number}: invalid trade date")
            continue

        asset_type = _text(row.get("Type"))
        side = _text(row.get("Side")).upper()
        if asset_type == XLSX_FX_ASSET_TYPE:
            activity_type = FX_ACTIVITY_TYPE
        else:
            activity_type = XLSX_SIDE_MAP.get(side, side or "__MISSING__")

        quantity_text = _text(row.get("Qty"))
        price_text = _text(row.get("Price"))
        gross = parse_decimal(row.get("Net Amount")) or Decimal("0")
        commission = parse_decimal(row.get("Commission")) or Decimal("0")
        # CSV 符号约定：买入现金流出为负；净额与总额之差即费用
        signed_gross = -abs(gross) if activity_type == "买" else abs(gross)
        signed_net = signed_gross - abs(commission)

        name = _text(row.get("Name"))
        trade_id = _text(row.get("Trade ID"))
        description = f"{name}; trade_id={trade_id}" if trade_id else name

        data_rows.append(
            (
                row_number,
                {
                    "日期": trade_date,
                    "账户": "",
                    "说明": description,
                    "交易类型": activity_type,
                    "资产类别": asset_type,
                    "代码": _text(row.get("Symbol")),
                    "数量": quantity_text,
                    "价格": price_text,
                    "Price Currency": _text(row.get("Ccy")),
                    "总额": str(signed_gross),
                    "佣金": str(-abs(commission)) if commission else "0",
                    "净额": str(signed_net),
                },
            )
        )

    return data_rows, BASE_CURRENCY_FALLBACK, len(frame), errors


def is_ibkr_xlsx_filename(filename: str) -> bool:
    return filename.lower().endswith(".xlsx")


def parse_rows(
    contents: bytes,
    filename: str,
    *,
    name_overrides: Optional[Dict[tuple[str, str], str]] = None,
    excluded_symbols: frozenset = frozenset(),
) -> tuple[List[ParsedIbkrFlow], Dict[str, int], int, List[str]]:
    if is_ibkr_xlsx_filename(filename):
        data_rows, base_currency, total_rows, errors = read_ibkr_trade_history_xlsx(
            contents
        )
    else:
        data_rows, base_currency, total_rows, errors = read_ibkr_transaction_history(
            contents
        )
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
            # xlsx 行带显式资产类别（OPT），比符号启发式更可靠；CSV 行无此键
            if (
                strip_text(row.get("资产类别")) == XLSX_OPTION_ASSET_TYPE
                or is_option_symbol(raw_symbol, description)
            ):
                skip_reason = "option"
            elif not symbol or not market:
                skip_reason = "unsupported"
            elif quantity is None or quantity == 0 or price is None or price <= 0:
                skip_reason = "invalid"
        else:
            skip_reason = "unsupported"

        # 排除清单（security_rules EXCLUDE）：命中标的的行只归档不入账，
        # 且不进 eligible 判重——被 owner 删除交易的标的（如 FXE）留下的
        # 孤儿来源行因此不再阻断重导。放在跳过原因链之后、行权持仓策略
        # 之前：排除优先于入账语义，且不参与持仓推演。
        if skip_reason is None and symbol and symbol in excluded_symbols:
            skip_reason = "excluded"

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
    enrich_security_names(parsed_rows, name_overrides=name_overrides)
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


def _unsafe_existing_source(source: IbkrActivityFlow, reason: str) -> ValueError:
    return ValueError(
        "IBKR 历史来源记录无法安全判重："
        f"row_hash={source.row_hash}；{reason}。"
        "请先完成旧 IBKR 数据的账户迁移，当前导入不会静默跳过该记录"
    )


def _decimal_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    # Imported source decimals are persisted at 8-10 places. Compare within the
    # narrowest persisted scale so a safe re-import is not rejected solely
    # because the original calculation carried additional decimal places.
    return abs(Decimal(str(left)) - Decimal(str(right))) <= Decimal("0.000000005")


def _source_matches_parsed_flow(
    source: IbkrActivityFlow,
    flow: ParsedIbkrFlow,
) -> bool:
    text_fields = (
        ("account", source.account, flow.account),
        ("description", source.description, flow.description),
        ("activity_type", source.activity_type, flow.activity_type),
        ("raw_symbol", source.raw_symbol, flow.raw_symbol),
        ("symbol", source.symbol, flow.symbol),
        ("market", source.market, flow.market),
        ("price_currency", source.price_currency, flow.price_currency),
        ("base_currency", source.base_currency, flow.base_currency),
    )
    if any(strip_text(left) != strip_text(right) for _, left, right in text_fields):
        return False
    if source.trade_date != flow.trade_date:
        return False
    return all(
        _decimal_equal(left, right)
        for left, right in (
            (source.quantity, flow.quantity),
            (source.price, flow.price),
            (source.gross_amount, flow.gross_amount),
            (source.commission, flow.commission),
            (source.net_amount, flow.net_amount),
            (source.fee_in_price_currency, flow.fee_in_price_currency),
        )
    )


def _transaction_matches_flow(transaction: Transaction, flow: ParsedIbkrFlow) -> bool:
    return (
        transaction.transaction_type == flow.transaction_type
        and transaction.symbol == flow.symbol
        and transaction.market == flow.market
        and transaction.transaction_date == flow.trade_date
        and transaction.currency == (flow.price_currency or flow.base_currency)
        and _decimal_equal(transaction.quantity, abs(flow.quantity or Decimal("0")))
        and _decimal_equal(transaction.price, flow.price)
        and _decimal_equal(
            transaction.fee or Decimal("0"),
            flow.fee_in_price_currency or Decimal("0"),
        )
    )


def _validate_dividend_action_sources(
    action: CorporateAction,
    linked_sources: List[IbkrActivityFlow],
    broker_account: BrokerAccount,
) -> bool:
    dividend_sources = [
        source
        for source in linked_sources
        if source.activity_type == DIVIDEND_TYPE
        and source.gross_amount is not None
        and source.gross_amount > 0
    ]
    tax_sources = [
        source
        for source in linked_sources
        if source.activity_type == WITHHOLDING_TAX_TYPE
        and source.gross_amount is not None
        and source.gross_amount < 0
    ]
    if len(dividend_sources) != 1:
        return False
    if len(dividend_sources) + len(tax_sources) != len(linked_sources):
        return False
    if any(
        not account_identifier_matches(source.account, broker_account.account_number_masked)
        for source in linked_sources
    ):
        return False
    if any(
        source.broker_account_id not in {None, broker_account.id}
        for source in linked_sources
    ):
        return False

    dividend_source = dividend_sources[0]
    total_dividend = dividend_source.gross_amount
    total_tax = sum(
        (abs(source.gross_amount or Decimal("0")) for source in tax_sources),
        Decimal("0"),
    )
    expected_net = max(Decimal("0"), total_dividend - total_tax)
    return (
        action.action_type == "CASH_DIVIDEND"
        and action.symbol == dividend_source.symbol
        and action.market == dividend_source.market
        and action.currency == dividend_source.base_currency
        and (
            action.ex_date == dividend_source.trade_date
            or action.payment_date == dividend_source.trade_date
        )
        and _decimal_equal(action.total_dividend, total_dividend)
        and _decimal_equal(action.tax_withheld or Decimal("0"), total_tax)
        and _decimal_equal(action.net_dividend, expected_net)
    )


def resolve_existing_sources(
    db: Session,
    user_id: int,
    parsed_rows: Iterable[ParsedIbkrFlow],
    *,
    broker_account_id: int,
    broker_account: BrokerAccount,
) -> ExistingSourceResolution:
    flow_by_hash = {flow.row_hash: flow for flow in parsed_rows}
    hash_list = list(flow_by_hash)
    if not hash_list:
        return ExistingSourceResolution()
    sources = (
        db.query(IbkrActivityFlow)
        .filter(
            IbkrActivityFlow.user_id == user_id,
            IbkrActivityFlow.row_hash.in_(hash_list),
        )
        .order_by(IbkrActivityFlow.id)
        .all()
    )
    if not sources:
        return ExistingSourceResolution()

    sources_by_hash: Dict[str, List[IbkrActivityFlow]] = {}
    for source in sources:
        sources_by_hash.setdefault(source.row_hash, []).append(source)
    for row_hash, matching_sources in sources_by_hash.items():
        if len(matching_sources) != 1:
            raise _unsafe_existing_source(
                matching_sources[0],
                f"同一 row_hash 存在 {len(matching_sources)} 条来源记录",
            )
        parsed_flow = flow_by_hash[row_hash]
        if not _source_matches_parsed_flow(matching_sources[0], parsed_flow):
            raise _unsafe_existing_source(
                matching_sources[0],
                "row_hash 相同但来源经济事实与本次 CSV 不一致",
            )
        # trade_history.xlsx 来源行没有账户标识（文件不含账户列）：
        # 该来源已直接归属到 broker_account_id，且 row_hash/经济事实一致，
        # 归属一致即视为安全判重；有账户标识的（CSV 来源）仍按掩码严格校验。
        if not strip_text(matching_sources[0].account):
            if matching_sources[0].broker_account_id not in (None, broker_account.id):
                raise _unsafe_existing_source(
                    matching_sources[0],
                    "无账户标识的历史来源已归属其他券商账户",
                )
        elif not account_identifier_matches(
            matching_sources[0].account,
            broker_account.account_number_masked,
        ):
            raise _unsafe_existing_source(
                matching_sources[0],
                "历史来源账户标识与所选券商账户不匹配",
            )

    transaction_ids = {
        source.transaction_id for source in sources if source.transaction_id is not None
    }
    corporate_action_ids = {
        source.corporate_action_id
        for source in sources
        if source.corporate_action_id is not None
    }
    transactions = (
        {
            transaction.id: transaction
            for transaction in db.query(Transaction)
            .filter(Transaction.id.in_(transaction_ids))
            .all()
        }
        if transaction_ids
        else {}
    )
    corporate_actions = (
        {
            action.id: action
            for action in db.query(CorporateAction)
            .filter(CorporateAction.id.in_(corporate_action_ids))
            .all()
        }
        if corporate_action_ids
        else {}
    )
    all_action_sources = (
        db.query(IbkrActivityFlow)
        .filter(IbkrActivityFlow.corporate_action_id.in_(corporate_action_ids))
        .order_by(IbkrActivityFlow.id)
        .all()
        if corporate_action_ids
        else []
    )
    action_sources_by_id: Dict[int, List[IbkrActivityFlow]] = {}
    for source in all_action_sources:
        action_sources_by_id.setdefault(source.corporate_action_id, []).append(source)

    resolution = ExistingSourceResolution()
    for source in sources:
        if source.broker_account_id != broker_account_id:
            raise _unsafe_existing_source(
                source,
                "历史来源记录属于其他券商账户"
                f"（实际={source.broker_account_id}，所选={broker_account_id}）",
            )
        has_transaction = source.transaction_id is not None
        has_corporate_action = source.corporate_action_id is not None
        if has_transaction and has_corporate_action:
            raise _unsafe_existing_source(
                source,
                "同一来源同时链接交易和公司行动，链接冲突",
            )
        if not has_transaction and not has_corporate_action:
            if (
                source.activity_type == WITHHOLDING_TAX_TYPE
                and source.skip_reason == "unattributed_tax"
            ):
                resolution.unresolved_tax_sources[source.row_hash] = source
                continue
            raise _unsafe_existing_source(
                source,
                "来源没有可解析的交易或公司行动链接，属于孤儿记录",
            )

        if has_transaction:
            canonical_type = "transaction"
            canonical_id = source.transaction_id
            canonical_record = transactions.get(canonical_id)
        else:
            canonical_type = "corporate_action"
            canonical_id = source.corporate_action_id
            canonical_record = corporate_actions.get(canonical_id)

        if canonical_record is None or canonical_record.user_id != user_id:
            raise _unsafe_existing_source(
                source,
                "链接的规范记录不存在或不属于当前用户，属于孤儿记录",
            )
        if canonical_type == "transaction":
            if not _transaction_matches_flow(
                canonical_record,
                flow_by_hash[source.row_hash],
            ):
                raise _unsafe_existing_source(
                    source,
                    "链接交易的日期、标的、方向、数量、价格、费用或币种不一致",
                )
            link_count = (
                db.query(IbkrActivityFlow.id)
                .filter(IbkrActivityFlow.transaction_id == canonical_id)
                .count()
            )
            if link_count != 1:
                raise _unsafe_existing_source(
                    source,
                    f"链接交易被 {link_count} 条 IBKR 来源共同引用",
                )
        elif not _validate_dividend_action_sources(
            canonical_record,
            action_sources_by_id.get(canonical_id, []),
            broker_account,
        ):
            raise _unsafe_existing_source(
                source,
                "链接股息与其唯一股息来源、税款来源或金额汇总不一致",
            )

        if canonical_record.broker_account_id != broker_account_id:
            raise _unsafe_existing_source(
                source,
                "链接的规范记录属于其他券商账户"
                f"（实际={canonical_record.broker_account_id}，所选={broker_account_id}）",
            )
        resolution.duplicate_hashes.add(source.row_hash)
        resolution.booked_hashes.add(source.row_hash)

    return resolution


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
    booked_source_hashes: set[str],
    imported_transactions: int,
    imported_corporate_actions: int,
    imported_tax_adjustments: int,
    affected_symbols: int,
    imported_cash_events: int = 0,
    errors: List[str],
    warnings: Optional[List[str]] = None,
    source_accounts: Optional[List[str]] = None,
    canonical_objects_changed: int = 0,
) -> Dict[str, Any]:
    rows = eligible_rows(parsed_rows)
    trade_rows = [flow for flow in rows if flow.is_trade]
    dividend_rows = [flow for flow in rows if flow.is_cash_dividend]
    tax_rows = [flow for flow in rows if flow.is_withholding_tax]
    # 可入账的现金/外汇行与交易/股息/税同属审计口径：它们会生成 CashEvent
    # 并计入 booked/duplicate，而不是被当成"未入账来源"拖垮批次状态
    bookable_cash_rows = [
        flow
        for flow in parsed_rows
        if flow.is_cash_business or flow.fx_legs is not None
    ]
    # "调整"（FX 折算损益等纸面项）是设计上有意只归档的行：预期跳过，
    # 不算数据问题；方向异常/货币对异常的行不在此列，仍按未解决行处理
    expected_archived_rows = [
        flow
        for flow in parsed_rows
        if flow.skip_reason == "cash"
        and flow.activity_type not in IBKR_CASH_EVENT_TYPES
    ]
    audited_rows = rows + bookable_cash_rows
    seen_hashes: set[str] = set()
    duplicate_rows = []
    import_rows = []
    for flow in audited_rows:
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
        "excluded": len([flow for flow in parsed_rows if flow.skip_reason == "excluded"]),
    }
    booked_source_rows = len(
        [flow for flow in audited_rows if flow.row_hash in booked_source_hashes]
    )
    eligible_unbooked_source_rows = max(0, len(audited_rows) - booked_source_rows)
    unbooked_source_rows = max(0, total_rows - booked_source_rows)

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
        "canonical_objects_changed": canonical_objects_changed,
        "booked_source_rows": booked_source_rows,
        "unbooked_source_rows": unbooked_source_rows,
        "eligible_unbooked_source_rows": eligible_unbooked_source_rows,
        "duplicate_rows": len(duplicate_rows),
        "skipped_non_trade_rows": max(
            0,
            total_rows
            - len(trade_rows)
            - len(dividend_rows)
            - len(tax_rows)
            - len(bookable_cash_rows)
            - len(expected_archived_rows)
            - skip_counts["excluded"],
        ),
        "expected_archived_rows": len(expected_archived_rows),
        "skipped_excluded_rows": skip_counts["excluded"],
        "excluded_unbooked_rows": len(
            [
                flow
                for flow in parsed_rows
                if flow.skip_reason == "excluded" and flow.row_hash not in existing_hashes
            ]
        ),
        "skipped_invalid_rows": skip_counts["invalid"] + len(errors),
        "skipped_option_rows": skip_counts["option"],
        # 跳过计数只含真正不入账的行；可入账现金/外汇行分列在 eligible_* 里，
        # 否则预览会把将要入账的存款/利息显示成"现金类跳过"误导用户
        "skipped_fx_rows": skip_counts["fx"]
        - len([flow for flow in parsed_rows if flow.fx_legs is not None]),
        "skipped_cash_rows": skip_counts["cash"]
        - len([flow for flow in parsed_rows if flow.is_cash_business]),
        "eligible_cash_event_rows": len(
            [flow for flow in parsed_rows if flow.is_cash_business]
        ),
        "eligible_fx_rows": len(
            [flow for flow in parsed_rows if flow.fx_legs is not None]
        ),
        "imported_cash_events": imported_cash_events,
        "skipped_unsupported_rows": skip_counts["unsupported"],
        "affected_symbols": affected_symbols,
        "date_start": min(dates).isoformat() if dates else None,
        "date_end": max(dates).isoformat() if dates else None,
        "source_account_masks": source_accounts or [],
        "business_counts": business_counts,
        "duplicate_samples": [flow_to_sample(flow, True) for flow in duplicate_rows[:10]],
        "import_samples": [flow_to_sample(flow, False) for flow in import_rows[:10]],
        "errors": errors[:50],
        "warnings": (warnings or [])[:50],
    }


def preview_booked_source_hashes(
    db: Session,
    user_id: int,
    parsed_rows: List[ParsedIbkrFlow],
    *,
    broker_account_id: int,
    resolution: ExistingSourceResolution,
    errors: List[str],
) -> set[str]:
    """Dry-run source-to-canonical coverage without mutating the database."""
    booked_hashes = set(resolution.booked_hashes)
    prospective_dividends = [
        flow
        for flow in parsed_rows
        if flow.is_cash_dividend and flow.row_hash not in resolution.booked_hashes
    ]
    for flow in parsed_rows:
        if (
            flow.is_trade
            or flow.is_cash_dividend
            or flow.is_cash_business
            or flow.fx_legs is not None
        ) and flow.row_hash not in booked_hashes:
            booked_hashes.add(flow.row_hash)

    for flow in parsed_rows:
        if not flow.is_withholding_tax or flow.row_hash in booked_hashes:
            continue
        candidate_ids = {
            action.id
            for action in find_dividend_candidates_for_tax(
                db,
                user_id,
                flow,
                broker_account_id=broker_account_id,
            )
        }
        virtual_candidates = [
            dividend
            for dividend in prospective_dividends
            if dividend.symbol == flow.symbol
            and dividend.market == flow.market
            and dividend.base_currency == flow.base_currency
            and dividend.trade_date == flow.trade_date
        ]
        candidate_count = len(candidate_ids) + len(virtual_candidates)
        if candidate_count == 1:
            booked_hashes.add(flow.row_hash)
        else:
            errors.append(
                f"row {flow.source_row_number}: withholding tax requires exactly one "
                f"same-account, same-security, same-date dividend candidate; "
                f"found {candidate_count}"
            )
    return booked_hashes


def resolve_archived_only_hashes(
    db: Session,
    user_id: int,
    parsed_rows: List[ParsedIbkrFlow],
    *,
    broker_account_id: int,
) -> set[str]:
    """期权与现金/外汇行的独立归档判重（预览与正式导入共用）。

    这些行不入 resolve_existing_sources（那里只看 eligible 行）：
    同账户既有归档行返回其 hash 集合，归属他账户则阻断。
    """
    archived_only_hashes = [
        flow.row_hash
        for flow in parsed_rows
        if flow.skip_reason in ("option", "cash", "fx", "excluded")
    ]
    existing: set[str] = set()
    if not archived_only_hashes:
        return existing
    for existing_archived in (
        db.query(IbkrActivityFlow)
        .filter(
            IbkrActivityFlow.user_id == user_id,
            IbkrActivityFlow.row_hash.in_(archived_only_hashes),
        )
        .all()
    ):
        if existing_archived.broker_account_id != broker_account_id:
            raise ValueError(
                "IBKR 归档来源记录无法安全判重："
                f"row_hash={existing_archived.row_hash} 已归属其他券商账户"
                f"（broker_account_id={existing_archived.broker_account_id}）。"
                "请确认此前是否选错账户导入；当前导入不会静默跳过该记录"
            )
        existing.add(existing_archived.row_hash)
    return existing


def cash_flow_anomaly_warning(flow: ParsedIbkrFlow) -> Optional[str]:
    """现金/外汇行的异常报警文案（预览、导入、回填共用同一口径）。"""
    if flow.skip_reason == "cash":
        amount = flow.cash_amount
        if (
            flow.activity_type in IBKR_CASH_EVENT_TYPES
            and amount is not None
            and amount != 0
            and flow.cash_event_type is None
        ):
            return (
                f"row {flow.source_row_number}: {flow.activity_type} 金额方向与"
                f"业务类型不符（{amount}），已归档未入账，请人工核对"
            )
        return None
    if flow.skip_reason == "fx" and flow.fx_legs is None:
        return (
            f"row {flow.source_row_number}: 外汇兑换行货币对无法解析或与 "
            f"Price Currency 列不一致（{flow.raw_symbol} / "
            f"{flow.price_currency or '-'}），已归档未入账，请人工核对"
        )
    return None


def cash_flow_anomaly_warnings(parsed_rows: List[ParsedIbkrFlow]) -> List[str]:
    warnings: List[str] = []
    for flow in parsed_rows:
        warning = cash_flow_anomaly_warning(flow)
        if warning is not None:
            warnings.append(warning)
    return warnings


def mark_archived_bookable_duplicates(
    parsed_rows: List[ParsedIbkrFlow],
    existing_archived_hashes: set[str],
    *,
    duplicate_hashes: set[str],
    booked_source_hashes: set[str],
) -> None:
    """既有归档且可入账的现金/外汇行按"已入账重复"计入审计口径。"""
    for flow in parsed_rows:
        if (
            (flow.is_cash_business or flow.fx_legs is not None)
            and flow.row_hash in existing_archived_hashes
        ):
            duplicate_hashes.add(flow.row_hash)
            booked_source_hashes.add(flow.row_hash)


def preview_ibkr_activity(
    db: Session,
    user_id: int,
    contents: bytes,
    filename: str,
    broker_account_id: Optional[int] = None,
) -> Dict[str, Any]:
    if broker_account_id is None:
        raise ValueError("请选择 IBKR 券商账户后再预览")
    broker_account = validate_import_account(
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
    parsed_rows, business_counts, total_rows, errors = parse_rows(
        contents,
        filename,
        name_overrides=get_name_overrides(db, user_id),
        excluded_symbols=frozenset(get_excluded_symbols(db, user_id)),
    )
    is_xlsx = is_ibkr_xlsx_filename(filename)
    source_accounts = validate_statement_accounts(
        parsed_rows, broker_account, allow_missing_accounts=is_xlsx
    )
    warnings_extra = (
        [
            "trade_history.xlsx 不含账户标识列，无法与所选账户交叉校验，"
            "请人工确认文件属于该 IBKR 账户"
        ]
        if is_xlsx and not source_accounts
        else []
    )
    warnings_extra.extend(cash_flow_anomaly_warnings(parsed_rows))
    resolution = resolve_existing_sources(
        db,
        user_id,
        eligible_rows(parsed_rows),
        broker_account_id=broker_account_id,
        broker_account=broker_account,
    )
    booked_source_hashes = preview_booked_source_hashes(
        db,
        user_id,
        parsed_rows,
        broker_account_id=broker_account_id,
        resolution=resolution,
        errors=errors,
    )
    # 现金/外汇行的归档判重与正式导入共用：既有行计入 duplicate/booked，
    # 他账户归档行同样在预览阶段阻断
    existing_archived_hashes = resolve_archived_only_hashes(
        db, user_id, parsed_rows, broker_account_id=broker_account_id
    )
    duplicate_hashes = set(resolution.duplicate_hashes)
    mark_archived_bookable_duplicates(
        parsed_rows,
        existing_archived_hashes,
        duplicate_hashes=duplicate_hashes,
        booked_source_hashes=booked_source_hashes,
    )
    return build_import_result(
        filename=filename,
        total_rows=total_rows,
        parsed_rows=parsed_rows,
        business_counts=business_counts,
        existing_hashes=duplicate_hashes,
        booked_source_hashes=booked_source_hashes,
        imported_transactions=0,
        imported_corporate_actions=0,
        imported_tax_adjustments=0,
        affected_symbols=0,
        errors=errors,
        warnings=warnings_extra,
        source_accounts=source_accounts,
    )


def create_ibkr_activity_flow(
    *,
    user_id: int,
    filename: str,
    flow: ParsedIbkrFlow,
    broker_account_id: Optional[int] = None,
    import_batch_id: Optional[int] = None,
    transaction_id: Optional[int] = None,
    corporate_action_id: Optional[int] = None,
) -> IbkrActivityFlow:
    return IbkrActivityFlow(
        user_id=user_id,
        broker_account_id=broker_account_id,
        import_batch_id=import_batch_id,
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


def create_cash_events_for_flow(
    db: Session,
    *,
    flow: ParsedIbkrFlow,
    archived: IbkrActivityFlow,
    warnings: Optional[List[str]] = None,
) -> int:
    """为一条已归档的现金/外汇行创建链接的 CashEvent，返回创建数量。

    现金业务行（存款/利息）一行一事件；外汇兑换行两条腿各一事件、
    佣金再一事件（IBKR 现汇佣金以账户基础货币收取，且腿净额不含佣金，
    故单独入账不重复计费）。调整（纸面损益）与方向异常的行不入账，
    方向异常报 warning。导入与回填共用此口径。
    """

    def _event(event_type: str, amount: Decimal, currency: str, label: str) -> CashEvent:
        event = CashEvent(
            user_id=archived.user_id,
            broker_account_id=archived.broker_account_id,
            event_type=event_type,
            amount=abs(amount),
            currency=currency,
            event_date=flow.trade_date,
            notes=(
                f"{BROKER_NAME}对账单{label}(导入); "
                f"{flow.description or flow.activity_type}; "
                f"row_hash={flow.row_hash}"
            ),
        )
        db.add(event)
        return event

    if flow.is_cash_business:
        event = _event(
            flow.cash_event_type,
            flow.cash_amount,
            flow.price_currency or flow.base_currency,
            flow.activity_type,
        )
        db.flush()
        archived.cash_event_id = event.id
        return 1

    if flow.skip_reason == "cash":
        if warnings is not None:
            warning = cash_flow_anomaly_warning(flow)
            if warning is not None:
                warnings.append(warning)
        return 0

    legs = flow.fx_legs
    if legs is None:
        if warnings is not None:
            warning = cash_flow_anomaly_warning(flow)
            if warning is not None:
                warnings.append(warning)
        return 0

    (base_currency, base_amount), (quote_currency, quote_amount) = legs
    base_event = _event(
        "FX_IN" if base_amount > 0 else "FX_OUT",
        base_amount,
        base_currency,
        f"外汇兑换{flow.raw_symbol}基础腿",
    )
    quote_event = _event(
        "FX_IN" if quote_amount > 0 else "FX_OUT",
        quote_amount,
        quote_currency,
        f"外汇兑换{flow.raw_symbol}对价腿",
    )
    fee_event = None
    if flow.commission:
        fee_event = _event(
            "FEE",
            flow.commission,
            flow.base_currency,
            f"外汇兑换{flow.raw_symbol}佣金",
        )
    db.flush()
    archived.cash_event_id = base_event.id
    archived.fx_quote_cash_event_id = quote_event.id
    if fee_event is not None:
        archived.fx_fee_cash_event_id = fee_event.id
    return 3 if fee_event is not None else 2
def find_dividend_for_tax(
    db: Session,
    user_id: int,
    flow: ParsedIbkrFlow,
    broker_account_id: Optional[int] = None,
) -> Optional[CorporateAction]:
    candidates = find_dividend_candidates_for_tax(
        db,
        user_id,
        flow,
        broker_account_id=broker_account_id,
    )
    return candidates[0] if len(candidates) == 1 else None


def find_dividend_candidates_for_tax(
    db: Session,
    user_id: int,
    flow: ParsedIbkrFlow,
    broker_account_id: Optional[int] = None,
) -> List[CorporateAction]:
    """Return only same-account, same-security, same-payment-date candidates."""
    return (
        db.query(CorporateAction)
        .filter(
            CorporateAction.user_id == user_id,
            CorporateAction.symbol == flow.symbol,
            CorporateAction.market == flow.market,
            CorporateAction.action_type == "CASH_DIVIDEND",
            CorporateAction.currency == flow.base_currency,
            CorporateAction.broker_account_id == broker_account_id,
            or_(
                CorporateAction.ex_date == flow.trade_date,
                CorporateAction.payment_date == flow.trade_date,
            ),
        )
        .order_by(CorporateAction.id)
        .all()
    )


def calculate_position_before(
    db: Session,
    user_id: int,
    symbol: str,
    market: str,
    before_date: date,
    broker_account_id: Optional[int] = None,
) -> tuple[Decimal, Decimal]:
    query = db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.symbol == symbol,
        Transaction.market == market,
        Transaction.transaction_date < before_date,
        Transaction.broker_account_id == broker_account_id,
    )
    transactions = (
        query.order_by(Transaction.transaction_date, Transaction.id)
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
    *,
    broker_account_id: Optional[int] = None,
    import_batch_id: Optional[int] = None,
    relistings: Optional[List[Dict[str, Any]]] = None,
) -> int:
    """转板映射由调用方注入（security_rules RELISTING 类型），不再读模块常量。"""
    created = 0
    for relisting in relistings or []:
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

        existing_transfer_query = db.query(Transaction).filter(
            Transaction.user_id == user_id,
            Transaction.notes.like(f"%{SYNTHETIC_RELISTING_MARKER}%"),
            Transaction.notes.like(f"%{old_symbol}->{new_symbol}%"),
            Transaction.broker_account_id == broker_account_id,
        )
        if existing_transfer_query.first():
            continue

        first_new_trade_date = min(new_trade_dates)
        transfer_date = first_new_trade_date - timedelta(days=1)
        quantity, old_avg_cost = calculate_position_before(
            db,
            user_id,
            old_symbol,
            old_market,
            first_new_trade_date,
            broker_account_id=broker_account_id,
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
                broker_account_id=broker_account_id,
                import_batch_id=import_batch_id,
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
                broker_account_id=broker_account_id,
                import_batch_id=import_batch_id,
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
    *,
    import_batch_id: Optional[int] = None,
    existing_source: Optional[IbkrActivityFlow] = None,
) -> int:
    tax_amount = abs(flow.gross_amount or Decimal("0"))
    action.tax_withheld = (action.tax_withheld or Decimal("0")) + tax_amount
    if action.total_dividend is not None:
        action.net_dividend = max(Decimal("0"), action.total_dividend - action.tax_withheld)
    action.notes = (
        f"{action.notes or ''}; {BROKER_NAME} withholding tax row={flow.source_row_number}; "
        f"row_hash={flow.row_hash}"
    ).strip("; ")
    if existing_source is not None:
        existing_source.corporate_action_id = action.id
        existing_source.skip_reason = None
        existing_source.notes = (
            f"{existing_source.notes or ''}; attributed during account-scoped re-import"
        ).strip("; ")
        db.add(existing_source)
    else:
        db.add(
            create_ibkr_activity_flow(
                user_id=user_id,
                filename=filename,
                flow=flow,
                broker_account_id=action.broker_account_id,
                import_batch_id=import_batch_id,
                corporate_action_id=action.id,
            )
        )
    return 1


def import_ibkr_activity(
    db: Session,
    user_id: int,
    contents: bytes,
    filename: str,
    broker_account_id: Optional[int] = None,
) -> Dict[str, Any]:
    if broker_account_id is None:
        raise ValueError("请选择 IBKR 券商账户后再正式导入")

    broker_account = validate_import_account(
        db,
        user_id=user_id,
        broker_account_id=broker_account_id,
        broker=BROKER_NAME,
    )
    batch = start_import_batch(
        db,
        user_id=user_id,
        broker_account_id=broker_account_id,
        broker=BROKER_NAME,
        source_type=SOURCE_TYPE_XLSX if is_ibkr_xlsx_filename(filename) else SOURCE_TYPE,
        filename=filename,
        contents=contents,
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
    )
    batch_id = batch.id
    total_rows = 0
    imported_source_rows = 0
    records_committed = False
    imported_transactions = 0
    imported_corporate_actions = 0
    imported_tax_adjustments = 0
    imported_cash_events = 0
    imported_transfer_transactions = 0
    canonical_action_ids_changed: set[int] = set()
    booked_source_hashes: set[str] = set()
    source_accounts: List[str] = []
    duplicate_hashes: set[str] = set()

    try:
        parsed_rows, business_counts, total_rows, errors = parse_rows(
            contents,
            filename,
            name_overrides=get_name_overrides(db, user_id),
            excluded_symbols=frozenset(get_excluded_symbols(db, user_id)),
        )
        is_xlsx = is_ibkr_xlsx_filename(filename)
        source_accounts = validate_statement_accounts(
            parsed_rows, broker_account, allow_missing_accounts=is_xlsx
        )
        warnings_extra = (
            [
                "trade_history.xlsx 不含账户标识列，无法与所选账户交叉校验，"
                "请人工确认文件属于该 IBKR 账户"
            ]
            if is_xlsx and not source_accounts
            else []
        )
        dates = [flow.trade_date for flow in parsed_rows]
        set_import_batch_source_stats(
            batch,
            row_count=total_rows,
            period_start=min(dates) if dates else None,
            period_end=max(dates) if dates else None,
        )
        resolution = resolve_existing_sources(
            db,
            user_id,
            eligible_rows(parsed_rows),
            broker_account_id=broker_account_id,
            broker_account=broker_account,
        )
        duplicate_hashes = set(resolution.duplicate_hashes)
        booked_source_hashes.update(resolution.booked_hashes)

        affected_symbols: set[tuple[str, str]] = set()
        pending_tax_flows: List[ParsedIbkrFlow] = [
            flow
            for flow in parsed_rows
            if flow.is_withholding_tax
            and flow.row_hash not in resolution.booked_hashes
        ]

        # 期权行不在 eligible_rows 里，resolution 不覆盖其哈希；
        # 重复上传去重需单独查（否则再归档会撞 row_hash 唯一约束）。
        # 与 resolve_existing_sources 同口径：仅同账户可判重——归属其他账户
        # 的既有来源说明此前选错了账户，必须阻塞并提示，不能静默视为重复
        # （(user_id, row_hash) 唯一约束也使正确账户无法再补录该审计来源）。
        # 现金/外汇/期权行的归档判重与预览共用同一通道；异常行报警也在
        # 批级统一产生（重导入时行已归档、不再逐行入账，逐行报警会漏）
        warnings_extra.extend(cash_flow_anomaly_warnings(parsed_rows))
        existing_archived_hashes = resolve_archived_only_hashes(
            db, user_id, parsed_rows, broker_account_id=broker_account_id
        )
        mark_archived_bookable_duplicates(
            parsed_rows,
            existing_archived_hashes,
            duplicate_hashes=duplicate_hashes,
            booked_source_hashes=booked_source_hashes,
        )

        for flow in parsed_rows:
            if not flow.is_trade and not flow.is_cash_dividend and not flow.is_withholding_tax:
                # 期权成交跳过但归档（owner 2026-07-28 拍板）：不生成交易、
                # 不影响持仓，原始行留在 ibkr_activity_flows 供审计与去重。
                # 系统的已实现盈亏因此不含期权部分，报表口径需注明。
                if (
                    flow.skip_reason in ("cash", "fx")
                    and flow.row_hash not in existing_archived_hashes
                    and flow.row_hash not in booked_source_hashes
                ):
                    archived_cash = create_ibkr_activity_flow(
                        user_id=user_id,
                        filename=filename,
                        flow=flow,
                        broker_account_id=broker_account_id,
                        import_batch_id=batch_id,
                    )
                    db.add(archived_cash)
                    # warnings=None：异常报警已在批级统一产生
                    imported_cash_events += create_cash_events_for_flow(
                        db,
                        flow=flow,
                        archived=archived_cash,
                        warnings=None,
                    )
                    booked_source_hashes.add(flow.row_hash)
                    continue
                if (
                    flow.skip_reason in ("option", "excluded")
                    and flow.row_hash not in existing_archived_hashes
                    and flow.row_hash not in booked_source_hashes
                ):
                    # create_ibkr_activity_flow 已带上 flow.skip_reason
                    # （option/excluded），不得覆写——审计记录要保留真实原因
                    db.add(
                        create_ibkr_activity_flow(
                            user_id=user_id,
                            filename=filename,
                            flow=flow,
                            broker_account_id=broker_account_id,
                            import_batch_id=batch_id,
                        )
                    )
                    booked_source_hashes.add(flow.row_hash)
                continue
            if flow.row_hash in resolution.booked_hashes:
                continue
            if flow.is_withholding_tax:
                continue

            if flow.is_cash_dividend:
                action = CorporateAction(
                    user_id=user_id,
                    broker_account_id=broker_account_id,
                    import_batch_id=batch_id,
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
                        user_id=user_id,
                        filename=filename,
                        flow=flow,
                        broker_account_id=broker_account_id,
                        import_batch_id=batch_id,
                        corporate_action_id=action.id,
                    )
                )
                booked_source_hashes.add(flow.row_hash)
                imported_corporate_actions += 1
                canonical_action_ids_changed.add(action.id)
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
                broker_account_id=broker_account_id,
                import_batch_id=batch_id,
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
                    user_id=user_id,
                    filename=filename,
                    flow=flow,
                    broker_account_id=broker_account_id,
                    import_batch_id=batch_id,
                    transaction_id=transaction.id,
                )
            )
            booked_source_hashes.add(flow.row_hash)
            affected_symbols.add((flow.symbol, flow.market))
            imported_transactions += 1

        db.flush()
        for flow in pending_tax_flows:
            candidates = find_dividend_candidates_for_tax(
                db,
                user_id,
                flow,
                broker_account_id=broker_account_id,
            )
            if len(candidates) != 1:
                if flow.row_hash not in resolution.unresolved_tax_sources:
                    unresolved_source = create_ibkr_activity_flow(
                        user_id=user_id,
                        filename=filename,
                        flow=flow,
                        broker_account_id=broker_account_id,
                        import_batch_id=batch_id,
                    )
                    unresolved_source.skip_reason = "unattributed_tax"
                    unresolved_source.notes = (
                        "Preserved without canonical action: expected exactly one "
                        f"same-account same-security same-date dividend; found {len(candidates)}"
                    )
                    db.add(unresolved_source)
                errors.append(
                    f"row {flow.source_row_number}: withholding tax requires exactly one "
                    f"same-account, same-security, same-date dividend candidate; "
                    f"found {len(candidates)}"
                )
                continue
            imported_tax_adjustments += apply_withholding_tax(
                db,
                user_id,
                filename,
                flow,
                candidates[0],
                import_batch_id=batch_id,
                existing_source=resolution.unresolved_tax_sources.get(flow.row_hash),
            )
            booked_source_hashes.add(flow.row_hash)
            canonical_action_ids_changed.add(candidates[0].id)

        db.flush()
        imported_transfer_transactions = apply_known_relisting_transfers(
            db,
            user_id,
            parsed_rows,
            affected_symbols,
            broker_account_id=broker_account_id,
            import_batch_id=batch_id,
            relistings=get_relistings(db, user_id),
        )
        imported_transactions += imported_transfer_transactions
        canonical_objects_changed = (
            imported_transactions + len(canonical_action_ids_changed)
        )

        try:
            db.commit()
        except IntegrityError as exc:
            raise ValueError(
                "Duplicate IBKR activity flow detected during import"
            ) from exc
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
            booked_source_hashes=booked_source_hashes,
            imported_transactions=imported_transactions,
            imported_corporate_actions=imported_corporate_actions,
            imported_tax_adjustments=imported_tax_adjustments,
            affected_symbols=recalculated_symbols,
            imported_cash_events=imported_cash_events,
            errors=errors,
            warnings=warnings_extra,
            source_accounts=source_accounts,
            canonical_objects_changed=canonical_objects_changed,
        )
        imported_source_rows = (
            db.query(IbkrActivityFlow)
            .filter(IbkrActivityFlow.import_batch_id == batch_id)
            .count()
        )
        result["archived_source_rows"] = imported_source_rows
        imported_source_count = max(
            0,
            result["booked_source_rows"] - result["duplicate_rows"],
        )
        completed_batch = complete_import_batch(
            db,
            batch_id,
            result=result,
            imported_count=imported_source_count,
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
                    db.query(IbkrActivityFlow)
                    .filter(IbkrActivityFlow.import_batch_id == batch_id)
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
            imported_count=max(
                0,
                len(booked_source_hashes) - len(duplicate_hashes),
            ),
            duplicate_count=len(duplicate_hashes),
            archived_count=imported_source_rows,
        )
        raise
