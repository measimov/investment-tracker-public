from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
import pandas as pd
import io
from datetime import datetime


from ..database import get_db
from ..models.broker_account import BrokerAccount
from ..models.transaction import Transaction
from ..models.user import User
from ._ownership import get_owned_record
from ..schemas.broker_import import BrokerImportResult
from ..services.cmb_fund_flow_importer import import_cmb_fund_flow, preview_cmb_fund_flow
from ..services.eastmoney_statement_importer import (
    import_eastmoney_statement,
    preview_eastmoney_statement,
)
from ..services.ibkr_activity_importer import import_ibkr_activity, preview_ibkr_activity
from ..services.standard_import import (
    import_standard_corporate_actions_dataframe,
    import_standard_transactions_dataframe,
)
from ..core.deps import get_current_active_user
from ..core.logging import get_app_logger

logger = get_app_logger(__name__)

router = APIRouter()

def validate_excel_filename(filename: str) -> None:
    if not (filename.endswith(".xlsx") or filename.endswith(".xls")):
        raise HTTPException(status_code=400, detail="File must be an Excel file")


def validate_csv_filename(filename: str) -> None:
    if not filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="File must be a CSV")


# 上传体积上限：券商年度对账单 PDF 实测在 1MB 量级，20MB 已是充裕余量。
# 没有上限时，一个超大 xlsx/PDF 会被整份读进内存再交给 pandas/openpyxl 解析
# （解压放大比可观）。nginx 侧另有 client_max_body_size 兜底，两层都要有：
# 直连后端或调大 nginx 默认值时，这一层才是真正生效的那个。
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


async def read_upload(file: UploadFile) -> bytes:
    """读取上传内容，**读取本身就有上界**。

    此前是无参数 `await file.read()` 再检查 len——那只能阻止解析，阻止不了
    内存占用：任意大小的上传已经完整物化成 bytes 了。这里最多读
    MAX_UPLOAD_BYTES + 1 字节，多出的那一字节仅用于判定"是否超限"。
    """
    contents = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大，上限为 {MAX_UPLOAD_BYTES // 1024 // 1024}MB。",
        )
    return contents


class ImportDataError(ValueError):
    """用户数据错误，且 message 是**可安全展示**的中文文案。

    与"解析库抛出的异常"区分开：后者的 str() 常含内部路径、SQL 片段、
    字节偏移等实现细节，只能进服务端日志，不能进 HTTP 响应。
    """


# 解析库在遇到坏文件时抛的异常。它们确实是"用户的数据有问题"（→400），但
# 异常文本不可直接展示，统一换成稳定中文文案。
# 注意 pypdf/pdfminer 的异常**不是** ValueError 子类（EmptyFileError、
# PdfStreamError、PdfminerException 均直接继承 Exception），漏掉它们会让
# 损坏的招商/东财 PDF 从 400 退化成 500——正好与本次错误分类的目标相反。
def _parser_error_types() -> tuple:
    types: list = [
        pd.errors.ParserError,
        pd.errors.EmptyDataError,
        UnicodeDecodeError,
    ]
    try:
        from pypdf.errors import PdfReadError, PyPdfError

        types += [PyPdfError, PdfReadError]
    except ImportError:  # pragma: no cover - 依赖恒在
        pass
    try:
        from pdfplumber.utils.exceptions import PdfminerException

        types.append(PdfminerException)
    except ImportError:  # pragma: no cover
        pass
    try:
        from openpyxl.utils.exceptions import InvalidFileException

        types.append(InvalidFileException)
    except ImportError:  # pragma: no cover
        pass
    return tuple(types)


PARSER_ERRORS = _parser_error_types()

# 刻意**不含** KeyError：缺列由 normalize_* 显式检查并抛 ValueError，
# 裸 KeyError 几乎总是编程缺陷（拼错字典键），归为用户错误会把 bug 伪装成 400。
USER_DATA_ERRORS = (ImportDataError, ValueError)


def as_user_data_error(exc: Exception, prefix: str) -> HTTPException:
    """用户数据错误 → 400。

    **判断顺序是关键**：pandas 的 ParserError / EmptyDataError 以及
    UnicodeDecodeError 全都继承 ValueError，先判 USER_DATA_ERRORS 会让它们
    落进"可安全展示"分支，把 `Error tokenizing data ... /srv/app/x.csv line 42`
    这类含路径与字节偏移的原文回显给客户端。所以必须先判 PARSER_ERRORS。

    只有 ImportDataError 与「非解析库的」ValueError 才是我们自己写的文案。
    """
    if isinstance(exc, PARSER_ERRORS):
        detail = f"{prefix}：文件无法解析，请确认文件未损坏且格式正确。"
    elif isinstance(exc, USER_DATA_ERRORS):
        detail = f"{prefix}：{exc}"
    else:  # pragma: no cover - 端点只捕获上述两类
        detail = f"{prefix}：文件无法解析，请确认文件未损坏且格式正确。"
    logger.warning("%s [%s]: %s", prefix, type(exc).__name__, str(exc)[:300])
    return HTTPException(status_code=400, detail=detail)


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

    contents = await read_upload(file)
    try:
        df = pd.read_csv(io.BytesIO(contents), dtype={"symbol": str})
        return import_standard_transactions_dataframe(
            db, current_user.id, df, broker_account_id=broker_account_id
        )
    except (*USER_DATA_ERRORS, *PARSER_ERRORS) as exc:
        raise as_user_data_error(exc, "CSV 导入失败") from exc


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

    contents = await read_upload(file)
    try:
        df = pd.read_excel(io.BytesIO(contents))
        return import_standard_transactions_dataframe(
            db, current_user.id, df, broker_account_id=broker_account_id
        )
    except (*USER_DATA_ERRORS, *PARSER_ERRORS) as exc:
        raise as_user_data_error(exc, "Excel 导入失败") from exc


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

    contents = await read_upload(file)
    try:
        df = pd.read_csv(io.BytesIO(contents), dtype={"symbol": str})
        return import_standard_corporate_actions_dataframe(
            db, current_user.id, df, broker_account_id=broker_account_id
        )
    except (*USER_DATA_ERRORS, *PARSER_ERRORS) as exc:
        raise as_user_data_error(exc, "公司行动 CSV 导入失败") from exc


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

    contents = await read_upload(file)
    try:
        df = pd.read_excel(io.BytesIO(contents))
        return import_standard_corporate_actions_dataframe(
            db, current_user.id, df, broker_account_id=broker_account_id
        )
    except (*USER_DATA_ERRORS, *PARSER_ERRORS) as exc:
        raise as_user_data_error(exc, "公司行动 Excel 导入失败") from exc


