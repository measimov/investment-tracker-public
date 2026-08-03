from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from decimal import Decimal
from ..database import get_db
from ..models.holding import Holding
from ..models.user import User
from ..schemas.holding import HoldingResponse, HoldingPriceUpdate, PriceBatchUpdate
from ..services.price_refresh_jobs import (
    get_price_refresh_job,
    run_price_refresh_job,
    start_price_refresh_job,
)
from ..core.deps import get_current_active_user, get_current_admin_user

router = APIRouter()


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


@router.get("/{symbol}", response_model=HoldingResponse)
def get_holding(
    symbol: str,
    market: Optional[str] = Query(None),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Get a specific holding by symbol for the authenticated user.

    账户级持仓下同一 symbol 可能存在多行（每账户一行）；本端点保持单对象响应，
    多行时聚合数量与成本（加权均价），broker_account_id 置空表示跨账户汇总。
    """
    query = db.query(Holding).filter(Holding.symbol == symbol, Holding.user_id == current_user.id)

    if market:
        query = query.filter(Holding.market == market)

    rows = query.order_by(Holding.id).all()
    if not rows:
        raise HTTPException(status_code=404, detail="Holding not found")
    if len(rows) == 1:
        return rows[0]

    # 只在单一市场内做跨账户聚合：同一代码存在于多个市场时（价格键本就是
    # market-qualified），币种与数值不可混合，要求调用方用 market 消歧。
    markets = {row.market for row in rows}
    if len(markets) > 1:
        raise HTTPException(
            status_code=422,
            detail=f"Symbol {symbol} exists in multiple markets "
            f"({', '.join(sorted(markets))}); pass the market query parameter",
        )

    total_quantity = sum(Decimal(str(row.quantity)) for row in rows)
    total_cost = sum(Decimal(str(row.total_cost)) for row in rows)
    first = rows[0]
    return HoldingResponse(
        id=first.id,
        user_id=first.user_id,
        broker_account_id=None,
        symbol=first.symbol,
        name=first.name,
        market=first.market,
        quantity=total_quantity,
        avg_cost=(total_cost / total_quantity) if total_quantity > 0 else Decimal("0"),
        total_cost=total_cost,
        currency=first.currency,
        current_price=next((row.current_price for row in rows if row.current_price), None),
        price_updated_at=max(
            (row.price_updated_at for row in rows if row.price_updated_at), default=None
        ),
        updated_at=max(row.updated_at for row in rows),
    )


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
        raise HTTPException(status_code=404, detail="Holding not found")

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
        row.current_price = price_update.current_price
        row.price_updated_at = updated_at

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
            for holding in rows:
                holding.current_price = update.price
                holding.price_updated_at = datetime.now(timezone.utc)
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
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"批量更新失败: {str(e)}")


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
        raise HTTPException(status_code=404, detail="Refresh job not found")
    return job


# Admin endpoints
@router.get("/admin/all", response_model=List[HoldingResponse])
def get_all_holdings_admin(
    current_user: User = Depends(get_current_admin_user), db: Session = Depends(get_db)
):
    """Get all holdings from all users (admin only)."""
    holdings = db.query(Holding).order_by(Holding.user_id, Holding.total_cost.desc()).all()
    users = db.query(User.id, User.username).all()
    username_by_id = {user.id: user.username for user in users}
    for holding in holdings:
        holding.username = username_by_id.get(holding.user_id)
    return holdings


@router.get("/admin/users/{user_id}", response_model=List[HoldingResponse])
def get_user_holdings_admin(
    user_id: int,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """Get holdings for a specific user (admin only)."""
    holdings = (
        db.query(Holding)
        .filter(Holding.user_id == user_id)
        .order_by(Holding.total_cost.desc())
        .all()
    )
    user = db.query(User).filter(User.id == user_id).first()
    for holding in holdings:
        holding.username = user.username if user else None
    return holdings
