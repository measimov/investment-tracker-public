from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..core.deps import get_current_active_user
from ..database import get_db
from ..models.broker_account import BrokerAccount
from ..models.cash_event import CashEvent
from ..models.user import User
from ..schemas.cash_event import (
    CashEventCreate,
    CashEventResponse,
    CashEventType,
    CashEventUpdate,
    DividendTaxAllocationsUpdate,
)
from ..services.broker_import_common import lock_broker_import
from ..services.dividend_tax_service import (
    replace_tax_allocations,
    validate_existing_tax_allocations,
    validate_tax_event,
)
from ..services.holding_service import lock_record
from ._ownership import annotate_read_only, ensure_record_is_mutable, get_owned_record


router = APIRouter()


def _validate_broker_account(db: Session, user_id: int, account_id: int) -> None:
    get_owned_record(
        db,
        BrokerAccount,
        account_id,
        user_id,
        "券商账户不存在",
    )


IMMUTABLE_IMPORTED_CASH_EVENT_DETAIL = "导入的现金事件不能修改或删除；请更正来源对账单后重新导入"


def _ensure_cash_event_is_mutable(db: Session, user_id: int, event: CashEvent) -> None:
    ensure_record_is_mutable(
        db, user_id, event, kind="cash_event", detail=IMMUTABLE_IMPORTED_CASH_EVENT_DETAIL
    )


@router.post("", response_model=CashEventResponse, status_code=201)
def create_cash_event(
    cash_event: CashEventCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    lock_broker_import(db, current_user.id)
    _validate_broker_account(db, current_user.id, cash_event.broker_account_id)
    db_event = CashEvent(**cash_event.model_dump(), user_id=current_user.id)
    validate_tax_event(db_event)
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
    return annotate_read_only(db, current_user.id, "cash_event", events)


@router.get("/{event_id:int}", response_model=CashEventResponse)
def get_cash_event(
    event_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    event = get_owned_record(db, CashEvent, event_id, current_user.id, "现金事件不存在")
    return annotate_read_only(db, current_user.id, "cash_event", [event])[0]


@router.put("/{event_id:int}", response_model=CashEventResponse)
def update_cash_event(
    event_id: int,
    cash_event_update: CashEventUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    lock_broker_import(db, current_user.id)
    lock_record(db, "cash-event-record", event_id)
    db_event = get_owned_record(
        db,
        CashEvent,
        event_id,
        current_user.id,
        "现金事件不存在",
    )
    _ensure_cash_event_is_mutable(db, current_user.id, db_event)
    update_data = cash_event_update.model_dump(exclude_unset=True)
    account_id = update_data.get("broker_account_id")
    if account_id is not None:
        _validate_broker_account(db, current_user.id, account_id)
    for field, value in update_data.items():
        setattr(db_event, field, value)
    validate_existing_tax_allocations(db, event=db_event)
    db.commit()
    db.refresh(db_event)
    return db_event


@router.delete("/{event_id:int}", status_code=204)
def delete_cash_event(
    event_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    lock_broker_import(db, current_user.id)
    lock_record(db, "cash-event-record", event_id)
    db_event = get_owned_record(
        db,
        CashEvent,
        event_id,
        current_user.id,
        "现金事件不存在",
    )
    _ensure_cash_event_is_mutable(db, current_user.id, db_event)
    db.delete(db_event)
    db.commit()
    return None


@router.put("/{event_id:int}/dividend-allocations", response_model=CashEventResponse)
def update_dividend_tax_allocations(
    event_id: int,
    update: DividendTaxAllocationsUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    # 导入事实仍只读；显式归属可调整。与导入、股息更新/删除共用用户串行锁。
    lock_broker_import(db, current_user.id)
    lock_record(db, "cash-event-record", event_id)
    event = get_owned_record(db, CashEvent, event_id, current_user.id, "现金事件不存在")
    db.refresh(event)
    for action_id in sorted({item.corporate_action_id for item in update.allocations}):
        lock_record(db, "corporate-action-record", action_id)
    replace_tax_allocations(db, event, update.allocations)
    db.commit()
    db.refresh(event)
    return annotate_read_only(db, current_user.id, "cash_event", [event])[0]
