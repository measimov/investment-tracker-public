"""导入端点的错误处理与上传上限（issue #130）。

此前每个端点都是 `except Exception as e: HTTPException(400, str(e))`：
- 编程错误（AttributeError）、DB 故障统统被降级成 400，监控里无法区分
  "用户数据有问题"和"服务端有 bug"；
- `str(e)` 把内部错误文本（SQL 片段、文件路径、异常类型）原样回显给客户端；
- 上传无字节上限，整份读进内存后交给 pandas/openpyxl（解压放大比可观）。
"""

import io

import httpx
import pytest

from app.api import import_export
from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.broker_account import BrokerAccount
from app.models.user import User


@pytest.fixture
def token_password():
    password = "import-error-handling-password"
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "demo").one()
        original = user.hashed_password
        user.hashed_password = get_password_hash(password)
        db.commit()
        yield password
        user.hashed_password = original
        db.commit()
    finally:
        db.close()


@pytest.fixture
def cmb_account():
    """招商 PDF 端点要求先指定账户，否则在解析前就被挡下。"""
    db = SessionLocal()
    try:
        account = BrokerAccount(
            user_id=2, broker="招商证券", account_name="e2e-cmb", base_currency="CNY"
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        account_id = account.id
        yield account_id
        db.query(BrokerAccount).filter(BrokerAccount.id == account_id).delete()
        db.commit()
    finally:
        db.close()


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


async def _auth_headers(client, password):
    response = await client.post(
        "/api/auth/token", json={"username": "demo", "password": password}
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.anyio
async def test_oversized_upload_is_rejected_with_413(token_password, monkeypatch):
    """超过上限直接 413，且不进入解析——不能整份喂给 pandas。"""
    monkeypatch.setattr(import_export, "MAX_UPLOAD_BYTES", 1024)

    def explode(*args, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("超限文件不得进入解析阶段")

    monkeypatch.setattr(import_export.pd, "read_csv", explode)

    async with _client() as client:
        headers = await _auth_headers(client, token_password)
        response = await client.post(
            "/api/import/csv",
            headers=headers,
            files={"file": ("big.csv", io.BytesIO(b"x" * 4096), "text/csv")},
        )

    assert response.status_code == 413
    assert "上限" in response.json()["detail"]


@pytest.mark.anyio
async def test_malformed_csv_is_a_400_with_chinese_message(token_password):
    """坏数据仍是 400，文案中文且不泄漏内部异常类型。"""
    async with _client() as client:
        headers = await _auth_headers(client, token_password)
        response = await client.post(
            "/api/import/csv",
            headers=headers,
            files={"file": ("bad.csv", io.BytesIO("不是,合法\n表头,内容\n".encode()), "text/csv")},
        )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "导入失败" in detail
    assert "Traceback" not in detail


@pytest.mark.anyio
async def test_server_side_bug_is_not_disguised_as_400(token_password, monkeypatch):
    """服务端缺陷必须逃逸成 500，不能被当成用户数据错误。

    这是本次改动的核心：`except Exception` 会把 AttributeError 也吞成 400。
    """
    def boom(*args, **kwargs):
        raise AttributeError("内部缺陷：'NoneType' object has no attribute 'foo'")

    monkeypatch.setattr(import_export, "import_standard_transactions_dataframe", boom)

    csv = (
        "symbol,market,transaction_type,quantity,price,transaction_date\n"
        "600000,A股,BUY,1,1,2026-01-01\n"
    ).encode()
    async with _client() as client:
        headers = await _auth_headers(client, token_password)
        with pytest.raises(AttributeError):
            # ASGITransport 下未处理异常会直接上抛；关键是它**没有**变成 400
            await client.post(
                "/api/import/csv",
                headers=headers,
                files={"file": ("ok.csv", io.BytesIO(csv), "text/csv")},
            )


@pytest.mark.anyio
async def test_invalid_row_returns_400_naming_the_row(token_password):
    """行级校验失败：400 + 指明行号与字段（不是 500，也不是静默入库）。"""
    csv = (
        "symbol,market,transaction_type,quantity,price,transaction_date\n"
        "600000,A股,BUY,100,10,2026-01-01\n"
        "600000,A股,TRANSFER_OUT,50,10,2026-02-01\n"
    ).encode()
    async with _client() as client:
        headers = await _auth_headers(client, token_password)
        response = await client.post(
            "/api/import/csv",
            headers=headers,
            files={"file": ("x.csv", io.BytesIO(csv), "text/csv")},
        )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "第 2 行" in detail
    assert "transaction_type" in detail


# ---------------------------------------------------------------------------
# PR #151 复审的四条 P1/P2
# ---------------------------------------------------------------------------


class _RecordingUpload:
    """记录 read() 调用参数的假 UploadFile。"""

    def __init__(self, payload: bytes):
        self._payload = payload
        self.read_calls: list = []

    async def read(self, size: int = -1):
        self.read_calls.append(size)
        return self._payload[:size] if size and size > 0 else self._payload


@pytest.mark.anyio
async def test_read_upload_bounds_the_read_itself(monkeypatch):
    """读取必须有上界——只在读完之后查 len 挡不住内存占用。"""
    monkeypatch.setattr(import_export, "MAX_UPLOAD_BYTES", 1024)
    upload = _RecordingUpload(b"x" * 8192)

    with pytest.raises(import_export.HTTPException) as excinfo:
        await import_export.read_upload(upload)

    assert excinfo.value.status_code == 413
    # 修复前是无参数 read()，这里会记到 -1（即"全部读入"）
    assert upload.read_calls == [1025], f"read 调用未限长：{upload.read_calls}"


@pytest.mark.anyio
async def test_read_upload_passes_through_within_limit(monkeypatch):
    monkeypatch.setattr(import_export, "MAX_UPLOAD_BYTES", 1024)
    upload = _RecordingUpload(b"y" * 100)

    assert await import_export.read_upload(upload) == b"y" * 100
    assert upload.read_calls == [1025]


@pytest.mark.parametrize(
    "exc_factory,label",
    [
        (lambda: __import__("pypdf.errors", fromlist=["x"]).EmptyFileError("empty"), "空 PDF"),
        (
            lambda: __import__("pypdf.errors", fromlist=["x"]).PdfStreamError("stream"),
            "损坏 PDF",
        ),
        (
            lambda: __import__(
                "pdfplumber.utils.exceptions", fromlist=["x"]
            ).PdfminerException("pdfminer"),
            "pdfminer 失败",
        ),
    ],
)
def test_parser_errors_are_classified_as_user_data(exc_factory, label):
    """坏 PDF 必须是 400——这些异常都不是 ValueError 子类，漏掉就退化成 500。"""
    exc = exc_factory()
    assert isinstance(exc, import_export.PARSER_ERRORS), (
        f"{label} 未被纳入 PARSER_ERRORS，会退化成 500"
    )


def test_parser_error_detail_does_not_leak_internals():
    """解析库异常的原文可能含路径/偏移，不得进 HTTP 响应。"""
    from pypdf.errors import PdfStreamError

    leaky = PdfStreamError("/srv/app/secret/path.pdf: stream error at offset 0xDEAD")
    http_exc = import_export.as_user_data_error(leaky, "招商证券对账单导入失败")

    assert http_exc.status_code == 400
    assert "/srv/app" not in http_exc.detail
    assert "0xDEAD" not in http_exc.detail
    assert "文件无法解析" in http_exc.detail


def test_domain_error_message_is_still_shown():
    """我们自己写的中文校验文案仍要展示给用户，否则报错没法定位。"""
    http_exc = import_export.as_user_data_error(
        ValueError("第 3 行数据不合法（quantity: 必须大于 0）"), "CSV 导入失败"
    )

    assert "第 3 行" in http_exc.detail


def test_key_error_is_not_treated_as_user_data():
    """裸 KeyError 几乎总是编程缺陷，不得伪装成 400。"""
    assert not isinstance(KeyError("symbol"), import_export.USER_DATA_ERRORS)
    assert not isinstance(KeyError("symbol"), import_export.PARSER_ERRORS)


@pytest.mark.anyio
async def test_corrupt_pdf_preview_is_a_stable_400(token_password, cmb_account):
    """损坏 PDF 走真实端点：必须是稳定 400 中文文案，不是 500。

    复审指出 pypdf/pdfminer 的异常不是 ValueError 子类，此前会退化成 500。
    """
    async with _client() as client:
        headers = await _auth_headers(client, token_password)
        response = await client.post(
            "/api/import/cmb-fund-flows/preview",
            headers=headers,
            files={"file": ("garbage.pdf", io.BytesIO(b"not a pdf at all"), "application/pdf")},
            data={"broker_account_id": str(cmb_account)},
        )

    assert response.status_code == 400, f"坏 PDF 应为 400，实际 {response.status_code}"
    detail = response.json()["detail"]
    assert "文件无法解析" in detail
    assert "b'not a" not in detail


@pytest.mark.anyio
async def test_empty_pdf_import_is_a_stable_400(token_password, cmb_account):
    async with _client() as client:
        headers = await _auth_headers(client, token_password)
        response = await client.post(
            "/api/import/cmb-fund-flows",
            headers=headers,
            files={"file": ("empty.pdf", io.BytesIO(b""), "application/pdf")},
            data={"broker_account_id": str(cmb_account)},
        )

    assert response.status_code == 400
    assert "文件无法解析" in response.json()["detail"]


@pytest.mark.anyio
async def test_pdf_endpoint_still_surfaces_server_bugs_as_500(token_password, cmb_account, monkeypatch):
    """PDF 端点同样不得把编程缺陷伪装成 400。"""
    def boom(*args, **kwargs):
        raise AttributeError("内部缺陷")

    monkeypatch.setattr(import_export, "preview_cmb_fund_flow", boom)

    async with _client() as client:
        headers = await _auth_headers(client, token_password)
        with pytest.raises(AttributeError):
            await client.post(
                "/api/import/cmb-fund-flows/preview",
                headers=headers,
                files={"file": ("x.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
                data={"broker_account_id": str(cmb_account)},
            )


# ---------------------------------------------------------------------------
# 第二轮复审：pandas 解析异常同时继承 ValueError，判断顺序决定是否泄漏
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_factory,label",
    [
        (
            lambda: __import__("pandas").errors.ParserError(
                "Error tokenizing data. C error: /srv/app/secret.csv line 42, offset 0xDEAD"
            ),
            "ParserError",
        ),
        (
            lambda: __import__("pandas").errors.EmptyDataError(
                "No columns to parse from file /srv/app/empty.csv"
            ),
            "EmptyDataError",
        ),
        (
            lambda: UnicodeDecodeError("utf-8", b"\xff\xfe", 0, 1, "invalid start byte at /srv/x"),
            "UnicodeDecodeError",
        ),
    ],
)
def test_pandas_parse_errors_do_not_leak_despite_being_valueerror(exc_factory, label):
    """三者都是 ValueError 子类——先判 USER_DATA_ERRORS 会把原文回显出去。"""
    import pandas as _pd

    exc = exc_factory()
    assert isinstance(exc, ValueError), f"{label} 前提变了"

    detail = import_export.as_user_data_error(exc, "CSV 导入失败").detail

    assert "/srv/app" not in detail, f"{label} 泄漏了内部路径：{detail}"
    assert "/srv/x" not in detail, f"{label} 泄漏了内部路径：{detail}"
    assert "0xDEAD" not in detail, f"{label} 泄漏了偏移：{detail}"
    assert "文件无法解析" in detail
    del _pd


def test_own_validation_message_still_visible_after_ordering_change():
    """调整判断顺序后，我们自己写的中文文案不得被一并吞掉。"""
    detail = import_export.as_user_data_error(
        ValueError("第 3 行数据不合法（quantity: 必须大于 0）"), "CSV 导入失败"
    ).detail

    assert "第 3 行" in detail
    assert "文件无法解析" not in detail
