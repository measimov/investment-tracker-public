"""雪球发言采集器：状态、关注作者名单、立即运行（管理员）。

抓取永远不在 Web 进程里跑：「立即运行」只写 `xueqiu_collector_state.run_requested_at`，
由 xueqiu-collector 进程在下一个轮询点（≤30s）拾取。作者名单是全局配置（不是用户域
数据），读对所有登录用户开放，增删改仅管理员。

按标的监控（每日一轮）：组合跟踪名单同一权限模型；`/symbol-feed`（标的的雪球公告/
讨论）是全局数据的只读展示，按本仓 (symbol, market) 查询。原 `/hots`（今日热帖）已于
2026-09-28 随热帖采集一起下线。
"""

from datetime import timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from ..config import settings
from ..core.deps import get_current_active_user, get_current_admin_user
from ..core.timeutil import local_today
from ..database import get_db
from ..models.user import User
from ..models.xueqiu_collector import XueqiuCollectorAuthor, XueqiuCollectorCube
from ..schemas.xueqiu_collector import (
    CollectorAuthorCreate,
    CollectorAuthorResponse,
    CollectorAuthorUpdate,
    CollectorCubeCreate,
    CollectorCubeResponse,
    CollectorCubeUpdate,
    CollectorStatusResponse,
    CollectorSymbolsStatus,
    XueqiuSymbolFeedResponse,
)
from ..services.opinion_summary_jobs import OPINION_MARKETS
from ..services.symbol_normalization import normalize_manual_symbol
from ..services.xueqiu_collector import cookie_health, feed_store
from ..services.xueqiu_collector import state as collector_state
from ..services.xueqiu_collector.symbols import known_work_items
from ..services.xueqiu_collector.feed_parsing import KIND_ANNOUNCEMENT, KIND_DISCUSSION, KINDS

router = APIRouter(prefix="/api/xueqiu-collector", tags=["Xueqiu Collector"])


def _list_authors(db: Session):
    return (
        db.query(XueqiuCollectorAuthor)
        .order_by(XueqiuCollectorAuthor.created_at.asc(), XueqiuCollectorAuthor.xueqiu_user_id.asc())
        .all()
    )


def _list_cubes(db: Session):
    return (
        db.query(XueqiuCollectorCube)
        .order_by(XueqiuCollectorCube.created_at.asc(), XueqiuCollectorCube.cube_id.asc())
        .all()
    )


def build_status(db: Session) -> CollectorStatusResponse:
    state = collector_state.get_state(db)
    now = collector_state.utcnow()
    pending = collector_state.todays_symbols_pending(state, local_today())
    health_limit = timedelta(minutes=settings.xueqiu_collector_health_max_age_minutes)
    alive = state.heartbeat_at is not None and now - state.heartbeat_at <= health_limit
    cooldown_until = collector_state.waf_cooldown_until(
        state, settings.xueqiu_collector_waf_cooldown_seconds
    )
    cookie = cookie_health.check_expiry(
        settings.xueqiu_cookie_file,
        warn_days=settings.xueqiu_cookie_warn_days,
        critical_days=settings.xueqiu_cookie_critical_days,
    )
    if not (settings.xueqiu_cookies or "").strip() and not (
        settings.xueqiu_cookie_file or ""
    ).strip():
        cookie = {**cookie, "message": "未配置雪球 Cookie：采集器与雪球行情均不可用"}
    return CollectorStatusResponse(
        enabled=settings.xueqiu_collector_enabled,
        alive=alive,
        heartbeat_at=state.heartbeat_at,
        cycle_minutes=settings.xueqiu_collector_cycle_minutes,
        last_cycle_started_at=state.last_cycle_started_at,
        last_cycle_finished_at=state.last_cycle_finished_at,
        last_cycle_status=state.last_cycle_status,
        last_cycle_message=state.last_cycle_message,
        last_waf_at=state.last_waf_at,
        waf_cooldown_until=cooldown_until if cooldown_until and cooldown_until > now else None,
        run_requested_at=state.run_requested_at,
        run_pending=collector_state.run_request_pending(state),
        cookie=cookie,
        recent_runs=[
            collector_state.scan_run_to_dict(run)
            for run in collector_state.recent_scan_runs(db, 10)
        ],
        authors=[CollectorAuthorResponse.model_validate(item) for item in _list_authors(db)],
        symbols=CollectorSymbolsStatus(
            enabled=settings.xueqiu_collector_symbols_enabled,
            run_after=collector_state.parse_run_after(
                settings.xueqiu_collector_symbols_run_after
            ).strftime("%H:%M"),
            last_started_at=state.symbols_last_started_at,
            last_finished_at=state.symbols_last_finished_at,
            last_status=state.symbols_last_status,
            last_message=state.symbols_last_message,
            last_business_date=state.symbols_last_business_date,
            last_stats=state.symbols_last_stats or {},
            run_requested_at=state.symbols_run_requested_at,
            run_pending=collector_state.symbols_request_pending(state),
            retry_pending=pending is not None,
            retry_attempts=int((pending or {}).get("attempts") or 0),
            retry_item_count=(
                # 与重试执行同口径：已下线类型（热帖）不计入
                len(known_work_items(pending["items"]))
                if pending and isinstance(pending.get("items"), list)
                else None
            ),
        ),
        cubes=[CollectorCubeResponse.model_validate(item) for item in _list_cubes(db)],
    )


