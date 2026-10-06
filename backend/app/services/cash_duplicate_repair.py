"""仅按明确审阅的来源配对合并重复现金事实；不猜测、保留来源指纹。"""

from decimal import Decimal

from ..models.broker_fund_flow import BrokerFundFlow
from ..models.cash_event import CashEvent
from ..models.dividend_tax_allocation import DividendTaxAllocation
from ..models.ibkr_activity_flow import IbkrActivityFlow
from ..models.reconciliation_snapshot import ReconciliationSnapshot
from .broker_import_common import append_note, lock_broker_import
from .cmb_fund_flow_importer import BROKER_NAME, booked_cash_key
from .reconciliation_service import derive_account_cash_asof, run_and_store_compare


def record_fingerprint(record):
    """审阅后任何来源/事实字段变化都拒绝执行，包括人工备注和关联。"""
    return {
        column.name: None
        if getattr(record, column.name) is None
        else str(getattr(record, column.name))
        for column in record.__table__.columns
    }


def build_cash_duplicate_repair_plan(db, user_id, pairs):
    """pairs 为显式 (重复来源 ID, 保留来源 ID)，不自动把等值流水当成重复。"""
    pairs = sorted(tuple(pair) for pair in pairs)
    changes, blockers, affected = [], [], {}
    duplicate_ids = {pair[0] for pair in pairs}
    canonical_ids = {pair[1] for pair in pairs}
    if len(duplicate_ids) != len(pairs) or duplicate_ids & canonical_ids:
        blockers.append("来源配对重复或形成合并链")
    deleted_events = set()
    for duplicate_id, canonical_id in pairs:
        duplicate = db.get(BrokerFundFlow, duplicate_id)
        canonical = db.get(BrokerFundFlow, canonical_id)
        if (
            not duplicate
            or not canonical
            or any(
                row.user_id != user_id or row.broker != BROKER_NAME
                for row in (duplicate, canonical)
            )
        ):
            blockers.append(f"来源 {duplicate_id}:{canonical_id} 不属于该用户的招商账户")
            continue
        if (
            duplicate.cash_event_id is not None
            and duplicate.cash_event_id == canonical.cash_event_id
        ):
            continue  # 已合并；二次执行零动作。
        events = [
            db.get(CashEvent, row.cash_event_id) if row.cash_event_id else None
            for row in (duplicate, canonical)
        ]
        duplicate_event, canonical_event = events
        if any(event is None for event in events):
            blockers.append(f"来源 {duplicate_id}:{canonical_id} 没有完整现金关联")
            continue
        if (
            duplicate.id <= canonical.id
            or duplicate.source_filename == canonical.source_filename
            or not duplicate.source_filename
            or not canonical.source_filename
            or duplicate.row_hash == canonical.row_hash
            or duplicate.trade_price == canonical.trade_price
            or duplicate.broker_account_id is None
            or duplicate.broker_account_id != canonical.broker_account_id
            or any(
                row.skip_reason is not None
                or row.transaction_id is not None
                or row.corporate_action_id is not None
                for row in (duplicate, canonical)
            )
            or duplicate_event.event_type not in {"TRANSFER_IN", "TRANSFER_OUT"}
            or duplicate_event.event_type != canonical_event.event_type
            or booked_cash_key(duplicate, duplicate_event.event_type)
            != booked_cash_key(canonical, canonical_event.event_type)
            or any(
                event.user_id != user_id
                or event.broker_account_id != duplicate.broker_account_id
                or event.tax_kind is not None
                or event.currency != duplicate.currency
                or event.event_date != duplicate.trade_date
                or event.amount != abs(duplicate.amount)
                for event in events
            )
            or (duplicate.amount > 0) != (duplicate_event.event_type == "TRANSFER_IN")
            or duplicate_event.id in deleted_events
        ):
            blockers.append(f"来源 {duplicate_id}:{canonical_id} 不满足相同现金事实和精度漂移条件")
            continue
        references = (
            db.query(BrokerFundFlow)
            .filter(BrokerFundFlow.cash_event_id == duplicate_event.id)
            .all()
        )
        if (
            len(references) != 1
            or references[0].id != duplicate.id
            or db.query(IbkrActivityFlow.id)
            .filter(IbkrActivityFlow.cash_event_id == duplicate_event.id)
            .first()
            or db.query(DividendTaxAllocation.id)
            .filter(DividendTaxAllocation.cash_event_id == duplicate_event.id)
            .first()
        ):
            blockers.append(f"现金事实 {duplicate_event.id} 还有其他来源/税款分摊，拒绝删除")
            continue
        deleted_events.add(duplicate_event.id)
        changes.append(
            {
                "duplicate_source": record_fingerprint(duplicate),
                "canonical_source": record_fingerprint(canonical),
                "duplicate_event": record_fingerprint(duplicate_event),
                "canonical_event": record_fingerprint(canonical_event),
            }
        )
        key = (duplicate.broker_account_id, duplicate.currency)
        affected[key] = min(affected.get(key, duplicate.trade_date), duplicate.trade_date)
    comparisons = []
    for (account_id, currency), earliest in sorted(affected.items()):
        snapshots = (
            db.query(ReconciliationSnapshot)
            .filter(
                ReconciliationSnapshot.user_id == user_id,
                ReconciliationSnapshot.broker_account_id == account_id,
                ReconciliationSnapshot.snapshot_date >= earliest,
            )
            .order_by(ReconciliationSnapshot.id)
            .all()
        )
        for snapshot in snapshots:
            before = derive_account_cash_asof(db, user_id, account_id, snapshot.snapshot_date).get(
                currency, Decimal(0)
            )
            delta = sum(
                (
                    Decimal(change["duplicate_source"]["amount"])
                    for change in changes
                    if int(change["duplicate_source"]["broker_account_id"]) == account_id
                    and change["duplicate_source"]["currency"] == currency
                    and change["duplicate_source"]["trade_date"]
                    <= snapshot.snapshot_date.isoformat()
                ),
                Decimal(0),
            )
            comparisons.append(
                {
                    "snapshot": record_fingerprint(snapshot),
                    "currency": currency,
                    "cash_before": str(before),
                    "cash_after": str(before - delta),
                    "removed_signed_cash": str(delta),
                }
            )
    return {
        "version": 1,
        "user_id": user_id,
        "pairs": [list(pair) for pair in pairs],
        "changes": changes,
        "comparisons": comparisons,
        "blockers": blockers,
    }


