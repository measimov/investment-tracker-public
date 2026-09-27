"""当日 / 本月 / 本年损益（权益仓口径，与 TTWR 曲线同一算法）的手算向量。"""

from datetime import date
from decimal import Decimal

import pytest

from app.database import SessionLocal
from app.models.corporate_action import CorporateAction
from app.models.exchange_rate import ExchangeRate
from app.models.holding import Holding
from app.models.security_price import SecurityPrice
from app.models.transaction import Transaction
from app.services.statistics import calculate_period_pnl
from app.services.statistics.period_pnl import period_start, summarize_curve
from tests.helpers import add_transaction, reset_tables

RESET_MODELS = (SecurityPrice, Holding, CorporateAction, Transaction, ExchangeRate)
TODAY = date(2026, 9, 26)


def _txn(db, **overrides):
    values = {"symbol": "600000", "name": "审计标的", "market": "A股", "currency": "CNY"}
    values.update(overrides)
    return add_transaction(db, **values)


def _price(db, day, close):
    db.add(SecurityPrice(symbol="600000", market="A股", ts_code="600000.SH", price_date=day,
                         currency="CNY", close_price=close, source="audit"))


@pytest.fixture
def db():
    session = SessionLocal()
    reset_tables(session, RESET_MODELS)
    try:
        yield session
    finally:
        reset_tables(session, RESET_MODELS)
        session.close()


def test_period_starts():
    assert period_start("daily", TODAY) == TODAY
    assert period_start("mtd", TODAY) == date(2026, 9, 1)
    assert period_start("ytd", TODAY) == date(2026, 1, 1)
    with pytest.raises(ValueError):
        period_start("wtd", TODAY)


def test_daily_mtd_ytd_pnl_hand_computed(db):
    """去年 12/15 买 100@10；收盘 12/31=10、8/31=12、9/25=13；9/10 卖 50@14；9/15 现金分红净 20；
    今天（9/26）现价 13.5。
    本年：期初 100×10=1000，期末 50×13.5=675，流出 = 卖出 700 + 分红 20 → 675+720−1000 = 395
    本月：期初 100×12=1200 → 675+720−1200 = 195
    当日：期初 50×13（9/25 收盘）=650，期末 675 → 25"""
    _txn(db, transaction_type="BUY", quantity=Decimal("100"), price=Decimal("10"), fee=Decimal("0"),
         transaction_date=date(2025, 12, 15))
    _txn(db, transaction_type="SELL", quantity=Decimal("50"), price=Decimal("14"), fee=Decimal("0"),
         transaction_date=date(2026, 9, 10))
    db.add(CorporateAction(user_id=1, symbol="600000", name="审计标的", market="A股",
                           action_type="CASH_DIVIDEND", ex_date=date(2026, 9, 15),
                           payment_date=date(2026, 9, 15), total_dividend=Decimal("20"),
                           tax_withheld=Decimal("0"), net_dividend=Decimal("20"), currency="CNY"))
    _price(db, date(2025, 12, 31), Decimal("10"))
    _price(db, date(2026, 8, 31), Decimal("12"))
    _price(db, date(2026, 9, 25), Decimal("13"))
    db.commit()

    result = calculate_period_pnl(db, 1, {"600000:A股": 13.5}, today=TODAY)
    periods = result["periods"]
    assert result["as_of"] == "2026-09-26"
    assert result["methodology"]["scope"] == "invested_securities_only"

    assert periods["ytd"]["start_date"] == "2026-01-01"
    assert periods["ytd"]["opening_market_value_cny"] == pytest.approx(1000)
    assert periods["ytd"]["closing_market_value_cny"] == pytest.approx(675)
    assert periods["ytd"]["pnl_cny"] == pytest.approx(395)
    assert periods["ytd"]["dividend_income_cny"] == pytest.approx(20)

    assert periods["mtd"]["opening_market_value_cny"] == pytest.approx(1200)
    assert periods["mtd"]["pnl_cny"] == pytest.approx(195)

    assert periods["daily"]["opening_market_value_cny"] == pytest.approx(650)
    assert periods["daily"]["pnl_cny"] == pytest.approx(25)
    assert periods["daily"]["return_rate"] == pytest.approx(25 / 650 * 100, abs=1e-3)
    assert all(p["status"] == "exact" for p in periods.values())
    assert result["data_quality"]["warnings"] == []
    # 各区间都满足：损益 = 期末 + 流出 − 期初 − 流入
    for summary in periods.values():
        identity = (summary["closing_market_value_cny"] + summary["cash_out_cny"]
                    - summary["opening_market_value_cny"] - summary["cash_in_cny"])
        assert summary["pnl_cny"] == pytest.approx(identity, abs=0.02)


