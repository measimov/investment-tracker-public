from __future__ import annotations

import csv
import io
from types import SimpleNamespace
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd
from sqlalchemy import func, or_
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
    append_note,
    archive_and_link,
    attribute_source,
    attribute_tax_source,
    base_import_result,
    book_suspected_source,
    disambiguated_row_hash,
    fail_broker_import,
    import_note,
    iso_date_range,
    lock_broker_import,
    mark_suspected_duplicate,
    normalize_hash_value as normalize_hash_value,  # 测试断言导入器命名空间,
    ProspectiveTransaction,
    RESULT_SAMPLE_LIMIT,
    split_new_and_duplicate_rows,
    strip_text,
    SUSPECTED_DUPLICATE,
    SuspectedDuplicateResolution,
    UNATTRIBUTED_TAX,
)
from ..config import settings
from ..services.dividend_sync_service import MATCH_WINDOW_BEFORE_DAYS
from .portfolio.semantics import dividend_cash_date
from ..services.holding_service import (
    AccountReplayError,
    account_precheck_events,
    recalculate_holdings,
    replay_account_quantities,
    replay_transactions_per_account,
)
from ..services.security_rule_service import (
    get_excluded_symbols,
    get_name_overrides,
    get_relistings,
)
from ..services.import_batch_service import (
    complete_import_batch,
    set_import_batch_source_stats,
    start_import_batch,
    validate_import_account,
    validate_source_file_account,
)
from .dividend_tax_service import create_dividend_tax_event, attribute_tax_cash_source
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
# v7: 不唯一或非到账日的股息税按实际日期独立入账，允许显式归属
PARSER_VERSION = "7"
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
    # 上次已归档为疑似重复（skip_reason=suspected_duplicate、无任何链接）的来源行：
    # 不是孤儿，是等人工确认的保留行（与未归属税行同一套三段式）
    suspected_sources: Dict[str, IbkrActivityFlow] = field(default_factory=dict)


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
        return (
            len(configured_suffix) >= 4
            and len(configured_suffix) <= 6
            and statement_suffix.endswith(configured_suffix)
        )

    if statement_masked and not configured_masked:
        if 4 <= len(configured_suffix) <= 6 and statement_suffix.endswith(configured_suffix):
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
        raise ValueError("所选 IBKR 账户缺少账户掩码或尾号；请先在账户资料中填写后再导入")

    configured_masks = [
        value for value in re.split(r"[/,，;；、|\\n]+", configured) if strip_text(value)
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
            f"IBKR CSV 账户与所选券商账户不匹配：CSV={', '.join(mismatched)}；所选账户={configured}"
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
        frame = pd.read_excel(io.BytesIO(contents), sheet_name=XLSX_TRADE_SHEET, dtype=object)
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
    excluded_symbols: frozenset = frozenset(),
) -> tuple[List[ParsedIbkrFlow], Dict[str, int], int, List[str]]:
    """纯解析：同样的字节输入恒得同样的输出，不打外网、不读全局缓存。

    此前这里在末尾调 enrich_security_names（Tushare 查名 + 进程级缓存），
    于是 parse 的输出依赖外网状态与跨调用的缓存——同一份文件两次解析可能
    得到不同的 name，测试必须 monkeypatch，预览延迟也与外网耦合。
    招商/东财的 parse 层都是真纯函数（规则全部注入），只有这里例外。

    名称仅是展示字段，与入账语义无关，因此由编排层（preview_/import_）在
    解析之后显式调用 enrich_security_names 补齐。
    """
    if is_ibkr_xlsx_filename(filename):
        data_rows, base_currency, total_rows, errors = read_ibkr_trade_history_xlsx(contents)
    else:
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
            # xlsx 行带显式资产类别（OPT），比符号启发式更可靠；CSV 行无此键
            if strip_text(row.get("资产类别")) == XLSX_OPTION_ASSET_TYPE or is_option_symbol(
                raw_symbol, description
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

        row_hash = disambiguated_row_hash(hash_values, hash_occurrences, calculate_row_hash)

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
    if any(source.broker_account_id not in {None, broker_account.id} for source in linked_sources):
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
        source.corporate_action_id for source in sources if source.corporate_action_id is not None
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
    # 每条链接交易被多少条 IBKR 来源引用：一次 GROUP BY（此前逐条 COUNT，#279）
    transaction_link_counts: Dict[int, int] = (
        dict(
            db.query(IbkrActivityFlow.transaction_id, func.count(IbkrActivityFlow.id))
            .filter(IbkrActivityFlow.transaction_id.in_(transaction_ids))
            .group_by(IbkrActivityFlow.transaction_id)
            .all()
        )
        if transaction_ids
        else {}
    )

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
        if source.activity_type == WITHHOLDING_TAX_TYPE and source.cash_event_id is not None:
            event = db.get(CashEvent, source.cash_event_id)
            if (
                has_transaction
                or has_corporate_action
                or event is None
                or event.user_id != user_id
                or event.broker_account_id != broker_account_id
                or event.tax_kind != "DIVIDEND"
                or event.event_type != "TAX"
                or source.skip_reason is not None
                or source.fx_quote_cash_event_id is not None
                or source.fx_fee_cash_event_id is not None
                or event.event_date != source.trade_date
                or event.currency != source.base_currency
                or not _decimal_equal(event.amount, abs(source.gross_amount))
            ):
                raise _unsafe_existing_source(source, "股息税现金事实与来源不一致")
            resolution.booked_hashes.add(source.row_hash)
            resolution.duplicate_hashes.add(source.row_hash)
            continue
        if not has_transaction and not has_corporate_action:
            if (
                source.activity_type == WITHHOLDING_TAX_TYPE
                and source.skip_reason == UNATTRIBUTED_TAX
            ):
                resolution.unresolved_tax_sources[source.row_hash] = source
                continue
            if source.skip_reason == SUSPECTED_DUPLICATE:
                resolution.suspected_sources[source.row_hash] = source
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
            link_count = transaction_link_counts.get(canonical_id, 0)
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


# ---------------------------------------------------------------------------
# 疑似重复守卫（IBKR 版）——row_hash 之外的第二层，**不改任何 hash 输入**
#
# 同一笔经济事实在账本里可能已经以另一种形态存在，row_hash 判重看不见：
#   - 成交：trade_history.xlsx 把 Trade ID 并进说明（hash 不同于 CSV 同笔成交），
#     或用户在没有券商流水时手工录入；
#   - 股息：分红公告建议入账的 CASH_DIVIDEND（港股按 HKD、除净日/派息日为真实日期），
#     而 IBKR 报表按 USD、在到账日记一行股息 + 另一行外国预扣税。
# 于是按下面的二级键找「账本里已有、但不是被本文件同 hash 行解释掉的」记录：
#
#   成交 = (代码, 市场, 成交日, 方向, |数量|)，对象是同账户或未指定账户的 BUY/SELL
#          交易（含手工录入、含其他来源类型的 IBKR 流水链接的交易）；成交价**不进键**
#          （xlsx/CSV/手工的价格精度不同），只在配对时按相对差 ≤ 1% 放行，同键多笔时
#          按价格最接近优先配对——同日同量不同价的两笔真实成交不会互相顶替。
#          美股成交日允许 ±1 天（xlsx 日期是香港时间，美股盘中跨过香港午夜），同日优先。
#   股息 = (代码, 市场) + 日期窗口，对象是同账户或未指定账户的 CASH_DIVIDEND；
#          **不比金额与币种**（USD vs HKD）。窗口沿用分红同步判重
#          （dividend_sync_service.match_existing_action）的口径、角色互换：
#          到账日 ∈ [除净日 − 3 天, (派息日 or 除净日) + dividend_sync_match_window_days]；
#          已链接 IBKR 股息来源的记录本身就是到账日记账，同一笔股息换一种导出形态
#          日期不变，窗口收紧为 ±3 天（否则月度派息标的会把上个月的股息当成本月的）。
#   预扣税 = 随同日同标的的疑似股息一并扣住；本文件没有同日股息、账本也没有同币种
#          同日股息可归属、但日期窗口命中已入账股息的孤立税行同样扣住。
#
# 三条纪律与招商 #190 一致：
#   1. **计数而非存在**：每条既有记录只抵一行，多出来的真实成交照常入账；
#   2. 被本文件同 hash 行解释掉的既有记录（hash 重复）不占额度；
#   3. 扣住的行归档为 skip_reason=suspected_duplicate、不入账；重导时按重复计；
#      用户勾选确认（confirm_suspected_row_hashes）后在原归档行上转正，绝不插新行。
#      确认股息时其同日预扣税一并转正。
# ---------------------------------------------------------------------------

SUSPECTED_TRADE_PRICE_TOLERANCE = Decimal("0.01")  # 相对差，1%
SUSPECTED_US_TRADE_DAY_TOLERANCE = 1  # 美股：香港时间/美东时间的成交日可差一天
# 已链接 IBKR 股息来源的记录：同一笔股息换导出形态，到账日不变
SUSPECTED_IBKR_DIVIDEND_DAYS = MATCH_WINDOW_BEFORE_DAYS


@dataclass
class SuspectedMatch:
    """疑似重复的配对目标（交易或公司行动）及其来源说明。"""

    kind: str  # "transaction" | "corporate_action" | PREVIOUSLY_HELD_KIND
    record: Any  # PREVIOUSLY_HELD_KIND 时为 None（配对目标是上次归档的疑似行）
    reason: str
    source: Optional[IbkrActivityFlow] = None
    source_label: str = ""


# 税行随「上次已归档为疑似、未确认」的同日股息一并扣住时的配对类型：没有账本记录可指
PREVIOUSLY_HELD_KIND = "previously_held_dividend"


def _dividend_key(flow: ParsedIbkrFlow) -> tuple:
    """股息与其预扣税的配对键——与 preview_booked_source_hashes 的虚拟候选同口径。"""
    return (flow.symbol, flow.market, flow.base_currency, flow.trade_date)


def _record_source_label(record: Any, source: Optional[IbkrActivityFlow]) -> str:
    if source is not None:
        return f"IBKR 导入 {source.source_filename or ''} 第 {source.source_row_number} 行"
    notes = strip_text(getattr(record, "notes", None))
    if notes.startswith("来自分红公告建议"):
        return notes
    if getattr(record, "import_batch_id", None):
        return f"导入批次 #{record.import_batch_id}"
    return "手工录入"


def _fmt_decimal(value: Any) -> str:
    if value is None:
        return "—"
    return format(Decimal(str(value)).normalize(), "f")


def _linked_ibkr_sources(db: Session, column, ids: set[int]) -> Dict[int, List[IbkrActivityFlow]]:
    if not ids:
        return {}
    linked: Dict[int, List[IbkrActivityFlow]] = {}
    for source in (
        db.query(IbkrActivityFlow).filter(column.in_(sorted(ids))).order_by(IbkrActivityFlow.id)
    ):
        linked.setdefault(getattr(source, column.key), []).append(source)
    return linked


def _trade_day_tolerance(market: Optional[str]) -> int:
    """美股允许 ±1 天：trade_history.xlsx 的日期是香港时间（美股盘中跨过香港午夜），
    手工录入也常按北京时间记；其他市场与香港同时区，要求同日。"""
    return SUSPECTED_US_TRADE_DAY_TOLERANCE if market == "美股" else 0


def _match_suspected_trades(
    db: Session,
    user_id: int,
    broker_account_id: int,
    pool: List[ParsedIbkrFlow],
    batch_hashes: set[str],
    resolution: SuspectedDuplicateResolution,
    warnings: List[str],
) -> None:
    if not pool:
        return
    query_dates = {
        flow.trade_date + timedelta(days=offset)
        for flow in pool
        for offset in range(
            -_trade_day_tolerance(flow.market), _trade_day_tolerance(flow.market) + 1
        )
    }
    transactions = (
        db.query(Transaction)
        .filter(
            Transaction.user_id == user_id,
            or_(
                Transaction.broker_account_id == broker_account_id,
                Transaction.broker_account_id.is_(None),
            ),
            Transaction.transaction_type.in_(("BUY", "SELL")),
            Transaction.symbol.in_(sorted({flow.symbol for flow in pool})),
            Transaction.transaction_date.in_(sorted(query_dates)),
        )
        .order_by(Transaction.id)
        .all()
    )
    sources = _linked_ibkr_sources(
        db, IbkrActivityFlow.transaction_id, {txn.id for txn in transactions}
    )
    existing_by_key: Dict[tuple, List[Transaction]] = {}
    for txn in transactions:
        linked = sources.get(txn.id, [])
        if any(source.row_hash in batch_hashes for source in linked):
            continue  # 本文件同 hash 行已解释它，不占额度
        if SYNTHETIC_RELISTING_MARKER in (txn.notes or ""):
            continue  # 转板合成交易不是券商成交
        key = (txn.symbol, txn.market, txn.transaction_type)
        existing_by_key.setdefault(key, []).append(txn)

    flows_by_key: Dict[tuple, List[ParsedIbkrFlow]] = {}
    for flow in pool:
        key = (flow.symbol, flow.market, flow.transaction_type)
        flows_by_key.setdefault(key, []).append(flow)

    for key, flows in flows_by_key.items():
        existing = existing_by_key.get(key, [])
        tolerance = _trade_day_tolerance(key[1])
        pairs = []
        for flow_index, flow in enumerate(flows):
            quantity = abs(flow.quantity)
            for txn in existing:
                day_gap = abs((txn.transaction_date - flow.trade_date).days)
                if day_gap > tolerance or Decimal(str(txn.quantity)) != quantity:
                    continue
                txn_price = Decimal(str(txn.price))
                if txn_price <= 0:
                    continue
                diff = abs(flow.price - txn_price) / txn_price
                if diff <= SUSPECTED_TRADE_PRICE_TOLERANCE:
                    pairs.append((day_gap, diff, flow_index, txn.id, flow, txn))
        # 同日优先、价格最接近优先：同键多笔时真实成交不会互相顶替
        used_flows: set[int] = set()
        used_txns: set[int] = set()
        for _gap, _diff, flow_index, txn_id, flow, txn in sorted(pairs, key=lambda p: p[:4]):
            if flow_index in used_flows or txn_id in used_txns:
                continue
            used_flows.add(flow_index)
            used_txns.add(txn_id)
            source = (sources.get(txn.id) or [None])[0]
            label = _record_source_label(txn, source)
            resolution.held_hashes.add(flow.row_hash)
            resolution.matches[flow.row_hash] = SuspectedMatch(
                kind="transaction",
                record=txn,
                source=source,
                source_label=label,
                reason=(
                    f"与已有交易 #{txn.id}（{txn.transaction_date} {txn.transaction_type} "
                    f"{_fmt_decimal(txn.quantity)} @ {_fmt_decimal(txn.price)} "
                    f"{txn.currency}，{label}）"
                    + ("同日同向同数量" if _gap == 0 else "同向同数量、日期相邻（时区口径）")
                ),
            )
        # 数量/价格对不上的同日同向交易：可能是手工合并录入的多笔成交，只提示不扣住
        leftover_by_day: Dict[date, tuple[list, list]] = {}
        for index, flow in enumerate(flows):
            if index not in used_flows:
                leftover_by_day.setdefault(flow.trade_date, ([], []))[0].append(flow)
        for txn in existing:
            if txn.id not in used_txns and txn.transaction_date in leftover_by_day:
                leftover_by_day[txn.transaction_date][1].append(txn)
        symbol, _market, transaction_type = key
        for trade_date, (leftover_flows, leftover_txns) in sorted(leftover_by_day.items()):
            if not leftover_txns:
                continue
            warnings.append(
                f"{trade_date} {symbol} {transaction_type}：本文件有 {len(leftover_flows)} 笔"
                f"成交将入账，账本已有 {len(leftover_txns)} 笔同日同向但数量或价格对不上的交易"
                f"（#{', #'.join(str(txn.id) for txn in leftover_txns)}），"
                "请核对是否为合并录入的同一批成交"
            )


def _dividend_window_gap(
    action: CorporateAction, flow_date: date, *, ibkr_linked: bool
) -> Optional[int]:
    """命中返回日期差（越小越优先配对），不命中返回 None。"""
    anchor = dividend_cash_date(action)
    if anchor is None:
        return None
    gap = abs((flow_date - anchor).days)
    if ibkr_linked:
        return gap if gap <= SUSPECTED_IBKR_DIVIDEND_DAYS else None
    start = (action.ex_date or anchor) - timedelta(days=MATCH_WINDOW_BEFORE_DAYS)
    end = anchor + timedelta(days=settings.dividend_sync_match_window_days)
    return gap if start <= flow_date <= end else None


def _dividend_reason(action: CorporateAction, label: str) -> str:
    return (
        f"与已入账股息 #{action.id}（除净日 {action.ex_date}，派息日 "
        f"{action.payment_date or '—'}，{_fmt_decimal(action.total_dividend)} "
        f"{action.currency or ''}，{label}）日期窗口重合"
    )


def _dividend_match(action: CorporateAction, source, reason_prefix: str = "") -> SuspectedMatch:
    label = _record_source_label(action, source)
    return SuspectedMatch(
        kind="corporate_action",
        record=action,
        source=source,
        source_label=label,
        reason=f"{reason_prefix}{_dividend_reason(action, label)}",
    )


_PER_SHARE_RE = re.compile(
    r"([A-Z]{3})\s*([0-9]+(?:\.[0-9]+)?)\s*(?:每股|per\s+share)", re.IGNORECASE
)


def _per_share_descriptor(description: Optional[str]) -> Optional[tuple]:
    """IBKR 股息/预扣税描述里的「币种 每股金额」：
    `883(…) 现金红利 HKD 0.75 每股 (普通股息)` 与 `… HKD 0.75 每股 - CN 税` 是同一笔。"""
    match = _PER_SHARE_RE.search(description or "")
    if not match:
        return None
    return match.group(1).upper(), Decimal(match.group(2)).normalize()


def _match_suspected_dividends(
    db: Session,
    user_id: int,
    broker_account_id: int,
    dividend_pool: List[ParsedIbkrFlow],
    tax_pool: List[ParsedIbkrFlow],
    parsed_rows: List[ParsedIbkrFlow],
    batch_hashes: set[str],
    resolution: SuspectedDuplicateResolution,
) -> None:
    if not dividend_pool and not tax_pool:
        return
    actions = (
        db.query(CorporateAction)
        .filter(
            CorporateAction.user_id == user_id,
            CorporateAction.action_type == "CASH_DIVIDEND",
            CorporateAction.symbol.in_(sorted({flow.symbol for flow in dividend_pool + tax_pool})),
            or_(
                CorporateAction.broker_account_id == broker_account_id,
                CorporateAction.broker_account_id.is_(None),
            ),
        )
        .order_by(CorporateAction.id)
        .all()
    )
    sources = _linked_ibkr_sources(
        db, IbkrActivityFlow.corporate_action_id, {action.id for action in actions}
    )
    candidates: List[tuple] = []  # (action, 链接的 IBKR 股息来源 or None)
    for action in actions:
        linked = sources.get(action.id, [])
        if any(source.row_hash in batch_hashes for source in linked):
            continue  # 本文件同 hash 行已解释它（hash 重复），不占额度
        dividend_source = next(
            (source for source in linked if source.activity_type == DIVIDEND_TYPE), None
        )
        candidates.append((action, dividend_source))

    def gaps_for(flow: ParsedIbkrFlow):
        for action, source in candidates:
            if action.symbol != flow.symbol or action.market != flow.market:
                continue
            gap = _dividend_window_gap(action, flow.trade_date, ibkr_linked=source is not None)
            if gap is not None:
                yield gap, action, source

    pairs = [
        (gap, flow_index, action.id, flow, action, source)
        for flow_index, flow in enumerate(dividend_pool)
        for gap, action, source in gaps_for(flow)
    ]
    used_flows: set[int] = set()
    used_actions: set[int] = set()
    held_by_key: Dict[tuple, SuspectedMatch] = {}
    for _gap, flow_index, action_id, flow, action, source in sorted(pairs, key=lambda p: p[:3]):
        if flow_index in used_flows or action_id in used_actions:
            continue
        used_flows.add(flow_index)
        used_actions.add(action_id)
        match = _dividend_match(action, source)
        resolution.held_hashes.add(flow.row_hash)
        resolution.matches[flow.row_hash] = match
        held_by_key.setdefault(_dividend_key(flow), match)

    # 本文件里同日（同标的/币种/日期）的股息：税行跟它们走。同日股息**部分被扣、部分入账**
    # 时不能按「同日还有一笔入账股息」放行全部税行——正式导入时税只能归到入账的那笔，
    # 被扣股息的税会被静默累加进去（PR #253 评审 P1）。先按描述里的「币种 每股金额」
    # 把税行配对到唯一一笔同日股息，跟随它的扣留状态；配不上又是混合日的，扣住待确认。
    dividends_by_key: Dict[tuple, List[ParsedIbkrFlow]] = {}
    for flow in parsed_rows:
        if flow.is_cash_dividend:
            dividends_by_key.setdefault(_dividend_key(flow), []).append(flow)
    # 「未入账」的同日股息 = 本轮判疑似的 + 上次已归档为疑似、本次仍未确认的。后者早已
    # 排出匹配池、本轮没有 matches——只看本轮集合会把它们当成「照常入账」，重导时税行
    # 全部放行并归到真正入账的那笔（PR #253 复审 P1：分两次导入，税额 11.46 应为 1.91）
    unconfirmed_prior = unconfirmed_previously_held(resolution)

    def is_held(dividend: ParsedIbkrFlow) -> bool:
        return dividend.row_hash in resolution.held_hashes or dividend.row_hash in unconfirmed_prior

    def match_of(dividend: ParsedIbkrFlow) -> SuspectedMatch:
        found = resolution.matches.get(dividend.row_hash) or held_by_key.get(
            _dividend_key(dividend)
        )
        if found is not None:
            return found
        return SuspectedMatch(
            kind=PREVIOUSLY_HELD_KIND,
            record=None,
            reason="同日股息上次导入已归档为疑似重复、尚未确认",
        )

    def archived_held_dividends(flow: ParsedIbkrFlow) -> List[IbkrActivityFlow]:
        """库里已归档为疑似、仍未确认、且不在本文件里的同日股息来源行。

        后续文件只含税行时，本文件的同日股息列表为空——不查库就会把税归到恰好唯一的那笔
        已入账股息上，而它可能属于另一笔被扣留的股息（PR #253 复审 P1）。"""
        rows = (
            db.query(IbkrActivityFlow)
            .filter(
                IbkrActivityFlow.user_id == user_id,
                IbkrActivityFlow.broker_account_id == broker_account_id,
                IbkrActivityFlow.activity_type == DIVIDEND_TYPE,
                IbkrActivityFlow.skip_reason == SUSPECTED_DUPLICATE,
                IbkrActivityFlow.symbol == flow.symbol,
                IbkrActivityFlow.market == flow.market,
                IbkrActivityFlow.base_currency == flow.base_currency,
                IbkrActivityFlow.trade_date == flow.trade_date,
            )
            .all()
        )
        return [
            row
            for row in rows
            if row.row_hash not in batch_hashes and row.row_hash not in resolution.confirmed_hashes
        ]

    tax_index = TaxCandidateIndex(db, user_id, tax_pool, broker_account_id)

    def booked_unrepresented(flow: ParsedIbkrFlow) -> List[tuple]:
        """同币种同日已入账、且不由本文件某行解释的股息：[(每股描述集合, action)]。"""
        actions = tax_index.candidates(flow)
        if not actions:
            return []
        result = []
        for action in actions:
            sources = tax_index.dividend_sources(action.id)
            if any(source.row_hash in batch_hashes for source in sources):
                continue  # 本文件同 hash 股息行已代表它
            result.append(({_per_share_descriptor(src.description) for src in sources}, action))
        return result

    for flow in tax_pool:
        key = _dividend_key(flow)
        # 同日股息的统一候选集：(每股描述集合, 是否未入账, 配对说明取法)
        entities: List[tuple] = []
        for dividend in dividends_by_key.get(key, []):
            entities.append(
                (
                    {_per_share_descriptor(dividend.description)},
                    is_held(dividend),
                    lambda d=dividend: match_of(d),
                )
            )
        for source in archived_held_dividends(flow):
            entities.append(
                (
                    {_per_share_descriptor(source.description)},
                    True,
                    lambda: SuspectedMatch(
                        kind=PREVIOUSLY_HELD_KIND,
                        record=None,
                        reason="同日股息此前已归档为疑似重复、尚未确认",
                    ),
                )
            )
        for descriptors, _action in booked_unrepresented(flow):
            entities.append((descriptors, False, None))

        if entities:
            held_entities = [entity for entity in entities if entity[1]]
            if not held_entities:
                continue  # 同日股息全部照常入账（或已入账）：税行随它们入账
            descriptor = _per_share_descriptor(flow.description)
            paired = [
                entity for entity in entities if descriptor is not None and descriptor in entity[0]
            ]
            paired_entity = paired[0] if len(paired) == 1 else None
            if paired_entity is not None and not paired_entity[1]:
                continue  # 明确属于一笔入账股息
            follow = paired_entity or (
                held_entities[0] if len(held_entities) == len(entities) else None
            )
            if follow is not None:
                dividend_match = follow[2]()
                reason = f"随同日疑似重复股息一并扣住：{dividend_match.reason}"
            else:
                # 同日有扣留也有入账、又无法从描述确定归属：扣住待确认，不因「唯一可入账
                # 候选恰好是另一笔股息」就自动归属
                dividend_match = held_entities[0][2]()
                reason = (
                    "同日多笔股息部分疑似重复、部分照常入账，无法从描述确定这条预扣税属于哪一笔，"
                    f"请核对后确认：{dividend_match.reason}"
                )
            resolution.held_hashes.add(flow.row_hash)
            resolution.matches[flow.row_hash] = SuspectedMatch(
                kind=dividend_match.kind,
                record=dividend_match.record,
                source=dividend_match.source,
                source_label=dividend_match.source_label,
                reason=reason,
            )
            continue
        # 孤立税行：库里同币种同日既无入账股息也无扣留股息，但日期窗口命中已入账股息
        best = min(gaps_for(flow), key=lambda item: (item[0], item[1].id), default=None)
        if best is not None:
            _gap, action, source = best
            resolution.held_hashes.add(flow.row_hash)
            resolution.matches[flow.row_hash] = _dividend_match(
                action, source, "预扣税对应的股息疑似已入账："
            )


def resolve_suspected_duplicates(
    db: Session,
    user_id: int,
    broker_account_id: int,
    parsed_rows: List[ParsedIbkrFlow],
    *,
    resolution: ExistingSourceResolution,
    confirmed_row_hashes: frozenset[str] = frozenset(),
) -> tuple[SuspectedDuplicateResolution, List[str]]:
    """预览与导入共用的唯一入口——两边不可能对同一份文件得出不同的疑似结论。

    返回 (结论, 提示)。提示只针对「数量/价格对不上的同日同向交易」，不扣住任何行。
    """
    batch_hashes = {flow.row_hash for flow in parsed_rows}
    unknown = sorted(confirmed_row_hashes - batch_hashes)
    if unknown:
        raise ValueError(f"确认列表包含本文件中不存在的流水: {unknown[0][:12]}…")
    previously_held = dict(resolution.suspected_sources)
    # 确认一条股息 = 连同它的同日预扣税一起转正（税行不需要逐条勾选）
    confirmed = set(confirmed_row_hashes)
    confirmed_dividend_keys = {
        _dividend_key(flow)
        for flow in parsed_rows
        if flow.is_cash_dividend and flow.row_hash in confirmed
    }
    for flow in parsed_rows:
        if (
            flow.is_withholding_tax
            and flow.row_hash in previously_held
            and _dividend_key(flow) in confirmed_dividend_keys
        ):
            confirmed.add(flow.row_hash)
    suspected = SuspectedDuplicateResolution(
        previously_held=previously_held, confirmed_hashes=frozenset(confirmed)
    )
    pool = [
        flow
        for flow in parsed_rows
        if flow.row_hash not in resolution.booked_hashes
        and flow.row_hash not in previously_held
        and flow.row_hash not in confirmed
    ]
    warnings: List[str] = []
    _match_suspected_trades(
        db,
        user_id,
        broker_account_id,
        [
            flow
            for flow in pool
            if flow.is_trade
            and flow.symbol
            and flow.market
            and flow.quantity
            and flow.price is not None
            and flow.price > 0
        ],
        batch_hashes,
        suspected,
        warnings,
    )
    _match_suspected_dividends(
        db,
        user_id,
        broker_account_id,
        [flow for flow in pool if flow.is_cash_dividend],
        [flow for flow in pool if flow.is_withholding_tax],
        parsed_rows,
        batch_hashes,
        suspected,
    )
    return suspected, warnings


def unconfirmed_previously_held(suspected: SuspectedDuplicateResolution) -> set[str]:
    """上次已归档为疑似、本次未确认的行：按重复计（#189 同款守卫）。"""
    return {
        row_hash
        for row_hash in suspected.previously_held
        if row_hash not in suspected.confirmed_hashes
    }


def suspected_note(match: Optional[SuspectedMatch]) -> str:
    if match is None:
        return "suspected duplicate; manual confirmation required"
    if match.record is None:
        return f"suspected duplicate ({match.reason}); manual confirmation required"
    record = match.record
    record_date = getattr(record, "transaction_date", None) or getattr(record, "ex_date", None)
    return (
        f"suspected duplicate of {match.kind} id={record.id} (date {record_date}; "
        f"{match.source_label}); manual confirmation required"
    )


def suspected_sample(
    flow: ParsedIbkrFlow,
    match: Optional[SuspectedMatch],
    *,
    previously_held: bool,
    held_source: Optional[IbkrActivityFlow] = None,
) -> Dict[str, Any]:
    record = match.record if match is not None else None
    source = match.source if match is not None else None
    existing_date = existing_amount = existing_price = None
    if match is None:
        reason = "上次导入已归档为疑似重复，仍待确认（本次按重复跳过）"
        notes = strip_text(held_source.notes if held_source is not None else None)
        marker = notes.find("suspected duplicate of ")
        if marker >= 0:
            reason += f"：{notes[marker:]}"
    else:
        reason = match.reason
        if record is None:
            pass  # 配对目标是上次归档的疑似行，没有账本记录可展示
        elif match.kind == "transaction":
            existing_date = record.transaction_date.isoformat()
            existing_price = _fmt_decimal(record.price)
        else:
            existing_date = dividend_cash_date(record).isoformat()
            if record.total_dividend is not None:
                existing_amount = _fmt_decimal(record.total_dividend)
    return {
        "row_number": flow.source_row_number,
        "symbol": flow.symbol or flow.raw_symbol,
        "name": flow.name,
        "market": flow.market or "",
        "transaction_type": flow.transaction_type
        or ("CASH_DIVIDEND" if flow.is_cash_dividend else "DIVIDEND_TAX"),
        "trade_date": flow.trade_date.isoformat(),
        "quantity": str(abs(flow.quantity)) if flow.quantity is not None else "0",
        # 发生金额是 CSV 的基础货币金额（港股成交价是 HKD、金额可能是 USD），
        # 币种不能取成交价币种（PR #253 评审 P2）；成交价币种另给
        "amount": str(flow.gross_amount if flow.gross_amount is not None else Decimal("0")),
        "price": str(flow.price) if flow.price is not None else "",
        "currency": flow.base_currency,
        "price_currency": flow.price_currency if flow.transaction_type else None,
        "existing_price": existing_price,
        "existing_source_filename": source.source_filename if source is not None else None,
        "existing_import_batch_id": (
            source.import_batch_id
            if source is not None
            else getattr(record, "import_batch_id", None)
        ),
        "existing_row_number": source.source_row_number if source is not None else None,
        "existing_row_hash": source.row_hash if source is not None else None,
        "match_kind": match.kind if match is not None else None,
        "existing_id": record.id if record is not None else None,
        "existing_date": existing_date,
        "existing_currency": getattr(record, "currency", None),
        "existing_amount": existing_amount,
        "existing_source": match.source_label if match is not None else None,
        "reason": reason,
        "row_hash": flow.row_hash,
        "previously_held": previously_held,
    }


def suspected_samples(
    parsed_rows: List[ParsedIbkrFlow], suspected: Optional[SuspectedDuplicateResolution]
) -> List[Dict[str, Any]]:
    if suspected is None:
        return []
    pending = unconfirmed_previously_held(suspected)
    return [
        suspected_sample(flow, suspected.matches.get(flow.row_hash), previously_held=False)
        for flow in parsed_rows
        if flow.row_hash in suspected.held_hashes
    ] + [
        suspected_sample(
            flow,
            None,
            previously_held=True,
            held_source=suspected.previously_held.get(flow.row_hash),
        )
        for flow in parsed_rows
        if flow.row_hash in pending
    ]


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
    suspected: Optional[SuspectedDuplicateResolution] = None,
) -> Dict[str, Any]:
    held_hashes = suspected.held_hashes if suspected is not None else set()
    rows = eligible_rows(parsed_rows)
    trade_rows = [flow for flow in rows if flow.is_trade]
    dividend_rows = [flow for flow in rows if flow.is_cash_dividend]
    tax_rows = [flow for flow in rows if flow.is_withholding_tax]
    # 可入账的现金/外汇行与交易/股息/税同属审计口径：它们会生成 CashEvent
    # 并计入 booked/duplicate，而不是被当成"未入账来源"拖垮批次状态
    bookable_cash_rows = [
        flow for flow in parsed_rows if flow.is_cash_business or flow.fx_legs is not None
    ]
    # "调整"（FX 折算损益等纸面项）是设计上有意只归档的行：预期跳过，
    # 不算数据问题；方向异常/货币对异常的行不在此列，仍按未解决行处理
    expected_archived_rows = [
        flow
        for flow in parsed_rows
        if flow.skip_reason == "cash" and flow.activity_type not in IBKR_CASH_EVENT_TYPES
    ]
    audited_rows = rows + bookable_cash_rows
    import_rows, duplicate_rows = split_new_and_duplicate_rows(audited_rows, existing_hashes)
    import_rows = [flow for flow in import_rows if flow.row_hash not in held_hashes]
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

    date_start, date_end = iso_date_range([flow.trade_date for flow in parsed_rows])
    eligible_cash_event_rows = len([flow for flow in parsed_rows if flow.is_cash_business])
    eligible_fx_rows = len([flow for flow in parsed_rows if flow.fx_legs is not None])
    result = base_import_result(
        broker=BROKER_NAME,
        filename=filename,
        total_rows=total_rows,
        eligible_trade_rows=len(trade_rows),
        eligible_dividend_rows=len(dividend_rows),
        eligible_tax_rows=len(tax_rows),
        imported_transactions=imported_transactions,
        imported_corporate_actions=imported_corporate_actions,
        imported_tax_adjustments=imported_tax_adjustments,
        imported_cash_events=imported_cash_events,
        duplicate_rows=len(duplicate_rows),
        skipped_non_trade_rows=max(
            0,
            total_rows
            - len(trade_rows)
            - len(dividend_rows)
            - len(tax_rows)
            - len(bookable_cash_rows)
            - len(expected_archived_rows)
            - skip_counts["excluded"],
        ),
        expected_archived_rows=len(expected_archived_rows),
        skipped_excluded_rows=skip_counts["excluded"],
        excluded_unbooked_rows=len(
            [
                flow
                for flow in parsed_rows
                if flow.skip_reason == "excluded" and flow.row_hash not in existing_hashes
            ]
        ),
        skipped_invalid_rows=skip_counts["invalid"] + len(errors),
        skipped_option_rows=skip_counts["option"],
        # 跳过计数只含真正不入账的行；可入账现金/外汇行分列在 eligible_* 里，
        # 否则预览会把将要入账的存款/利息显示成"现金类跳过"误导用户
        skipped_fx_rows=skip_counts["fx"] - eligible_fx_rows,
        skipped_cash_rows=skip_counts["cash"] - eligible_cash_event_rows,
        skipped_unsupported_rows=skip_counts["unsupported"],
        affected_symbols=affected_symbols,
        date_start=date_start,
        date_end=date_end,
        business_counts=business_counts,
        duplicate_samples=[
            flow_to_sample(flow, True) for flow in duplicate_rows[:RESULT_SAMPLE_LIMIT]
        ],
        import_samples=[flow_to_sample(flow, False) for flow in import_rows[:RESULT_SAMPLE_LIMIT]],
        errors=errors,
        warnings=warnings,
        suspected_duplicate_rows=len(held_hashes),
        suspected_duplicate_samples=suspected_samples(parsed_rows, suspected),
    )
    result.update(
        {
            "canonical_objects_changed": canonical_objects_changed,
            "booked_source_rows": booked_source_rows,
            "unbooked_source_rows": unbooked_source_rows,
            "eligible_unbooked_source_rows": eligible_unbooked_source_rows,
            "eligible_cash_event_rows": eligible_cash_event_rows,
            "eligible_fx_rows": eligible_fx_rows,
            "source_account_masks": source_accounts or [],
        }
    )
    return result


