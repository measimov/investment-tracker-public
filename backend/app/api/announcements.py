"""官方公告读取 API（#306）：单只标的的公告时间线（任意标的可读，与标的档案同约定）与
当前用户持仓∪自选范围内的近期公告（持仓/自选页徽标）。只读库，不外呼。"""

from datetime import date, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..core.deps import get_current_active_user
from ..core.markets import MANUAL_MARKET_SET, MANUAL_MARKETS
from ..core.timeutil import local_today
from ..database import get_db
from ..models.user import User
from ..schemas.announcement import (
    AnnouncementGroup,
    ImportanceFilter,
    RecentAnnouncementsResponse,
    SecurityAnnouncementsResponse,
)
from ..services import announcement_service, announcement_sync
from ..services.announcement_classifier import CATEGORIES

router = APIRouter(prefix="/api", tags=["Announcements"])


def _group_model(group: Dict[str, Any], name: Optional[str] = None) -> AnnouncementGroup:
    return AnnouncementGroup(**{**group, "name": name or group.get("sec_name")})


@router.get(
    "/securities/{market}/{symbol}/announcements", response_model=SecurityAnnouncementsResponse
)
def get_security_announcements(
    market: str,
    symbol: str,
    importance: ImportanceFilter = Query("all"),
    category: Optional[str] = Query(None, max_length=30),
    before: Optional[date] = Query(None, description="只取公告日早于此日的组（翻页）"),
    limit: int = Query(30, ge=1, le=100),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> SecurityAnnouncementsResponse:
    if market not in MANUAL_MARKET_SET:
        raise HTTPException(
            status_code=422, detail=f"market 必须是 {' / '.join(MANUAL_MARKETS)} 之一"
        )
    if category and category not in CATEGORIES:
        raise HTTPException(status_code=422, detail=f"category 必须是 {sorted(CATEGORIES)} 之一")
    groups, has_more = announcement_service.load_groups_page(
        db,
        keys=[(symbol, market)],
        before=before,
        importance=importance,
        category=category,
        limit=limit,
        complete_days=True,
    )
    status = announcement_sync.sync_status(db, symbol, market)
    return SecurityAnnouncementsResponse(
        symbol=symbol,
        market=market,
        sync_status=status["status"],
        last_synced=status["last_synced"],
        unsupported_reason=status["reason"],
        groups=[_group_model(group) for group in groups],
        has_more=has_more,
    )


@router.get("/announcements/recent", response_model=RecentAnnouncementsResponse)
def get_recent_announcements(
    days: int = Query(7, ge=1, le=90),
    importance: ImportanceFilter = Query("major"),
    limit: int = Query(200, ge=1, le=500),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> RecentAnnouncementsResponse:
    """当前用户持仓（数量>0）∪ 自选范围内、公告日在近 days 天内的组。"""
    entries = announcement_service.user_scope(db, current_user.id).get(current_user.id, {})
    groups = announcement_service.load_groups(
        db,
        keys=sorted(entries),
        since=local_today() - timedelta(days=days - 1),
        importance=importance,
        limit=limit,
    )
    return RecentAnnouncementsResponse(
        days=days,
        importance=importance,
        groups=[
            _group_model(group, entries.get((group["symbol"], group["market"]))) for group in groups
        ],
    )
