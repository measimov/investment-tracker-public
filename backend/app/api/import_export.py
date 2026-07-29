from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
import pandas as pd
import io
from datetime import datetime
from decimal import Decimal
from ..database import get_db
from ..models.broker_account import BrokerAccount
from ..models.corporate_action import CorporateAction
from ..models.transaction import Transaction
from ..models.user import User
from ._ownership import get_owned_record
from ..schemas.corporate_action import CorporateActionCreate
from ..schemas.broker_import import BrokerImportResult
from ..services.cmb_fund_flow_importer import import_cmb_fund_flow, preview_cmb_fund_flow
from ..services.eastmoney_statement_importer import (
    import_eastmoney_statement,
    preview_eastmoney_statement,
)
from ..services.ibkr_activity_importer import import_ibkr_activity, preview_ibkr_activity
from ..services.holding_service import recalculate_holdings
from ..core.deps import get_current_active_user

router = APIRouter()

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


def validate_excel_filename(filename: str) -> None:
    if not (filename.endswith(".xlsx") or filename.endswith(".xls")):
        raise HTTPException(status_code=400, detail="File must be an Excel file")


def validate_csv_filename(filename: str) -> None:
    if not filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="File must be a CSV")


def validate_standard_import_account(
    db: Session, user_id: int, broker_account_id: int | None
) -> None:
    """标准 CSV/Excel 导入的可选账户归属：账户必须存在且属于当前用户。

    不同于券商专用导入器，标准导入不限定券商名称（HSBC 等手工整理的来源
    也走这里），所以只校验归属，不校验 broker 字段。
    """
    if broker_account_id is None:
        return
    get_owned_record(
        db,
        BrokerAccount,
        broker_account_id,
        user_id,
        "Broker account not found",
    )


def validate_ibkr_filename(filename: str) -> None:
    if not (filename.endswith(".csv") or filename.lower().endswith(".xlsx")):
        raise HTTPException(
            status_code=400,
            detail="IBKR file must be an Activity CSV or trade_history xlsx",
        )


def validate_pdf_filename(filename: str) -> None:
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="File must be a PDF")


def validate_cmb_fund_flow_filename(filename: str) -> None:
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="招商证券对账单 must be a PDF file",
        )


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


def import_standard_transactions_dataframe(
    db: Session,
    user_id: int,
    df: pd.DataFrame,
    broker_account_id: int | None = None,
):
    normalized = normalize_standard_transactions_dataframe(df)
    imported_count = 0
    symbols_markets = set()

    try:
        for _, row in normalized.iterrows():
            transaction_data = {
                "symbol": row["symbol"],
                "name": row["name"] if pd.notna(row["name"]) else None,
                "market": row["market"],
                "transaction_type": row["transaction_type"],
                "quantity": float(row["quantity"]),
                "price": float(row["price"]),
                "fee": float(row["fee"]) if pd.notna(row["fee"]) else 0,
                "transaction_date": row["transaction_date"],
                "currency": row["currency"] if pd.notna(row["currency"]) else "CNY",
                "notes": row["notes"] if pd.notna(row["notes"]) else None,
                "user_id": user_id,
                "broker_account_id": broker_account_id,
            }

            db_transaction = Transaction(**transaction_data)
            db.add(db_transaction)
            symbols_markets.add((row["symbol"], row["market"]))
            imported_count += 1

        db.flush()

        for symbol, market in symbols_markets:
            recalculate_holdings(db, user_id, symbol, market, commit=False)

        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "message": f"Successfully imported {imported_count} transactions",
        "count": imported_count,
    }


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


