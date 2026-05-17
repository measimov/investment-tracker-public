from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import date
from ..database import get_db
from ..models.corporate_action import CorporateAction
from ..models.user import User
from ..schemas.corporate_action import (
    CorporateActionCreate,
    CorporateActionUpdate,
    CorporateActionResponse,
    CashDividendCreate,
    StockDividendCreate
)
from ..services.holding_service import recalculate_holdings
from ..core.deps import get_current_active_user

router = APIRouter()


def _build_corporate_action_query(
    db: Session,
    user_id: int,
    symbol: Optional[str] = None,
    market: Optional[str] = None,
    action_type: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
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
    db_action = CorporateAction(**action.model_dump(), user_id=current_user.id)
    db.add(db_action)
    db.commit()
    db.refresh(db_action)

    # 如果是会影响持仓的公司行动，重新计算持仓
    if action.action_type in ["STOCK_DIVIDEND", "RIGHTS_ISSUE", "STOCK_SPLIT", "REVERSE_SPLIT", "BONUS_ISSUE"]:
        recalculate_holdings(db, current_user.id, action.symbol, action.market)

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
    ).count()
    return {"total": total}


@router.get("/symbol/{symbol}", response_model=List[CorporateActionResponse])
def get_actions_by_symbol(
    symbol: str,
    market: Optional[str] = Query(None, description="市场筛选"),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """获取特定股票的所有公司行动记录"""
    query = db.query(CorporateAction).filter(
        CorporateAction.symbol.ilike(f"%{symbol.strip()}%"),
        CorporateAction.user_id == current_user.id
    )

    if market:
        query = query.filter(CorporateAction.market == market)

    actions = query.order_by(CorporateAction.ex_date.desc()).all()
    return actions


@router.get("/statistics/summary")
def get_corporate_actions_summary(
    symbol: Optional[str] = Query(None, description="股票代码"),
    market: Optional[str] = Query(None, description="市场"),
    start_date: Optional[date] = Query(None, description="开始日期"),
    end_date: Optional[date] = Query(None, description="结束日期"),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    获取公司行动统计摘要

    包括总股息收入、税费等
    """
    from decimal import Decimal

    query = _build_corporate_action_query(
        db,
        current_user.id,
        symbol=symbol,
        market=market,
        start_date=start_date,
        end_date=end_date,
    )

    # 按类型统计
    actions = query.all()

    summary = {
        "total_count": len(actions),
        "by_type": {},
        "cash_dividends": {
            "count": 0,
            "total_dividend": Decimal("0"),
            "total_tax": Decimal("0"),
            "net_dividend": Decimal("0")
        }
    }

    for action in actions:
        # 按类型统计
        if action.action_type not in summary["by_type"]:
            summary["by_type"][action.action_type] = 0
        summary["by_type"][action.action_type] += 1

        # 现金股息统计
        if action.action_type == "CASH_DIVIDEND":
            summary["cash_dividends"]["count"] += 1
            if action.total_dividend:
                summary["cash_dividends"]["total_dividend"] += action.total_dividend
            if action.tax_withheld:
                summary["cash_dividends"]["total_tax"] += action.tax_withheld
            if action.net_dividend:
                summary["cash_dividends"]["net_dividend"] += action.net_dividend

    # 转换Decimal为float以便JSON序列化
    summary["cash_dividends"]["total_dividend"] = float(summary["cash_dividends"]["total_dividend"])
    summary["cash_dividends"]["total_tax"] = float(summary["cash_dividends"]["total_tax"])
    summary["cash_dividends"]["net_dividend"] = float(summary["cash_dividends"]["net_dividend"])

    return summary


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
    """更新公司行动记录"""
    db_action = db.query(CorporateAction).filter(
        CorporateAction.id == action_id,
        CorporateAction.user_id == current_user.id
    ).first()
    if not db_action:
        raise HTTPException(status_code=404, detail="公司行动记录不存在")

    # 记录旧的symbol和market以便重新计算持仓
    old_symbol = db_action.symbol
    old_market = db_action.market
    old_action_type = db_action.action_type

    # 更新字段
    update_data = action_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(db_action, field, value)

    db.commit()
    db.refresh(db_action)

    # 如果是会影响持仓的公司行动，重新计算持仓
    if old_action_type in ["STOCK_DIVIDEND", "RIGHTS_ISSUE", "STOCK_SPLIT", "REVERSE_SPLIT", "BONUS_ISSUE"]:
        # 重新计算旧的symbol/market
        recalculate_holdings(db, current_user.id, old_symbol, old_market)
        # 如果symbol或market改变了，也重新计算新的
        if db_action.symbol != old_symbol or db_action.market != old_market:
            recalculate_holdings(db, current_user.id, db_action.symbol, db_action.market)

    return db_action


@router.delete("/{action_id:int}", status_code=204)
def delete_corporate_action(
    action_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """删除公司行动记录"""
    db_action = db.query(CorporateAction).filter(
        CorporateAction.id == action_id,
        CorporateAction.user_id == current_user.id
    ).first()
    if not db_action:
        raise HTTPException(status_code=404, detail="公司行动记录不存在")

    symbol = db_action.symbol
    market = db_action.market
    action_type = db_action.action_type

    db.delete(db_action)
    db.commit()

    # 如果是会影响持仓的公司行动，重新计算持仓
    if action_type in ["STOCK_DIVIDEND", "RIGHTS_ISSUE", "STOCK_SPLIT", "REVERSE_SPLIT", "BONUS_ISSUE"]:
        recalculate_holdings(db, current_user.id, symbol, market)

    return None