def test_buy_today_counts_only_the_move_after_purchase(db):
    """今天才买：当日损益只算买入后的涨跌（100@10 买入、现价 10.5 → +50），不把买入金额算成亏损。"""
    _txn(db, transaction_type="BUY", quantity=Decimal("100"), price=Decimal("10"), fee=Decimal("0"),
         transaction_date=TODAY)
    db.commit()
    daily = calculate_period_pnl(db, 1, {"600000:A股": 10.5}, today=TODAY)["periods"]["daily"]
    assert daily["opening_market_value_cny"] == 0
    assert daily["cash_in_cny"] == pytest.approx(1000)
    assert daily["pnl_cny"] == pytest.approx(50)


def test_no_transactions_returns_empty_periods(db):
    result = calculate_period_pnl(db, 1, {}, today=TODAY)
    assert set(result["periods"]) == {"daily", "mtd", "ytd"}
    assert all(p["pnl_cny"] == 0 and p["return_rate"] is None for p in result["periods"].values())
    assert all(p["status"] == "exact" for p in result["periods"].values())


def test_opening_position_only_account_is_not_empty(db):
    """评审 P1：只有期初建仓（OPENING_POSITION，无 BUY 交易）的账户不是空账户。
    去年转入 100 股（成本 10），收盘 12/31=10、8/31=12、9/25=19，现价 20：
    本年 100×(20−10)=1000、本月 100×(20−12)=800、当日 100×(20−19)=100。"""
    db.add(CorporateAction(user_id=1, symbol="600000", name="审计标的", market="A股",
                           action_type="OPENING_POSITION", ex_date=date(2025, 12, 1), currency="CNY",
                           adjusted_quantity=Decimal("100"), adjusted_cost_per_share=Decimal("10")))
    db.add(Holding(user_id=1, symbol="600000", name="审计标的", market="A股", quantity=Decimal("100"),
                   avg_cost=Decimal("10"), total_cost=Decimal("1000"), currency="CNY"))
    _price(db, date(2025, 12, 31), Decimal("10"))
    _price(db, date(2026, 8, 31), Decimal("12"))
    _price(db, date(2026, 9, 25), Decimal("19"))
    db.commit()
    periods = calculate_period_pnl(db, 1, {"600000:A股": 20.0}, today=TODAY)["periods"]
    assert periods["daily"]["closing_market_value_cny"] == pytest.approx(2000)
    assert periods["daily"]["pnl_cny"] == pytest.approx(100)
    assert periods["mtd"]["pnl_cny"] == pytest.approx(800)
    assert periods["ytd"]["pnl_cny"] == pytest.approx(1000)
    assert all(p["status"] == "exact" for p in periods.values())


def test_stale_trade_price_basis_is_flagged_not_shown_as_daily_gain(db):
    """评审 P1 复现：去年 1/1 买 100@10、从未有收盘价、今天现价 20。期初只能按一年多前的成交价
    估值——当日「+1000、100%」其实是一年多的累计涨幅。必须标为估算并列出基准日，而不是正常当日收益。"""
    _txn(db, transaction_type="BUY", quantity=Decimal("100"), price=Decimal("10"), fee=Decimal("0"),
         transaction_date=date(2025, 1, 1))
    db.commit()
    result = calculate_period_pnl(db, 1, {"600000:A股": 20.0}, today=TODAY)
    for key in ("daily", "mtd", "ytd"):
        period = result["periods"][key]
        assert period["status"] == "estimated", key
        assert period["stale_opening_basis"] == [
            {"symbol": "600000", "market": "A股", "basis_date": "2025-01-01", "basis_source": "transaction"}
        ]
    warnings = result["data_quality"]["warnings"]
    assert any("当日损益为估算" in w and "2025-01-01成交价" in w for w in warnings)


def test_missing_close_only_at_month_start_marks_that_period_only(db):
    """本年初有收盘（12/31），但 12/31 之后行情断更：年度基准可靠；本月与当日的期初基准是
    9 个月前的收盘 → 估算（典型的行情断更，如此前 PDD 停在 7/31）。"""
    _txn(db, transaction_type="BUY", quantity=Decimal("100"), price=Decimal("10"), fee=Decimal("0"),
         transaction_date=date(2025, 12, 15))
    _price(db, date(2025, 12, 31), Decimal("10"))
    db.commit()
    periods = calculate_period_pnl(db, 1, {"600000:A股": 15.0}, today=TODAY)["periods"]
    assert periods["ytd"]["status"] == "exact" and periods["ytd"]["pnl_cny"] == pytest.approx(500)
    assert periods["mtd"]["status"] == "estimated"
    assert periods["daily"]["status"] == "estimated"
    assert periods["daily"]["stale_opening_basis"][0]["basis_date"] == "2025-12-31"


