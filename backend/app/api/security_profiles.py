"""标的档案 API：基本面数据、LLM 分析与分析任务。

分析与档案是全局数据（不分用户）：登录即可读；分析任务按用户入队
（每用户单活跃任务去重）。仅 A 股支持基本面数据，其他市场显式 409。
"""

from typing import Any, Dict, List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from ..core.deps import get_current_active_user
from ..database import get_db
from ..models.holding import Holding
from ..models.security_profile import SecurityAnalysis
from ..models.user import User
from ..services.llm_client import is_llm_configured
from ..services.security_analysis_jobs import (
    AnalysisBusyError,
    get_security_analysis_job,
    run_security_analysis_job,
    start_security_analysis_job,
)
from ..services.security_profile_service import (
    SUPPORTED_MARKETS,
    load_security_events_for,
    load_symbol_profile,
)

router = APIRouter()


def _latest_analysis(db: Session, symbol: str, market: str) -> SecurityAnalysis | None:
    return (
        db.query(SecurityAnalysis)
        .filter(SecurityAnalysis.symbol == symbol, SecurityAnalysis.market == market)
        .order_by(SecurityAnalysis.created_at.desc(), SecurityAnalysis.id.desc())
        .first()
    )


def _analysis_summary(analysis: SecurityAnalysis) -> Dict[str, Any]:
    return {
        "id": analysis.id,
        "symbol": analysis.symbol,
        "market": analysis.market,
        "name": analysis.name,
        "tags": analysis.tags,
        "risk_level": analysis.risk_level,
        "summary": analysis.summary,
        "created_at": analysis.created_at.isoformat() if analysis.created_at else None,
        "data_fetched_at": analysis.data_fetched_at.isoformat() if analysis.data_fetched_at else None,
    }


@router.get("/analyses", response_model=List[Dict[str, Any]])
def list_holding_analyses(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """当前用户持仓标的的最新分析摘要（持仓页 AI 标签列，一次取全）。"""
    held = (
        db.query(Holding.symbol, Holding.market)
        .filter(Holding.user_id == current_user.id, Holding.quantity > 0)
        .distinct()
        .all()
    )
    results = []
    for symbol, market in held:
        analysis = _latest_analysis(db, symbol, market)
        if analysis:
            results.append(_analysis_summary(analysis))
    return results


@router.get("/analysis-jobs/{job_id}")
def get_analysis_job(
    job_id: str,
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    job = get_security_analysis_job(job_id, current_user.id)
    if not job:
        raise HTTPException(status_code=404, detail="分析任务不存在")
    return job


@router.post("/{market}/{symbol}/analysis-jobs")
def start_analysis(
    market: str,
    symbol: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """启动标的分析（同步基本面 → LLM 生成；每用户单活跃任务去重）。"""
    if market not in SUPPORTED_MARKETS:
        raise HTTPException(
            status_code=409, detail=f"{market} 暂不支持基本面数据分析（仅 A 股）"
        )
    if not is_llm_configured():
        raise HTTPException(
            status_code=409,
            detail="未配置 LLM API Key（LLM_REPORT_API_KEY），无法生成标的分析。",
        )
    try:
        job = start_security_analysis_job(current_user.id, symbol, market)
    except AnalysisBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if job["status"] == "queued":
        background_tasks.add_task(run_security_analysis_job, job["id"])
    return job


@router.get("/{market}/{symbol}/analysis")
def get_latest_analysis(
    market: str,
    symbol: str,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """最新一条完整分析（含 Markdown 全文）。"""
    analysis = _latest_analysis(db, symbol, market)
    if not analysis:
        raise HTTPException(status_code=404, detail="该标的暂无分析，请先生成")
    return {
        **_analysis_summary(analysis),
        "content": analysis.content,
        "model": analysis.model,
        "total_tokens": analysis.total_tokens,
    }


@router.get("/{market}/{symbol}/profile")
def get_symbol_profile(
    market: str,
    symbol: str,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """基本面档案（分组、封顶）+ 标的事件，供详情页表格展示。"""
    profile = load_symbol_profile(db, symbol, market)
    profile["events"] = load_security_events_for(db, symbol, market)
    profile["supported"] = market in SUPPORTED_MARKETS
    return profile
