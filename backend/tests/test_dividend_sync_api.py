"""分红建议 API：job 触发、列表过滤、接受/忽略/恢复状态机、所有权、事件查询。"""

from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from app.core.timeutil import local_today
from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.background_job import BackgroundJob
from app.models.corporate_action import CorporateAction
from app.models.corporate_action_suggestion import CorporateActionSuggestion
from app.models.holding import Holding
from app.models.security_event import SecurityEvent
from app.models.security_catalog import SecurityCatalogEntry
from app.models.transaction import Transaction
from app.models.user import User

from .helpers import reset_tables

RESET_MODELS = [
    CorporateActionSuggestion,
    SecurityEvent,
    CorporateAction,
    Holding,
    Transaction,
]

TODAY = local_today()


@pytest.fixture
def api_users():
    db = SessionLocal()
    try:
        demo = db.query(User).filter(User.username == "demo").one()
        admin = db.query(User).filter(User.username == "admin").one()
        originals = {u.id: u.hashed_password for u in (demo, admin)}
        for u in (demo, admin):
            u.hashed_password = get_password_hash("dividend-api-password")
        reset_tables(db, RESET_MODELS)
        db.query(BackgroundJob).filter(BackgroundJob.job_type == "dividend_sync").delete()
        db.commit()
        yield {"demo": demo.id, "admin": admin.id}
        for u in (demo, admin):
            u.hashed_password = originals[u.id]
        reset_tables(db, RESET_MODELS)
        db.query(BackgroundJob).filter(BackgroundJob.job_type == "dividend_sync").delete()
        db.commit()
    finally:
        db.close()


