import os

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import tuple_
from sqlalchemy.orm import Session
from typing import Any, Dict, List, Optional
from datetime import date, timedelta
from ..config import settings
from ..database import get_db
from ..models.corporate_action import CorporateAction
from ..models.corporate_action_suggestion import CorporateActionSuggestion
from ..models.broker_account import BrokerAccount
from ..models.holding import Holding
from ..models.security_event import SecurityEvent
from ..models.user import User
from ..schemas.corporate_action import (
    CorporateActionCreate,
    CorporateActionUpdate,
    CorporateActionResponse,
    CashDividendCreate,
    StockDividendCreate
)
from ..schemas.corporate_action_suggestion import (
    SecurityEventResponse,
    SuggestionAccept,
    SuggestionResponse,
)
from ..services.corporate_action_service import summarize_cash_dividends
from ..services.dividend_sync_jobs import (
    get_dividend_sync_job,
    run_dividend_sync_job,
    start_dividend_sync_job,
)
from ..services.dividend_sync_service import (
    SuggestionStateError,
    accept_suggestion,
    ignore_suggestion,
    restore_suggestion,
)
from ..services.holding_service import lock_record, lock_security_timeline, recalculate_holdings
from ..core.deps import get_current_active_user
from ._ownership import ensure_record_is_mutable, get_owned_record, validate_owned_references

router = APIRouter()


def _require_tushare_configured() -> None:
    if not (os.environ.get("TUSHARE_TOKEN") or settings.tushare_token):
        raise HTTPException(
            status_code=409,
            detail="未配置 TUSHARE_TOKEN，无法同步分红公告；请在 backend/.env 中配置后重启。",
        )


IMMUTABLE_IMPORTED_ACTION_DETAIL = (
    "Imported corporate actions cannot be modified or deleted; correct the source import instead."
)


def _ensure_corporate_action_is_mutable(db: Session, user_id: int, action: CorporateAction) -> None:
    ensure_record_is_mutable(
        db,
        user_id,
        action,
        source_link_field="corporate_action_id",
        detail=IMMUTABLE_IMPORTED_ACTION_DETAIL,
    )


def _build_corporate_action_query(
    db: Session,
    user_id: int,
    symbol: Optional[str] = None,
    market: Optional[str] = None,
    action_type: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    broker_account_id: Optional[int] = None,
    unassigned_account: bool = False,
):
    query = db.query(CorporateAction).filter(CorporateAction.user_id == user_id)

    if symbol and symbol.strip():
        query = query.filter(CorporateAction.symbol.ilike(f"%{symbol.strip()}%"))
    if market:
        query = query.filter(CorporateAction.market == market)
    if action_type:
        query = query.filter(CorporateAction.action_type == action_type)
    if start_date:
        query = query.filter(CorporateAction.ex_date >= start_date)
    if end_date:
        query = query.filter(CorporateAction.ex_date <= end_date)
    if unassigned_account:
        query = query.filter(CorporateAction.broker_account_id.is_(None))
    elif broker_account_id is not None:
        query = query.filter(CorporateAction.broker_account_id == broker_account_id)

    return query


