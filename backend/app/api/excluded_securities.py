"""排除清单兼容路由：外部契约保留，内部读写 security_rules 的 EXCLUDE 类型。

导入只归档不入账，对账比对双侧忽略。（#89 P2 教训：HTTP 路由是外部契约，
不因内部改表而破坏。）"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.deps import get_current_active_user
from ..database import get_db
from ..models.security_rule import SecurityRule
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
        db.query(SecurityRule)
        .filter(
            SecurityRule.user_id == current_user.id,
            SecurityRule.rule_type == "EXCLUDE",
        )
        .order_by(SecurityRule.symbol, SecurityRule.market)
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

    row = SecurityRule(
        user_id=current_user.id,
        rule_type="EXCLUDE",
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
        SecurityRule,
        record_id,
        current_user.id,
        "Excluded security not found",
    )
    if row.rule_type != "EXCLUDE":
        raise HTTPException(status_code=404, detail="Excluded security not found")
    db.delete(row)
    db.commit()
