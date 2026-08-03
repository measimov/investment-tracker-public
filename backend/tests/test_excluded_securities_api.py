"""排除清单 API：CRUD、唯一约束与所有权隔离。"""

import httpx
import pytest

from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.security_rule import SecurityRule
from app.models.user import User



@pytest.fixture
def api_users():
    db = SessionLocal()
    try:
        demo = db.query(User).filter(User.username == "demo").one()
        admin = db.query(User).filter(User.username == "admin").one()
        originals = {u.id: u.hashed_password for u in (demo, admin)}
        for u in (demo, admin):
            u.hashed_password = get_password_hash("excluded-api-password")
        db.query(SecurityRule).delete()
        db.commit()
        yield
        for u in (demo, admin):
            u.hashed_password = originals[u.id]
        db.query(SecurityRule).delete()
        db.commit()
    finally:
        db.close()


async def _token(client, username):
    response = await client.post(
        "/api/auth/token",
        json={"username": username, "password": "excluded-api-password"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


@pytest.mark.anyio
async def test_excluded_securities_crud_and_ownership(api_users):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        user_auth = {"Authorization": f"Bearer {await _token(client, 'demo')}"}
        admin_auth = {"Authorization": f"Bearer {await _token(client, 'admin')}"}

        created = await client.post(
            "/api/excluded-securities",
            json={"symbol": "511880", "market": "A股", "note": "货币基金"},
            headers=user_auth,
        )
        assert created.status_code == 201
        record = created.json()
        assert record["symbol"] == "511880"

        # 重复：409
        duplicate = await client.post(
            "/api/excluded-securities",
            json={"symbol": "511880", "market": "A股"},
            headers=user_auth,
        )
        assert duplicate.status_code == 409

        # 未知市场：422
        bad_market = await client.post(
            "/api/excluded-securities",
            json={"symbol": "511880", "market": "火星"},
            headers=user_auth,
        )
        assert bad_market.status_code == 422

        listed = await client.get("/api/excluded-securities", headers=user_auth)
        assert [row["symbol"] for row in listed.json()] == ["511880"]

        # 所有权隔离：admin 看不到、删不掉 demo 的记录
        other_list = await client.get("/api/excluded-securities", headers=admin_auth)
        assert other_list.json() == []
        stolen_delete = await client.delete(
            f"/api/excluded-securities/{record['id']}", headers=admin_auth
        )
        assert stolen_delete.status_code == 404

        deleted = await client.delete(
            f"/api/excluded-securities/{record['id']}", headers=user_auth
        )
        assert deleted.status_code == 204
        assert (await client.get("/api/excluded-securities", headers=user_auth)).json() == []


@pytest.mark.anyio
async def test_preview_response_serializes_skipped_excluded_rows(api_users, monkeypatch):
    """API 层回归：skipped_excluded_rows 必须进 BrokerImportResult 序列化结果，
    否则预览 UI 看不到排除生效情况（Pydantic 会静默丢弃未声明字段）。"""
    from datetime import date
    from decimal import Decimal

    from app.models.broker_account import BrokerAccount
    from app.services import cmb_fund_flow_importer as cmb_importer

    db = SessionLocal()
    try:
        demo = db.query(User).filter(User.username == "demo").one()
        db.query(BrokerAccount).filter(
            BrokerAccount.account_name == "排除清单预览测试"
        ).delete()
        account = BrokerAccount(
            user_id=demo.id,
            broker="招商证券",
            account_name="排除清单预览测试",
            account_number_masked="****A123",
            base_currency="CNY",
        )
        db.add(account)
        db.add(SecurityRule(rule_type="EXCLUDE", user_id=demo.id, symbol="511880", market="A股"))
        db.commit()
        db.refresh(account)
        account_id = account.id
    finally:
        db.close()

    def make_flow(row_hash, security_code):
        return cmb_importer.ParsedFlow(
            source_row_number=2,
            row_hash=row_hash,
            security_code=security_code,
            security_name="测试",
            currency="CNY",
            trade_date=date(2026, 1, 2),
            trade_price=Decimal("10"),
            trade_quantity=Decimal("100"),
            amount=Decimal("-1001"),
            cash_balance=Decimal("10000"),
            remaining_quantity=Decimal("100"),
            contract_number="c-2",
            serial_number="s-2",
            business_name="证券买入",
            stamp_tax=Decimal("0"),
            commission=Decimal("1"),
            handling_fee=Decimal("0"),
            management_fee=Decimal("0"),
            settlement_fee=Decimal("0"),
            transfer_fee=Decimal("0"),
            other_fee=Decimal("0"),
            shareholder_code="A123",
            notes=None,
        )

    flows = [make_flow("1" * 64, "600000"), make_flow("2" * 64, "511880")]
    monkeypatch.setattr(
        cmb_importer,
        "parse_rows",
        lambda contents, filename, **kwargs: (flows, {"证券买入": 2}, 2, []),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = {"Authorization": f"Bearer {await _token(client, 'demo')}"}
        response = await client.post(
            "/api/import/cmb-fund-flows/preview",
            headers=auth,
            data={"broker_account_id": str(account_id)},
            files={"file": ("cmb.pdf", b"%PDF", "application/pdf")},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["skipped_excluded_rows"] == 1
        assert body["eligible_trade_rows"] == 1

    cleanup = SessionLocal()
    try:
        cleanup.query(BrokerAccount).filter(BrokerAccount.id == account_id).delete()
        cleanup.commit()
    finally:
        cleanup.close()


@pytest.mark.anyio
async def test_whitespace_only_symbol_is_rejected(api_users):
    """min_length 在 strip 前校验：全空格 symbol 必须被 schema validator 拦下。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = {"Authorization": f"Bearer {await _token(client, 'demo')}"}
        for payload in (
            {"symbol": "   ", "market": "A股"},
            {"symbol": "511880", "market": "  "},
        ):
            response = await client.post(
                "/api/excluded-securities", json=payload, headers=auth
            )
            assert response.status_code == 422, payload
        assert (await client.get("/api/excluded-securities", headers=auth)).json() == []
