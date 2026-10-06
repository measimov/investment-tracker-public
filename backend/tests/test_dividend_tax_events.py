"""实际扣税日、显式分摊、不可变券商事实和无损历史升级的回归。"""

from datetime import date
from decimal import Decimal
import hashlib

import httpx
import pytest

from app.core.deps import get_current_active_user
from app.database import SessionLocal
from app.main import app
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.cash_event import CashEvent
from app.models.corporate_action import CorporateAction
from app.models.dividend_tax_allocation import DividendTaxAllocation
from app.models.ibkr_activity_flow import IbkrActivityFlow
from app.models.reconciliation_snapshot import ReconciliationSnapshot
from app.models.user import User
from app.services.dividend_tax_upgrade import (
    build_dividend_tax_upgrade_plan,
    apply_dividend_tax_upgrade_plan,
)
from app.services.portfolio.curve import build_return_curve
from app.services.portfolio.fx import ExchangeRateLookup, dividend_tax_cash_flow
from app.services.reconciliation_service import derive_account_cash_asof
from app.services.statistics import (
    get_dividend_summary,
    calculate_period_pnl,
    calculate_performance_summary,
)
from app.services.statistics import aggregates, analytics, calculate_performance_analytics
from app.models.transaction import Transaction
from tests.helpers import reset_tables, make_account


RESET_MODELS = (
    BrokerFundFlow,
    IbkrActivityFlow,
    ReconciliationSnapshot,
    CashEvent,
    CorporateAction,
    BrokerAccount,
    Transaction,
)


@pytest.fixture
def db():
    session = SessionLocal()
    reset_tables(session, RESET_MODELS)
    app.dependency_overrides[get_current_active_user] = lambda: session.get(User, 1)
    try:
        yield session
    finally:
        app.dependency_overrides.pop(get_current_active_user, None)
        session.rollback()
        reset_tables(session, RESET_MODELS)
        session.close()


def dividend(db, account, *, day=date(2026, 1, 1), **kwargs):
    values = dict(
        user_id=1,
        broker_account_id=account.id,
        symbol="600000",
        market="A股",
        action_type="CASH_DIVIDEND",
        ex_date=day,
        payment_date=day,
        total_dividend=Decimal(100),
        tax_withheld=Decimal(0),
        net_dividend=Decimal(100),
        currency="CNY",
    )
    values.update(kwargs)
    action = CorporateAction(**values)
    db.add(action)
    db.flush()
    return action


def tax_event(db, account, *, day=date(2026, 2, 1), **kwargs):
    values = dict(
        user_id=1,
        broker_account_id=account.id,
        event_type="TAX",
        tax_kind="DIVIDEND",
        amount=Decimal(10),
        currency="CNY",
        event_date=day,
    )
    values.update(kwargs)
    event = CashEvent(**values)
    db.add(event)
    db.flush()
    return event


def tax_source(db, account, *, action=None, amount="-10", day=date(2026, 2, 1), **kwargs):
    values = dict(
        user_id=1,
        broker_account_id=account.id,
        broker="招商证券",
        row_hash=hashlib.sha256(str((account.id, day, amount, kwargs)).encode()).hexdigest(),
        source_filename="fixture.pdf",
        source_row_number=1,
        security_code="600000",
        trade_date=day,
        business_name="股息红利税补缴",
        currency="CNY",
        amount=Decimal(amount),
        corporate_action_id=action.id if action else None,
    )
    values.update(kwargs)
    source = BrokerFundFlow(**values)
    db.add(source)
    db.flush()
    return source


def test_tax_curve_and_cash_use_actual_date_even_without_allocation(db):
    account = make_account(db)
    action = dividend(db, account)
    event = tax_event(db, account)
    # 普通税费仍影响账户现金，但不能混入股息收益。
    tax_event(db, account, tax_kind=None, amount=Decimal(3))
    db.commit()
    curve, _, _ = build_return_curve(
        [],
        [action],
        {},
        {},
        {},
        date(2026, 1, 1),
        date(2026, 2, 2),
        rate_lookup=ExchangeRateLookup([]),
        fallback_currency=lambda _: "CNY",
        today=date(2026, 2, 2),
        dividend_tax_events=[event],
    )
    by_date = {p["date"]: p for p in curve}
    assert by_date["2026-01-01"]["dividend_income_cny"] == 100
    assert by_date["2026-02-01"]["dividend_income_cny"] == 90
    assert by_date["2026-02-01"]["cash_out_cny"] == -10
    assert derive_account_cash_asof(db, 1, account.id, date(2026, 1, 31))["CNY"] == 100
    assert derive_account_cash_asof(db, 1, account.id, date(2026, 2, 1))["CNY"] == 87
    summary = get_dividend_summary(db, 1)
    assert summary["total_tax"] == 10
    assert summary["total_dividend_net"] == 90
    assert summary["unallocated_tax_count"] == 1
    assert summary["by_symbol"][0]["total_tax"] == 0
    periods = calculate_period_pnl(db, 1, {}, today=date(2026, 2, 1))
    assert periods["periods"]["daily"]["dividend_income_cny"] == -10
    # 先到税款/仅有股息的账户没有成交，也不能吞掉已知现金事实。
    analytics_result = calculate_performance_analytics(db, 1, {}, today=date(2026, 2, 2))
    assert analytics_result["curve"][-1]["dividend_income_cny"] == 90
    assert analytics_result["range_summary"]["dividend_net_cny"] == 90