@router.get("/status", response_model=CollectorStatusResponse)
def get_collector_status(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    return build_status(db)


@router.get("/authors", response_model=list[CollectorAuthorResponse])
def list_collector_authors(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    return [CollectorAuthorResponse.model_validate(item) for item in _list_authors(db)]


@router.post(
    "/authors", response_model=CollectorAuthorResponse, status_code=status.HTTP_201_CREATED
)
def create_collector_author(
    payload: CollectorAuthorCreate,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    if db.get(XueqiuCollectorAuthor, payload.xueqiu_user_id) is not None:
        raise HTTPException(status_code=409, detail="该雪球用户已在关注名单中")
    author = XueqiuCollectorAuthor(
        xueqiu_user_id=payload.xueqiu_user_id,
        display_name=payload.display_name,
        note=payload.note,
        enabled=payload.enabled,
    )
    db.add(author)
    db.commit()
    db.refresh(author)
    return CollectorAuthorResponse.model_validate(author)


@router.patch("/authors/{xueqiu_user_id}", response_model=CollectorAuthorResponse)
def update_collector_author(
    xueqiu_user_id: str,
    payload: CollectorAuthorUpdate,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    author = db.get(XueqiuCollectorAuthor, xueqiu_user_id)
    if author is None:
        raise HTTPException(status_code=404, detail="关注名单中没有该雪球用户")
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is None:
            continue
        setattr(author, field, value.strip() if isinstance(value, str) else value)
    db.commit()
    db.refresh(author)
    return CollectorAuthorResponse.model_validate(author)


@router.delete("/authors/{xueqiu_user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_collector_author(
    xueqiu_user_id: str,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """只移出名单；已采集的发言保留（观点摘要的历史输入不应随名单变动消失）。"""
    author = db.get(XueqiuCollectorAuthor, xueqiu_user_id)
    if author is None:
        raise HTTPException(status_code=404, detail="关注名单中没有该雪球用户")
    db.delete(author)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/run-now", response_model=CollectorStatusResponse)
def request_collector_run(
    target: Literal["authors", "symbols"] = Query(
        "authors", description="authors=作者发言一轮 / symbols=按标的采集一轮"
    ),
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    if not settings.xueqiu_collector_enabled:
        raise HTTPException(
            status_code=409,
            detail="采集器未启用（XUEQIU_COLLECTOR_ENABLED=false），请求不会被执行",
        )
    if target == "symbols":
        if not settings.xueqiu_collector_symbols_enabled:
            raise HTTPException(
                status_code=409,
                detail="按标的采集未启用（XUEQIU_COLLECTOR_SYMBOLS_ENABLED=false）",
            )
        collector_state.request_symbols_run(db)
    else:
        collector_state.request_run(db)
    return build_status(db)


# --------------------------------------------------------------------------- #
# 组合跟踪名单（与作者名单同一权限模型：读对登录用户开放，增删改仅管理员）
# --------------------------------------------------------------------------- #
@router.get("/cubes", response_model=list[CollectorCubeResponse])
def list_collector_cubes(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    return [CollectorCubeResponse.model_validate(item) for item in _list_cubes(db)]


@router.post("/cubes", response_model=CollectorCubeResponse, status_code=status.HTTP_201_CREATED)
def create_collector_cube(
    payload: CollectorCubeCreate,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    if db.get(XueqiuCollectorCube, payload.cube_id) is not None:
        raise HTTPException(status_code=409, detail="该组合已在跟踪名单中")
    cube = XueqiuCollectorCube(
        cube_id=payload.cube_id,
        display_name=payload.display_name,
        note=payload.note,
        enabled=payload.enabled,
    )
    db.add(cube)
    db.commit()
    db.refresh(cube)
    return CollectorCubeResponse.model_validate(cube)


@router.patch("/cubes/{cube_id}", response_model=CollectorCubeResponse)
def update_collector_cube(
    cube_id: str,
    payload: CollectorCubeUpdate,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    cube = db.get(XueqiuCollectorCube, cube_id.strip().upper())
    if cube is None:
        raise HTTPException(status_code=404, detail="跟踪名单中没有该组合")
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is None:
            continue
        setattr(cube, field, value.strip() if isinstance(value, str) else value)
    db.commit()
    db.refresh(cube)
    return CollectorCubeResponse.model_validate(cube)


@router.delete("/cubes/{cube_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_collector_cube(
    cube_id: str,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """只移出名单；已采集的调仓记录保留。"""
    cube = db.get(XueqiuCollectorCube, cube_id.strip().upper())
    if cube is None:
        raise HTTPException(status_code=404, detail="跟踪名单中没有该组合")
    db.delete(cube)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# 只读展示：标的的公告/讨论流（全局数据，不接 LLM）
# --------------------------------------------------------------------------- #
@router.get("/symbol-feed", response_model=XueqiuSymbolFeedResponse)
def get_symbol_feed(
    symbol: str = Query(..., min_length=1, max_length=20),
    market: str = Query(...),
    kind: Optional[Literal["announcement", "discussion"]] = Query(
        None, description="只取一类；缺省两类都取"
    ),
    limit: int = Query(20, ge=1, le=100, description="每类最多条数"),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    if market not in OPINION_MARKETS:
        raise HTTPException(
            status_code=422,
            detail=f"{market} 暂不支持雪球公告/讨论（支持：{'/'.join(OPINION_MARKETS)}）",
        )
    code = normalize_manual_symbol(symbol, market)
    kinds = [kind] if kind else list(KINDS)
    feed = feed_store.symbol_feed(db, code, market, kinds, limit)
    state = collector_state.get_state(db)
    return XueqiuSymbolFeedResponse(
        symbol=code,
        market=market,
        announcements=feed.get(KIND_ANNOUNCEMENT, []),
        discussions=feed.get(KIND_DISCUSSION, []),
        last_cycle_finished_at=state.symbols_last_finished_at,
    )
