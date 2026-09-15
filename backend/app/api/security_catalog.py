"""标的全集 API：检索（账本优先 + 目录）、按需解析、目录状态与管理员触发同步。
挂在 /api/securities 下；security_profiles 的动态路由全是三段 `/{market}/{symbol}/x`，
这里的一段静态路径不会被吞。"""

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..core.deps import get_current_active_user, get_current_admin_user
from ..database import get_db
from ..models.user import User
from ..schemas.security_catalog import (
    CatalogStatusResponse,
    CatalogSyncAccepted,
    SecurityResolveResponse,
    SecuritySearchResponse,
)
from ..schemas.security_rule import VALID_MARKETS
from ..services import security_catalog_service as catalog

router = APIRouter()


def _validate_market(market: Optional[str]) -> Optional[str]:
    if market is None or market == "":
        return None
    if market not in VALID_MARKETS:
        raise HTTPException(status_code=422, detail=f"market 必须是 {sorted(VALID_MARKETS)} 之一")
    return market


@router.get("/search", response_model=SecuritySearchResponse)
def search_securities(
    q: str = Query(default="", max_length=40),
    market: Optional[str] = Query(default=None, max_length=20),
    limit: int = Query(default=20, ge=1, le=50),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> SecuritySearchResponse:
    """手工录入 / 筛选的标的检索：账本行（持仓/自选/历史）排前，目录命中排后。
    支持代码前缀、拼音缩写、代码包含、简/繁/英文名包含。空查询只返回账本行。"""
    market = _validate_market(market)
    items, health = catalog.search_securities(
        db, user_id=current_user.id, q=q, market=market, limit=limit
    )
    return SecuritySearchResponse(items=items, catalog=health)


@router.get("/resolve", response_model=SecurityResolveResponse)
def resolve_security(
    symbol: str = Query(..., min_length=1, max_length=20),
    market: str = Query(..., max_length=20),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> SecurityResolveResponse:
    """输入了目录里没有的代码时按需解析名称/币种（腾讯行情）并沉淀进目录；
    解析不到显式返回 name=null + error。"""
    if market not in VALID_MARKETS:
        raise HTTPException(status_code=422, detail=f"market 必须是 {sorted(VALID_MARKETS)} 之一")
    return SecurityResolveResponse(**catalog.resolve_security(db, symbol=symbol, market=market))


@router.get("/catalog-status", response_model=CatalogStatusResponse)
def get_catalog_status(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> CatalogStatusResponse:
    return CatalogStatusResponse(**catalog.catalog_status(db))


@router.post("/catalog-sync", response_model=CatalogSyncAccepted, status_code=202)
def trigger_catalog_sync(
    background_tasks: BackgroundTasks,
    market: Optional[str] = Query(default=None, max_length=20),
    force: bool = Query(default=True),
    current_user: User = Depends(get_current_admin_user),
) -> CatalogSyncAccepted:
    market = _validate_market(market)
    if catalog.is_sync_running():
        raise HTTPException(status_code=409, detail="目录同步进行中")
    markets = [market] if market else None
    sources = [
        spec.source for spec in catalog.LOADERS if not markets or set(spec.markets) & set(markets)
    ]
    background_tasks.add_task(catalog.run_sync_in_background, markets=markets, force=force)
    return CatalogSyncAccepted(started=True, sources=sources)
