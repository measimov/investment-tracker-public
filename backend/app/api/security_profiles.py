"""标的档案 API：基本面数据、LLM 分析与分析任务。

**全局表读取口径**（issue #137，全仓三处读取端点统一为这一条并各自声明）：
分析与档案是全局数据（不分用户），登录即可读；**列表端点缺省按当前持仓
收敛**（组合视角，如 /analyses），**单标的端点按请求标的返回**（允许查
未持仓标的——详情页在建仓前调研正是这个场景）。corporate_actions 的
/security-events 同口径。

分析任务按用户入队（每用户单活跃任务去重）。支持市场见 SUPPORTED_MARKETS
（A股/美股/港股），其他市场显式 409。
"""

from typing import Any, Dict, List, Optional

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
    """当前用户持仓标的的最新分析摘要（持仓页 AI 标签列，一次取全）。

    列表端点：按持仓收敛（见模块 docstring 的全局表读取口径）。
    """
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
        ensure_no_conflicting_analysis_job(db, current_user.id, BATCH_JOB_TYPE)
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
    db: Session = Depends(get_db),
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
        ensure_no_conflicting_analysis_job(db, current_user.id, "security_analysis")
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
    """最新一条完整分析（含 Markdown 全文）。

    单标的端点：允许查未持仓标的（见模块 docstring 的全局表读取口径）。
    """
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
    """基本面档案（分组、封顶）+ 标的事件 + 财报摘要，供详情页展示。

    单标的端点：允许查未持仓标的（见模块 docstring 的全局表读取口径）。
    """
    from ..services.report_digest_service import digest_progress, load_report_digests
    from ..services.report_statement_service import STATEMENT_MARKETS, statement_progress

    from ..services.business_profile_service import load_business_profile
    from ..services.earnings_quality import compute_earnings_quality, market_statements
    from ..services.security_profile_service import compute_graham_for

    profile = load_symbol_profile(db, symbol, market)
    profile["events"] = load_security_events_for(db, symbol, market)
    profile["supported"] = market in SUPPORTED_MARKETS
    profile["capabilities"] = MARKET_CAPABILITIES.get(market, {})
    profile["report_digests"] = load_report_digests(db, symbol, market)
    profile["digest_progress"] = digest_progress(db, symbol, market)
    # 港股三张报表抽取进度（其他市场 None）：与 capabilities.statements 配对
    profile["statement_progress"] = (
        statement_progress(db, symbol, market) if market in STATEMENT_MARKETS else None
    )
    profile["business"] = load_business_profile(db, symbol, market)
    # 按市场取报表行（美股=EDGAR 透视、港股=Yahoo 透视），与分析输入同口径
    statements = market_statements(market, profile["datasets"])
    profile["earnings_quality"] = compute_earnings_quality(
        statements["income"],
        statements["balancesheet"],
        statements["cashflow"],
        statements["fina_indicator"],
    )
    # 准则取数走年度行专取口径（caps 窗口的季报会挤掉年度行，见
    # load_graham_inputs 注释），与分析输入一致
    profile["graham_screen"] = compute_graham_for(db, symbol, market) or {
        "status": "no_data"
    }
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
    db: Session = Depends(get_db),
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
        ensure_no_conflicting_analysis_job(db, current_user.id, "report_digest_backfill")
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
        ensure_no_conflicting_analysis_job(db, current_user.id, DIGEST_BATCH_JOB_TYPE)
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


# --------------------------------------------------------------------------- #
# 雪球观点摘要（数据源 = xueqiu-timeline-archiver 写入同库的关注用户发言）。
# 全局产物读取口径与 /analyses 一致：列表端点按持仓∪自选收敛，单标的端点
# 按请求标的返回。全部新路由首段独立（opinion-*），不与 /{market}/{symbol}
# 通配互吞。
# --------------------------------------------------------------------------- #