def preview_booked_source_hashes(
    db: Session,
    user_id: int,
    parsed_rows: List[ParsedIbkrFlow],
    *,
    broker_account_id: int,
    resolution: ExistingSourceResolution,
    errors: List[str],
    suspected: Optional[SuspectedDuplicateResolution] = None,
) -> set[str]:
    """Dry-run source-to-canonical coverage without mutating the database.

    疑似重复（本次扣住的、以及上次已归档仍未确认的）不会入账，不进覆盖口径；
    后者由调用方按重复计。
    """
    booked_hashes = set(resolution.booked_hashes)
    not_booking: set[str] = set()
    if suspected is not None:
        not_booking = suspected.held_hashes | unconfirmed_previously_held(suspected)
    prospective_dividends = [
        flow
        for flow in parsed_rows
        if flow.is_cash_dividend
        and flow.row_hash not in resolution.booked_hashes
        and flow.row_hash not in not_booking
    ]
    for flow in parsed_rows:
        if (
            (
                flow.is_trade
                or flow.is_cash_dividend
                or flow.is_cash_business
                or flow.fx_legs is not None
            )
            and flow.row_hash not in booked_hashes
            and flow.row_hash not in not_booking
        ):
            booked_hashes.add(flow.row_hash)

    pending_taxes = [
        flow
        for flow in parsed_rows
        if flow.is_withholding_tax
        and flow.row_hash not in booked_hashes
        and flow.row_hash not in not_booking
    ]
    tax_index = TaxCandidateIndex(db, user_id, pending_taxes, broker_account_id)
    for flow in pending_taxes:
        real_candidates, virtual_candidates = narrow_tax_candidates(
            db,
            flow,
            find_dividend_candidates_for_tax(
                db,
                user_id,
                flow,
                broker_account_id=broker_account_id,
                index=tax_index,
            ),
            [
                dividend
                for dividend in prospective_dividends
                if dividend.symbol == flow.symbol
                and dividend.market == flow.market
                and dividend.base_currency == flow.base_currency
                and dividend.trade_date == flow.trade_date
            ],
            index=tax_index,
        )
        candidate_count = len({action.id for action in real_candidates}) + len(virtual_candidates)
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
        flow.row_hash for flow in parsed_rows if flow.skip_reason in ARCHIVE_ONLY_SKIP_REASONS
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


