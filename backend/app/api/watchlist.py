"""观察清单 API（用户域）。

标的档案/分析/事件都是全局能力，观察标的的详情页零成本复用；本路由只管
"谁在观察什么"。用户域守卫按惯例在查询里直接带 user_id（非 _ownership）。
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..core.deps import get_current_active_user
from ..core.logging import get_app_logger
from ..database import SessionLocal, get_db
from ..models.user import User
from ..models.watchlist_item import WatchlistItem
from ..schemas.watchlist import (
    WatchlistItemCreate,
    WatchlistItemResponse,
    WatchlistItemUpdate,
    WatchlistMembershipResponse,
)
from ..services.security_profile_service import graham_summaries_for, graham_summary_for
from ..services.symbol_normalization import normalize_manual_symbol

router = APIRouter(prefix="/watchlist", tags=["watchlist"])
logger = get_app_logger(__name__)


def _refresh_added_quote(user_id: int, symbol: str, market: str) -> None:
    """加入自选后取一次报价（响应之后在后台跑，独立会话）：写入现价，并把这次报价记为
    「加入以来涨跌幅」的基准价（口径 quote）。失败只记日志——之后由日线尾部同步按加入日
    收盘补上基准价（口径 close_on_add，见 watchlist_price_service）。"""
    from ..services.stock_price_service import refresh_quotes

    db = SessionLocal()
    try:
        refresh_quotes(db, user_id=user_id, keys=[(symbol, market)])
    except Exception:  # noqa: BLE001 - 后台补价失败不影响已完成的加入
        logger.exception("加入自选后取报价失败 %s/%s", market, symbol)
    finally:
        db.close()


def _item_response(db: Session, item: WatchlistItem) -> WatchlistItemResponse:
    response = WatchlistItemResponse.model_validate(item)
    # 用户域上下文：该用户的 ADS_RATIO 规则覆盖 20-F 封面解析值（美股估值两项）
    response.graham_summary = graham_summary_for(db, item.symbol, item.market, user_id=item.user_id)
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
    summaries = graham_summaries_for(
        db, [(item.symbol, item.market) for item in items], user_id=current_user.id
    )
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
    enriched 列表（评审 P2）。symbol 按写入口径（normalize_manual_symbol）规范化后比对。"""
    market = market.strip()
    normalized = normalize_manual_symbol(symbol, market)
    item = (
        db.query(WatchlistItem.id)
        .filter(
            WatchlistItem.user_id == current_user.id,
            WatchlistItem.symbol == normalized,
            WatchlistItem.market == market,
        )
        .first()
    )
    return WatchlistMembershipResponse(watching=item is not None, item_id=item[0] if item else None)


@router.post("", response_model=WatchlistItemResponse, status_code=201)
def add_watchlist_item(
    payload: WatchlistItemCreate,
    background_tasks: BackgroundTasks,
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
    if settings.quote_auto_refresh_enabled:
        background_tasks.add_task(_refresh_added_quote, current_user.id, item.symbol, item.market)
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


@router.delete("/{item_id}", status_code=204)
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
