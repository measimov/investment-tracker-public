"""标的档案 API：基本面数据、LLM 分析与分析任务。

分析与档案是全局数据（不分用户）：登录即可读；分析任务按用户入队
（每用户单活跃任务去重）。支持市场见 SUPPORTED_MARKETS（A股/美股/港股），其他市场显式 409。
"""

from typing import Any, Dict, List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..core.deps import get_current_active_user
from ..database import get_db
from ..models.holding import Holding
from ..models.security_profile import SecurityAnalysis
from ..models.user import User
from ..services.llm_client import is_llm_configured
from ..services.security_analysis_batch_jobs import (
    ANALYSIS_EXCLUSIVE_JOB_TYPES,
    NoBatchTargetsError,
    ensure_no_conflicting_analysis_job,
    get_batch_analysis_job,
    get_batch_analysis_targets,
    request_batch_cancel,
    run_batch_analysis_job,
    start_batch_analysis_job,
)
from ..services.security_analysis_batch_jobs import JOB_TYPE as BATCH_JOB_TYPE
from ..services.security_analysis_jobs import (
    AnalysisBusyError,
    get_security_analysis_job,
    run_security_analysis_job,
    start_security_analysis_job,
)
from ..services.security_profile_service import (
    MARKET_CAPABILITIES,
    SUPPORTED_MARKETS,
    load_security_events_for,
    load_symbol_profile,
)

router = APIRouter()

# 节选预览长度：够看清抽到的是不是正确章节，又不至于把整章塞进响应
SECTION_PREVIEW_CHARS = 5_000


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


# ---------------------------------------------------------------------------
# 批量分析（持仓页一键分析）
#
# 路由命名注意：上面的 `/analysis-jobs/{job_id}` 声明在前，任何形如
# `GET /analysis-jobs/<字面量>` 的两段路由都会被它吞掉（job_id="batch" → 404）。
# 因此批量与活跃任务查询都用独立首段，与声明顺序无关。
# ---------------------------------------------------------------------------