# 只归档不入账的行（保留原因）。unsupported（含「公司行动」行：拆股、分拆、换股）与 invalid
# 此前既不归档也不报错，原始流水从表里丢失、事后无法追溯，也没有重导判重锚点（#279 第 2 条）
ARCHIVE_ONLY_SKIP_REASONS = ("option", "cash", "fx", "excluded", "unsupported", "invalid")
UNBOOKABLE_ARCHIVE_REASONS = ("option", "excluded", "unsupported", "invalid")


def unsupported_action_warnings(parsed_rows: List[ParsedIbkrFlow]) -> List[str]:
    """「公司行动」行不自动入账：列出来提示手工补录（预览与导入同一口径）。"""
    rows = [flow for flow in parsed_rows if flow.activity_type == "公司行动"]
    if not rows:
        return []
    samples = "；".join(
        f"行 {flow.source_row_number} {flow.trade_date} {flow.raw_symbol} {flow.description or ''}".strip()
        for flow in rows[:5]
    )
    more = f" 等 {len(rows)} 条" if len(rows) > 5 else ""
    return [
        f"IBKR 公司行动行未自动入账（已归档留痕）：{samples}{more}。"
        "拆股、分拆、换股等请在公司行动里手工补录，否则持仓可能不准"
    ]


def cash_flow_anomaly_warnings(parsed_rows: List[ParsedIbkrFlow]) -> List[str]:
    warnings: List[str] = []
    for flow in parsed_rows:
        warning = cash_flow_anomaly_warning(flow)
        if warning is not None:
            warnings.append(warning)
    return warnings


