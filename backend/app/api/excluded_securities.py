"""现金管理标的排除清单：导入只归档不入账，对账比对双侧忽略。"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.deps import get_current_active_user
from ..database import get_db
from ..models.excluded_security import ExcludedSecurity
from ..models.user import User
from ..schemas.excluded_security import (
    VALID_MARKETS,
    ExcludedSecurityCreate,
    ExcludedSecurityResponse,
)
from ._ownership import get_owned_record

router = APIRouter()


@router.get("", response_model=List[ExcludedSecurityResponse])
def list_excluded_securities(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    return (
        db.query(ExcludedSecurity)
        .filter(ExcludedSecurity.user_id == current_user.id)
        .order_by(ExcludedSecurity.symbol, ExcludedSecurity.market)
        .all()
    )


@router.post("", response_model=ExcludedSecurityResponse, status_code=201)
def create_excluded_security(
    payload: ExcludedSecurityCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    symbol = payload.symbol.strip().upper()
    market = payload.market.strip()
    if market not in VALID_MARKETS:
        raise HTTPException(status_code=422, detail=f"未知市场类型: {market}")

    row = ExcludedSecurity(
        user_id=current_user.id,
        symbol=symbol,
        market=market,
        note=(payload.note or "").strip() or None,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该标的已在排除清单中")
    db.refresh(row)
    return row


@router.delete("/{record_id}", status_code=204)
def delete_excluded_security(
    record_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    row = get_owned_record(
        db,
        ExcludedSecurity,
        record_id,
        current_user.id,
        "Excluded security not found",
    )
    db.delete(row)
    db.commit()
