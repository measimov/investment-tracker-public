from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import Dict, List, Any
from ..database import get_db
from ..models.user import User
from ..core.deps import get_current_active_user
from ..services.statistics_service import (
    get_summary_statistics,
    get_statistics_by_market,
    get_statistics_by_time,
    get_profit_loss_analysis,
    calculate_current_holdings_performance,
    calculate_realized_pnl_fifo,
    get_dividend_summary,
    calculate_total_realized_return,
    calculate_account_total_return
)

router = APIRouter()


@router.get("/summary", response_model=Dict[str, Any])
def get_summary(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Get overall summary statistics."""
    return get_summary_statistics(db, current_user.id)


@router.get("/by-market", response_model=List[Dict[str, Any]])
def get_by_market(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Get statistics grouped by market."""
    return get_statistics_by_market(db, current_user.id)


@router.get("/by-time", response_model=List[Dict[str, Any]])
def get_by_time(
    group_by: str = Query("month", regex="^(month|year)$"),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Get statistics grouped by time period."""
    return get_statistics_by_time(db, current_user.id, group_by)


@router.get("/profit-loss", response_model=List[Dict[str, Any]])
def get_profit_loss(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Get profit/loss analysis."""
    return get_profit_loss_analysis(db, current_user.id)


@router.post("/current-holdings-performance", response_model=Dict[str, Any])
def get_current_holdings_performance(
    current_prices: Dict[str, float],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    获取当前持仓表现（基于FIFO剩余批次）

    请求体示例：
    {
        "600000": 12.5,
        "515180": 1.45,
        "AAPL": 185.0
    }

    返回：
    {
        "unrealized_pnl": 未实现盈亏,
        "current_holdings_cost": 当前持仓成本,
        "unrealized_pnl_rate": 浮盈率,
        "current_market_value": 当前市值,
        "holdings_detail": [...]
    }
    """
    return calculate_current_holdings_performance(db, current_user.id, current_prices)


@router.get("/realized-pnl-fifo", response_model=Dict[str, Any])
def get_realized_pnl_fifo(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    获取已实现盈亏（FIFO方法）

    返回：
    {
        "realized_pnl": 已实现盈亏,
        "sold_cost": 已卖出成本,
        "realized_pnl_rate": 已实现收益率,
        "trades_detail": [...]
    }
    """
    return calculate_realized_pnl_fifo(db, current_user.id)


@router.get("/dividend-summary", response_model=Dict[str, Any])
def get_dividend_summary_api(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    获取股息统计摘要（独立模块）

    返回：
    {
        "total_dividend_gross": 税前总额,
        "total_tax": 总税费,
        "total_dividend_net": 税后总额,
        "by_symbol": [...]
    }
    """
    return get_dividend_summary(db, current_user.id)


@router.get("/total-realized-return", response_model=Dict[str, Any])
def get_total_realized_return(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    获取综合已实现收益。

    Total realized return = realized trading PnL + net dividend income.
    The return rate denominator is sold_cost_cny.
    """
    return calculate_total_realized_return(db, current_user.id)


@router.post("/account-total-return", response_model=Dict[str, Any])
def get_account_total_return(
    current_prices: Dict[str, float],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    获取账户级总收益。

    Total return = realized trading PnL + unrealized PnL + net dividend income.
    Simple return denominator is estimated net invested principal.
    Annualized return uses XIRR over buy/sell/dividend cash flows plus current
    market value as the terminal value.
    """
    return calculate_account_total_return(db, current_user.id, current_prices)
