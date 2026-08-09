from datetime import date
from decimal import Decimal
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Dict, List, Any
from ..database import get_db
from ..models.user import User
from ..core.deps import get_current_active_user
from ..services import benchmark_service
from ..services.statistics import (
    build_portfolio_snapshot,
    get_summary_statistics,
    get_statistics_by_market,
    get_statistics_by_time,
    get_holdings_cost_breakdown,
    calculate_performance_analytics,
    calculate_performance_summary,
    resolve_server_prices,
)
from ..services.performance_history_jobs import (
    get_performance_history_sync_job,
    run_performance_history_sync_job,
    start_performance_history_sync_job,
)

router = APIRouter()


def _validate_date_range(start_date: date | None, end_date: date | None) -> None:
    if start_date and end_date and end_date < start_date:
        raise HTTPException(
            status_code=422,
            detail="end_date must be on or after start_date",
        )


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
    group_by: str = Query("month", pattern="^(month|year)$"),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Get statistics grouped by time period."""
    return get_statistics_by_time(db, current_user.id, group_by)


@router.get("/holdings-cost-breakdown", response_model=List[Dict[str, Any]])
def get_holdings_cost_breakdown_api(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """Get current holdings sorted by total cost (cost distribution chart)."""
    return get_holdings_cost_breakdown(db, current_user.id)


@router.get("/portfolio-snapshot", response_model=Dict[str, Any])
def get_portfolio_snapshot(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """组合快照：一次调用返回看板全量数据（表现/持仓/价格新鲜度/市场/近期交易/对账状态）。

    同时是 LLM 报告（目的③）的结构化输入底座；估算口径与数据质量信号原样携带。
    """
    return build_portfolio_snapshot(db, current_user.id)


@router.post("/performance-summary", response_model=Dict[str, Any])
def get_performance_summary(
    current_prices: Dict[str, float],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    获取统计分析页核心收益卡片数据（手工试算：使用请求体中的价格）。

    This endpoint calculates shared FIFO-dependent metrics once and returns the
    performance cards together so the statistics tab can render them sooner.
    """
    return calculate_performance_summary(db, current_user.id, current_prices)


@router.get("/performance-summary", response_model=Dict[str, Any])
def get_performance_summary_server_priced(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """获取核心收益卡片数据；估值价格由服务端权威数据决定（issue #46）。

    Prices come from Holding.current_price, falling back to the latest cached
    SecurityPrice close, so results are reproducible without client input.
    """
    prices, sources, freshness = resolve_server_prices(db, current_user.id)
    result = calculate_performance_summary(db, current_user.id, prices)
    result["price_sources"] = sources
    result["price_freshness"] = freshness
    return result


MAX_BENCHMARKS = 3


def _parse_benchmarks(raw: str) -> list[str]:
    """逗号分隔的基准 code；未知 code → 422，上限 3 个。空串 = 不算基准。"""
    codes = [code.strip() for code in (raw or "").split(",") if code.strip()]
    if not codes:
        return []
    if len(codes) > MAX_BENCHMARKS:
        raise HTTPException(status_code=422, detail=f"最多同时对比 {MAX_BENCHMARKS} 个基准")
    unknown = [code for code in codes if not benchmark_service.is_valid_benchmark(code)]
    if unknown:
        raise HTTPException(status_code=422, detail=f"未知基准代码: {', '.join(unknown)}")
    return codes


def _run_performance_analytics(
    db: Session,
    user_id: int,
    current_prices: Dict[str, float],
    start_date: date | None,
    end_date: date | None,
    risk_free_rate: Decimal,
    refresh_history: bool,
    benchmarks: str = "",
) -> Dict[str, Any]:
    _validate_date_range(start_date, end_date)
    return calculate_performance_analytics(
        db,
        user_id,
        current_prices,
        start_date=start_date,
        end_date=end_date,
        risk_free_rate=risk_free_rate,
        refresh_history=refresh_history,
        benchmarks=_parse_benchmarks(benchmarks),
    )


@router.post("/performance-analytics", response_model=Dict[str, Any])
def get_performance_analytics(
    current_prices: Dict[str, float],
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    risk_free_rate: Decimal = Query(Decimal("0")),
    refresh_history: bool = Query(False),
    benchmarks: str = Query("", description="逗号分隔的基准指数 code，最多 3 个"),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """
    获取收益率曲线和量化指标（手工试算：使用请求体中的价格）。

    Uses cached daily price history when available. When refresh_history=true,
    the service attempts to pull supported symbols from Tushare before
    calculating the curve.
    """
    return _run_performance_analytics(
        db, current_user.id, current_prices,
        start_date, end_date, risk_free_rate, refresh_history, benchmarks,
    )


@router.get("/performance-analytics", response_model=Dict[str, Any])
def get_performance_analytics_server_priced(
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    risk_free_rate: Decimal = Query(Decimal("0")),
    refresh_history: bool = Query(False),
    benchmarks: str = Query("", description="逗号分隔的基准指数 code，最多 3 个"),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """获取收益率曲线和量化指标；估值价格由服务端权威数据决定（issue #46）。"""
    prices, sources, freshness = resolve_server_prices(db, current_user.id)
    result = _run_performance_analytics(
        db, current_user.id, prices,
        start_date, end_date, risk_free_rate, refresh_history, benchmarks,
    )
    result.setdefault("data_quality", {})["price_sources"] = sources
    result["data_quality"]["price_freshness"] = freshness
    return result


@router.get("/benchmarks", response_model=List[Dict[str, Any]])
def list_benchmarks(
    current_user: User = Depends(get_current_active_user),
):
    """基准指数目录（供前端选择器；无用户数据，登录即可读）。"""
    return benchmark_service.benchmark_catalog()


@router.post("/performance-history-sync", response_model=Dict[str, Any])
def start_performance_history_sync(
    background_tasks: BackgroundTasks,
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    current_user: User = Depends(get_current_active_user),
):
    """Start an incremental Tushare history sync job for performance analytics."""
    _validate_date_range(start_date, end_date)
    job = start_performance_history_sync_job(
        current_user.id,
        start_date=start_date,
        end_date=end_date,
    )
    if job["status"] == "queued":
        background_tasks.add_task(run_performance_history_sync_job, job["id"])
    return job


@router.get("/performance-history-sync/{job_id}", response_model=Dict[str, Any])
def get_performance_history_sync(
    job_id: str,
    current_user: User = Depends(get_current_active_user),
):
    job = get_performance_history_sync_job(job_id, current_user.id)
    if not job:
        raise HTTPException(status_code=404, detail="Performance history sync job not found")
    return job
