"""手工现金股息的税额口径（#220）。

此前通用 `POST /corporate-actions` 只把 `tax_rate` 原样入库，不算 `tax_withheld`；
`semantics.cash_dividend_amounts` 只读 `tax_withheld`，于是预扣税汇总漏税、
税后按全额进统计与对账。这里钉住：只给税率时推导税额、只给税额时原样、
编辑改总额清掉已存的税后净额、总额必填、导入只读记录仍 409。
"""

import hashlib
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.corporate_action import CorporateAction
from app.models.import_batch import ImportBatch
from app.models.user import User
from app.services.portfolio.semantics import cash_dividend_amounts
from tests.helpers import make_account, reset_tables

RESET_MODELS = (BrokerFundFlow, CorporateAction, ImportBatch, BrokerAccount)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        reset_tables(session, RESET_MODELS)
        yield session
        session.rollback()
        reset_tables(session, RESET_MODELS)
    finally:
        session.close()


@pytest.fixture
def api_user():
    session = SessionLocal()
    try:
        user = session.query(User).filter(User.username == "demo").one()
        original = user.hashed_password
        user.hashed_password = get_password_hash("known-api-password")
        session.commit()
        yield user.id
        user.hashed_password = original
        session.commit()
    finally:
        session.close()


async def _client_auth(client):
    login = await client.post(
        "/api/auth/token", json={"username": "demo", "password": "known-api-password"},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


BASE = {
    "symbol": "600036",
    "name": "招商银行",
    "market": "A股",
    "action_type": "CASH_DIVIDEND",
    "ex_date": "2026-07-01",
    "currency": "CNY",
}


def _amounts(db, action_id):
    db.expire_all()
    action = db.get(CorporateAction, action_id)
    return action, cash_dividend_amounts(action)


@pytest.mark.anyio
async def test_create_derives_tax_from_rate_and_keeps_net_derived(db, api_user):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _client_auth(client)

        # 只给税率：税额 = 总额 × 税率；税后净额不存，由 gross−tax 派生
        only_rate = await client.post("/api/corporate-actions", headers=auth, json={
            **BASE, "total_dividend": "1000", "tax_rate": "0.1",
        })
        assert only_rate.status_code == 201, only_rate.text
        action, (gross, tax, net) = _amounts(db, only_rate.json()["id"])
        assert action.net_dividend is None
        assert (gross, tax, net) == (Decimal("1000"), Decimal("100"), Decimal("900"))

        # 只给税额：原样入库，不被任何默认税率覆盖
        only_tax = await client.post("/api/corporate-actions", headers=auth, json={
            **BASE, "total_dividend": "500", "tax_withheld": "25",
        })
        assert only_tax.status_code == 201, only_tax.text
        action, (gross, tax, net) = _amounts(db, only_tax.json()["id"])
        assert action.tax_rate is None and action.net_dividend is None
        assert (gross, tax, net) == (Decimal("500"), Decimal("25"), Decimal("475"))

        # 显式税额优先于税率（两者不一致时以用户录入的税额为准）
        both = await client.post("/api/corporate-actions", headers=auth, json={
            **BASE, "total_dividend": "500", "tax_withheld": "0", "tax_rate": "0.1",
        })
        assert both.status_code == 201, both.text
        _, (_, tax, net) = _amounts(db, both.json()["id"])
        assert (tax, net) == (Decimal("0"), Decimal("500"))

        # 都不给：税额 0，数值与改动前一致
        neither = await client.post("/api/corporate-actions", headers=auth, json={
            **BASE, "total_dividend": "300",
        })
        assert neither.status_code == 201, neither.text
        _, (_, tax, net) = _amounts(db, neither.json()["id"])
        assert (tax, net) == (Decimal("0"), Decimal("300"))


@pytest.mark.anyio
async def test_cash_dividend_requires_total(db, api_user):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _client_auth(client)

        missing = await client.post("/api/corporate-actions", headers=auth, json={
            **BASE, "dividend_per_share": "0.5",
        })
        assert missing.status_code == 422

        shortcut_missing = await client.post(
            "/api/corporate-actions/cash-dividend", headers=auth,
            json={k: v for k, v in BASE.items() if k != "action_type"}
            | {"dividend_per_share": "0.5"},
        )
        assert shortcut_missing.status_code == 422

        created = await client.post("/api/corporate-actions", headers=auth, json={
            **BASE, "total_dividend": "100",
        })
        assert created.status_code == 201
        cleared = await client.put(
            f"/api/corporate-actions/{created.json()['id']}", headers=auth,
            json={"total_dividend": None},
        )
        assert cleared.status_code == 422


@pytest.mark.anyio
async def test_shortcut_and_generic_use_same_tax_formula(db, api_user):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _client_auth(client)
        shortcut = await client.post(
            "/api/corporate-actions/cash-dividend", headers=auth,
            json={k: v for k, v in BASE.items() if k != "action_type"}
            | {"dividend_per_share": "1", "total_dividend": "1234.56", "tax_rate": "0.2"},
        )
        generic = await client.post("/api/corporate-actions", headers=auth, json={
            **BASE, "dividend_per_share": "1", "total_dividend": "1234.56", "tax_rate": "0.2",
        })
        assert shortcut.status_code == generic.status_code == 201
        _, shortcut_amounts = _amounts(db, shortcut.json()["id"])
        _, generic_amounts = _amounts(db, generic.json()["id"])
        assert shortcut_amounts == generic_amounts


@pytest.mark.anyio
async def test_update_clears_stale_net_dividend(db, api_user):
    """分红建议入账的记录带显式 net；改总额/税额后必须按新值重新派生。"""
    action = CorporateAction(
        user_id=api_user, symbol="600036", name="招商银行", market="A股",
        action_type="CASH_DIVIDEND", ex_date=date(2026, 7, 1), currency="CNY",
        total_dividend=Decimal("1000"), tax_withheld=Decimal("0"),
        net_dividend=Decimal("1000"),
    )
    db.add(action)
    db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _client_auth(client)

        # 只改备注：总额/税额都没变，已存的 net 保留
        notes_only = await client.put(
            f"/api/corporate-actions/{action.id}", headers=auth, json={"notes": "核对过"},
        )
        assert notes_only.status_code == 200, notes_only.text
        stored, _ = _amounts(db, action.id)
        assert stored.net_dividend == Decimal("1000")

        # 前端编辑会把未变化的总额原样带回：值相同不算变化
        same_total = await client.put(
            f"/api/corporate-actions/{action.id}", headers=auth,
            json={"total_dividend": "1000.00", "tax_withheld": "0"},
        )
        assert same_total.status_code == 200
        stored, _ = _amounts(db, action.id)
        assert stored.net_dividend == Decimal("1000")

        # 改总额：旧 net 清空，按 gross−tax 派生
        changed = await client.put(
            f"/api/corporate-actions/{action.id}", headers=auth, json={"total_dividend": "1200"},
        )
        assert changed.status_code == 200, changed.text
        stored, (gross, tax, net) = _amounts(db, action.id)
        assert stored.net_dividend is None
        assert (gross, tax, net) == (Decimal("1200"), Decimal("0"), Decimal("1200"))

        # 只给税率：按合并后的总额推导税额
        with_rate = await client.put(
            f"/api/corporate-actions/{action.id}", headers=auth, json={"tax_rate": "0.1"},
        )
        assert with_rate.status_code == 200, with_rate.text
        _, (gross, tax, net) = _amounts(db, action.id)
        assert (gross, tax, net) == (Decimal("1200"), Decimal("120"), Decimal("1080"))

        # 显式 net 与总额一起给：尊重用户录入
        explicit = await client.put(
            f"/api/corporate-actions/{action.id}", headers=auth,
            json={"total_dividend": "1500", "tax_withheld": "150", "net_dividend": "1340"},
        )
        assert explicit.status_code == 200
        _, (_, _, net) = _amounts(db, action.id)
        assert net == Decimal("1340")


@pytest.mark.anyio
async def test_non_amount_edit_keeps_explicit_net_when_tax_is_null(db, api_user):
    """显式净额 + 空税额的合法记录：只改备注（前端会把空税额带成 0）不得改变净额。"""
    action = CorporateAction(
        user_id=api_user, symbol="600036", name="招商银行", market="A股",
        action_type="CASH_DIVIDEND", ex_date=date(2026, 7, 1), currency="CNY",
        total_dividend=Decimal("100"), tax_withheld=None, net_dividend=Decimal("90"),
    )
    db.add(action)
    db.commit()
    _, (_, _, net_before) = _amounts(db, action.id)
    assert net_before == Decimal("90")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _client_auth(client)

        # 编辑表单的真实 payload：总额原样、空税额提交为 0、不带 net_dividend、改了备注
        form_payload = await client.put(
            f"/api/corporate-actions/{action.id}", headers=auth,
            json={"total_dividend": "100", "tax_withheld": 0, "tax_rate": None, "notes": "核对"},
        )
        assert form_payload.status_code == 200, form_payload.text
        stored, (gross, tax, net) = _amounts(db, action.id)
        assert stored.net_dividend == Decimal("90")
        assert (gross, net) == (Decimal("100"), Decimal("90"))

        # 显式 null 税额同样不算变化
        null_tax = await client.put(
            f"/api/corporate-actions/{action.id}", headers=auth,
            json={"tax_withheld": None, "notes": "再核对"},
        )
        assert null_tax.status_code == 200, null_tax.text
        stored, _ = _amounts(db, action.id)
        assert stored.net_dividend == Decimal("90")

        # 真正改了税额（0 → 5）才清掉显式净额、按 gross−tax 派生
        real_change = await client.put(
            f"/api/corporate-actions/{action.id}", headers=auth, json={"tax_withheld": "5"},
        )
        assert real_change.status_code == 200, real_change.text
        stored, (_, tax, net) = _amounts(db, action.id)
        assert stored.net_dividend is None
        assert (tax, net) == (Decimal("5"), Decimal("95"))


@pytest.mark.anyio
async def test_imported_cash_dividend_stays_read_only(db, api_user):
    account = make_account(db, "招商证券", user_id=api_user, commit=True)
    batch = ImportBatch(
        user_id=api_user, broker_account_id=account.id, broker="招商证券",
        source_type="cmb_fund_flow", source_filename="a.pdf", status="COMPLETED", row_count=1,
    )
    db.add(batch)
    db.commit()
    action = CorporateAction(
        user_id=api_user, broker_account_id=account.id, import_batch_id=batch.id,
        symbol="600036", name="招商银行", market="A股", action_type="CASH_DIVIDEND",
        ex_date=date(2026, 7, 1), currency="CNY", total_dividend=Decimal("1000"),
    )
    db.add(action)
    db.commit()
    # 被来源流水链接但无批次的记录同样只读
    linked = CorporateAction(
        user_id=api_user, broker_account_id=account.id, symbol="600036", name="招商银行",
        market="A股", action_type="CASH_DIVIDEND", ex_date=date(2026, 7, 2), currency="CNY",
        total_dividend=Decimal("500"),
    )
    db.add(linked)
    db.commit()
    db.add(BrokerFundFlow(
        user_id=api_user, broker_account_id=account.id, import_batch_id=batch.id,
        source_filename="a.pdf", source_row_number=1,
        row_hash=hashlib.sha256(b"linked-dividend").hexdigest(), trade_date=date(2026, 7, 2),
        business_name="红利入账", security_code="600036", security_name="招商银行",
        trade_quantity=Decimal("0"), trade_price=Decimal("0"), amount=Decimal("500"),
        currency="CNY", corporate_action_id=linked.id,
    ))
    db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        auth = await _client_auth(client)
        for target in (action, linked):
            blocked = await client.put(
                f"/api/corporate-actions/{target.id}", headers=auth,
                json={"total_dividend": "2000", "tax_rate": "0.1"},
            )
            assert blocked.status_code == 409, blocked.text

        listed = await client.get("/api/corporate-actions", headers=auth)
        read_only = {row["id"]: row["read_only"] for row in listed.json()}
        assert read_only[action.id] is True and read_only[linked.id] is True
    db.expire_all()
    assert db.get(CorporateAction, action.id).total_dividend == Decimal("1000")
