from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..core.deps import get_current_active_user
from ..database import get_db
from ..models.import_batch import ImportBatch
from ..models.user import User
from ..schemas.import_batch import ImportBatchResponse, ImportBatchStatus
from ._ownership import get_owned_record


router = APIRouter()


@router.get("", response_model=List[ImportBatchResponse])
def list_import_batches(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    broker_account_id: Optional[int] = None,
    broker: Optional[str] = None,
    status: Optional[ImportBatchStatus] = None,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    query = db.query(ImportBatch).filter(ImportBatch.user_id == current_user.id)
    if broker_account_id is not None:
        query = query.filter(ImportBatch.broker_account_id == broker_account_id)
    if broker:
        query = query.filter(ImportBatch.broker == broker)
    if status:
        query = query.filter(ImportBatch.status == status)
    return (
        query.order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.get("/{batch_id:int}", response_model=ImportBatchResponse)
def get_import_batch(
    batch_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    return get_owned_record(
        db,
        ImportBatch,
        batch_id,
        current_user.id,
        "Import batch not found",
    )
