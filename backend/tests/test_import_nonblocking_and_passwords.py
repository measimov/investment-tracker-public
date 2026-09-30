"""#269：导入不再阻塞事件循环；超长口令给中文 422 而不是 bcrypt 5.x 的 500。"""

import ast
import io
import time
from pathlib import Path

import anyio
import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import import_export
from app.core.security import MAX_PASSWORD_BYTES, get_password_hash, verify_password
from app.database import SessionLocal
from app.main import app
from app.models.user import User
from app.schemas.user import UserCreate, UserPasswordReset, UserPasswordUpdate

API_DIR = Path(__file__).resolve().parent.parent / "app" / "api"
# 允许的 async：只做 await 的内部包装，不含同步 DB/解析调用
# （_NoInputEchoRoute.get_route_handler 返回的 handler 只 await 原始处理器并改写校验错误）
ASYNC_ALLOWLIST = {("xueqiu_collector.py", "handler")}


def test_api_layer_has_no_async_endpoints():
    """同步的解析/写库放进 async def 会阻塞整个事件循环（单进程 uvicorn：整站卡死、
    健康检查超时）。路由一律普通 def，由 FastAPI 放进线程池。"""
    offenders = []
    for path in sorted(API_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and (path.name, node.name) not in (
                ASYNC_ALLOWLIST
            ):
                offenders.append(f"{path.name}:{node.lineno} {node.name}")
    assert offenders == [], f"app/api 下不应再有 async def：{offenders}"


@pytest.fixture
def user_password():
    password = "nonblocking-test-password"
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


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _auth_headers(client, password):
    response = await client.post("/api/auth/token", json={"username": "demo", "password": password})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.anyio
async def test_health_answers_while_an_import_is_parsing(user_password, monkeypatch):
    """导入解析期间 /health 仍即时返回：解析跑在线程池里，不占事件循环。"""

    def slow_read_csv(*args, **kwargs):
        time.sleep(1.5)  # 模拟 pdfplumber/pandas 的同步重活
        raise ValueError("格式不对")

    monkeypatch.setattr(import_export.pd, "read_csv", slow_read_csv)

    async with _client() as client:
        headers = await _auth_headers(client, user_password)
        health_elapsed = {}

        async def do_import():
            await client.post(
                "/api/import/csv",
                headers=headers,
                files={"file": ("trades.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")},
            )

        async def probe_health():
            await anyio.sleep(0.3)  # 等导入进入解析
            started = time.monotonic()
            response = await client.get("/health")
            health_elapsed["seconds"] = time.monotonic() - started
            assert response.status_code == 200

        async with anyio.create_task_group() as group:
            group.start_soon(do_import)
            group.start_soon(probe_health)

    assert health_elapsed["seconds"] < 0.8, f"/health 被导入阻塞了 {health_elapsed['seconds']:.2f}s"


@pytest.mark.parametrize("filename", ["trades.csv", "TRADES.CSV", "Trades.Csv"])
def test_csv_suffix_is_case_insensitive(filename):
    import_export.validate_csv_filename(filename)


@pytest.mark.parametrize("filename", [None, "", "trades.txt", "report.pdf"])
def test_bad_or_missing_filename_is_a_422_not_a_500(filename):
    with pytest.raises(HTTPException) as info:
        import_export.validate_csv_filename(filename)
    assert info.value.status_code == 422
    assert "CSV" in info.value.detail


def test_ibkr_and_pdf_suffixes_are_case_insensitive():
    import_export.validate_ibkr_filename("ACTIVITY.CSV")
    import_export.validate_ibkr_filename("trade_history.XLSX")
    import_export.validate_pdf_filename("STATEMENT.PDF")
    import_export.validate_cmb_fund_flow_filename("招商.Pdf")


# --------------------------------------------------------------------------- 口令上限

LONG_ASCII = "a" * (MAX_PASSWORD_BYTES + 1)
LONG_CHINESE = "密" * (MAX_PASSWORD_BYTES // 3 + 1)  # 25 个汉字 = 75 字节
EXACT_CHINESE = "密" * (MAX_PASSWORD_BYTES // 3)  # 24 个汉字 = 72 字节，正好可用


@pytest.mark.parametrize("password", [LONG_ASCII, LONG_CHINESE])
def test_new_password_schemas_reject_more_than_72_bytes(password):
    for build in (
        lambda: UserCreate(username="someone", password=password),
        lambda: UserPasswordUpdate(old_password="x", new_password=password),
        lambda: UserPasswordReset(new_password=password),
    ):
        with pytest.raises(ValidationError, match="密码过长"):
            build()


def test_72_bytes_is_accepted_and_hashes():
    assert UserPasswordReset(new_password=EXACT_CHINESE).new_password == EXACT_CHINESE
    hashed = get_password_hash(EXACT_CHINESE)
    assert verify_password(EXACT_CHINESE, hashed)
    assert verify_password(LONG_CHINESE, hashed) is False  # 超长输入直接判错，不抛


@pytest.mark.anyio
async def test_change_password_with_overlong_password_is_422(user_password):
    async with _client() as client:
        headers = await _auth_headers(client, user_password)
        response = await client.put(
            "/api/auth/me/password",
            json={"old_password": user_password, "new_password": LONG_CHINESE},
            headers=headers,
        )
    assert response.status_code == 422
    assert "密码过长" in response.text


def test_concurrent_imports_of_the_same_trade_book_it_once(monkeypatch):
    """PR #295 评审：导入改成同步 def 后在线程池里并发，疑似重复守卫（#190）不持锁读已入账行。

    同一笔成交的两份导出（成交价小数位不同 → row_hash 不同）几乎同时提交：按用户串行化之后，
    后到的一份能看到先到的那份已入账，扣作疑似重复；否则两边都看不到对方而双份入账。
    """
    import threading
    from decimal import Decimal

    from app.models.broker_account import BrokerAccount
    from app.models.broker_fund_flow import BrokerFundFlow
    from app.models.corporate_action import CorporateAction
    from app.models.holding import Holding
    from app.models.ibkr_activity_flow import IbkrActivityFlow
    from app.models.import_batch import ImportBatch
    from app.models.transaction import Transaction
    from app.services import ibkr_activity_importer as ibkr
    from tests.helpers import ibkr_csv, reset_tables

    models = (
        BrokerFundFlow,
        IbkrActivityFlow,
        Holding,
        CorporateAction,
        Transaction,
        ImportBatch,
    )
    monkeypatch.setattr(ibkr, "lookup_tushare_security_name", lambda symbol, market: None)
    db = SessionLocal()
    reset_tables(db, models)
    db.query(BrokerAccount).filter(BrokerAccount.account_name == "并发导入账户").delete()
    account = BrokerAccount(
        user_id=1,
        broker="IBKR",
        account_name="并发导入账户",
        account_number_masked="****0001",
        base_currency="USD",
    )
    db.add(account)
    db.commit()
    account_id = account.id
    exports = [
        ibkr_csv(
            "Transaction History,Data,2026-01-05,U***00001,APPLE INC,买,AAPL,"
            f"100.0,{price},USD,-1000.0,-1.0,-1001.0"
        )
        for price in ("10.0", "10.01")
    ]
    start = threading.Barrier(2)
    errors = []

    def run(contents, name):
        session = SessionLocal()
        try:
            start.wait()
            ibkr.import_ibkr_activity(session, 1, contents, name, broker_account_id=account_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            session.close()

    threads = [
        threading.Thread(target=run, args=(contents, f"export-{index}.csv"))
        for index, contents in enumerate(exports)
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert errors == []
        db.expire_all()
        booked = db.query(Transaction).filter(Transaction.broker_account_id == account_id).all()
        assert len(booked) == 1, "两份导出并发提交不得双份入账"
        assert booked[0].quantity == Decimal("100")
        held = (
            db.query(IbkrActivityFlow)
            .filter(IbkrActivityFlow.skip_reason == "suspected_duplicate")
            .count()
        )
        assert held == 1
    finally:
        reset_tables(db, models)
        db.query(BrokerAccount).filter(BrokerAccount.id == account_id).delete()
        db.commit()
        db.close()
