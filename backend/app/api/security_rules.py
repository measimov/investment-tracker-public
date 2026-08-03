"""账本特例规则（security_rules）：手工维护的表驱动配置（issue #82）。"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.deps import get_current_active_user
from ..database import get_db
from ..models.security_rule import SecurityRule
from ..models.user import User
from ..schemas.security_rule import (
    VALID_RULE_TYPES,
    SecurityRuleCreate,
    SecurityRuleResponse,
)
from ._ownership import get_owned_record

router = APIRouter()


@router.get("", response_model=List[SecurityRuleResponse])
def list_security_rules(
    rule_type: Optional[str] = Query(None),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    query = db.query(SecurityRule).filter(SecurityRule.user_id == current_user.id)
    if rule_type:
        if rule_type not in VALID_RULE_TYPES:
            raise HTTPException(status_code=422, detail=f"未知规则类型: {rule_type}")
        query = query.filter(SecurityRule.rule_type == rule_type)
    return query.order_by(SecurityRule.rule_type, SecurityRule.symbol, SecurityRule.market).all()


@router.post("", response_model=SecurityRuleResponse, status_code=201)
def create_security_rule(
    payload: SecurityRuleCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    # CMB 业务名保留原样（中文），证券代码统一大写
    symbol = payload.symbol if payload.rule_type == "CMB_CASH_BUSINESS" else payload.symbol.upper()
    row = SecurityRule(
        user_id=current_user.id,
        rule_type=payload.rule_type,
        symbol=symbol,
        market=payload.market,
        payload=payload.payload,
        note=(payload.note or "").strip() or None,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="同类型下该键已存在规则")
    db.refresh(row)
    return row


@router.delete("/{record_id}", status_code=204)
def delete_security_rule(
    record_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    row = get_owned_record(
        db,
        SecurityRule,
        record_id,
        current_user.id,
        "Security rule not found",
    )
    db.delete(row)
    db.commit()
