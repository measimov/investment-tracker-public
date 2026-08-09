"""标准 CSV/Excel 导入（从路由层下沉，issue #137）。

列规范化、逐行 schema 校验、超卖预检、写入与持仓重算编排都在这里；
路由层只留文件解析入口、参数解析与错误映射。与券商专用导入器不同，
标准导入不建 ImportBatch 审计（手工整理的小批量来源），但共享同一套
时间线锁与"先锁再校验再写入"的口径（POST /api/transactions 同款）。
"""

from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.orm import Session

from ..models.corporate_action import CorporateAction
from ..models.transaction import Transaction
from ..schemas.corporate_action import CorporateActionCreate
from ..schemas.transaction import TransactionCreate
from .holding_service import (
    lock_security_timeline,
    recalculate_holdings,
    validate_no_oversell,
)

STANDARD_REQUIRED_COLUMNS = [
    "symbol",
    "market",
    "transaction_type",
    "quantity",
    "price",
    "transaction_date",
]

STANDARD_OPTIONAL_DEFAULTS = {
    "name": None,
    "fee": 0,
    "currency": "CNY",
    "notes": None,
}

STANDARD_CORPORATE_ACTION_REQUIRED_COLUMNS = [
    "symbol",
    "market",
    "action_type",
    "ex_date",
]

STANDARD_CORPORATE_ACTION_OPTIONAL_DEFAULTS = {
    "name": None,
    "record_date": None,
    "payment_date": None,
    "dividend_per_share": None,
    "total_dividend": None,
    "tax_withheld": Decimal("0"),
    "tax_rate": None,
    "net_dividend": None,
    "shares_received": None,
    "distribution_ratio": None,
    "subscription_price": None,
    "subscription_quantity": None,
    "subscription_amount": None,
    "split_ratio": None,
    "new_shares": None,
    "cost_basis_adjustment": None,
    "adjusted_quantity": None,
    "adjusted_cost_per_share": None,
    "currency": "CNY",
    "notes": None,
}

HOLDING_AFFECTING_ACTION_TYPES = {
    "STOCK_DIVIDEND",
    "RIGHTS_ISSUE",
    "STOCK_SPLIT",
    "REVERSE_SPLIT",
    "BONUS_ISSUE",
}


def normalize_symbol_value(value) -> str:
    text = "" if pd.isna(value) else str(value).replace("\ufeff", "").strip()
    if text.endswith(".0") and text.replace(".", "", 1).isdigit():
        text = text[:-2]
    return text


def optional_value(value, default=None):
    if pd.isna(value):
        return default
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "":
            return default
        return stripped
    return value


def optional_decimal(value, default=None):
    value = optional_value(value, default)
    if value is default:
        return default
    return Decimal(str(value).replace(",", ""))


def optional_date(value, default=None):
    value = optional_value(value, default)
    if value is default:
        return default
    return pd.to_datetime(value).date()


def normalize_standard_transactions_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    missing_columns = [col for col in STANDARD_REQUIRED_COLUMNS if col not in df.columns]

    if missing_columns:
        raise ValueError(f"Missing required columns: {', '.join(missing_columns)}")

    normalized = df.copy()
    normalized["symbol"] = normalized["symbol"].apply(normalize_symbol_value)
    normalized["transaction_date"] = pd.to_datetime(normalized["transaction_date"]).dt.date

    for column, default in STANDARD_OPTIONAL_DEFAULTS.items():
        if column not in normalized.columns:
            normalized[column] = default

    return normalized


def normalize_standard_corporate_actions_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    missing_columns = [
        col for col in STANDARD_CORPORATE_ACTION_REQUIRED_COLUMNS if col not in df.columns
    ]

    if missing_columns:
        raise ValueError(f"Missing required columns: {', '.join(missing_columns)}")

    normalized = df.copy()
    normalized["symbol"] = normalized["symbol"].apply(normalize_symbol_value)
    normalized["ex_date"] = pd.to_datetime(normalized["ex_date"]).dt.date

    for column in ("record_date", "payment_date"):
        if column in normalized.columns:
            normalized[column] = normalized[column].apply(optional_date)

    for column, default in STANDARD_CORPORATE_ACTION_OPTIONAL_DEFAULTS.items():
        if column not in normalized.columns:
            normalized[column] = default

    return normalized


def _validated_standard_transaction(row, index: int, broker_account_id: int | None) -> dict:
    """单行 → 经 TransactionCreate 校验后的字段字典。

    此前这里直接 `Transaction(**row)` 建 ORM 行，绕开了 schema 的全部约束：
    一份 CSV 可以写入 transaction_type="TRANSFER_OUT"（伪造无联动腿的转仓）、
    负数量、负价格、超长字符串。对照同文件的公司行动导入——它早就先过
    CorporateActionCreate 了，交易导入是漏掉的那个。
    """
    payload = {
        "symbol": row["symbol"],
        "name": row["name"] if pd.notna(row["name"]) else None,
        "market": row["market"],
        "transaction_type": row["transaction_type"],
        "quantity": row["quantity"],
        "price": row["price"],
        "fee": row["fee"] if pd.notna(row["fee"]) else 0,
        "transaction_date": row["transaction_date"],
        "currency": row["currency"] if pd.notna(row["currency"]) else "CNY",
        "notes": row["notes"] if pd.notna(row["notes"]) else None,
        "broker_account_id": broker_account_id,
    }
    try:
        validated = TransactionCreate(**payload)
    except PydanticValidationError as exc:
        problems = "；".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        # 行号按表格习惯从 1 计（不含表头），让用户能直接定位到那一行
        raise ValueError(f"第 {index + 1} 行数据不合法（{problems}）") from exc
    # Decimal 直通，不再经 float——与 POST /api/transactions 同精度
    return validated.model_dump()


