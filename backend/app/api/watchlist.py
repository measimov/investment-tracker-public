"""观察清单 API（用户域）。

标的档案/分析/事件都是全局能力，观察标的的详情页零成本复用；本路由只管
"谁在观察什么"。用户域守卫按惯例在查询里直接带 user_id（非 _ownership）。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.deps import get_current_active_user
from ..database import get_db
from ..models.user import User
from ..models.watchlist_item import WatchlistItem
from ..schemas.watchlist import (
    WatchlistItemCreate,
    WatchlistItemResponse,
    WatchlistItemUpdate,
    WatchlistMembershipResponse,
)
from ..services.security_profile_service import graham_summaries_for, graham_summary_for

router = APIRouter(prefix="/watchlist", tags=["watchlist"])


def _item_response(db: Session, item: WatchlistItem) -> WatchlistItemResponse:
    response = WatchlistItemResponse.model_validate(item)
    response.graham_summary = graham_summary_for(db, item.symbol, item.market)
    return response


@router.get("", response_model=list[WatchlistItemResponse])
def list_watchlist(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    items = (
        db.query(WatchlistItem)
        .filter(WatchlistItem.user_id == current_user.id)
        .order_by(WatchlistItem.created_at.desc(), WatchlistItem.id.desc())
        .all()
    )
    # 摘要批量聚合：一条 IN 查询代替每条 3-4 次往返（评审 P2）
    summaries = graham_summaries_for(db, [(item.symbol, item.market) for item in items])
    responses = []
    for item in items:
        response = WatchlistItemResponse.model_validate(item)
        response.graham_summary = summaries.get((item.symbol, item.market))
        responses.append(response)
    return responses


@router.get("/contains", response_model=WatchlistMembershipResponse)
def watchlist_contains(
    symbol: str,
    market: str,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """轻量 membership 查询：详情页只需知道"在不在"，不该为此拉整份
    enriched 列表（评审 P2）。symbol 按写入口径规范化后比对。"""
    normalized = symbol.strip().upper()
    item = (
        db.query(WatchlistItem.id)
        .filter(
            WatchlistItem.user_id == current_user.id,
            WatchlistItem.symbol == normalized,
            WatchlistItem.market == market,
        )
        .first()
    )
    return WatchlistMembershipResponse(
        watching=item is not None, item_id=item[0] if item else None
    )


@router.post("", response_model=WatchlistItemResponse, status_code=201)
def add_watchlist_item(
    payload: WatchlistItemCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    item = WatchlistItem(user_id=current_user.id, **payload.model_dump())
    db.add(item)
    try:
        db.commit()
    except IntegrityError:
        # 唯一约束 (user, symbol, market)：重复加入按冲突报出，前端提示已在观察
        db.rollback()
        raise HTTPException(status_code=409, detail="该标的已在观察清单中")
    db.refresh(item)
    return _item_response(db, item)


@router.put("/{item_id}", response_model=WatchlistItemResponse)
def update_watchlist_item(
    item_id: int,
    payload: WatchlistItemUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    item = (
        db.query(WatchlistItem)
        .filter(WatchlistItem.id == item_id, WatchlistItem.user_id == current_user.id)
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="观察条目不存在")
    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(item, field, value)
    db.commit()
    db.refresh(item)
    return _item_response(db, item)


@router.delete("/{item_id}")
def remove_watchlist_item(
    item_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    item = (
        db.query(WatchlistItem)
        .filter(WatchlistItem.id == item_id, WatchlistItem.user_id == current_user.id)
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="观察条目不存在")
    db.delete(item)
    db.commit()
    return {"message": "已移出观察清单"}