def test_position_without_any_price_makes_period_unavailable(db):
    """期初持仓完全没有价格（opening_unpriced）：期初市值不含它、期末按现价计入，整笔市值会被
    当成收益——该区间不给数（pnl/收益率为 None），并说明原因。"""
    db.add(CorporateAction(user_id=1, symbol="600000", name="审计标的", market="A股",
                           action_type="OPENING_POSITION", ex_date=date(2025, 12, 1), currency="CNY",
                           adjusted_quantity=Decimal("100")))  # 成本未知、从无行情
    db.commit()
    result = calculate_period_pnl(db, 1, {"600000:A股": 20.0}, today=TODAY)
    daily = result["periods"]["daily"]
    assert daily["status"] == "unavailable"
    assert daily["pnl_cny"] is None and daily["return_rate"] is None
    assert daily["opening_unpriced_positions"] == [{"symbol": "600000", "market": "A股"}]
    assert any("当日损益无法计算" in w for w in result["data_quality"]["warnings"])


def test_summarize_curve_without_valid_points_has_no_rate():
    summary = summarize_curve([{"total_return_cny": 0, "daily_return_rate": None, "market_value_cny": 0}], {})
    assert summary["return_rate"] is None and summary["pnl_cny"] == 0


@pytest.mark.anyio
async def test_period_pnl_endpoint_uses_server_prices(db):
    import httpx

    from app.core.security import get_password_hash
    from app.main import app
    from app.models.user import User

    user = db.query(User).filter(User.username == "demo").one()
    original = user.hashed_password
    user.hashed_password = get_password_hash("period-pnl-pw")
    db.commit()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            token = (await client.post("/api/auth/token", json={"username": "demo", "password": "period-pnl-pw"})).json()["access_token"]
            response = await client.get("/api/statistics/period-pnl", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        body = response.json()
        assert set(body["periods"]) == {"daily", "mtd", "ytd"}
        assert "price_freshness" in body["data_quality"]
    finally:
        user.hashed_password = original
        db.commit()


@pytest.mark.parametrize(
    "fx_change_on, expected",
    [
        # USD/CNY 7.0 → 7.1，股价始终 10、持有 100 股：汇兑损益 = 100 × 10 × 0.1 = 100 元
        (TODAY, {"daily": 100, "mtd": 100, "ytd": 100}),          # 评审复现：今天变动
        (date(2026, 9, 1), {"daily": 0, "mtd": 100, "ytd": 100}),  # 月初当天变动
        (date(2026, 1, 1), {"daily": 0, "mtd": 0, "ytd": 100}),    # 年初当天变动
    ],
)
def test_fx_change_on_period_boundary_is_counted(db, fx_change_on, expected):
    """评审 P2：期初必须是起点前一日的本币价值。此前期初与期末都用起点当天汇率，边界日的汇兑
    损益被两边抵消，股价不变、汇率上升时当日显示 0 且标为精确。"""
    _txn(db, symbol="AAPL", name="Apple", market="美股", currency="USD", transaction_type="BUY",
         quantity=Decimal("100"), price=Decimal("10"), fee=Decimal("0"), transaction_date=date(2025, 6, 2))
    for day in (date(2025, 12, 31), date(2026, 8, 31), date(2026, 9, 25)):
        db.add(SecurityPrice(symbol="AAPL", market="美股", ts_code="AAPL", price_date=day,
                             currency="USD", close_price=Decimal("10"), source="audit"))
    db.add(ExchangeRate(from_currency="USD", to_currency="CNY", rate=Decimal("7.0"),
                        effective_date=date(2025, 1, 1), source="audit", is_active=True))
    db.add(ExchangeRate(from_currency="USD", to_currency="CNY", rate=Decimal("7.1"),
                        effective_date=fx_change_on, source="audit", is_active=True))
    db.commit()
    result = calculate_period_pnl(db, 1, {"AAPL:美股": 10.0}, today=TODAY)
    for key, pnl in expected.items():
        period = result["periods"][key]
        assert period["pnl_cny"] == pytest.approx(pnl, abs=0.01), key
        assert period["closing_market_value_cny"] == pytest.approx(7100)
        assert period["status"] == "exact", key
