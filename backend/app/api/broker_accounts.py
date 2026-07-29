from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..core.deps import get_current_active_user
from ..database import get_db
from ..models.broker_account import BrokerAccount
from ..models.broker_fund_flow import BrokerFundFlow
from ..models.cash_event import CashEvent
from ..models.corporate_action import CorporateAction
from ..models.import_batch import ImportBatch
from ..models.reconciliation_snapshot import ReconciliationSnapshot
from ..models.transaction import Transaction
from ..models.user import User
from ..schemas.broker_account import (
    BrokerAccountCreate,
    BrokerAccountResponse,
    BrokerAccountUpdate,
)
from ._ownership import get_owned_record


router = APIRouter()


ACCOUNT_REFERENCE_FIELDS = (
    (BrokerFundFlow, BrokerFundFlow.broker_account_id),
    (Transaction, Transaction.broker_account_id),
    (CorporateAction, CorporateAction.broker_account_id),
    (ImportBatch, ImportBatch.broker_account_id),
    (CashEvent, CashEvent.broker_account_id),
    (ReconciliationSnapshot, ReconciliationSnapshot.broker_account_id),
)


def account_has_records(
    db: Session,
    *,
    user_id: int,
    account_id: int,
) -> bool:
    return any(
        db.query(model.id)
        .filter(
            model.user_id == user_id,
            foreign_key == account_id,
        )
        .first()
        is not None
        for model, foreign_key in ACCOUNT_REFERENCE_FIELDS
    )


@router.post("", response_model=BrokerAccountResponse, status_code=201)
def create_broker_account(
    account: BrokerAccountCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    db_account = BrokerAccount(**account.model_dump(), user_id=current_user.id)
    db.add(db_account)
    db.commit()
    db.refresh(db_account)
    return db_account


@router.get("", response_model=List[BrokerAccountResponse])
def list_broker_accounts(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    broker: Optional[str] = None,
    is_active: Optional[bool] = None,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    query = db.query(BrokerAccount).filter(BrokerAccount.user_id == current_user.id)
    if broker:
        query = query.filter(BrokerAccount.broker == broker)
    if is_active is not None:
        query = query.filter(BrokerAccount.is_active == is_active)
    return (
        query.order_by(BrokerAccount.broker, BrokerAccount.account_name, BrokerAccount.id)
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.get("/{account_id:int}", response_model=BrokerAccountResponse)
def get_broker_account(
    account_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    return get_owned_record(
        db,
        BrokerAccount,
        account_id,
        current_user.id,
        "Broker account not found",
    )


@router.put("/{account_id:int}", response_model=BrokerAccountResponse)
def update_broker_account(
    account_id: int,
    account_update: BrokerAccountUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    db_account = get_owned_record(
        db,
        BrokerAccount,
        account_id,
        current_user.id,
        "Broker account not found",
    )
    updates = account_update.model_dump(exclude_unset=True)
    if (
        "broker" in updates
        and updates["broker"] != db_account.broker
        and account_has_records(
            db,
            user_id=current_user.id,
            account_id=db_account.id,
        )
    ):
        raise HTTPException(
            status_code=409,
            detail="Broker cannot be changed after ledger or audit records exist.",
        )
    for field, value in updates.items():
        setattr(db_account, field, value)
    db.commit()
    db.refresh(db_account)
    return db_account


@router.delete("/{account_id:int}", status_code=204)
def delete_broker_account(
    account_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    db_account = get_owned_record(
        db,
        BrokerAccount,
        account_id,
        current_user.id,
        "Broker account not found",
    )
    if account_has_records(
        db,
        user_id=current_user.id,
        account_id=db_account.id,
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Broker account has ledger or audit records. "
                "Deactivate it instead of deleting it."
            ),
        )
    db.delete(db_account)
    db.commit()
    return None