async def _auth(client, username):
    response = await client.post(
        "/api/auth/token",
        json={"username": username, "password": "dividend-api-password"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _seed_suggestion(user_id, **overrides):
    db = SessionLocal()
    try:
        values = {
            "user_id": user_id,
            "symbol": "600036",
            "name": "招商银行",
            "market": "A股",
            "action_type": "CASH_DIVIDEND",
            "ex_date": TODAY - timedelta(days=10),
            "pay_date": TODAY - timedelta(days=9),
            "currency": "CNY",
            "cash_div_pre_tax": Decimal("1.0"),
            "cash_div_after_tax": Decimal("0.9"),
            "record_date_quantity": Decimal("1000"),
            "quantity_basis": "per_account",
            "estimated_total_dividend": Decimal("1000"),
            "status": "NEW",
        }
        values.update(overrides)
        suggestion = CorporateActionSuggestion(**values)
        db.add(suggestion)
        db.commit()
        db.refresh(suggestion)
        return suggestion.id
    finally:
        db.close()


@pytest.mark.anyio
async def test_suggestion_lifecycle_and_ownership(api_users):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        user_auth = await _auth(client, "demo")
        admin_auth = await _auth(client, "admin")
        suggestion_id = _seed_suggestion(api_users["demo"])

        # 列表默认 NEW+MATCHED
        listed = await client.get("/api/corporate-actions/suggestions", headers=user_auth)
        assert [row["id"] for row in listed.json()] == [suggestion_id]
        count = await client.get("/api/corporate-actions/suggestions/count", headers=user_auth)
        assert count.json()["total"] == 1

        # 所有权隔离：admin 看不到、动不了
        other = await client.get("/api/corporate-actions/suggestions", headers=admin_auth)
        assert other.json() == []
        forbidden = await client.post(
            f"/api/corporate-actions/suggestions/{suggestion_id}/ignore",
            headers=admin_auth,
        )
        assert forbidden.status_code == 404

        # 忽略 → 默认列表消失 → 恢复
        ignored = await client.post(
            f"/api/corporate-actions/suggestions/{suggestion_id}/ignore",
            headers=user_auth,
        )
        assert ignored.json()["status"] == "IGNORED"
        assert (
            await client.get("/api/corporate-actions/suggestions", headers=user_auth)
        ).json() == []
        assert (
            await client.get("/api/corporate-actions/suggestions?status=IGNORED", headers=user_auth)
        ).json()[0]["id"] == suggestion_id

        restored = await client.post(
            f"/api/corporate-actions/suggestions/{suggestion_id}/restore",
            headers=user_auth,
        )
        assert restored.json()["status"] == "NEW"
        # 非 IGNORED 恢复 → 409
        again = await client.post(
            f"/api/corporate-actions/suggestions/{suggestion_id}/restore",
            headers=user_auth,
        )
        assert again.status_code == 409


@pytest.mark.anyio
async def test_matched_suggestion_blocked_and_restores_to_matched(api_users):
    """[评审回归] MATCHED 不可接受；忽略→恢复回 MATCHED 而非可入账 NEW。"""
    db = SessionLocal()
    try:
        recorded = CorporateAction(
            user_id=api_users["demo"],
            symbol="600036",
            market="A股",
            action_type="CASH_DIVIDEND",
            ex_date=TODAY - timedelta(days=5),
            total_dividend=Decimal("1000"),
            tax_withheld=Decimal("0"),
            net_dividend=Decimal("1000"),
            currency="CNY",
        )
        db.add(recorded)
        db.commit()
        db.refresh(recorded)
        recorded_id = recorded.id
    finally:
        db.close()
    suggestion_id = _seed_suggestion(
        api_users["demo"],
        status="MATCHED",
        matched_corporate_action_id=recorded_id,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        user_auth = await _auth(client, "demo")

        blocked = await client.post(
            f"/api/corporate-actions/suggestions/{suggestion_id}/accept",
            json={},
            headers=user_auth,
        )
        assert blocked.status_code == 409
        assert "双计" in blocked.json()["detail"]

        # 忽略后恢复：回 MATCHED、关联保留，不会洗成可入账的 NEW
        await client.post(
            f"/api/corporate-actions/suggestions/{suggestion_id}/ignore",
            headers=user_auth,
        )
        restored = await client.post(
            f"/api/corporate-actions/suggestions/{suggestion_id}/restore",
            headers=user_auth,
        )
        assert restored.json()["status"] == "MATCHED"
        assert restored.json()["matched_corporate_action_id"] == recorded_id

        # 恢复后仍不可接受
        still_blocked = await client.post(
            f"/api/corporate-actions/suggestions/{suggestion_id}/accept",
            json={},
            headers=user_auth,
        )
        assert still_blocked.status_code == 409


@pytest.mark.anyio
async def test_accept_explicit_null_account_clears_attribution(api_users):
    """[评审回归] 请求体显式 broker_account_id: null → 归属清空，
    不沿用建议原账户；省略该键 → 沿用建议原账户。"""
    from app.models.broker_account import BrokerAccount

    db = SessionLocal()
    try:
        account = BrokerAccount(
            user_id=api_users["demo"],
            broker="测试券商",
            account_name="测试账户",
            base_currency="CNY",
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        account_id = account.id
    finally:
        db.close()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        user_auth = await _auth(client, "demo")

        # 显式 null → 归属清空
        s1 = _seed_suggestion(
            api_users["demo"],
            broker_account_id=account_id,
            action_type="STOCK_DIVIDEND",
            stk_div_per_share=Decimal("0.1"),
        )
        cleared = await client.post(
            f"/api/corporate-actions/suggestions/{s1}/accept",
            json={"broker_account_id": None},
            headers=user_auth,
        )
        assert cleared.status_code == 200
        assert cleared.json()["broker_account_id"] is None

        # 省略键 → 沿用建议原账户（另一除权日避免与 s1 判重命中）
        s2 = _seed_suggestion(
            api_users["demo"],
            broker_account_id=account_id,
            action_type="STOCK_DIVIDEND",
            stk_div_per_share=Decimal("0.1"),
            symbol="600519",
            ex_date=TODAY - timedelta(days=100),
            pay_date=TODAY - timedelta(days=99),
        )
        kept = await client.post(
            f"/api/corporate-actions/suggestions/{s2}/accept",
            json={},
            headers=user_auth,
        )
        assert kept.status_code == 200
        assert kept.json()["broker_account_id"] == account_id


@pytest.mark.anyio
async def test_accept_rejects_tax_over_gross(api_users):
    """[评审回归] 税额 > 总额 → 409 且零残留。"""
    suggestion_id = _seed_suggestion(api_users["demo"])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        user_auth = await _auth(client, "demo")
        rejected = await client.post(
            f"/api/corporate-actions/suggestions/{suggestion_id}/accept",
            json={"tax_withheld": "1200"},
            headers=user_auth,
        )
        assert rejected.status_code == 409
        assert "不能通过接受公告" in rejected.json()["detail"]

    db = SessionLocal()
    try:
        assert db.query(CorporateAction).count() == 0
        assert db.query(CorporateActionSuggestion).one().status == "NEW"
    finally:
        db.close()


@pytest.mark.anyio
async def test_accept_creates_action_and_rejects_double_accept(api_users):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        user_auth = await _auth(client, "demo")
        suggestion_id = _seed_suggestion(api_users["demo"])

        accepted = await client.post(
            f"/api/corporate-actions/suggestions/{suggestion_id}/accept",
            json={"tax_withheld": "100"},
            headers=user_auth,
        )
        assert accepted.status_code == 409
        assert "不能通过接受公告" in accepted.json()["detail"]
        detail = await client.get("/api/corporate-actions/suggestions", headers=user_auth)
        assert detail.json()[0]["status"] == "NEW"
        assert detail.json()[0]["created_corporate_action_id"] is None
        actions = await client.get("/api/corporate-actions", headers=user_auth)
        assert actions.json() == []


@pytest.mark.anyio
async def test_sync_job_endpoints(api_users, monkeypatch):
    from app.services import dividend_sync_service as svc_mod

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        user_auth = await _auth(client, "demo")

        # 未配 token 也可入队（港股走披露易不依赖 Tushare；A/B 股在 job 内跳过并标注）；
        # 执行内联跑（空持仓 → 立即成功）
        monkeypatch.setattr(svc_mod.settings, "tushare_token", "")
        monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
        started = await client.post("/api/corporate-actions/dividend-sync-jobs", headers=user_auth)
        assert started.status_code == 200
        job = started.json()
        assert job["status"] in ("queued", "running", "succeeded")

        polled = await client.get(
            f"/api/corporate-actions/dividend-sync-jobs/{job['id']}", headers=user_auth
        )
        assert polled.status_code == 200

        missing = await client.get(
            "/api/corporate-actions/dividend-sync-jobs/nonexistent", headers=user_auth
        )
        assert missing.status_code == 404


@pytest.mark.anyio
async def test_security_events_filtered_by_holdings(api_users):
    db = SessionLocal()
    try:
        db.add(
            Holding(
                user_id=api_users["demo"],
                symbol="600036",
                name="招商银行",
                market="A股",
                quantity=Decimal("100"),
                avg_cost=Decimal("30"),
                total_cost=Decimal("3000"),
                currency="CNY",
            )
        )
        db.add_all(
            [
                SecurityEvent(
                    symbol="600036",
                    market="A股",
                    event_type="EARNINGS_DISCLOSURE",
                    event_date=TODAY + timedelta(days=10),
                    source="tushare-disclosure_date",
                    payload={"period": "20260630"},
                ),
                # 非持仓标的的事件不应返回
                SecurityEvent(
                    symbol="600519",
                    market="A股",
                    event_type="SHARE_UNLOCK",
                    event_date=TODAY + timedelta(days=5),
                    source="tushare-share_float",
                    payload={},
                ),
                # 超出窗口的事件不返回
                SecurityEvent(
                    symbol="600036",
                    market="A股",
                    event_type="DIVIDEND_PLAN",
                    event_date=TODAY + timedelta(days=200),
                    source="tushare-dividend",
                    payload={},
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        user_auth = await _auth(client, "demo")
        events = await client.get(
            "/api/corporate-actions/security-events?days_ahead=90", headers=user_auth
        )
        assert events.status_code == 200
        rows = events.json()
        assert len(rows) == 1
        assert rows[0]["symbol"] == "600036"
        assert rows[0]["event_type"] == "EARNINGS_DISCLOSURE"

        # 显式 symbol 查询不受持仓限制（详情页用）
        by_symbol = await client.get(
            "/api/corporate-actions/security-events?symbol=600519&market=A股&days_ahead=90",
            headers=user_auth,
        )
        assert len(by_symbol.json()) == 1


@pytest.mark.anyio
async def test_suggestion_names_are_read_only_scoped_and_paginated(api_users):
    user_id = api_users["demo"]
    specs = [
        ("UI-CATALOG", "美股", None, "目录展示名"),
        ("UI-SAME", "A股", None, "当前用户A股名"),
        ("UI-SAME", "港股", None, "当前用户港股名"),
        ("UI-OTHER", "美股", None, None),
        ("UI-UNKNOWN", "美股", None, None),
        ("UI-CATALOG", "美股", "公告既有名", "公告既有名"),
    ]
    ids = [
        _seed_suggestion(
            user_id,
            symbol=symbol,
            market=market,
            name=name,
            ex_date=TODAY - timedelta(days=20 + index),
            estimated_total_dividend=Decimal("123.456789"),
        )
        for index, (symbol, market, name, _) in enumerate(specs)
    ]
    db = SessionLocal()
    try:
        db.add(
            SecurityCatalogEntry(
                symbol="UI-CATALOG", market="美股", name="目录展示名", source="test-fixture"
            )
        )
        for owner, symbol, market, name in [
            (user_id, "UI-SAME", "A股", "当前用户A股名"),
            (user_id, "UI-SAME", "港股", "当前用户港股名"),
            (api_users["admin"], "UI-OTHER", "美股", "另一用户私有名"),
        ]:
            db.add(
                Holding(
                    user_id=owner,
                    symbol=symbol,
                    market=market,
                    name=name,
                    quantity=Decimal("1"),
                    avg_cost=Decimal("1"),
                    total_cost=Decimal("1"),
                )
            )
        db.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            auth = await _auth(client, "demo")
            listed = await client.get("/api/corporate-actions/suggestions", headers=auth)
            assert listed.status_code == 200
            rows = listed.json()
            assert [row["id"] for row in rows] == ids
            assert [row["name"] for row in rows] == [spec[3] for spec in specs]
            assert all(
                Decimal(row["estimated_total_dividend"]) == Decimal("123.456789")
                and Decimal(row["cash_div_pre_tax"]) == Decimal("1.0")
                for row in rows
            )
            page = await client.get(
                "/api/corporate-actions/suggestions?skip=1&limit=2", headers=auth
            )
            assert page.status_code == 200
            assert page.json() == rows[1:3]
            pending_page = await client.get(
                "/api/corporate-actions/suggestions?pending_only=true&skip=1&limit=2",
                headers=auth,
            )
            assert pending_page.status_code == 200
            assert pending_page.json() == rows[1:3]
        db.expire_all()
        stored = (
            db.query(CorporateActionSuggestion).filter(CorporateActionSuggestion.id.in_(ids)).all()
        )
        assert {row.id: row.name for row in stored} == {
            row_id: spec[2] for row_id, spec in zip(ids, specs)
        }
    finally:
        db.query(SecurityCatalogEntry).filter(
            SecurityCatalogEntry.symbol == "UI-CATALOG", SecurityCatalogEntry.market == "美股"
        ).delete()
        db.commit()
        db.close()