@router.post("/import/csv")
async def import_csv(
    file: UploadFile = File(...),
    broker_account_id: int | None = Form(None),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Import transactions from CSV file, optionally attributed to a broker account."""
    validate_csv_filename(file.filename)
    validate_standard_import_account(db, current_user.id, broker_account_id)

    try:
        contents = await file.read()
        df = pd.read_csv(io.BytesIO(contents), dtype={"symbol": str})
        return import_standard_transactions_dataframe(
            db, current_user.id, df, broker_account_id=broker_account_id
        )

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error processing CSV: {str(e)}")


@router.post("/import/excel")
async def import_excel(
    file: UploadFile = File(...),
    broker_account_id: int | None = Form(None),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Import transactions from Excel file, optionally attributed to a broker account."""
    validate_excel_filename(file.filename)
    validate_standard_import_account(db, current_user.id, broker_account_id)

    try:
        contents = await file.read()
        df = pd.read_excel(io.BytesIO(contents))
        return import_standard_transactions_dataframe(
            db, current_user.id, df, broker_account_id=broker_account_id
        )

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error processing Excel: {str(e)}")


@router.post("/import/corporate-actions/csv")
async def import_corporate_actions_csv(
    file: UploadFile = File(...),
    broker_account_id: int | None = Form(None),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Import corporate actions from a standard CSV file."""
    validate_csv_filename(file.filename)
    validate_standard_import_account(db, current_user.id, broker_account_id)

    try:
        contents = await file.read()
        df = pd.read_csv(io.BytesIO(contents), dtype={"symbol": str})
        return import_standard_corporate_actions_dataframe(
            db, current_user.id, df, broker_account_id=broker_account_id
        )

    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"Error processing corporate actions CSV: {str(e)}"
        )


@router.post("/import/corporate-actions/excel")
async def import_corporate_actions_excel(
    file: UploadFile = File(...),
    broker_account_id: int | None = Form(None),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Import corporate actions from a standard Excel file."""
    validate_excel_filename(file.filename)
    validate_standard_import_account(db, current_user.id, broker_account_id)

    try:
        contents = await file.read()
        df = pd.read_excel(io.BytesIO(contents))
        return import_standard_corporate_actions_dataframe(
            db, current_user.id, df, broker_account_id=broker_account_id
        )

    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"Error processing corporate actions Excel: {str(e)}"
        )


@router.post("/import/cmb-fund-flows/preview", response_model=BrokerImportResult)
async def preview_cmb_fund_flows(
    file: UploadFile = File(...),
    broker_account_id: int | None = Form(None),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Preview 招商证券 PDF 对账单 import."""
    validate_cmb_fund_flow_filename(file.filename)

    try:
        contents = await file.read()
        return preview_cmb_fund_flow(
            db,
            current_user.id,
            contents,
            file.filename,
            broker_account_id=broker_account_id,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error previewing 招商证券对账单: {str(e)}")


@router.post("/import/cmb-fund-flows", response_model=BrokerImportResult)
async def import_cmb_fund_flows(
    file: UploadFile = File(...),
    broker_account_id: int | None = Form(None),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Import 招商证券 PDF 对账单. Duplicate row hashes are skipped."""
    validate_cmb_fund_flow_filename(file.filename)

    try:
        contents = await file.read()
        return import_cmb_fund_flow(
            db,
            current_user.id,
            contents,
            file.filename,
            broker_account_id=broker_account_id,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error importing 招商证券对账单: {str(e)}")


@router.post("/import/ibkr-activity/preview", response_model=BrokerImportResult)
async def preview_ibkr_activity_statement(
    file: UploadFile = File(...),
    broker_account_id: int | None = Form(None),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Preview IBKR import (Activity Statement CSV or trade_history.xlsx)."""
    validate_ibkr_filename(file.filename)

    try:
        contents = await file.read()
        return preview_ibkr_activity(
            db,
            current_user.id,
            contents,
            file.filename,
            broker_account_id=broker_account_id,
        )
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"Error previewing IBKR activity statement: {str(e)}"
        )


@router.post("/import/ibkr-activity", response_model=BrokerImportResult)
async def import_ibkr_activity_statement(
    file: UploadFile = File(...),
    broker_account_id: int | None = Form(None),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Import IBKR trades (Activity CSV or trade_history.xlsx). Duplicate row hashes are skipped."""
    validate_ibkr_filename(file.filename)

    try:
        contents = await file.read()
        return import_ibkr_activity(
            db,
            current_user.id,
            contents,
            file.filename,
            broker_account_id=broker_account_id,
        )
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"Error importing IBKR activity statement: {str(e)}"
        )


@router.post("/import/eastmoney-statement/preview", response_model=BrokerImportResult)
async def preview_eastmoney_statement_pdf(
    file: UploadFile = File(...),
    broker_account_id: int | None = Form(None),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Preview 东方财富普通股票或港股通 PDF 对账单."""
    validate_pdf_filename(file.filename)

    try:
        contents = await file.read()
        return preview_eastmoney_statement(
            db,
            current_user.id,
            contents,
            file.filename,
            broker_account_id=broker_account_id,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error previewing 东方财富对账单: {str(e)}")


@router.post("/import/eastmoney-statement", response_model=BrokerImportResult)
async def import_eastmoney_statement_pdf(
    file: UploadFile = File(...),
    broker_account_id: int | None = Form(None),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Import 东方财富普通股票或港股通 PDF 对账单 into a selected account."""
    validate_pdf_filename(file.filename)

    try:
        contents = await file.read()
        return import_eastmoney_statement(
            db,
            current_user.id,
            contents,
            file.filename,
            broker_account_id=broker_account_id,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error importing 东方财富对账单: {str(e)}")


@router.get("/export/csv")
def export_csv(
    current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)
):
    """Export all transactions to CSV file."""
    transactions = (
        db.query(Transaction)
        .filter(Transaction.user_id == current_user.id)
        .order_by(Transaction.transaction_date.desc())
        .all()
    )

    data = []
    for txn in transactions:
        data.append(
            {
                "id": txn.id,
                "symbol": txn.symbol,
                "name": txn.name,
                "market": txn.market,
                "transaction_type": txn.transaction_type,
                "quantity": float(txn.quantity),
                "price": float(txn.price),
                "fee": float(txn.fee),
                "transaction_date": txn.transaction_date.isoformat(),
                "currency": txn.currency,
                "notes": txn.notes,
            }
        )

    df = pd.DataFrame(data)

    # Create CSV in memory
    stream = io.StringIO()
    df.to_csv(stream, index=False)
    stream.seek(0)

    return StreamingResponse(
        iter([stream.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=transactions_{datetime.now().strftime('%Y%m%d')}.csv"
        },
    )


@router.get("/export/excel")
def export_excel(
    current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)
):
    """Export all transactions to Excel file."""
    transactions = (
        db.query(Transaction)
        .filter(Transaction.user_id == current_user.id)
        .order_by(Transaction.transaction_date.desc())
        .all()
    )

    data = []
    for txn in transactions:
        data.append(
            {
                "id": txn.id,
                "symbol": txn.symbol,
                "name": txn.name,
                "market": txn.market,
                "transaction_type": txn.transaction_type,
                "quantity": float(txn.quantity),
                "price": float(txn.price),
                "fee": float(txn.fee),
                "transaction_date": txn.transaction_date.isoformat(),
                "currency": txn.currency,
                "notes": txn.notes,
            }
        )

    df = pd.DataFrame(data)

    # Create Excel in memory
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Transactions")
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename=transactions_{datetime.now().strftime('%Y%m%d')}.xlsx"
        },
    )