@pytest.mark.anyio
async def test_imported_tax_allocation_is_editable_fact_remains_read_only(db):
    account = make_account(db)
    first = dividend(db, account)
    second = dividend(db, account, day=date(2026, 1, 2))
    event = tax_event(db, account)
    source = tax_source(db, account)
    source.cash_event_id = event.id
    db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.put(
            f"/api/cash-events/{event.id}/dividend-allocations",
            json={
                "allocations": [
                    {"corporate_action_id": first.id, "amount": "3"},
                    {"corporate_action_id": second.id, "amount": "4"},
                ]
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["read_only"] is True
        assert Decimal(response.json()["unallocated_tax_amount"]) == 3
        for path, payload in ((f"/api/cash-events/{event.id}", {"amount": "9"}),):
            assert (await client.put(path, json=payload)).status_code == 409
        assert (await client.delete(f"/api/cash-events/{event.id}")).status_code == 409
        # 总额/总现金不随分摊变化，证券明细只计分配的 7 元。
        db.expire_all()
        summary = get_dividend_summary(db, 1)
        assert summary["total_tax"] == 10
        assert summary["by_symbol"][0]["total_tax"] == 7
        assert summary["unallocated_tax_cny"] == 3
        # 仅扣税所在月份的公司行动摘要，即使股息在上个月也不能漏税。
        filtered = await client.get(
            "/api/corporate-actions/statistics/summary",
            params={
                "start_date": "2026-02-01",
                "end_date": "2026-02-28",
                "broker_account_id": account.id,
            },
        )
        assert filtered.json()["cash_dividends"]["count"] == 0
        assert filtered.json()["cash_dividends"]["net_dividend"] == -10
        # 证券筛选时只显示明确归属部分。
        filtered = await client.get(
            "/api/corporate-actions/statistics/summary",
            params={"start_date": "2026-02-01", "symbol": "600000"},
        )
        assert filtered.json()["cash_dividends"]["total_tax"] == 7
        assert (await client.delete(f"/api/corporate-actions/{first.id}")).status_code == 204
    db.expire_all()
    assert db.get(CashEvent, event.id).unallocated_tax_amount == 6
    assert get_dividend_summary(db, 1)["total_tax"] == 10


@pytest.mark.anyio
async def test_allocation_rejects_wrong_owner_account_currency_date_and_overflow(db):
    account = make_account(db)
    other = make_account(db, account_name="other")
    valid = dividend(db, account)
    actions = [
        dividend(db, other),
        dividend(db, account, currency="USD"),
        dividend(db, account, day=date(2026, 3, 1)),
        dividend(db, account, user_id=2),
    ]
    event = tax_event(db, account)
    db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        path = f"/api/cash-events/{event.id}/dividend-allocations"
        for action in actions:
            result = await client.put(
                path, json={"allocations": [{"corporate_action_id": action.id, "amount": "1"}]}
            )
            assert result.status_code in (404, 422)
        for allocations in (
            [{"corporate_action_id": valid.id, "amount": "11"}],
            [{"corporate_action_id": valid.id, "amount": "1"}] * 2,
            [{"corporate_action_id": valid.id, "amount": "0.000000001"}],
        ):
            assert (await client.put(path, json={"allocations": allocations})).status_code == 422
        assert (
            await client.put(
                path, json={"allocations": [{"corporate_action_id": valid.id, "amount": "10"}]}
            )
        ).status_code == 200
        # 修改任何一端都不能使已分摊的账本产生跨币种/账户或超额。
        assert (
            await client.put(f"/api/cash-events/{event.id}", json={"amount": "9"})
        ).status_code == 422
        assert (
            await client.put(f"/api/corporate-actions/{valid.id}", json={"currency": "USD"})
        ).status_code == 422
        assert (await client.put(path, json={"allocations": []})).status_code == 200
        assert (
            await client.put(f"/api/cash-events/{event.id}", json={"amount": "9"})
        ).status_code == 200
        assert (await client.delete(f"/api/cash-events/{event.id}")).status_code == 204
    db.expire_all()
    assert db.query(DividendTaxAllocation).count() == 0


def test_xirr_and_range_summary_include_negative_flow_at_tax_date(db, monkeypatch):
    account = make_account(db)
    action = dividend(db, account)
    event = tax_event(db, account)
    db.add(
        Transaction(
            user_id=1,
            broker_account_id=account.id,
            symbol="600000",
            market="A股",
            transaction_type="BUY",
            quantity=Decimal(10),
            price=Decimal(10),
            fee=Decimal(0),
            currency="CNY",
            transaction_date=date(2025, 12, 1),
        )
    )
    db.commit()
    flows = []

    def capture(cash_flows):
        flows.extend(cash_flows)
        return None

    monkeypatch.setattr(aggregates, "xirr", capture)
    calculate_performance_summary(db, 1, {}, today=date(2026, 2, 2))
    assert (date(2026, 1, 1), Decimal(100)) in flows
    assert (date(2026, 2, 1), Decimal(-10)) in flows
    assert (date(2026, 1, 1), Decimal(90)) not in flows
    flows.clear()
    monkeypatch.setattr(analytics, "xirr", capture)
    from app.services.statistics import calculate_performance_analytics

    result = calculate_performance_analytics(
        db, 1, {}, start_date=date(2026, 2, 1), end_date=date(2026, 2, 2), today=date(2026, 2, 2)
    )
    assert (event.event_date, Decimal(-10)) in flows
    assert (action.payment_date, Decimal(100)) not in flows
    assert result["range_summary"]["dividend_net_cny"] == -10
    assert result["range_summary"]["dividend_count"] == 0


def test_unallocated_foreign_tax_never_mixes_raw_amount_into_cny(db):
    account = make_account(db)
    tax_event(db, account, currency="ZZZ", amount=Decimal(17))
    db.commit()
    result = get_dividend_summary(db, 1)
    assert result["total_tax"] == 0
    assert result["missing_rate_currencies"] == ["ZZZ"]
    assert result["unallocated_tax_count"] == 1
    performance = calculate_performance_summary(db, 1, {}, today=date(2026, 2, 2))
    assert performance["account_return"]["annualized_return_rate"] is None
    assert performance["account_return"]["missing_tax_rate_currencies"] == ["ZZZ"]
    analytics_result = calculate_performance_analytics(db, 1, {}, today=date(2026, 2, 2))
    assert analytics_result["curve"][-1]["dividend_income_cny"] == 0
    assert analytics_result["range_summary"]["dividend_net_cny"] == 0
    assert analytics_result["range_summary"]["xirr_annualized_rate"] is None
    assert analytics_result["data_quality"]["missing_tax_rate_currencies"] == ["ZZZ"]
    assert analytics_result["metrics"]["status"] == "indeterminate"
    periods = calculate_period_pnl(db, 1, {}, today=date(2026, 2, 1))
    assert periods["periods"]["daily"]["pnl_cny"] is None
    assert periods["periods"]["daily"]["status"] == "unavailable"
    assert any("股息税扣款日缺少汇率" in w for w in periods["data_quality"]["warnings"])
    from types import SimpleNamespace

    event = SimpleNamespace(currency="USD", amount=Decimal(17), event_date=date(2026, 2, 1))
    lookup = ExchangeRateLookup(
        [
            SimpleNamespace(
                from_currency="USD",
                to_currency="CNY",
                rate=Decimal(7),
                effective_date=date(2026, 2, 2),
            )
        ]
    )
    assert dividend_tax_cash_flow(event, lookup) is None  # 未来汇率不能冒充扣税日汇率。
    event.event_date = date(2026, 2, 2)
    assert dividend_tax_cash_flow(event, lookup) == (event.event_date, Decimal(-119))


def test_historical_upgrade_preserves_hash_dates_and_recomputes_outflow_anchor(db):
    account = make_account(db)
    action = dividend(db, account, tax_withheld=Decimal(10), net_dividend=Decimal(90))
    linked = tax_source(db, account, action=action)
    missing = tax_source(
        db, account, day=date(2026, 2, 2), amount="-5", skip_reason="unattributed_tax"
    )
    # 原快照 40 = 股息旧净额 90 − 残差锚点 50，遗漏的 5 元被残差吸收。
    anchor = CashEvent(
        user_id=1,
        broker_account_id=account.id,
        event_type="TRANSFER_OUT",
        amount=Decimal(50),
        currency="CNY",
        event_date=date(2025, 12, 31),
        notes="对账锚点：残差反推",
    )
    snapshot = ReconciliationSnapshot(
        user_id=1,
        broker_account_id=account.id,
        snapshot_date=date(2026, 2, 3),
        source_filename="original.pdf",
        cash_balances={"CNY": "40"},
        positions=[],
    )
    db.add_all([anchor, snapshot])
    db.commit()
    plan_without_anchor = build_dividend_tax_upgrade_plan(db, 1)
    assert plan_without_anchor["blockers"]
    plan = build_dividend_tax_upgrade_plan(db, 1, anchors={anchor.id: snapshot.id})
    assert plan["blockers"] == []
    assert len(plan["facts"]) == 2
    assert Decimal(plan["anchor_changes"][0]["amount_after"]) == 45
    assert db.get(CashEvent, anchor.id).amount == 50  # dry-run 不写库
    original_hashes = {linked.id: linked.row_hash, missing.id: missing.row_hash}
    assert apply_dividend_tax_upgrade_plan(db, plan) == 2
    db.commit()
    db.expire_all()
    assert db.get(CorporateAction, action.id).tax_withheld == 0
    assert db.get(CorporateAction, action.id).net_dividend == 100
    assert db.get(CashEvent, anchor.id).amount == 45
    assert derive_account_cash_asof(db, 1, account.id, snapshot.snapshot_date)["CNY"] == 40
    assert {row.id: row.row_hash for row in db.query(BrokerFundFlow)} == original_hashes
    assert db.query(DividendTaxAllocation).count() == 1
    again = build_dividend_tax_upgrade_plan(db, 1, anchors={anchor.id: snapshot.id})
    assert again["facts"] == again["dividend_changes"] == again["anchor_changes"] == []
    assert apply_dividend_tax_upgrade_plan(db, again) == 0


def test_upgrade_rejects_stale_plan_and_lossy_legacy_tax(db):
    account = make_account(db)
    action = dividend(db, account, tax_withheld=Decimal(10), net_dividend=Decimal(90))
    source = tax_source(db, account, action=action)
    db.commit()
    plan = build_dividend_tax_upgrade_plan(db, 1)
    source.amount = Decimal(-11)
    db.commit()
    with pytest.raises(ValueError, match="账本已变化"):
        apply_dividend_tax_upgrade_plan(db, plan)
    db.rollback()
    current = build_dividend_tax_upgrade_plan(db, 1)
    assert current["blockers"]
    assert db.query(CashEvent).filter_by(tax_kind="DIVIDEND").count() == 0


def test_upgrade_cash_gap_blocks_by_default_and_is_preserved_only_explicitly(db):
    account = make_account(db)
    dividend(db, account)
    tax_source(db, account, skip_reason=None)  # 旧 skip_reason NULL 孤儿也应修复
    anchor = CashEvent(
        user_id=1,
        broker_account_id=account.id,
        event_type="WITHDRAWAL",
        currency="CNY",
        amount=Decimal(50),
        event_date=date(2025, 12, 1),
        notes="残差锚点",
    )
    snapshot = ReconciliationSnapshot(
        user_id=1,
        broker_account_id=account.id,
        snapshot_date=date(2026, 2, 2),
        cash_balances={"CNY": "47"},
        source_filename="broker.pdf",
        positions=[],
    )
    db.add_all([anchor, snapshot])
    db.commit()
    plan = build_dividend_tax_upgrade_plan(db, 1, anchors={anchor.id: snapshot.id})
    assert plan["blockers"]
    with pytest.raises(ValueError):
        apply_dividend_tax_upgrade_plan(db, plan)
    db.rollback()
    reviewed = build_dividend_tax_upgrade_plan(
        db, 1, anchors={anchor.id: snapshot.id}, preserve_existing_cash_delta=True
    )
    assert reviewed["blockers"] == []
    assert Decimal(reviewed["anchor_changes"][0]["existing_cash_delta"]) == 3
    assert apply_dividend_tax_upgrade_plan(db, reviewed) == 1
    db.commit()
    assert derive_account_cash_asof(db, 1, account.id, snapshot.snapshot_date)["CNY"] == 50