def apply_cash_duplicate_repair_plan(db, plan):
    """持用户导入锁重建并逐字段校验；调用者在验证后统一 commit。"""
    lock_broker_import(db, plan["user_id"])
    current = build_cash_duplicate_repair_plan(db, plan["user_id"], plan["pairs"])
    if current != plan or plan["blockers"]:
        raise ValueError("计划存在阻断或账本已变化，请重新生成 dry-run")
    for change in plan["changes"]:
        source = db.get(BrokerFundFlow, int(change["duplicate_source"]["id"]))
        source.cash_event_id = int(change["canonical_event"]["id"])
        source.notes = append_note(
            source.notes,
            f"verified cash duplicate; retained source {change['canonical_source']['id']}, removed cash event {change['duplicate_event']['id']}",
        )
        db.flush()
        db.delete(db.get(CashEvent, int(change["duplicate_event"]["id"])))
    db.flush()
    snapshot_ids = set()
    for comparison in plan["comparisons"]:
        snapshot = db.get(ReconciliationSnapshot, int(comparison["snapshot"]["id"]))
        cash = derive_account_cash_asof(
            db, plan["user_id"], snapshot.broker_account_id, snapshot.snapshot_date
        ).get(comparison["currency"], Decimal(0))
        if cash != Decimal(comparison["cash_after"]):
            raise ValueError("清理后的现金与审阅计划不一致，必须回滚")
        snapshot_ids.add(snapshot.id)
    for snapshot_id in sorted(snapshot_ids):
        run_and_store_compare(db, db.get(ReconciliationSnapshot, snapshot_id), commit=False)
    db.flush()
    return len(plan["changes"])