def _latest_opinions(db: Session, pairs: List[tuple]) -> Dict[tuple, Any]:
    """给定 (symbol, market) 对，各取最新一条摘要。"""
    from ..models.security_opinion import SecurityOpinionSummary

    if not pairs:
        return {}
    rows = (
        db.query(SecurityOpinionSummary)
        .filter(SecurityOpinionSummary.symbol.in_({s for s, _ in pairs}))
        .order_by(
            SecurityOpinionSummary.created_at.desc(), SecurityOpinionSummary.id.desc()
        )
        .all()
    )
    wanted = set(pairs)
    latest: Dict[tuple, Any] = {}
    for row in rows:
        pair = (row.symbol, row.market)
        if pair in wanted and pair not in latest:
            latest[pair] = row
    return latest


def _opinion_row(summary) -> Dict[str, Any]:
    return {
        "id": summary.id,
        "symbol": summary.symbol,
        "market": summary.market,
        "name": summary.name,
        "tags": summary.tags,
        "summary": summary.summary,
        "author_stances": summary.author_stances,
        "utterance_count": summary.utterance_count,
        "recent_utterance_count": summary.recent_utterance_count,
        "recent_days": summary.recent_days,
        "latest_utterance_at": (
            summary.latest_utterance_at.isoformat() if summary.latest_utterance_at else None
        ),
        "created_at": summary.created_at.isoformat() if summary.created_at else None,
    }


