from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from decimal import Decimal
from ..database import get_db
from ..models.holding import Holding
from ..models.user import User
from ..schemas.holding import (
    AdminHoldingResponse,
    HoldingResponse,
    HoldingPriceUpdate,
    PriceBatchUpdate,
)
from ..services.price_refresh_jobs import (
    get_price_refresh_job,
    run_price_refresh_job,
    start_price_refresh_job,
)
from ..core.deps import get_current_active_user, get_current_admin_user
from ..core.logging import get_app_logger

logger = get_app_logger(__name__)

router = APIRouter()

MANUAL_PRICE_SOURCE = "manual"


def _apply_manual_price(row: Holding, price: Decimal, updated_at: datetime) -> None:
    """手工改价：行情日期未知（写 NULL，前端显示「刷新于 …」），来源标 manual。"""
    row.current_price = price
    row.price_updated_at = updated_at
    row.price_as_of = None
    row.price_source = MANUAL_PRICE_SOURCE


@router.get("", response_model=List[HoldingResponse])
def get_holdings(
    market: Optional[str] = None,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Get list of current holdings for the authenticated user."""
    query = db.query(Holding).filter(Holding.user_id == current_user.id)

    if market:
        query = query.filter(Holding.market == market)

    holdings = query.order_by(Holding.total_cost.desc()).all()
    return holdings


@router.put("/{holding_id}/price", response_model=HoldingResponse)
def update_holding_price(
    holding_id: int,
    price_update: HoldingPriceUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Update a single holding's current price.
    Used when user manually inputs price in UI.
    """
    holding = (
        db.query(Holding)
        .filter(Holding.id == holding_id, Holding.user_id == current_user.id)
        .first()
    )
    if not holding:
        raise HTTPException(status_code=404, detail="持仓不存在")

    # Price is security-level: keep every account-scoped row of this security
    # in sync, otherwise resolve_server_prices would pick between fresh and
    # stale rows nondeterministically.
    sibling_rows = (
        db.query(Holding)
        .filter(
            Holding.user_id == current_user.id,
            Holding.symbol == holding.symbol,
            Holding.market == holding.market,
        )
        .all()
    )
    updated_at = datetime.now(timezone.utc)
    for row in sibling_rows:
        _apply_manual_price(row, price_update.current_price, updated_at)

    db.commit()
    db.refresh(holding)

    return holding


@router.post("/prices/batch-update")
def batch_update_prices(
    updates: List[PriceBatchUpdate],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Batch update multiple holdings' prices.
    Used in Statistics page when user inputs multiple prices.
    """
    success_list = []
    failed_list = []

    for update in updates:
        # Price is security-level: update every account-scoped row of the symbol.
        rows = (
            db.query(Holding)
            .filter(
                Holding.symbol == update.symbol,
                Holding.market == update.market,
                Holding.user_id == current_user.id,
            )
            .all()
        )

        if rows:
            updated_at = datetime.now(timezone.utc)
            for holding in rows:
                _apply_manual_price(holding, update.price, updated_at)
            success_list.append(
                {"symbol": update.symbol, "market": update.market, "price": float(update.price)}
            )
        else:
            failed_list.append(
                {"symbol": update.symbol, "market": update.market, "error": "持仓不存在"}
            )

    try:
        db.commit()
        return {
            "success": True,
            "success_count": len(success_list),
            "failed_count": len(failed_list),
            "success_list": success_list,
            "failed_list": failed_list,
        }
    except Exception:
        db.rollback()
        # 数据库异常原文（SQL 片段、约束名）只进日志，不回显给客户端（#277）
        logger.exception("批量更新持仓价格失败 user=%s", current_user.id)
        raise HTTPException(status_code=500, detail="批量更新价格失败，请稍后重试")


@router.post("/prices/refresh-from-api")
def refresh_all_prices(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """
    Start a background refresh for all holdings' prices from external APIs.
    """
    job = start_price_refresh_job(current_user.id)
    if job["status"] == "queued":
        background_tasks.add_task(run_price_refresh_job, job["id"])
    return job


@router.get("/prices/refresh-jobs/{job_id}")
def get_refresh_job_status(
    job_id: str,
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    job = get_price_refresh_job(job_id, current_user.id)
    if not job:
        raise HTTPException(status_code=404, detail="刷新任务不存在")
    return job


def _admin_holding_response(holding: Holding, username: str | None) -> AdminHoldingResponse:
    """显式组装 admin 响应，不再给 ORM 实例挂动态属性（issue #137）。"""
    payload = AdminHoldingResponse.model_validate(holding)
    payload.username = username
    return payload


# Admin endpoints
@router.get("/admin/all", response_model=List[AdminHoldingResponse])
def get_all_holdings_admin(
    current_user: User = Depends(get_current_admin_user), db: Session = Depends(get_db)
):
    """Get all holdings from all users (admin only)."""
    holdings = db.query(Holding).order_by(Holding.user_id, Holding.total_cost.desc()).all()
    users = db.query(User.id, User.username).all()
    username_by_id = {user.id: user.username for user in users}
    return [
        _admin_holding_response(holding, username_by_id.get(holding.user_id))
        for holding in holdings
    ]


@router.get("/admin/users/{user_id}", response_model=List[AdminHoldingResponse])
def get_user_holdings_admin(
    user_id: int,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """Get holdings for a specific user (admin only)."""
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        # 与 GET /api/users/{user_id} 同口径：不存在的用户是 404，
        # 不是空列表（后者与"该用户没有持仓"混为一谈）。
        raise HTTPException(status_code=404, detail="用户不存在")
    holdings = (
        db.query(Holding)
        .filter(Holding.user_id == user_id)
        .order_by(Holding.total_cost.desc())
        .all()
    )
    return [_admin_holding_response(holding, user.username) for holding in holdings]