@router.post("/import/cmb-fund-flows/preview", response_model=BrokerImportResult)
async def preview_cmb_fund_flows(
    file: UploadFile = File(...),
    broker_account_id: int | None = Form(None),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Preview 招商证券 PDF 对账单 import."""
    validate_cmb_fund_flow_filename(file.filename)

    contents = await read_upload(file)
    try:
        return preview_cmb_fund_flow(
            db,
            current_user.id,
            contents,
            file.filename,
            broker_account_id=broker_account_id,
        )
    except (*USER_DATA_ERRORS, *PARSER_ERRORS) as exc:
        raise as_user_data_error(exc, "Error previewing 招商证券对账单") from exc


@router.post("/import/cmb-fund-flows", response_model=BrokerImportResult)
async def import_cmb_fund_flows(
    file: UploadFile = File(...),
    broker_account_id: int | None = Form(None),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Import 招商证券 PDF 对账单. Duplicate row hashes are skipped."""
    validate_cmb_fund_flow_filename(file.filename)

    contents = await read_upload(file)
    try:
        return import_cmb_fund_flow(
            db,
            current_user.id,
            contents,
            file.filename,
            broker_account_id=broker_account_id,
        )
    except (*USER_DATA_ERRORS, *PARSER_ERRORS) as exc:
        raise as_user_data_error(exc, "Error importing 招商证券对账单") from exc


@router.post("/import/ibkr-activity/preview", response_model=BrokerImportResult)
async def preview_ibkr_activity_statement(
    file: UploadFile = File(...),
    broker_account_id: int | None = Form(None),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Preview IBKR import (Activity Statement CSV or trade_history.xlsx)."""
    validate_ibkr_filename(file.filename)

    contents = await read_upload(file)
    try:
        return preview_ibkr_activity(
            db,
            current_user.id,
            contents,
            file.filename,
            broker_account_id=broker_account_id,
        )
    except (*USER_DATA_ERRORS, *PARSER_ERRORS) as exc:
        raise as_user_data_error(exc, "IBKR 对账单预览失败") from exc


@router.post("/import/ibkr-activity", response_model=BrokerImportResult)
async def import_ibkr_activity_statement(
    file: UploadFile = File(...),
    broker_account_id: int | None = Form(None),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Import IBKR trades (Activity CSV or trade_history.xlsx). Duplicate row hashes are skipped."""
    validate_ibkr_filename(file.filename)

    contents = await read_upload(file)
    try:
        return import_ibkr_activity(
            db,
            current_user.id,
            contents,
            file.filename,
            broker_account_id=broker_account_id,
        )
    except (*USER_DATA_ERRORS, *PARSER_ERRORS) as exc:
        raise as_user_data_error(exc, "IBKR 对账单导入失败") from exc


@router.post("/import/eastmoney-statement/preview", response_model=BrokerImportResult)
async def preview_eastmoney_statement_pdf(
    file: UploadFile = File(...),
    broker_account_id: int | None = Form(None),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Preview 东方财富普通股票或港股通 PDF 对账单."""
    validate_pdf_filename(file.filename)

    contents = await read_upload(file)
    try:
        return preview_eastmoney_statement(
            db,
            current_user.id,
            contents,
            file.filename,
            broker_account_id=broker_account_id,
        )
    except (*USER_DATA_ERRORS, *PARSER_ERRORS) as exc:
        raise as_user_data_error(exc, "Error previewing 东方财富对账单") from exc


@router.post("/import/eastmoney-statement", response_model=BrokerImportResult)
async def import_eastmoney_statement_pdf(
    file: UploadFile = File(...),
    broker_account_id: int | None = Form(None),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Import 东方财富普通股票或港股通 PDF 对账单 into a selected account."""
    validate_pdf_filename(file.filename)

    contents = await read_upload(file)
    try:
        return import_eastmoney_statement(
            db,
            current_user.id,
            contents,
            file.filename,
            broker_account_id=broker_account_id,
        )
    except (*USER_DATA_ERRORS, *PARSER_ERRORS) as exc:
        raise as_user_data_error(exc, "Error importing 东方财富对账单") from exc


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
