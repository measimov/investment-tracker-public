from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..core.deps import get_current_active_user
from ..database import get_db
from ..models.broker_account import BrokerAccount
from ..models.broker_fund_flow import BrokerFundFlow
from ..models.cash_event import CashEvent
from ..models.ibkr_activity_flow import IbkrActivityFlow
from ..models.user import User
from ..schemas.cash_event import (
    CashEventCreate,
    CashEventResponse,
    CashEventType,
    CashEventUpdate,
)
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


def _ensure_cash_event_is_mutable(
    db: Session,
    user_id: int,
    event_id: int,
) -> None:
    broker_source = db.query(BrokerFundFlow.id).filter(
        BrokerFundFlow.user_id == user_id,
        BrokerFundFlow.cash_event_id == event_id,
    ).first()
    if broker_source is None:
        broker_source = db.query(IbkrActivityFlow.id).filter(
            IbkrActivityFlow.user_id == user_id,
            or_(
                IbkrActivityFlow.cash_event_id == event_id,
                IbkrActivityFlow.fx_quote_cash_event_id == event_id,
                IbkrActivityFlow.fx_fee_cash_event_id == event_id,
            ),
        ).first()
    if broker_source:
        raise HTTPException(
            status_code=409,
            detail=(
                "Imported cash events cannot be modified or deleted; "
                "correct the source import instead."
            ),
        )


@router.post("", response_model=CashEventResponse, status_code=201)
def create_cash_event(
    cash_event: CashEventCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    _validate_broker_account(db, current_user.id, cash_event.broker_account_id)
    db_event = CashEvent(**cash_event.model_dump(), user_id=current_user.id)
    db.add(db_event)
    db.commit()
    db.refresh(db_event)
    return db_event


@router.get("", response_model=List[CashEventResponse])
def list_cash_events(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    broker_account_id: Optional[int] = None,
    event_type: Optional[CashEventType] = None,
    currency: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    query = db.query(CashEvent).filter(CashEvent.user_id == current_user.id)
    if broker_account_id is not None:
        query = query.filter(CashEvent.broker_account_id == broker_account_id)
    if event_type:
        query = query.filter(CashEvent.event_type == event_type)
    if currency:
        query = query.filter(CashEvent.currency == currency)
    if start_date:
        query = query.filter(CashEvent.event_date >= start_date)
    if end_date:
        query = query.filter(CashEvent.event_date <= end_date)
    events = (
        query.order_by(CashEvent.event_date.desc(), CashEvent.id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    linked_ids = set()
    if events:
        event_ids = [event.id for event in events]
        linked_ids.update(
            event_id
            for (event_id,) in db.query(BrokerFundFlow.cash_event_id).filter(
                BrokerFundFlow.user_id == current_user.id,
                BrokerFundFlow.cash_event_id.in_(event_ids),
            )
        )
        for column in (
            IbkrActivityFlow.cash_event_id,
            IbkrActivityFlow.fx_quote_cash_event_id,
            IbkrActivityFlow.fx_fee_cash_event_id,
        ):
            linked_ids.update(
                event_id
                for (event_id,) in db.query(column).filter(
                    IbkrActivityFlow.user_id == current_user.id,
                    column.in_(event_ids),
                )
            )
    responses = []
    for event in events:
        payload = CashEventResponse.model_validate(event)
        payload.imported = event.id in linked_ids
        responses.append(payload)
    return responses


@router.get("/{event_id:int}", response_model=CashEventResponse)
def get_cash_event(
    event_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    return get_owned_record(
        db,
        CashEvent,
        event_id,
        current_user.id,
        "Cash event not found",
    )


@router.put("/{event_id:int}", response_model=CashEventResponse)
def update_cash_event(
    event_id: int,
    cash_event_update: CashEventUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    db_event = get_owned_record(
        db,
        CashEvent,
        event_id,
        current_user.id,
        "Cash event not found",
    )
    _ensure_cash_event_is_mutable(db, current_user.id, db_event.id)
    update_data = cash_event_update.model_dump(exclude_unset=True)
    account_id = update_data.get("broker_account_id")
    if account_id is not None:
        _validate_broker_account(db, current_user.id, account_id)
    for field, value in update_data.items():
        setattr(db_event, field, value)
    db.commit()
    db.refresh(db_event)
    return db_event


@router.delete("/{event_id:int}", status_code=204)
def delete_cash_event(
    event_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    db_event = get_owned_record(
        db,
        CashEvent,
        event_id,
        current_user.id,
        "Cash event not found",
    )
    _ensure_cash_event_is_mutable(db, current_user.id, db_event.id)
    db.delete(db_event)
    db.commit()
    return None