def mark_previously_held_duplicates(
    suspected: SuspectedDuplicateResolution,
    *,
    duplicate_hashes: set[str],
    booked_source_hashes: set[str],
) -> None:
    """上次已归档为疑似、本次未确认的行按「已处理的重复」计（#189 同款）。"""
    pending = unconfirmed_previously_held(suspected)
    duplicate_hashes.update(pending)
    booked_source_hashes.update(pending)


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
            flow.is_cash_business or flow.fx_legs is not None
        ) and flow.row_hash in existing_archived_hashes:
            duplicate_hashes.add(flow.row_hash)
            booked_source_hashes.add(flow.row_hash)


def prepare_ibkr_dividend_receipts(db, user_id, account_id, rows, excluded, *, lock=False):
    from .dividend_receipt_service import DividendReceipt, prepare_dividend_receipts

    return prepare_dividend_receipts(
        db,
        user_id,
        account_id,
        [
            DividendReceipt(
                flow.row_hash,
                flow.symbol,
                flow.market,
                flow.trade_date,
                flow.base_currency,
                flow.gross_amount,
                gross=flow.gross_amount,
                tax=Decimal("0"),
            )
            for flow in rows
            if flow.is_cash_dividend
        ],
        existing_hashes=excluded,
        lock=lock,
    )


