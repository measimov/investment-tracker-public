from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
import pandas as pd
import io
from datetime import datetime
from ..database import get_db
from ..models.transaction import Transaction
from ..models.user import User
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
    'symbol',
    'market',
    'transaction_type',
    'quantity',
    'price',
    'transaction_date',
]

STANDARD_OPTIONAL_DEFAULTS = {
    'name': None,
    'fee': 0,
    'currency': 'CNY',
    'notes': None,
}


def validate_excel_filename(filename: str) -> None:
    if not (filename.endswith('.xlsx') or filename.endswith('.xls')):
        raise HTTPException(status_code=400, detail="File must be an Excel file")


def validate_csv_filename(filename: str) -> None:
    if not filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail="File must be a CSV")


def validate_pdf_filename(filename: str) -> None:
    if not filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="File must be a PDF")


def normalize_standard_transactions_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    missing_columns = [col for col in STANDARD_REQUIRED_COLUMNS if col not in df.columns]

    if missing_columns:
        raise ValueError(f"Missing required columns: {', '.join(missing_columns)}")

    normalized = df.copy()
    normalized['transaction_date'] = pd.to_datetime(normalized['transaction_date']).dt.date

    for column, default in STANDARD_OPTIONAL_DEFAULTS.items():
        if column not in normalized.columns:
            normalized[column] = default

    return normalized


def import_standard_transactions_dataframe(
    db: Session,
    user_id: int,
    df: pd.DataFrame,
):
    normalized = normalize_standard_transactions_dataframe(df)
    imported_count = 0
    symbols_markets = set()

    try:
        for _, row in normalized.iterrows():
            transaction_data = {
                'symbol': row['symbol'],
                'name': row['name'] if pd.notna(row['name']) else None,
                'market': row['market'],
                'transaction_type': row['transaction_type'],
                'quantity': float(row['quantity']),
                'price': float(row['price']),
                'fee': float(row['fee']) if pd.notna(row['fee']) else 0,
                'transaction_date': row['transaction_date'],
                'currency': row['currency'] if pd.notna(row['currency']) else 'CNY',
                'notes': row['notes'] if pd.notna(row['notes']) else None,
                'user_id': user_id,
            }

            db_transaction = Transaction(**transaction_data)
            db.add(db_transaction)
            symbols_markets.add((row['symbol'], row['market']))
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


@router.post("/import/csv")
async def import_csv(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Import transactions from CSV file."""
    validate_csv_filename(file.filename)

    try:
        contents = await file.read()
        df = pd.read_csv(io.BytesIO(contents))
        return import_standard_transactions_dataframe(db, current_user.id, df)

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error processing CSV: {str(e)}")


@router.post("/import/excel")
async def import_excel(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Import transactions from Excel file."""
    validate_excel_filename(file.filename)

    try:
        contents = await file.read()
        df = pd.read_excel(io.BytesIO(contents))
        return import_standard_transactions_dataframe(db, current_user.id, df)

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error processing Excel: {str(e)}")


@router.post("/import/cmb-fund-flows/preview", response_model=BrokerImportResult)
async def preview_cmb_fund_flows(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Preview 招商证券资金流水 import. Only 证券买入/证券卖出 rows are eligible."""
    validate_excel_filename(file.filename)

    try:
        contents = await file.read()
        return preview_cmb_fund_flow(db, current_user.id, contents, file.filename)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error previewing 招商证券资金流水: {str(e)}")


@router.post("/import/cmb-fund-flows", response_model=BrokerImportResult)
async def import_cmb_fund_flows(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Import 招商证券资金流水. Duplicate row hashes are skipped."""
    validate_excel_filename(file.filename)

    try:
        contents = await file.read()
        return import_cmb_fund_flow(db, current_user.id, contents, file.filename)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error importing 招商证券资金流水: {str(e)}")


@router.post("/import/ibkr-activity/preview", response_model=BrokerImportResult)
async def preview_ibkr_activity_statement(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Preview IBKR Activity Statement transaction history import."""
    validate_csv_filename(file.filename)

    try:
        contents = await file.read()
        return preview_ibkr_activity(db, current_user.id, contents, file.filename)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error previewing IBKR activity statement: {str(e)}")


@router.post("/import/ibkr-activity", response_model=BrokerImportResult)
async def import_ibkr_activity_statement(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Import IBKR Activity Statement transaction history. Duplicate row hashes are skipped."""
    validate_csv_filename(file.filename)

    try:
        contents = await file.read()
        return import_ibkr_activity(db, current_user.id, contents, file.filename)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error importing IBKR activity statement: {str(e)}")


@router.post("/import/eastmoney-statement/preview", response_model=BrokerImportResult)
async def preview_eastmoney_statement_pdf(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Preview 东方财富 PDF 股票明细对账单 import."""
    validate_pdf_filename(file.filename)

    try:
        contents = await file.read()
        return preview_eastmoney_statement(db, current_user.id, contents, file.filename)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error previewing 东方财富对账单: {str(e)}")


@router.post("/import/eastmoney-statement", response_model=BrokerImportResult)
async def import_eastmoney_statement_pdf(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Import 东方财富 PDF 股票明细对账单. Duplicate row hashes are skipped."""
    validate_pdf_filename(file.filename)

    try:
        contents = await file.read()
        return import_eastmoney_statement(db, current_user.id, contents, file.filename)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error importing 东方财富对账单: {str(e)}")


@router.get("/export/csv")
def export_csv(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Export all transactions to CSV file."""
    transactions = db.query(Transaction).filter(
        Transaction.user_id == current_user.id
    ).order_by(Transaction.transaction_date.desc()).all()

    data = []
    for txn in transactions:
        data.append({
            'id': txn.id,
            'symbol': txn.symbol,
            'name': txn.name,
            'market': txn.market,
            'transaction_type': txn.transaction_type,
            'quantity': float(txn.quantity),
            'price': float(txn.price),
            'fee': float(txn.fee),
            'transaction_date': txn.transaction_date.isoformat(),
            'currency': txn.currency,
            'notes': txn.notes
        })

    df = pd.DataFrame(data)

    # Create CSV in memory
    stream = io.StringIO()
    df.to_csv(stream, index=False)
    stream.seek(0)

    return StreamingResponse(
        iter([stream.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=transactions_{datetime.now().strftime('%Y%m%d')}.csv"}
    )


@router.get("/export/excel")
def export_excel(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Export all transactions to Excel file."""
    transactions = db.query(Transaction).filter(
        Transaction.user_id == current_user.id
    ).order_by(Transaction.transaction_date.desc()).all()

    data = []
    for txn in transactions:
        data.append({
            'id': txn.id,
            'symbol': txn.symbol,
            'name': txn.name,
            'market': txn.market,
            'transaction_type': txn.transaction_type,
            'quantity': float(txn.quantity),
            'price': float(txn.price),
            'fee': float(txn.fee),
            'transaction_date': txn.transaction_date.isoformat(),
            'currency': txn.currency,
            'notes': txn.notes
        })

    df = pd.DataFrame(data)

    # Create Excel in memory
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Transactions')
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=transactions_{datetime.now().strftime('%Y%m%d')}.xlsx"}
    )
