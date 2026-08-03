"""三家券商导入器共享的解析与去重底座。

HASH-CRITICAL：`normalize_hash_value` / `calculate_row_hash` 的任何行为改动都会让
历史已导入流水的 row_hash 漂移，破坏跨批次去重。改动前先看
`tests/test_import_hash_stability.py` 里重构前捕获的黄金摘要。

各导入器保留自己的 HASH_FIELDS 与文本清洗函数（招商用 `strip_bom`），通过
参数传入；语义因券商而异的逻辑（账户掩码校验、build_import_result、
get_existing_hashes、IBKR 的股息-税匹配）不在此层。
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from ..models.corporate_action import CorporateAction

HASH_DUPLICATE_OCCURRENCE_FIELD = "duplicate_occurrence"
STRICT_DECIMAL_PATTERN = re.compile(r"^[+-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)$")
SOURCE_ROW_ERROR_PATTERN = re.compile(r"^row (\d+):")


def strip_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\ufeff", "").strip()


def normalize_hash_value(value: Any, *, strip: Callable[[Any], str] = strip_text) -> str:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, date):
        return value.isoformat()
    return strip(value)


def calculate_row_hash(
    values: Dict[str, Any],
    fields: List[str],
    *,
    strip: Callable[[Any], str] = strip_text,
) -> str:
    if values.get(HASH_DUPLICATE_OCCURRENCE_FIELD):
        fields = fields + [HASH_DUPLICATE_OCCURRENCE_FIELD]
    payload = "|".join(normalize_hash_value(values.get(field, ""), strip=strip) for field in fields)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_strict_decimal(
    value: Any, *, strip: Callable[[Any], str] = strip_text
) -> Optional[Decimal]:
    text = strip(value)
    if not text or not STRICT_DECIMAL_PATTERN.fullmatch(text):
        return None
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def source_error_rows(errors: List[str], parsed_source_rows: set[int]) -> set[int]:
    """解析报错但没有产出任何 ParsedFlow 的源行号（skipped 口径对账用）。"""
    return {
        int(match.group(1))
        for error in errors
        if (match := SOURCE_ROW_ERROR_PATTERN.match(error))
        and int(match.group(1)) not in parsed_source_rows
    }


def find_dividend_for_tax(
    db: Session,
    user_id: int,
    flow: Any,
    market: str,
    broker_account_id: Optional[int] = None,
) -> Optional[CorporateAction]:
    """唯一匹配才归属：同标的/币种、除权日不晚于税项日的现金分红恰好一条时返回。"""
    query = db.query(CorporateAction).filter(
        CorporateAction.user_id == user_id,
        CorporateAction.symbol == flow.security_code,
        CorporateAction.market == market,
        CorporateAction.action_type == "CASH_DIVIDEND",
        CorporateAction.currency == flow.currency,
        CorporateAction.ex_date <= flow.trade_date,
        CorporateAction.broker_account_id == broker_account_id,
    )
    candidates = (
        query.order_by(CorporateAction.ex_date.desc(), CorporateAction.id.desc()).limit(2).all()
    )
    return candidates[0] if len(candidates) == 1 else None