def preview_ibkr_activity(
    db: Session,
    user_id: int,
    contents: bytes,
    filename: str,
    broker_account_id: Optional[int] = None,
    confirmed_row_hashes: frozenset[str] = frozenset(),
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
        excluded_symbols=frozenset(get_excluded_symbols(db, user_id)),
    )
    # 名称补齐（外呼）留在编排层，parse 保持纯函数
    enrich_security_names(parsed_rows, name_overrides=get_name_overrides(db, user_id))
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
    warnings_extra.extend(unsupported_action_warnings(parsed_rows))
    resolution = resolve_existing_sources(
        db,
        user_id,
        eligible_rows(parsed_rows),
        broker_account_id=broker_account_id,
        broker_account=broker_account,
    )
    # 疑似重复：与正式导入同一个解析器，结论只算一次
    suspected, suspected_warnings = resolve_suspected_duplicates(
        db,
        user_id,
        broker_account_id,
        parsed_rows,
        resolution=resolution,
        confirmed_row_hashes=confirmed_row_hashes,
    )
    warnings_extra.extend(suspected_warnings)
    try:
        dividend_plan = prepare_ibkr_dividend_receipts(
            db,
            user_id,
            broker_account_id,
            parsed_rows,
            set(resolution.booked_hashes)
            | unconfirmed_previously_held(suspected)
            | suspected.held_hashes,
        )
        warnings_extra.extend(dividend_plan.warnings())
    except ValueError as exc:
        errors.append(str(exc))
    booked_source_hashes = preview_booked_source_hashes(
        db,
        user_id,
        parsed_rows,
        broker_account_id=broker_account_id,
        resolution=resolution,
        errors=errors,
        suspected=suspected,
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
    mark_previously_held_duplicates(
        suspected,
        duplicate_hashes=duplicate_hashes,
        booked_source_hashes=booked_source_hashes,
    )
    # 整批一票否决的账户持仓预检在预览里也跑一遍（#279，与招商/东财同一契约）：
    # 本批会入账的成交与会合成的转板对用替身补进去，预览仍是只读的
    not_booking = (
        set(resolution.booked_hashes)
        | unconfirmed_previously_held(suspected)
        | suspected.held_hashes
    )
    prospective = prospective_trade_transactions(
        parsed_rows, not_booking, broker_account_id=broker_account_id
    )
    relisting_warnings: List[str] = []
    prospective += [
        ProspectiveTransaction(**{k: v for k, v in fields.items() if k != "notes"})
        for fields in plan_relisting_transfers(
            db,
            user_id,
            parsed_rows,
            broker_account_id=broker_account_id,
            relistings=get_relistings(db, user_id),
            extra_transactions=prospective,
            warnings=relisting_warnings,
        )
    ]
    warnings_extra.extend(relisting_warnings)
    try:
        validate_account_positions_before_commit(
            db,
            user_id=user_id,
            broker_account_id=broker_account_id,
            extra_transactions=prospective,
            # 本份对账单里的全部成交标的（含重复、疑似重复扣住的行）：重导重叠对账单时
            # 标的行全按重复处理，只看入账行会把它误判成「不在本份对账单里」（#312 复审）
            batch_keys=statement_trade_keys(parsed_rows)
            | {(txn.symbol, txn.market) for txn in prospective},
            context_notes=relisting_warnings,
        )
    except ValueError as exc:
        errors.append(str(exc))
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
        suspected=suspected,
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
            notes=import_note(BROKER_NAME, label, flow.description or flow.activity_type),
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


# 已删除本地的 find_dividend_for_tax：无任何调用方（入账与预览都直接用
# find_dividend_candidates_for_tax），且与 broker_import_common 的同名函数
# 匹配窗口不同（本地版是 ex_date/payment_date 严格同日，共享版是
# ex_date <= trade_date），同名异义容易被误当成同一份逻辑。


class TaxCandidateIndex:
    """本批税行的候选股息与其 IBKR 股息来源，一次预取（#279：此前每条税行各查 2–3 次，
    重导全年 CSV 时是数百到上千次往返）。

    必须在本批股息已 flush 之后构造：导入时税行要能归到同一批刚入账的股息。
    """

    def __init__(
        self,
        db: Session,
        user_id: int,
        tax_flows: Iterable[ParsedIbkrFlow],
        broker_account_id: Optional[int],
    ):
        keys = {(flow.symbol, flow.market) for flow in tax_flows if flow.symbol}
        self._actions: List[CorporateAction] = []
        self._sources: Dict[int, List[IbkrActivityFlow]] = {}
        if not keys:
            return
        self._actions = [
            action
            for action in db.query(CorporateAction)
            .filter(
                CorporateAction.user_id == user_id,
                CorporateAction.action_type == "CASH_DIVIDEND",
                CorporateAction.receipt_status == "RECEIVED",
                CorporateAction.broker_account_id == broker_account_id,
                CorporateAction.symbol.in_({symbol for symbol, _ in keys}),
            )
            .order_by(CorporateAction.id)
            if (action.symbol, action.market) in keys
        ]
        if self._actions:
            for source in (
                db.query(IbkrActivityFlow)
                .filter(
                    IbkrActivityFlow.corporate_action_id.in_([a.id for a in self._actions]),
                    IbkrActivityFlow.activity_type == DIVIDEND_TYPE,
                )
                .order_by(IbkrActivityFlow.id)
            ):
                self._sources.setdefault(source.corporate_action_id, []).append(source)

    def candidates(self, flow: ParsedIbkrFlow) -> List[CorporateAction]:
        return [
            action
            for action in self._actions
            if action.symbol == flow.symbol
            and action.market == flow.market
            and action.currency == flow.base_currency
            and flow.trade_date in (action.ex_date, action.payment_date)
        ]

    def dividend_sources(self, action_id: int) -> List[IbkrActivityFlow]:
        return self._sources.get(action_id, [])


def find_dividend_candidates_for_tax(
    db: Session,
    user_id: int,
    flow: ParsedIbkrFlow,
    broker_account_id: Optional[int] = None,
    *,
    index: Optional[TaxCandidateIndex] = None,
) -> List[CorporateAction]:
    """Return only same-account, same-security, same-payment-date candidates."""
    if index is not None:
        return index.candidates(flow)
    return (
        db.query(CorporateAction)
        .filter(
            CorporateAction.user_id == user_id,
            CorporateAction.symbol == flow.symbol,
            CorporateAction.market == flow.market,
            CorporateAction.action_type == "CASH_DIVIDEND",
            CorporateAction.receipt_status == "RECEIVED",
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


def narrow_tax_candidates(
    db: Session,
    flow: ParsedIbkrFlow,
    candidates: List[CorporateAction],
    virtual_dividends: Sequence[ParsedIbkrFlow] = (),
    *,
    index: Optional[TaxCandidateIndex] = None,
) -> tuple[List[CorporateAction], List[ParsedIbkrFlow]]:
    """按描述里的「币种 每股金额」收窄税行的同日候选股息。

    IBKR 的税行与股息行描述同源（`… HKD 0.75 每股 (常规股息)` / `… HKD 0.75 每股 - CN 税收`，
    生产全部历史逐条一致）。已入账候选看它链接的 IBKR 股息来源行，预览里的待入账股息看本文件
    原行：
    - 恰好一笔描述相同 → 只留它（同日两笔股息各带税时此前必然整条报错）；
    - 否则剔除**描述明确不同**的候选（公告建议入账等没有 IBKR 来源的候选描述未知，保留）——
      只剩一笔入账候选、但描述属于另一笔股息时不能归给它（PR #253 复审：人工确认一条税行时，
      它的股息仍被扣留，唯一的入账候选是别的股息）。
    税行本身没有每股描述时原样返回。
    """
    virtual = list(virtual_dividends)
    descriptor = _per_share_descriptor(flow.description)
    if descriptor is None or (not candidates and not virtual):
        return candidates, virtual
    linked: Dict[int, set] = {}
    if candidates:
        sources = (
            [source for c in candidates for source in index.dividend_sources(c.id)]
            if index is not None
            else db.query(IbkrActivityFlow).filter(
                IbkrActivityFlow.corporate_action_id.in_([c.id for c in candidates]),
                IbkrActivityFlow.activity_type == DIVIDEND_TYPE,
            )
        )
        for source in sources:
            linked.setdefault(source.corporate_action_id, set()).add(
                _per_share_descriptor(source.description)
            )
    exact = [c for c in candidates if descriptor in linked.get(c.id, set())]
    exact_virtual = [d for d in virtual if _per_share_descriptor(d.description) == descriptor]
    if len(exact) + len(exact_virtual) == 1:
        return exact, exact_virtual

    def compatible(descriptors: set) -> bool:
        known = {d for d in descriptors if d is not None}
        return not known or descriptor in known

    return (
        [c for c in candidates if compatible(linked.get(c.id, set()))],
        [d for d in virtual if compatible({_per_share_descriptor(d.description)})],
    )


def calculate_position_before(
    db: Session,
    user_id: int,
    symbol: str,
    market: str,
    before_date: date,
    broker_account_id: Optional[int] = None,
    warnings: Optional[List[str]] = None,
    extra_transactions: Sequence[Any] = (),
) -> tuple[Decimal, Decimal]:
    """转板前某账户桶的 (数量, 均价)：与 recalculate_holdings 同一重放（#270）。

    此前只数 BUY/SELL、按日期顺序手算，忽略拆股/送股/配股/期初建仓与转仓，超卖还被
    静默截成 0——转板前有公司行动时，合成的卖出/买入数量就是错的。现在对 before_date
    之前的全部交易与公司行动做按账户重放（holding_service 的同一套 semantics），取目标桶。

    归属矛盾（按账户重放不成立）时返回 0（调用方不合成转板），并把原因写进 warnings：
    合并桶是所有账户加未指定账户的合计，拿它当本账户的转板数量会凭空多卖别的桶的股数
    （PR #296 评审）。
    """
    transactions = (
        db.query(Transaction)
        .filter(
            Transaction.user_id == user_id,
            Transaction.symbol == symbol,
            Transaction.market == market,
            Transaction.transaction_date < before_date,
        )
        .all()
    )
    # 预览通道：本批还没落库的同标的成交（替身）也要算进转板前持仓
    transactions += [
        txn
        for txn in extra_transactions
        if txn.symbol == symbol and txn.market == market and txn.transaction_date < before_date
    ]
    corporate_actions = (
        db.query(CorporateAction)
        .filter(
            CorporateAction.user_id == user_id,
            CorporateAction.symbol == symbol,
            CorporateAction.market == market,
            CorporateAction.ex_date < before_date,
        )
        .all()
    )
    try:
        buckets = replay_transactions_per_account(transactions, corporate_actions, symbol, market)
        state = buckets.get(broker_account_id)
    except AccountReplayError as exc:
        message = (
            f"{symbol}（{market}）转板前持仓按账户重放不成立，未自动合成转板交易，"
            f"请核对该标的各账户的交易与公司行动后手工补录：{exc}"
        )
        logger.warning(message)
        if warnings is not None:
            warnings.append(message)
        return Decimal("0"), Decimal("0")
    if state is None or state["quantity"] <= 0:
        return Decimal("0"), Decimal("0")
    return state["quantity"], state["avg_cost"]


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


def plan_relisting_transfers(
    db: Session,
    user_id: int,
    parsed_rows: List[ParsedIbkrFlow],
    *,
    broker_account_id: Optional[int] = None,
    relistings: Optional[List[Dict[str, Any]]] = None,
    extra_transactions: Sequence[Any] = (),
    warnings: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """本批会合成的转板卖出/买入对（字段字典）；导入落库与预览替身共用（#279）。

    转板映射由调用方注入（security_rules RELISTING 类型），不再读模块常量。
    extra_transactions：预览通道里本批还没落库的成交，参与转板前持仓的推算。
    warnings：转板前持仓按账户重放不成立时的告警（不合成该转板，PR #296 评审）。
    """
    planned: List[Dict[str, Any]] = []
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
            warnings=warnings,
            extra_transactions=extra_transactions,
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
        common = dict(
            broker_account_id=broker_account_id,
            name=name,
            quantity=quantity,
            fee=Decimal("0"),
            transaction_date=transfer_date,
            notes=note,
        )
        planned.append(
            {
                **common,
                "symbol": old_symbol,
                "market": old_market,
                "transaction_type": "SELL",
                "price": old_avg_cost,
                "currency": relisting["old_currency"],
            }
        )
        planned.append(
            {
                **common,
                "symbol": new_symbol,
                "market": new_market,
                "transaction_type": "BUY",
                "price": new_avg_cost,
                "currency": relisting["new_currency"],
            }
        )
    return planned


def apply_known_relisting_transfers(
    db: Session,
    user_id: int,
    parsed_rows: List[ParsedIbkrFlow],
    affected_symbols: set[tuple[str, str]],
    *,
    broker_account_id: Optional[int] = None,
    import_batch_id: Optional[int] = None,
    relistings: Optional[List[Dict[str, Any]]] = None,
    warnings: Optional[List[str]] = None,
) -> int:
    planned = plan_relisting_transfers(
        db,
        user_id,
        parsed_rows,
        broker_account_id=broker_account_id,
        relistings=relistings,
        warnings=warnings,
    )
    for fields in planned:
        db.add(Transaction(user_id=user_id, import_batch_id=import_batch_id, **fields))
        affected_symbols.add((fields["symbol"], fields["market"]))
    return len(planned)


def prospective_trade_transactions(
    parsed_rows: List[ParsedIbkrFlow],
    not_booking: set[str],
    *,
    broker_account_id: int,
) -> List[ProspectiveTransaction]:
    """预览通道：本批会入账的成交替身（与导入循环的入账条件一致）。"""
    return [
        ProspectiveTransaction(
            symbol=flow.symbol,
            market=flow.market,
            transaction_type=flow.transaction_type,
            quantity=abs(flow.quantity),
            transaction_date=flow.trade_date,
            price=flow.price,
            fee=flow.fee_in_price_currency or Decimal("0"),
            currency=flow.price_currency or flow.base_currency,
            name=flow.name,
            broker_account_id=broker_account_id,
            source_row_number=flow.source_row_number,
        )
        for flow in parsed_rows
        if flow.is_trade
        and flow.symbol
        and flow.market
        and flow.quantity is not None
        and flow.price is not None
        and flow.row_hash not in not_booking
    ]


def statement_trade_keys(parsed_rows: List[ParsedIbkrFlow]) -> set:
    """本份对账单里出现的全部成交标的（不论本批是否入账）。"""
    return {
        (flow.symbol, flow.market)
        for flow in parsed_rows
        if flow.transaction_type and flow.symbol and flow.market
    }


def validate_account_positions_before_commit(
    db: Session,
    *,
    user_id: int,
    broker_account_id: int,
    extra_transactions: Sequence[Any] = (),
    batch_keys: Optional[set] = None,
    context_notes: Sequence[str] = (),
) -> None:
    """IBKR 账户持仓预检（#279）：与招商、东财同一装配与口径，单账户严格。

    此前 IBKR 没有账户预检：预览显示可以导入，正式导入却在合并桶重放超卖时整批
    回滚（错误不是账户视角），单账户内的超卖还会先静默降级到合并桶。现在预览与
    导入都按本账户桶重放全部交易与数量类行动，超卖即拒并指出标的与日期。
    没有额外的同日 tie-break：既有交易按 id、替身按对账单行序（排序稳定）排在其后，
    与导入 flush 后拿到的 id 次序一致。

    预检重放本账户的**全部**标的（与招商、东财同口径），本批不涉及的标的超卖同样拦住整批。
    batch_keys（本批涉及的标的）只用于给出对症的提示：不在本批的，问题出在库里已有的记录
    （例如期初买入手工记在「未指定账户」），导入更早的对账单解决不了（PR #310 评审）。
    context_notes：转板合成被跳过的原因等，拼进报错——否则新代码的卖出被拒时，报错只指向
    「缺买入」，真正的原因（旧代码的账户归属矛盾）看不到（PR #296 评审）。
    """

    def _reject(event, available: Decimal, needed: Decimal) -> None:
        verb = "转出" if event.transaction_type == "TRANSFER_OUT" else "卖出"
        in_batch = batch_keys is None or (event.symbol, event.market) in batch_keys
        advice = (
            "缺少期初持仓（对账单区间之前的买入）或证券转入记录，整批未导入。"
            "请先导入更早的对账单，或在公司行动里补录期初建仓"
            if in_batch
            else "该标的不在本份对账单里，是本账户库内已有记录对不上，整批未导入。"
            "请核对它在本账户的手工录入、转仓与公司行动（例如期初买入记在了「未指定账户」），"
            "修正后再导入"
        )
        message = (
            "IBKR 账户持仓预检失败："
            f"{event.symbol} {event.market} 在 {event.transaction_date} "
            f"{verb} {format(needed, 'f')}，"
            f"但账户内可用数量仅 {format(available, 'f')}；{advice}"
        )
        if context_notes:
            message += "。另：" + "；".join(context_notes)
        raise ValueError(message)

    replay_account_quantities(
        account_precheck_events(
            db,
            user_id=user_id,
            broker_account_id=broker_account_id,
            extra_transactions=extra_transactions,
        ),
        on_oversell=_reject,
    )


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
    action.notes = append_note(action.notes, f"{BROKER_NAME} 预扣税")
    if existing_source is not None:
        db.add(attribute_tax_source(existing_source, action.id))
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
    confirmed_row_hashes: frozenset[str] = frozenset(),
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
            excluded_symbols=frozenset(get_excluded_symbols(db, user_id)),
        )
        # 名称补齐（外呼）留在编排层，parse 保持纯函数
        enrich_security_names(parsed_rows, name_overrides=get_name_overrides(db, user_id))
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
        # 串行化同一用户的导入：判重/疑似重复读到的已入账行在提交前不会被并发导入改写
        lock_broker_import(db, user_id)
        resolution = resolve_existing_sources(
            db,
            user_id,
            eligible_rows(parsed_rows),
            broker_account_id=broker_account_id,
            broker_account=broker_account,
        )
        duplicate_hashes = set(resolution.duplicate_hashes)
        booked_source_hashes.update(resolution.booked_hashes)

        # 疑似重复：与预览同一个解析器，结论只算一次
        suspected, suspected_warnings = resolve_suspected_duplicates(
            db,
            user_id,
            broker_account_id,
            parsed_rows,
            resolution=resolution,
            confirmed_row_hashes=confirmed_row_hashes,
        )
        warnings_extra.extend(suspected_warnings)
        dividend_plan = prepare_ibkr_dividend_receipts(
            db,
            user_id,
            broker_account_id,
            parsed_rows,
            set(resolution.booked_hashes)
            | unconfirmed_previously_held(suspected)
            | suspected.held_hashes,
            lock=True,
        )
        warnings_extra.extend(dividend_plan.warnings())
        # 上次已归档为疑似、本次未确认：按重复计，原归档行继续保留待确认。
        # 确认过的在原归档行上转正（suspected_sources.pop），绝不插第二条同 hash 行
        pending_suspected = unconfirmed_previously_held(suspected)
        mark_previously_held_duplicates(
            suspected,
            duplicate_hashes=duplicate_hashes,
            booked_source_hashes=booked_source_hashes,
        )
        suspected_sources = {
            row_hash: source
            for row_hash, source in suspected.previously_held.items()
            if row_hash not in pending_suspected
        }

        affected_symbols: set[tuple[str, str]] = set()
        pending_tax_flows: List[ParsedIbkrFlow] = [
            flow
            for flow in parsed_rows
            if flow.is_withholding_tax
            and flow.row_hash not in resolution.booked_hashes
            and flow.row_hash not in pending_suspected
            and flow.row_hash not in suspected.held_hashes
        ]

        # 期权行不在 eligible_rows 里，resolution 不覆盖其哈希；
        # 重复上传去重需单独查（否则再归档会撞 row_hash 唯一约束）。
        # 与 resolve_existing_sources 同口径：仅同账户可判重——归属其他账户
        # 的既有来源说明此前选错了账户，必须阻塞并提示，不能静默视为重复
        # （(user_id, row_hash) 唯一约束也使正确账户无法再补录该审计来源）。
        # 现金/外汇/期权行的归档判重与预览共用同一通道；异常行报警也在
        # 批级统一产生（重导入时行已归档、不再逐行入账，逐行报警会漏）
        warnings_extra.extend(cash_flow_anomaly_warnings(parsed_rows))
        warnings_extra.extend(unsupported_action_warnings(parsed_rows))
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
                    flow.skip_reason in UNBOOKABLE_ARCHIVE_REASONS
                    and flow.row_hash not in existing_archived_hashes
                    and flow.row_hash not in booked_source_hashes
                ):
                    # create_ibkr_activity_flow 已带上 flow.skip_reason
                    # （option/excluded/unsupported/invalid），不得覆写——审计记录要保留真实原因
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
            if flow.row_hash in pending_suspected:
                continue
            if flow.row_hash in suspected.held_hashes:
                # 疑似重复：归档留痕、不入账。人工确认后带 confirm 清单重导，
                # 在这条归档行上原地转正
                note = suspected_note(suspected.matches.get(flow.row_hash))
                existing = resolution.unresolved_tax_sources.get(flow.row_hash)
                if existing is not None:
                    # 此前已归档为「未归属税行」：原地改标记，不插第二条同 hash 行
                    # （唯一约束，整批回滚——PR #253 评审 P2）
                    mark_suspected_duplicate(existing, note)
                    continue
                archived = create_ibkr_activity_flow(
                    user_id=user_id,
                    filename=filename,
                    flow=flow,
                    broker_account_id=broker_account_id,
                    import_batch_id=batch_id,
                )
                db.add(mark_suspected_duplicate(archived, note))
                continue
            if flow.is_withholding_tax:
                continue

            if flow.is_cash_dividend:
                action = dividend_plan.apply(
                    flow.row_hash,
                    batch_id=batch_id,
                    source_note=import_note(BROKER_NAME, flow.description),
                )
                if action is None:
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
                        amount_basis="GROSS_NET",
                        receipt_status="RECEIVED",
                        net_dividend=flow.gross_amount,
                        currency=flow.base_currency,
                        notes=import_note(BROKER_NAME, flow.description),
                    )
                    db.add(action)
                db.flush()
                # 人工确认的疑似股息：在原归档行上转正，不插第二条同 hash 行
                archive_and_link(
                    db,
                    suspected_sources,
                    flow.row_hash,
                    revive=lambda source, action_id=action.id: attribute_source(
                        source,
                        corporate_action_id=action_id,
                        note="confirmed as a distinct dividend during re-import",
                    ),
                    create=lambda action_id=action.id: create_ibkr_activity_flow(
                        user_id=user_id,
                        filename=filename,
                        flow=flow,
                        broker_account_id=broker_account_id,
                        import_batch_id=batch_id,
                        corporate_action_id=action_id,
                    ),
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
                notes=import_note(BROKER_NAME, flow.activity_type),
            )
            db.add(transaction)
            db.flush()
            # 人工确认的疑似成交：在原归档行上转正，不插第二条同 hash 行
            archive_and_link(
                db,
                suspected_sources,
                flow.row_hash,
                revive=lambda source, transaction_id=transaction.id: book_suspected_source(
                    source, transaction_id
                ),
                create=lambda transaction_id=transaction.id: create_ibkr_activity_flow(
                    user_id=user_id,
                    filename=filename,
                    flow=flow,
                    broker_account_id=broker_account_id,
                    import_batch_id=batch_id,
                    transaction_id=transaction_id,
                ),
            )
            booked_source_hashes.add(flow.row_hash)
            affected_symbols.add((flow.symbol, flow.market))
            imported_transactions += 1

        db.flush()
        tax_index = TaxCandidateIndex(db, user_id, pending_tax_flows, broker_account_id)
        for flow in pending_tax_flows:
            candidates, _ = narrow_tax_candidates(
                db,
                flow,
                find_dividend_candidates_for_tax(
                    db,
                    user_id,
                    flow,
                    broker_account_id=broker_account_id,
                    index=tax_index,
                ),
                index=tax_index,
            )
            preserved_tax = suspected_sources.pop(flow.row_hash, None)
            if len(candidates) != 1 or dividend_cash_date(candidates[0]) != flow.trade_date:
                event = create_dividend_tax_event(
                    db,
                    user_id=user_id,
                    broker_account_id=broker_account_id,
                    flow=SimpleNamespace(
                        amount=flow.gross_amount,
                        currency=flow.base_currency,
                        trade_date=flow.trade_date,
                        business_name=flow.activity_type,
                        security_code=flow.symbol,
                    ),
                    broker_name=BROKER_NAME,
                )
                source = preserved_tax or resolution.unresolved_tax_sources.get(flow.row_hash)
                if source is None:
                    source = create_ibkr_activity_flow(
                        user_id=user_id,
                        filename=filename,
                        flow=flow,
                        broker_account_id=broker_account_id,
                        import_batch_id=batch_id,
                    )
                db.add(attribute_tax_cash_source(source, event.id))
                imported_tax_adjustments += 1
                booked_source_hashes.add(flow.row_hash)
                continue
            imported_tax_adjustments += apply_withholding_tax(
                db,
                user_id,
                filename,
                flow,
                candidates[0],
                import_batch_id=batch_id,
                existing_source=(
                    preserved_tax or resolution.unresolved_tax_sources.get(flow.row_hash)
                ),
            )
            booked_source_hashes.add(flow.row_hash)
            canonical_action_ids_changed.add(candidates[0].id)

        db.flush()
        relisting_warnings: List[str] = []
        imported_transfer_transactions = apply_known_relisting_transfers(
            db,
            user_id,
            parsed_rows,
            affected_symbols,
            broker_account_id=broker_account_id,
            import_batch_id=batch_id,
            relistings=get_relistings(db, user_id),
            warnings=relisting_warnings,
        )
        warnings_extra.extend(relisting_warnings)
        imported_transactions += imported_transfer_transactions
        canonical_objects_changed = imported_transactions + len(canonical_action_ids_changed)
        # SessionLocal 是 autoflush=False：转板补建的交易只挂在 session 里，
        # 不显式 flush 的话下面的重算查不到它们（此前靠 db.commit() 顺带落库，
        # 重算移进事务后这条依赖就断了——转板标的会被误判成超卖）。
        db.flush()

        # 持仓重算必须在**同一事务内**完成再 commit（与东财同口径）。
        # 此前是先 commit 再逐标的重算、失败只 errors.append：交易已落库而
        # holdings 停在导入前的值，批次仅标 PARTIAL；进程若在 commit 与重算
        # 之间崩溃，连 PARTIAL 都没有，持仓静默过期。
        # 重算失败意味着账本本身不自洽（合并桶重放仍超卖 = 真的缺交易记录），
        # 应整批拒绝而不是留下半套数据。
        # 账户持仓预检（#279）：单账户严格，先于合并口径的持仓重算；失败整批回滚
        validate_account_positions_before_commit(
            db,
            user_id=user_id,
            broker_account_id=broker_account_id,
            batch_keys=statement_trade_keys(parsed_rows) | set(affected_symbols),
            context_notes=relisting_warnings,
        )
        recalculated_symbols = 0
        for symbol, market in sorted(affected_symbols):  # 时间线锁按键排序取，防死锁
            recalculate_holdings(db, user_id, symbol, market, commit=False)
            recalculated_symbols += 1

        try:
            db.commit()
        except IntegrityError as exc:
            raise ValueError("Duplicate IBKR activity flow detected during import") from exc
        records_committed = True

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
            suspected=suspected,
        )
        imported_source_rows = (
            db.query(IbkrActivityFlow).filter(IbkrActivityFlow.import_batch_id == batch_id).count()
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
        fail_broker_import(
            db,
            batch_id,
            exc,
            model=IbkrActivityFlow,
            records_committed=records_committed,
            row_count=total_rows,
            duplicate_count=len(duplicate_hashes),
            # IBKR 的入账来源行按 hash 集合记（含现金/外汇行），与完成路径同口径
            imported_count=max(0, len(booked_source_hashes) - len(duplicate_hashes)),
        )
        raise