@router.get("/opinion-summaries")
def list_opinion_summaries(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """持仓∪自选的观点概览（角标与观点页共用）。

    表不可用时 source_available=false、计数字段置 null，但**已存的摘要照常
    返回**——历史产物不因数据源下线而消失，且绝不静默返回空冒充"无观点"。
    """
    from datetime import datetime, timedelta, timezone

    from ..services.opinion_summary_batch_jobs import candidate_opinion_targets
    from ..services.xueqiu_opinion_source import (
        build_wanted_map,
        scan_matched_utterances,
        source_freshness,
    )
    from ..config import settings

    freshness = source_freshness(db)
    candidates = candidate_opinion_targets(db, current_user.id)
    pairs = [(t["symbol"], t["market"]) for t in candidates]
    latest = _latest_opinions(db, pairs)

    matched: Dict[str, list] = {}
    wanted: Dict[str, tuple] = {}
    if freshness["available"] and candidates:
        wanted = build_wanted_map(pairs)
        since = datetime.now(timezone.utc) - timedelta(
            days=settings.xueqiu_opinion_lookback_days
        )
        matched = scan_matched_utterances(db, set(wanted), since=since)
    reverse = {pair: key for key, pair in wanted.items()}

    items: List[Dict[str, Any]] = []
    for target in candidates:
        pair = (target["symbol"], target["market"])
        summary = latest.get(pair)
        rows = matched.get(reverse.get(pair, ""), [])
        if summary is None and not rows:
            continue  # 既无摘要也无匹配：不进列表
        if freshness["available"]:
            matched_count = len(rows)
            anchor = summary.latest_utterance_at if summary else None
            if anchor is not None and anchor.tzinfo is None:
                anchor = anchor.replace(tzinfo=timezone.utc)
            new_count = (
                sum(1 for row in rows if anchor is None or row["created_at"] > anchor)
            )
        else:
            matched_count = None
            new_count = None
        item = (
            _opinion_row(summary)
            if summary is not None
            else {
                "id": None, "symbol": target["symbol"], "market": target["market"],
                "name": None, "tags": [], "summary": None, "author_stances": [],
                "utterance_count": None, "recent_utterance_count": None,
                "recent_days": None, "latest_utterance_at": None, "created_at": None,
            }
        )
        item["origin"] = target["origin"]
        item["matched_count"] = matched_count
        item["new_utterance_count"] = new_count
        items.append(item)

    return {
        "source_available": freshness["available"],
        "freshness": freshness,
        "recent_days": settings.xueqiu_opinion_recent_days,
        "items": items,
    }


@router.get("/opinion-feed")
def get_opinion_feed(
    days: int = Query(default=30, ge=1, le=365),
    per_author: int = Query(default=50, ge=1, le=200),
    symbol: Optional[str] = Query(default=None, max_length=20),
    market: Optional[str] = Query(default=None, max_length=20),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """作者维度的匹配发言流。

    - 缺省：持仓∪自选全目标（观点页「作者动态」tab）。
    - 带 symbol+market：仅该标的（详情页观点区的「相关作者动态」，允许查
      未持仓标的——与单标的端点口径一致）。

    截断按**逐作者**封顶（per_author），不做全局截断：全局截断会让高产作者
    挤掉其他人的整段历史——实测 30 天窗口一位作者 187 条、全局 200 条上限时
    其余作者几乎整体消失（2026-09-03 反馈的"只有管我财"一半根因；另一半是
    前端整块堆叠的展示埋没）。每组返回 total 供前端展示"另有 N 条未显示"。
    作者按各自最新发言时间倒序排列。
    """
    from datetime import datetime, timedelta, timezone

    from ..services.opinion_summary_jobs import OPINION_MARKETS
    from ..services.opinion_summary_batch_jobs import candidate_opinion_targets
    from ..services.xueqiu_opinion_source import (
        KIND_LABELS,
        OpinionSourceUnavailable,
        build_wanted_map,
        scan_matched_utterances,
        source_freshness,
    )

    if (symbol is None) != (market is None):
        raise HTTPException(status_code=422, detail="symbol 与 market 必须成对提供")
    if market is not None and market not in OPINION_MARKETS:
        raise HTTPException(
            status_code=409,
            detail=f"{market} 暂不支持观点数据（支持：{'/'.join(OPINION_MARKETS)}）",
        )

    freshness = source_freshness(db)
    if not freshness["available"]:
        return {"source_available": False, "freshness": freshness, "authors": []}
    if symbol is not None and market is not None:
        pairs = [(symbol, market)]
    else:
        candidates = candidate_opinion_targets(db, current_user.id)
        pairs = [(t["symbol"], t["market"]) for t in candidates]
    wanted = build_wanted_map(pairs)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        matched = scan_matched_utterances(db, set(wanted), since=since)
    except OpinionSourceUnavailable:
        return {"source_available": False, "freshness": freshness, "authors": []}

    # 同一条发言可命中多标的：按 utterance_key 去重合并命中标的
    by_key: Dict[str, Dict[str, Any]] = {}
    for xq_key, rows in matched.items():
        row_symbol, row_market = wanted[xq_key]
        for row in rows:
            entry = by_key.setdefault(
                row["utterance_key"],
                {
                    "author": row["author_name"],
                    "date": row["created_at"].isoformat() if row["created_at"] else None,
                    "sort_key": row["created_at"],
                    "kind": KIND_LABELS.get(row["kind"], row["kind"]),
                    "text": (row["text"] or "")[:300],
                    "context": (row["context_text"] or "")[:150] or None,
                    "post_url": row["post_url"],
                    "symbols": [],
                },
            )
            entry["symbols"].append({"symbol": row_symbol, "market": row_market})

    epoch = datetime.min.replace(tzinfo=timezone.utc)
    ordered = sorted(
        by_key.values(), key=lambda item: item["sort_key"] or epoch, reverse=True
    )
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for entry in ordered:
        entry.pop("sort_key", None)
        grouped.setdefault(entry.pop("author") or "未知作者", []).append(entry)
    # 作者按各自最新发言倒序；ordered 已全局倒序，插入序即该序
    return {
        "source_available": True,
        "freshness": freshness,
        "authors": [
            {"author": name, "total": len(items), "items": items[:per_author]}
            for name, items in grouped.items()
        ],
    }


@router.get("/opinion-jobs/{job_id}")
def get_opinion_job(
    job_id: str,
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    from ..services.opinion_summary_jobs import get_opinion_summary_job

    job = get_opinion_summary_job(job_id, current_user.id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@router.post("/opinion-batch-jobs")
def start_opinion_batch(
    background_tasks: BackgroundTasks,
    force: bool = Query(default=False),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    from ..services.opinion_summary_batch_jobs import (
        run_opinion_batch_job,
        start_opinion_batch_job,
    )
    from ..services.xueqiu_opinion_source import OpinionSourceUnavailable

    if not is_llm_configured():
        raise HTTPException(status_code=409, detail="未配置 LLM API Key，无法生成观点摘要")
    try:
        ensure_no_conflicting_analysis_job(db, current_user.id, "opinion_summary_batch")
        job = start_opinion_batch_job(db, current_user.id, force=force)
    except OpinionSourceUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except NoBatchTargetsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AnalysisBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if job["status"] == "queued":
        background_tasks.add_task(run_opinion_batch_job, job["id"])
    return job


@router.get("/opinion-batch-targets")
def preview_opinion_batch_targets(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    from ..services.opinion_summary_batch_jobs import get_opinion_batch_targets

    return get_opinion_batch_targets(db, current_user.id)


@router.get("/opinion-batch-jobs/{job_id}")
def get_opinion_batch(
    job_id: str,
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    from ..services.opinion_summary_batch_jobs import get_opinion_batch_job

    job = get_opinion_batch_job(job_id, current_user.id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@router.post("/opinion-batch-jobs/{job_id}/cancel")
def cancel_opinion_batch(
    job_id: str,
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    from ..services.opinion_summary_batch_jobs import request_opinion_batch_cancel

    job = request_opinion_batch_cancel(job_id, current_user.id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已结束")
    return job


@router.post("/{market}/{symbol}/opinion-jobs")
def start_opinion_job(
    market: str,
    symbol: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    from ..services.opinion_summary_jobs import (
        OPINION_MARKETS,
        run_opinion_summary_job,
        start_opinion_summary_job,
    )
    from ..services.xueqiu_opinion_source import (
        OpinionSourceUnavailable,
        ensure_opinion_source,
    )

    if market not in OPINION_MARKETS:
        raise HTTPException(
            status_code=409,
            detail=f"{market} 暂不支持观点摘要（支持：{'/'.join(OPINION_MARKETS)}）",
        )
    if not is_llm_configured():
        raise HTTPException(status_code=409, detail="未配置 LLM API Key，无法生成观点摘要")
    try:
        ensure_opinion_source(db)
        ensure_no_conflicting_analysis_job(db, current_user.id, "opinion_summary")
        job = start_opinion_summary_job(current_user.id, symbol, market)
    except OpinionSourceUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AnalysisBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if job["status"] == "queued":
        background_tasks.add_task(run_opinion_summary_job, job["id"])
    return job


@router.get("/{market}/{symbol}/opinion-summary")
def get_opinion_summary(
    market: str,
    symbol: str,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """最新观点摘要全文 + 上一条的标签快照（前端展示"较上次变化"）。"""
    from ..models.security_opinion import SecurityOpinionSummary

    rows = (
        db.query(SecurityOpinionSummary)
        .filter(
            SecurityOpinionSummary.symbol == symbol,
            SecurityOpinionSummary.market == market,
        )
        .order_by(
            SecurityOpinionSummary.created_at.desc(), SecurityOpinionSummary.id.desc()
        )
        .limit(2)
        .all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="该标的暂无观点摘要")
    payload = _opinion_row(rows[0])
    payload["content"] = rows[0].content
    payload["model"] = rows[0].model
    payload["total_tokens"] = rows[0].total_tokens
    payload["lookback_days"] = rows[0].lookback_days
    payload["previous"] = (
        {
            "tags": rows[1].tags,
            "summary": rows[1].summary,
            "created_at": rows[1].created_at.isoformat() if rows[1].created_at else None,
        }
        if len(rows) > 1
        else None
    )
    return payload
