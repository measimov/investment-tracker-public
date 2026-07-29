from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..core.deps import get_current_active_user
from ..database import get_db
from ..models.broker_account import BrokerAccount
from ..models.reconciliation_snapshot import ReconciliationSnapshot
from ..models.user import User
from ..schemas.reconciliation_snapshot import (
    ReconciliationSnapshotCreate,
    ReconciliationSnapshotResponse,
    ReconciliationSnapshotUpdate,
    ReconciliationStatus,
)
from ..services.reconciliation_service import run_and_store_compare
from ._ownership import get_owned_record


router = APIRouter()


def _validate_broker_account(db: Session, user_id: int, account_id: int) -> None:
    get_owned_record(
        db,
        BrokerAccount,
        account_id,
        user_id,
        "Broker account not found",
    )


def _json_fields(data: dict) -> dict:
    if "cash_balances" in data and data["cash_balances"] is not None:
        data["cash_balances"] = {
            currency: str(amount)
            for currency, amount in data["cash_balances"].items()
        }
    if "positions" in data and data["positions"] is not None:
        json_positions = []
        for position in data["positions"]:
            position_data = (
                position.model_dump()
                if hasattr(position, "model_dump")
                else dict(position)
            )
            position_data["quantity"] = str(position_data["quantity"])
            json_positions.append(position_data)
        data["positions"] = json_positions
    return data


def _ensure_snapshot_is_mutable(snapshot: ReconciliationSnapshot) -> None:
    if snapshot.import_batch_id is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "Imported reconciliation snapshots cannot be modified or deleted; "
                "correct the source import instead."
            ),
        )


@router.post("", response_model=ReconciliationSnapshotResponse, status_code=201)
def create_reconciliation_snapshot(
    snapshot: ReconciliationSnapshotCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    _validate_broker_account(db, current_user.id, snapshot.broker_account_id)
    data = _json_fields(snapshot.model_dump())
    db_snapshot = ReconciliationSnapshot(**data, user_id=current_user.id)
    db.add(db_snapshot)
    db.flush()
    # 创建即自动比对（status/diff_detail/compared_at 由比对写入），同事务提交
    run_and_store_compare(db, db_snapshot, commit=False)
    db.commit()
    db.refresh(db_snapshot)
    return db_snapshot


@router.get("", response_model=List[ReconciliationSnapshotResponse])
def list_reconciliation_snapshots(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    broker_account_id: Optional[int] = None,
    status: Optional[ReconciliationStatus] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    query = db.query(ReconciliationSnapshot).filter(
        ReconciliationSnapshot.user_id == current_user.id
    )
    if broker_account_id is not None:
        query = query.filter(
            ReconciliationSnapshot.broker_account_id == broker_account_id
        )
    if status:
        query = query.filter(ReconciliationSnapshot.status == status)
    if start_date:
        query = query.filter(ReconciliationSnapshot.snapshot_date >= start_date)
    if end_date:
        query = query.filter(ReconciliationSnapshot.snapshot_date <= end_date)
    return (
        query.order_by(
            ReconciliationSnapshot.snapshot_date.desc(),
            ReconciliationSnapshot.id.desc(),
        )
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.get("/{snapshot_id:int}", response_model=ReconciliationSnapshotResponse)
def get_reconciliation_snapshot(
    snapshot_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    return get_owned_record(
        db,
        ReconciliationSnapshot,
        snapshot_id,
        current_user.id,
        "Reconciliation snapshot not found",
    )


@router.put("/{snapshot_id:int}", response_model=ReconciliationSnapshotResponse)
def update_reconciliation_snapshot(
    snapshot_id: int,
    snapshot_update: ReconciliationSnapshotUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    db_snapshot = get_owned_record(
        db,
        ReconciliationSnapshot,
        snapshot_id,
        current_user.id,
        "Reconciliation snapshot not found",
    )
    _ensure_snapshot_is_mutable(db_snapshot)
    update_data = snapshot_update.model_dump(exclude_unset=True)
    account_id = update_data.get("broker_account_id")
    if account_id is not None:
        _validate_broker_account(db, current_user.id, account_id)
    for field, value in _json_fields(update_data).items():
        setattr(db_snapshot, field, value)
    db.flush()
    run_and_store_compare(db, db_snapshot, commit=False)
    db.commit()
    db.refresh(db_snapshot)
    return db_snapshot


@router.post("/{snapshot_id:int}/compare", response_model=ReconciliationSnapshotResponse)
def compare_reconciliation_snapshot(
    snapshot_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """手动触发自动比对：账本（交易/公司行动/现金事件）变化后按需刷新结果。"""
    db_snapshot = get_owned_record(
        db,
        ReconciliationSnapshot,
        snapshot_id,
        current_user.id,
        "Reconciliation snapshot not found",
    )
    return run_and_store_compare(db, db_snapshot)


@router.delete("/{snapshot_id:int}", status_code=204)
def delete_reconciliation_snapshot(
    snapshot_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    db_snapshot = get_owned_record(
        db,
        ReconciliationSnapshot,
        snapshot_id,
        current_user.id,
        "Reconciliation snapshot not found",
    )
    _ensure_snapshot_is_mutable(db_snapshot)
    db.delete(db_snapshot)
    db.commit()
    return None