@router.post("", response_model=CorporateActionResponse, status_code=201)
def create_corporate_action(
    action: CorporateActionCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    创建公司行动记录

    支持的类型:
    - CASH_DIVIDEND: 现金股息
    - STOCK_DIVIDEND: 股票股息/红股
    - RIGHTS_ISSUE: 配股
    - STOCK_SPLIT: 拆股
    - REVERSE_SPLIT: 合股
    - BONUS_ISSUE: 送股
    - SPIN_OFF: 拆分
    - MERGER: 合并
    """
    action_data = action.model_dump()
    try:
        # 与交易/转仓写入口共用时间线锁；写入与重算同事务提交。
        lock_security_timeline(db, current_user.id, action.symbol, action.market)
        validate_owned_references(db, current_user.id, action_data)
        db_action = CorporateAction(**action_data, user_id=current_user.id)
        db.add(db_action)
        db.flush()

        # 如果是会影响持仓的公司行动，重新计算持仓
        if action.action_type in ["STOCK_DIVIDEND", "RIGHTS_ISSUE", "STOCK_SPLIT", "REVERSE_SPLIT", "BONUS_ISSUE"]:
            recalculate_holdings(db, current_user.id, action.symbol, action.market, commit=False)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"该公司行动会使持仓重放失败：{exc}") from exc
    except Exception:
        db.rollback()
        raise

    db.refresh(db_action)
    return db_action


@router.post("/cash-dividend", response_model=CorporateActionResponse, status_code=201)
def create_cash_dividend(
    dividend: CashDividendCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    快捷创建现金股息记录

    自动计算税后金额
    """
    action = dividend.to_corporate_action()
    return create_corporate_action(action, current_user, db)


@router.post("/stock-dividend", response_model=CorporateActionResponse, status_code=201)
def create_stock_dividend(
    dividend: StockDividendCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    快捷创建红股/股票股息记录

    会自动重新计算持仓成本
    """
    action = dividend.to_corporate_action()
    return create_corporate_action(action, current_user, db)


@router.get("", response_model=List[CorporateActionResponse])
def list_corporate_actions(
    skip: int = Query(0, ge=0, description="跳过记录数"),
    limit: int = Query(100, ge=1, le=1000, description="返回记录数"),
    symbol: Optional[str] = Query(None, description="按股票代码筛选"),
    market: Optional[str] = Query(None, description="按市场筛选"),
    action_type: Optional[str] = Query(None, description="按行动类型筛选"),
    start_date: Optional[date] = Query(None, description="开始日期"),
    end_date: Optional[date] = Query(None, description="结束日期"),
    broker_account_id: Optional[int] = Query(None, description="按券商账户筛选"),
    unassigned_account: bool = Query(False, description="仅显示未分配账户的记录"),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    获取公司行动列表

    支持多种筛选条件
    """
    query = _build_corporate_action_query(
        db,
        current_user.id,
        symbol=symbol,
        market=market,
        action_type=action_type,
        start_date=start_date,
        end_date=end_date,
        broker_account_id=broker_account_id,
        unassigned_account=unassigned_account,
    )

    actions = query.order_by(CorporateAction.ex_date.desc()).offset(skip).limit(limit).all()
    return actions


@router.get("/count")
def get_corporate_actions_count(
    symbol: Optional[str] = Query(None, description="按股票代码筛选"),
    market: Optional[str] = Query(None, description="按市场筛选"),
    action_type: Optional[str] = Query(None, description="按行动类型筛选"),
    start_date: Optional[date] = Query(None, description="开始日期"),
    end_date: Optional[date] = Query(None, description="结束日期"),
    broker_account_id: Optional[int] = Query(None, description="按券商账户筛选"),
    unassigned_account: bool = Query(False, description="仅显示未分配账户的记录"),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """获取公司行动总数。"""
    total = _build_corporate_action_query(
        db,
        current_user.id,
        symbol=symbol,
        market=market,
        action_type=action_type,
        start_date=start_date,
        end_date=end_date,
        broker_account_id=broker_account_id,
        unassigned_account=unassigned_account,
    ).count()
    return {"total": total}


@router.get("/statistics/summary")
def get_corporate_actions_summary(
    symbol: Optional[str] = Query(None, description="股票代码"),
    market: Optional[str] = Query(None, description="市场"),
    start_date: Optional[date] = Query(None, description="开始日期"),
    end_date: Optional[date] = Query(None, description="结束日期"),
    broker_account_id: Optional[int] = Query(None, description="按券商账户筛选"),
    unassigned_account: bool = Query(False, description="仅显示未分配账户的记录"),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    获取公司行动统计摘要

    包括总股息收入、税费等（聚合口径在 corporate_action_service，
    与统计页 get_dividend_summary 同口径）
    """
    query = _build_corporate_action_query(
        db,
        current_user.id,
        symbol=symbol,
        market=market,
        start_date=start_date,
        end_date=end_date,
        broker_account_id=broker_account_id,
        unassigned_account=unassigned_account,
    )
    return summarize_cash_dividends(db, query.all())


@router.get("/{action_id:int}", response_model=CorporateActionResponse)
def get_corporate_action(
    action_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """获取单个公司行动记录"""
    action = db.query(CorporateAction).filter(
        CorporateAction.id == action_id,
        CorporateAction.user_id == current_user.id
    ).first()
    if not action:
        raise HTTPException(status_code=404, detail="公司行动记录不存在")
    return action


@router.put("/{action_id:int}", response_model=CorporateActionResponse)
def update_corporate_action(
    action_id: int,
    action_update: CorporateActionUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """更新公司行动记录

    锁序：记录锁 → 锁内重读（等待锁期间 symbol/market 可能被并发修改）→
    时间线锁（旧+新键排序）。
    """
    update_data = action_update.model_dump(exclude_unset=True)
    try:
        lock_record(db, "corporate-action-record", action_id)

        db_action = db.query(CorporateAction).filter(
            CorporateAction.id == action_id,
            CorporateAction.user_id == current_user.id
        ).first()
        if not db_action:
            raise HTTPException(status_code=404, detail="公司行动记录不存在")
        db.refresh(db_action)

        _ensure_corporate_action_is_mutable(db, current_user.id, db_action)

        # 锁内重读后的新鲜旧键
        old_symbol = db_action.symbol
        old_market = db_action.market
        old_action_type = db_action.action_type

        new_symbol = update_data.get("symbol", old_symbol)
        new_market = update_data.get("market", old_market)
        # 锁旧、新两条时间线（排序取锁避免死锁）
        for lock_symbol, lock_market in sorted({(old_symbol, old_market), (new_symbol, new_market)}):
            lock_security_timeline(db, current_user.id, lock_symbol, lock_market)
        validate_owned_references(db, current_user.id, update_data)
        for field, value in update_data.items():
            setattr(db_action, field, value)
        db.flush()

        # 如果是会影响持仓的公司行动，重新计算持仓
        if old_action_type in ["STOCK_DIVIDEND", "RIGHTS_ISSUE", "STOCK_SPLIT", "REVERSE_SPLIT", "BONUS_ISSUE"]:
            recalculate_holdings(db, current_user.id, old_symbol, old_market, commit=False)
            if db_action.symbol != old_symbol or db_action.market != old_market:
                recalculate_holdings(db, current_user.id, db_action.symbol, db_action.market, commit=False)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"该修改会使持仓重放失败：{exc}") from exc
    except Exception:
        db.rollback()
        raise

    db.refresh(db_action)
    return db_action


@router.delete("/{action_id:int}", status_code=204)
def delete_corporate_action(
    action_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """删除公司行动记录

    锁序：记录锁 → 锁内重读 → 时间线锁。
    """
    try:
        lock_record(db, "corporate-action-record", action_id)

        db_action = db.query(CorporateAction).filter(
            CorporateAction.id == action_id,
            CorporateAction.user_id == current_user.id
        ).first()
        if not db_action:
            raise HTTPException(status_code=404, detail="公司行动记录不存在")
        db.refresh(db_action)

        _ensure_corporate_action_is_mutable(db, current_user.id, db_action)

        symbol = db_action.symbol
        market = db_action.market
        action_type = db_action.action_type

        lock_security_timeline(db, current_user.id, symbol, market)
        db.delete(db_action)
        db.flush()

        # 如果是会影响持仓的公司行动，重新计算持仓
        if action_type in ["STOCK_DIVIDEND", "RIGHTS_ISSUE", "STOCK_SPLIT", "REVERSE_SPLIT", "BONUS_ISSUE"]:
            recalculate_holdings(db, current_user.id, symbol, market, commit=False)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"删除该公司行动会使持仓重放失败：{exc}") from exc
    except Exception:
        db.rollback()
        raise

    return None


@router.get("/symbol/{symbol}", response_model=List[CorporateActionResponse])
def get_actions_by_symbol(
    symbol: str,
    market: Optional[str] = Query(None, description="市场筛选"),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """获取特定股票的所有公司行动记录。兼容保留：外部 API 客户端可能依赖。"""
    query = db.query(CorporateAction).filter(
        CorporateAction.symbol.ilike(f"%{symbol.strip()}%"),
        CorporateAction.user_id == current_user.id
    )
    if market:
        query = query.filter(CorporateAction.market == market)
    return query.order_by(CorporateAction.ex_date.desc()).all()


# ---------------------------------------------------------------------------
# 分红公告建议（Tushare 同步；仅 A/B 股）与标的事件
# ---------------------------------------------------------------------------


@router.post("/dividend-sync-jobs")
def start_dividend_sync(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """启动分红公告同步 job（去重：每用户单活跃任务）。"""
    _require_tushare_configured()
    job = start_dividend_sync_job(current_user.id)
    if job["status"] == "queued":
        background_tasks.add_task(run_dividend_sync_job, job["id"])
    return job


@router.get("/dividend-sync-jobs/{job_id}")
def get_dividend_sync_status(
    job_id: str,
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    job = get_dividend_sync_job(job_id, current_user.id)
    if not job:
        raise HTTPException(status_code=404, detail="同步任务不存在")
    return job


@router.get("/suggestions", response_model=List[SuggestionResponse])
def list_suggestions(
    status: Optional[str] = Query(None, description="按状态筛选；缺省为 NEW+MATCHED"),
    symbol: Optional[str] = Query(None),
    market: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    query = db.query(CorporateActionSuggestion).filter(
        CorporateActionSuggestion.user_id == current_user.id
    )
    if status:
        query = query.filter(CorporateActionSuggestion.status == status.upper())
    else:
        query = query.filter(CorporateActionSuggestion.status.in_(["NEW", "MATCHED"]))
    if symbol and symbol.strip():
        query = query.filter(CorporateActionSuggestion.symbol == symbol.strip())
    if market:
        query = query.filter(CorporateActionSuggestion.market == market)
    return (
        query.order_by(CorporateActionSuggestion.ex_date.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.get("/suggestions/count")
def count_suggestions(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Dict[str, int]:
    """待处理建议计数（NEW 徽标）。"""
    total = db.query(CorporateActionSuggestion).filter(
        CorporateActionSuggestion.user_id == current_user.id,
        CorporateActionSuggestion.status == "NEW",
    ).count()
    return {"total": total}


@router.post("/suggestions/{suggestion_id}/accept", response_model=CorporateActionResponse)
def accept_dividend_suggestion(
    suggestion_id: int,
    payload: SuggestionAccept,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """接受建议 → 创建正式公司行动记录（同事务重算持仓）。

    仅 NEW 可接受；并发接受由服务层记录锁串行化（第二个会话 409）。
    """
    overrides = payload.model_dump(exclude_unset=True)
    if overrides.get("broker_account_id") is not None:
        get_owned_record(
            db, BrokerAccount, overrides["broker_account_id"], current_user.id,
            "Broker account not found",
        )
    try:
        return accept_suggestion(db, current_user, suggestion_id, overrides)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SuggestionStateError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@router.post("/suggestions/{suggestion_id}/ignore", response_model=SuggestionResponse)
def ignore_dividend_suggestion(
    suggestion_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """忽略建议（幂等）；已接受的不可忽略。状态转换在服务层记录锁内进行。"""
    try:
        return ignore_suggestion(db, current_user.id, suggestion_id)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SuggestionStateError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/suggestions/{suggestion_id}/restore", response_model=SuggestionResponse)
def restore_dividend_suggestion(
    suggestion_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """恢复被忽略的建议到原状态（误点退路）。状态转换在服务层记录锁内进行。"""
    try:
        return restore_suggestion(db, current_user.id, suggestion_id)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SuggestionStateError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/security-events", response_model=List[SecurityEventResponse])
def list_security_events(
    days_ahead: int = Query(90, ge=1, le=365, description="返回未来 N 天内的事件"),
    days_back: int = Query(0, ge=0, le=365, description="额外包含过去 N 天的事件"),
    symbol: Optional[str] = Query(None),
    market: Optional[str] = Query(None),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """当前用户持仓标的的事件（供持仓页事件角标）。

    全局表读取口径（issue #137，与 security_profiles 的口径一致并显式声明）：
    标的事件是全局数据（不分用户），登录即可读；**列表缺省按当前持仓收敛**
    （组合视角），显式传 symbol 时按请求标的返回（单标的视角，允许查未持仓
    标的——与 /{market}/{symbol}/profile 同口径）。
    """
    today = date.today()
    query = db.query(SecurityEvent).filter(
        SecurityEvent.event_date >= today - timedelta(days=days_back),
        SecurityEvent.event_date <= today + timedelta(days=days_ahead),
    )
    if symbol:
        query = query.filter(SecurityEvent.symbol == symbol)
        if market:
            query = query.filter(SecurityEvent.market == market)
    else:
        held = (
            db.query(Holding.symbol, Holding.market)
            .filter(Holding.user_id == current_user.id, Holding.quantity > 0)
            .distinct()
            .all()
        )
        if not held:
            return []
        # 持仓键下推为 SQL IN：此前把窗口内全部事件 load 进 Python 再过滤
        query = query.filter(
            tuple_(SecurityEvent.symbol, SecurityEvent.market).in_(
                [(s, m) for s, m in held]
            )
        )
    return query.order_by(SecurityEvent.event_date.asc()).all()