@router.post("/analysis-batch-jobs")
def start_batch_analysis(
    background_tasks: BackgroundTasks,
    include_report_digests: bool = Query(
        False, description="是否顺带补齐财报摘要（慢：每只 6-12 分钟）"
    ),
    force: bool = Query(False, description="忽略新鲜度窗口，强制重新分析"),
    freshness_hours: int | None = Query(None, ge=0, le=720),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """对当前持仓的全部可分析标的逐个生成分析（串行，可能运行数十分钟）。"""
    if not is_llm_configured():
        raise HTTPException(
            status_code=409,
            detail="未配置 LLM API Key（LLM_REPORT_API_KEY），无法生成标的分析。",
        )
    try:
        ensure_no_conflicting_analysis_job(current_user.id, BATCH_JOB_TYPE)
        job = start_batch_analysis_job(
            db, current_user.id,
            include_report_digests=include_report_digests,
            force=force,
            freshness_hours=freshness_hours,
        )
    except NoBatchTargetsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AnalysisBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if job["status"] == "queued":
        background_tasks.add_task(run_batch_analysis_job, job["id"])
    return job


@router.get("/analysis-batch-targets")
def preview_batch_analysis_targets(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """批量分析的目标预览（前端确认框的数量与耗时估算必须与真实目标一致）。

    仅按"支持的市场"在前端本地估算会虚高：后端还要排除已清仓、EXCLUDE 与
    CASH_MANAGEMENT 规则命中的标的，用户持有货币基金时会看到虚高的数量与
    token 估算，启动后 job.total 又突然变小。
    """
    targets = get_batch_analysis_targets(db, current_user.id)
    return {"total": len(targets), "targets": targets}


@router.get("/analysis-batch-jobs/{job_id}")
def get_batch_analysis(
    job_id: str,
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    job = get_batch_analysis_job(job_id, current_user.id)
    if not job:
        raise HTTPException(status_code=404, detail="批量分析任务不存在")
    return job


@router.post("/analysis-batch-jobs/{job_id}/cancel")
def cancel_batch_analysis(
    job_id: str,
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """请求终止：当前标的跑完即收尾，已生成的分析保留。"""
    job = request_batch_cancel(job_id, current_user.id)
    if not job:
        raise HTTPException(status_code=404, detail="批量分析任务不存在")
    return job


@router.get("/active-analysis-jobs", response_model=List[Dict[str, Any]])
def list_active_analysis_jobs(
    current_user: User = Depends(get_current_active_user),
) -> List[Dict[str, Any]]:
    """该用户全部活跃的分析类任务（刷新页面后恢复进度显示用）。

    无活跃任务返回空列表而非 404——404 会触发前端的全局错误通知，
    而"当前没有任务"是完全正常的状态。
    """
    from ..services.background_job_store import find_active_job_of_types

    active = find_active_job_of_types(current_user.id, ANALYSIS_EXCLUSIVE_JOB_TYPES)
    return [active] if active else []


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
            status_code=409,
            detail=(
                f"{market} 暂不支持基本面数据分析"
                f"（支持：{'/'.join(SUPPORTED_MARKETS)}）"
            ),
        )
    if not is_llm_configured():
        raise HTTPException(
            status_code=409,
            detail="未配置 LLM API Key（LLM_REPORT_API_KEY），无法生成标的分析。",
        )
    try:
        # 跨类型互斥：批量分析进行中时不再受理单标的（会双倍打外部 API）
        ensure_no_conflicting_analysis_job(current_user.id, "security_analysis")
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
    """基本面档案（分组、封顶）+ 标的事件 + 财报摘要，供详情页展示。"""
    from ..services.report_digest_service import digest_progress, load_report_digests

    from ..services.business_profile_service import load_business_profile
    from ..services.earnings_quality import compute_earnings_quality, market_statements

    profile = load_symbol_profile(db, symbol, market)
    profile["events"] = load_security_events_for(db, symbol, market)
    profile["supported"] = market in SUPPORTED_MARKETS
    profile["capabilities"] = MARKET_CAPABILITIES.get(market, {})
    profile["report_digests"] = load_report_digests(db, symbol, market)
    profile["digest_progress"] = digest_progress(db, symbol, market)
    profile["business"] = load_business_profile(db, symbol, market)
    # 按市场取报表行（美股=EDGAR 透视、港股=Yahoo 透视），与分析输入同口径
    statements = market_statements(market, profile["datasets"])
    profile["earnings_quality"] = compute_earnings_quality(
        statements["income"],
        statements["balancesheet"],
        statements["cashflow"],
        statements["fina_indicator"],
    )
    return profile


@router.get("/{market}/{symbol}/report-sections")
def get_report_sections(
    market: str,
    symbol: str,
    full: bool = Query(False, description="返回章节全文（默认只回每节前若干字符）"),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> List[Dict[str, Any]]:
    """财报原文节选（最近 3 份**成功**记录，惰性加载——payload 大，不并入
    profile 响应）。

    成功状态在 SQL 层过滤：先 limit 再过滤会让最近三期恰好都失败时返回空
    数组，即便更早的报告期已有可用节选。

    默认每节只回前 `SECTION_PREVIEW_CHARS` 字符：抽取期不再截断后单节可达十万
    字符量级，三份报告的全文足以让这个响应到 MB 级。要全文用 `?full=1`。
    """
    from ..models.security_profile import SecurityProfileData

    rows = (
        db.query(SecurityProfileData)
        .filter(
            SecurityProfileData.symbol == symbol,
            SecurityProfileData.market == market,
            SecurityProfileData.dataset == "report_section",
            SecurityProfileData.payload["extract_status"].as_string() == "ok",
        )
        .order_by(SecurityProfileData.period_key.desc())
        .limit(3)
        .all()
    )
    items: List[Dict[str, Any]] = []
    for row in rows:
        payload = dict(row.payload or {})
        sections = payload.get("sections") or {}
        if not full:
            payload["sections"] = {
                name: body[:SECTION_PREVIEW_CHARS] for name, body in sections.items()
            }
            payload["truncated_preview"] = {
                name: len(body) > SECTION_PREVIEW_CHARS for name, body in sections.items()
            }
        items.append({"period_key": row.period_key, **payload})
    return items


@router.post("/{market}/{symbol}/report-backfill-jobs")
def start_report_backfill(
    market: str,
    symbol: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """启动财报摘要回填（每次最多补 4 份，可重复触发续跑至补齐十年）。"""
    from ..services.report_digest_jobs import (
        run_report_backfill_job,
        start_report_backfill_job,
    )

    from ..services.report_digest_service import REPORT_MARKETS

    if market not in REPORT_MARKETS:
        raise HTTPException(
            status_code=409,
            detail=f"{market} 暂不支持财报摘要（支持：{'/'.join(REPORT_MARKETS)}）",
        )
    if not is_llm_configured():
        raise HTTPException(
            status_code=409,
            detail="未配置 LLM API Key（LLM_REPORT_API_KEY），无法生成报告摘要。",
        )
    try:
        ensure_no_conflicting_analysis_job(current_user.id, "report_digest_backfill")
        job = start_report_backfill_job(current_user.id, symbol, market)
    except AnalysisBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if job["status"] == "queued":
        background_tasks.add_task(run_report_backfill_job, job["id"])
    return job


@router.get("/digest-backfill-preview")
def preview_digest_backfill_targets(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """批量回填确认框数据（纯 DB 统计，不打任何外部数据源）。"""
    from ..services.report_digest_batch_jobs import preview_digest_backfill

    return preview_digest_backfill(db, current_user.id)


@router.post("/digest-backfill-jobs")
def start_digest_batch_backfill(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """批量财报摘要回填：全部持仓标的、每标的每轮最多补 4 份，可重复触发续跑加深。"""
    from ..services.report_digest_batch_jobs import (
        JOB_TYPE as DIGEST_BATCH_JOB_TYPE,
    )
    from ..services.report_digest_batch_jobs import (
        run_digest_batch_job,
        start_digest_batch_job,
    )

    if not is_llm_configured():
        raise HTTPException(
            status_code=409,
            detail="未配置 LLM API Key（LLM_REPORT_API_KEY），无法生成报告摘要。",
        )
    try:
        ensure_no_conflicting_analysis_job(current_user.id, DIGEST_BATCH_JOB_TYPE)
        job = start_digest_batch_job(db, current_user.id)
    except AnalysisBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NoBatchTargetsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if job["status"] == "queued":
        background_tasks.add_task(run_digest_batch_job, job["id"])
    return job


@router.get("/digest-backfill-jobs/{job_id}")
def get_digest_batch_backfill(
    job_id: str,
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    from ..services.report_digest_batch_jobs import get_digest_batch_job

    job = get_digest_batch_job(job_id, current_user.id)
    if not job:
        raise HTTPException(status_code=404, detail="批量回填任务不存在")
    return job


@router.post("/digest-backfill-jobs/{job_id}/cancel")
def cancel_digest_batch_backfill(
    job_id: str,
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """请求终止：当前标的跑完即收尾，已生成的摘要保留，可再次触发续跑。"""
    from ..services.report_digest_batch_jobs import request_digest_batch_cancel

    job = request_digest_batch_cancel(job_id, current_user.id)
    if not job:
        raise HTTPException(status_code=404, detail="批量回填任务不存在")
    return job


@router.get("/report-backfill-jobs/{job_id}")
def get_report_backfill_status(
    job_id: str,
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    from ..services.report_digest_jobs import get_report_backfill_job

    job = get_report_backfill_job(job_id, current_user.id)
    if not job:
        raise HTTPException(status_code=404, detail="回填任务不存在")
    return job