def import_standard_transactions_dataframe(
    db: Session,
    user_id: int,
    df: pd.DataFrame,
    broker_account_id: int | None = None,
):
    normalized = normalize_standard_transactions_dataframe(df)
    candidates: list[dict] = []
    symbols_markets = set()

    for index, (_, row) in enumerate(normalized.iterrows()):
        data = _validated_standard_transaction(row, index, broker_account_id)
        candidates.append(data)
        symbols_markets.add((data["symbol"], data["market"]))

    try:
        # 与 POST /api/transactions 同口径：先取时间线锁再校验再写入，
        # 避免"校验读旧时间线、提交在别人之后"的竞态。
        for symbol, market in sorted(symbols_markets):
            lock_security_timeline(db, user_id, symbol, market)

        _validate_import_batch_sequence(db, user_id, candidates, broker_account_id)

        for data in candidates:
            db.add(Transaction(**data, user_id=user_id))

        db.flush()

        for symbol, market in symbols_markets:
            recalculate_holdings(db, user_id, symbol, market, commit=False)

        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "message": f"Successfully imported {len(candidates)} transactions",
        "count": len(candidates),
    }


def _validate_import_batch_sequence(
    db: Session,
    user_id: int,
    candidates: list[dict],
    broker_account_id: int | None,
):
    """整批 + 库内既有交易一起做超卖校验。

    按 (symbol, market) 分组、每组只查一次库：逐行调用会是 O(行数) 次全量查询，
    几千行的对账单导入下不可接受。validate_no_oversell 内部按
    (日期, 重放序) 排序，所以传入顺序无关。
    """
    by_key: dict[tuple[str, str], list] = {}
    for data in candidates:
        by_key.setdefault((data["symbol"], data["market"]), []).append(
            SimpleNamespace(id=None, **data)
        )

    for (symbol, market), new_rows in by_key.items():
        query = db.query(Transaction).filter(
            Transaction.user_id == user_id,
            Transaction.symbol == symbol,
            Transaction.market == market,
        )
        if broker_account_id is None:
            query = query.filter(Transaction.broker_account_id.is_(None))
        else:
            query = query.filter(Transaction.broker_account_id == broker_account_id)
        try:
            validate_no_oversell([*query.all(), *new_rows])
        except ValueError as exc:
            raise ValueError(f"{symbol}（{market}）导入后出现超卖：{exc}") from exc


def import_standard_corporate_actions_dataframe(
    db: Session,
    user_id: int,
    df: pd.DataFrame,
    broker_account_id: int | None = None,
):
    normalized = normalize_standard_corporate_actions_dataframe(df)
    imported_count = 0
    affected_symbols = set()

    try:
        for _, row in normalized.iterrows():
            action_data = {
                "broker_account_id": broker_account_id,
                "symbol": row["symbol"],
                "name": optional_value(row["name"]),
                "market": optional_value(row["market"]),
                "action_type": optional_value(row["action_type"]),
                "ex_date": row["ex_date"],
                "record_date": optional_date(row["record_date"]),
                "payment_date": optional_date(row["payment_date"]),
                "dividend_per_share": optional_decimal(row["dividend_per_share"]),
                "total_dividend": optional_decimal(row["total_dividend"]),
                "tax_withheld": optional_decimal(row["tax_withheld"], Decimal("0")),
                "tax_rate": optional_decimal(row["tax_rate"]),
                "net_dividend": optional_decimal(row["net_dividend"]),
                "shares_received": optional_decimal(row["shares_received"]),
                "distribution_ratio": optional_value(row["distribution_ratio"]),
                "subscription_price": optional_decimal(row["subscription_price"]),
                "subscription_quantity": optional_decimal(row["subscription_quantity"]),
                "subscription_amount": optional_decimal(row["subscription_amount"]),
                "split_ratio": optional_value(row["split_ratio"]),
                "new_shares": optional_decimal(row["new_shares"]),
                "cost_basis_adjustment": optional_decimal(row["cost_basis_adjustment"]),
                "adjusted_quantity": optional_decimal(row["adjusted_quantity"]),
                "adjusted_cost_per_share": optional_decimal(row["adjusted_cost_per_share"]),
                "currency": optional_value(row["currency"], "CNY"),
                "notes": optional_value(row["notes"]),
            }
            action = CorporateActionCreate(**action_data)

            db_action = CorporateAction(**action.model_dump(), user_id=user_id)
            db.add(db_action)
            if action.action_type in HOLDING_AFFECTING_ACTION_TYPES:
                affected_symbols.add((action.symbol, action.market))
            imported_count += 1

        db.flush()

        for symbol, market in affected_symbols:
            recalculate_holdings(db, user_id, symbol, market, commit=False)

        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "message": f"Successfully imported {imported_count} corporate actions",
        "count": imported_count,
        "affected_symbols": len(affected_symbols),
    }
