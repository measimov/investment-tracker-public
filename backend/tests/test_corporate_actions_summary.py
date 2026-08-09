"""公司行动统计摘要：多币种股息必须 CNY 折算，与统计分析页同口径。

背景：原实现把 CNY/HKD/USD 原币金额直接相加（重建库实测 43,414 vs 统计页
59,451），且 net_dividend 为 NULL 时计 0 而非 gross − tax 兜底。
"""

from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.corporate_action import CorporateAction
from app.models.exchange_rate import ExchangeRate
from app.models.user import User
from app.services.statistics import get_dividend_summary



@pytest.fixture
def dividend_scenario():
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "demo").one()
        original_password = user.hashed_password
        user.hashed_password = get_password_hash("dividend-summary-password")
        db.query(CorporateAction).filter(CorporateAction.user_id == user.id).delete()
        db.query(ExchangeRate).delete()
        db.add(ExchangeRate(
            from_currency="HKD", to_currency="CNY", rate=Decimal("0.9"),
            effective_date=date(2026, 1, 1), source="test", is_active=True,
        ))
        # CNY：净额显式给出
        db.add(CorporateAction(
            user_id=user.id, symbol="600000", name="甲", market="A股",
            action_type="CASH_DIVIDEND", ex_date=date(2026, 3, 1),
            total_dividend=Decimal("100"), tax_withheld=Decimal("10"),
            net_dividend=Decimal("90"), currency="CNY",
        ))
        # HKD：net 为 NULL → 须按 gross − tax 兜底并折算（(200−20)×0.9=162）
        db.add(CorporateAction(
            user_id=user.id, symbol="00700", name="乙", market="港股",
            action_type="CASH_DIVIDEND", ex_date=date(2026, 4, 1),
            total_dividend=Decimal("200"), tax_withheld=Decimal("20"),
            net_dividend=None, currency="HKD",
        ))
        # 显式 net=0 且 gross−tax=40≠0：旧的 `net or (gross−tax)` 实现会把
        # 显式 0 吞成 40（净额总计 292 而非 252）——本组数据必须能让旧实现变红
        db.add(CorporateAction(
            user_id=user.id, symbol="600036", name="丙", market="A股",
            action_type="CASH_DIVIDEND", ex_date=date(2026, 5, 1),
            total_dividend=Decimal("50"), tax_withheld=Decimal("10"),
            net_dividend=Decimal("0"), currency="CNY",
        ))
        # THB 无汇率：不得以原值混入 CNY 总额，应剔除并记录缺汇率币种
        db.add(CorporateAction(
            user_id=user.id, symbol="THB001", name="丁", market="美股",
            action_type="CASH_DIVIDEND", ex_date=date(2026, 6, 1),
            total_dividend=Decimal("300"), tax_withheld=Decimal("30"),
            net_dividend=None, currency="THB",
        ))
        db.commit()
        yield user.id
        user.hashed_password = original_password
        db.query(CorporateAction).filter(CorporateAction.user_id == user.id).delete()
        db.query(ExchangeRate).delete()
        db.commit()
    finally:
        db.close()


@pytest.mark.anyio
async def test_summary_converts_to_cny_and_matches_statistics(dividend_scenario):
    user_id = dividend_scenario
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        token_response = await client.post(
            "/api/auth/token",
            json={"username": "demo", "password": "dividend-summary-password"},
        )
        headers = {"Authorization": f"Bearer {token_response.json()['access_token']}"}
        response = await client.get("/api/corporate-actions/statistics/summary", headers=headers)
        assert response.status_code == 200
        cash = response.json()["cash_dividends"]

    # 手算（THB 缺汇率剔除）：gross = 100 + 50 + 200×0.9 = 330；
    # tax = 10 + 10 + 20×0.9 = 38；net = 90 + 0(显式) + 162(兜底) = 252
    # （旧 `or` 实现会得到 252 + 40 = 292，本断言即锁死该缺陷）
    assert cash["base_currency"] == "CNY"
    assert cash["count"] == 4
    assert cash["total_dividend"] == pytest.approx(330.0)
    assert cash["total_tax"] == pytest.approx(38.0)
    assert cash["net_dividend"] == pytest.approx(252.0)
    assert cash["missing_rate_currencies"] == ["THB"]

    # 原币明细保留（含被剔除折算的 THB 与显式 net=0）
    assert cash["by_currency"]["CNY"]["net_dividend"] == pytest.approx(90.0)
    assert cash["by_currency"]["HKD"]["net_dividend"] == pytest.approx(180.0)
    assert cash["by_currency"]["THB"]["net_dividend"] == pytest.approx(270.0)

    # 与统计分析页股息摘要完全同口径（无筛选时必须相等）
    db = SessionLocal()
    try:
        stats = get_dividend_summary(db, user_id)
    finally:
        db.close()
    assert cash["total_dividend"] == pytest.approx(stats["total_dividend_gross_cny"])
    assert cash["total_tax"] == pytest.approx(stats["total_tax_cny"])
    assert cash["net_dividend"] == pytest.approx(stats["total_dividend_net_cny"])
    assert cash["missing_rate_currencies"] == stats["missing_rate_currencies"]


def test_cash_dividend_amounts_distinguishes_zero_from_null():
    """聚焦单测：显式 0 与 NULL 的区分是共享 helper 的核心契约。"""
    from types import SimpleNamespace

    from app.services.portfolio.semantics import cash_dividend_amounts

    explicit_zero = SimpleNamespace(
        total_dividend=Decimal("50"), tax_withheld=Decimal("10"), net_dividend=Decimal("0")
    )
    assert cash_dividend_amounts(explicit_zero) == (Decimal("50"), Decimal("10"), Decimal("0"))

    null_net = SimpleNamespace(
        total_dividend=Decimal("50"), tax_withheld=Decimal("10"), net_dividend=None
    )
    assert cash_dividend_amounts(null_net) == (Decimal("50"), Decimal("10"), Decimal("40"))
