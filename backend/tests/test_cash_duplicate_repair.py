from datetime import date
from decimal import Decimal

import pytest

from app.database import SessionLocal
from app.models.broker_account import BrokerAccount
from app.models.broker_fund_flow import BrokerFundFlow
from app.models.cash_event import CashEvent
from app.models.dividend_tax_allocation import DividendTaxAllocation
from app.models.reconciliation_snapshot import ReconciliationSnapshot
from app.services.cash_duplicate_repair import (
    apply_cash_duplicate_repair_plan,
    build_cash_duplicate_repair_plan,
)
from app.services.cmb_fund_flow_importer import create_broker_fund_flow
from tests.helpers import reset_tables
from tests.test_cmb_fund_flow_importer import RESET_MODELS, _cash_drift, _cmb_account


@pytest.fixture
def ledger():
    from dataclasses import replace

    with SessionLocal() as db:
        models = (DividendTaxAllocation, ReconciliationSnapshot, *RESET_MODELS)
        reset_tables(db, models)
        account = _cmb_account(db, "修复演练账户")
        sources = []
        for index in range(2):
            event = CashEvent(
                user_id=1,
                broker_account_id=account.id,
                event_type="TRANSFER_OUT",
                amount=Decimal("161001.61"),
                currency="CNY",
                event_date=date(2026, 6, 26),
            )
            db.add(event)
            db.flush()
            flow = _cash_drift(f"repair-{index}")
            if index == 1:
                flow = replace(flow, trade_price=Decimal("1.37"))
            source = create_broker_fund_flow(
                user_id=1,
                broker_account_id=account.id,
                filename=f"statement-{index}.pdf",
                flow=flow,
                cash_event_id=event.id,
            )
            db.add(source)
            db.flush()
            sources.append(source)
        snapshot = ReconciliationSnapshot(
            user_id=1,
            broker_account_id=account.id,
            snapshot_date=date(2026, 7, 27),
            positions=[],
            cash_balances={"CNY": "-161001.61"},
        )
        db.add(snapshot)
        db.commit()
        try:
            yield db, account, sources, snapshot
        finally:
            db.rollback()
            reset_tables(db, models)


def test_reviewed_cash_cleanup_preserves_both_sources_and_refreshes_compare(ledger):
    db, account, sources, snapshot = ledger
    canonical, duplicate = sources
    fingerprints = [
        (row.row_hash, row.trade_price, row.source_filename, row.amount) for row in sources
    ]
    plan = build_cash_duplicate_repair_plan(db, 1, [(duplicate.id, canonical.id)])
    assert not plan["blockers"] and len(plan["changes"]) == 1
    assert Decimal(plan["comparisons"][0]["cash_after"]) == Decimal("-161001.61")
    assert db.query(CashEvent).count() == 2  # planning does not mutate
    assert apply_cash_duplicate_repair_plan(db, plan) == 1
    db.commit()
    assert db.query(CashEvent).count() == 1 and db.query(BrokerFundFlow).count() == 2
    assert duplicate.cash_event_id == canonical.cash_event_id
    assert [
        (row.row_hash, row.trade_price, row.source_filename, row.amount) for row in sources
    ] == fingerprints
    assert "verified cash duplicate" in duplicate.notes
    assert snapshot.status == "MATCHED" and snapshot.compared_at is not None
    repeat = build_cash_duplicate_repair_plan(db, 1, [(duplicate.id, canonical.id)])
    assert repeat["changes"] == repeat["blockers"] == []
    assert apply_cash_duplicate_repair_plan(db, repeat) == 0


@pytest.mark.parametrize("alter", ["source_notes", "event_amount", "snapshot_cash"])
def test_cleanup_rejects_changed_reviewed_plan(ledger, alter):
    db, account, sources, snapshot = ledger
    canonical, duplicate = sources
    plan = build_cash_duplicate_repair_plan(db, 1, [(duplicate.id, canonical.id)])
    if alter == "source_notes":
        duplicate.notes = "changed after review"
    elif alter == "event_amount":
        db.get(CashEvent, duplicate.cash_event_id).amount += Decimal("1")
    else:
        snapshot.cash_balances = {"CNY": "0"}
    db.commit()
    with pytest.raises(ValueError, match="账本已变化"):
        apply_cash_duplicate_repair_plan(db, plan)
    assert db.query(CashEvent).count() == 2
    assert duplicate.cash_event_id != canonical.cash_event_id


@pytest.mark.parametrize(
    "alter",
    ["quantity", "fee", "balance", "account", "user", "extra_source", "same_price", "merge_chain"],
)
def test_cleanup_blocks_nonidentical_or_shared_facts(ledger, alter):
    db, account, sources, snapshot = ledger
    canonical, duplicate = sources
    pairs = [(duplicate.id, canonical.id)]
    if alter == "quantity":
        duplicate.trade_quantity += 1
    elif alter == "fee":
        duplicate.commission += 1
    elif alter == "balance":
        duplicate.cash_balance += 1
    elif alter == "account":
        other = BrokerAccount(
            user_id=1, broker="招商证券", account_name="其他账户", base_currency="CNY"
        )
        db.add(other)
        db.flush()
        duplicate.broker_account_id = other.id
    elif alter == "user":
        duplicate.user_id = 2
    elif alter == "same_price":
        duplicate.trade_price = canonical.trade_price
    elif alter == "merge_chain":
        pairs.append((canonical.id, duplicate.id))
    else:
        alias = create_broker_fund_flow(
            user_id=1,
            broker_account_id=account.id,
            filename="extra.pdf",
            flow=_cash_drift("extra-reference"),
            cash_event_id=duplicate.cash_event_id,
        )
        db.add(alias)
    db.commit()
    plan = build_cash_duplicate_repair_plan(db, 1, pairs)
    assert plan["blockers"]
    with pytest.raises(ValueError, match="阻断"):
        apply_cash_duplicate_repair_plan(db, plan)
    assert db.query(CashEvent).count() == 2
