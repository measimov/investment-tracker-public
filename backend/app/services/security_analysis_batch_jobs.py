"""批量标的分析 job：对当前持仓逐个跑 analyze_one，串行 + 进度 + 可终止。

为什么必须是**独立 job_type**：background_jobs 的 partial unique index 是
(user_id, job_type)，而 start_security_analysis_job 还要求 data 里的 symbol
匹配——批量任务没有单一 symbol，套不进单标的的槽位。

外呼节流的三层（owner 明确要求不超出各 API 提供方限制）：
1. 各数据源自身的进程内全局串行闸（Tushare 0.35s / cninfo 1s / EDGAR 0.15s）
2. Tushare 接口级自适应冷却（撞限即跳过该数据集并如实标注，不毁掉整只标的）
3. 本模块：标的之间固定停顿 + 市场轮转排序（同一 Tushare 接口的相邻调用被
   其他市场的标的自然拉开）

成本护栏：默认 fast 模式（不新补财报摘要，但仍使用库内已有摘要）、24h 新鲜度
跳过、连续失败早停、致命错误立即中止。
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from ..config import settings
from ..core.logging import get_app_logger
from ..models.holding import Holding
from ..models.security_profile import SecurityAnalysis
from .background_job_store import (
    JobOwnershipLostError,
    create_or_get_active_job,
    get_job,
)
from .job_runtime import (
    batch_execution,
    is_cancel_requested,
    make_batch_progress,
    request_job_cancel,
    run_job_inline,
)
from .job_worker import register_runner
from .llm_client import LLMClientError, LLMNotConfiguredError
from .security_analysis_jobs import (
    ANALYSIS_STAGE_LABELS,
    FATAL_ANALYSIS_ERROR_KINDS,
    AnalysisBusyError,
    analyze_one,
)
from .security_profile_service import SUPPORTED_MARKETS
from .security_rule_service import get_cash_management_symbols, get_excluded_keys
from .stock_price_service import classify_tushare_error

logger = get_app_logger(__name__)
JOB_TYPE = "security_analysis_batch"

# 分析类任务互斥集合：同时跑会对同一批外部 API 双倍消耗
ANALYSIS_EXCLUSIVE_JOB_TYPES = [
    "security_analysis",
    "security_analysis_batch",
    "report_digest_backfill",
    "report_digest_batch",
]

# 连续失败早停：每只标的耗时 1.5-12 分钟且花 LLM token，5 连败等于白烧一小时，
# 因此比 performance_history_jobs 的 5 更严格
MAX_CONSECUTIVE_FAILURES = 3

# 最近结果只保留末尾若干条：几十只标的的完整结果会把 job.data 撑大
RESULTS_KEPT = 50

# 深度模式（顺带补财报摘要）的标的数上限：单只冷启动 6-12 分钟，放开会变成
# 数小时的任务，与"一键"的预期严重不符
FULL_MODE_MAX_SYMBOLS = 5
FULL_MODE_DIGEST_MAX_NEW = 2


class NoBatchTargetsError(Exception):
    """当前没有可分析的持仓标的（API 层映射 409）。"""


def get_batch_analysis_targets(db: Session, user_id: int) -> List[Dict[str, str]]:
    """持仓中可分析的去重标的，按市场轮转排序。

    - quantity>0 且市场在 SUPPORTED_MARKETS 内（持仓是账户维度的，必须去重）
    - 排除 EXCLUDE 与 CASH_MANAGEMENT 规则命中的标的：货币基金做基本面分析
      毫无意义，纯烧 token
    - 市场轮转（A股→美股→港股→…）而非按市场分组：让同一 Tushare 接口的相邻
      两次调用被其他市场的标的自然拉开，等于零成本的接口级降频
    """
    rows = (
        db.query(Holding.symbol, Holding.market)
        .filter(Holding.user_id == user_id, Holding.quantity > 0)
        .distinct()
        .all()
    )
    excluded = get_excluded_keys(db, user_id)
    cash_symbols = get_cash_management_symbols(db, user_id)

    by_market: Dict[str, List[Dict[str, str]]] = {}
    for symbol, market in rows:
        if market not in SUPPORTED_MARKETS:
            continue
        if (symbol, market) in excluded or symbol in cash_symbols:
            continue
        by_market.setdefault(market, []).append({"symbol": symbol, "market": market})

    for items in by_market.values():
        items.sort(key=lambda item: item["symbol"])
    ordered: List[Dict[str, str]] = []
    markets = [market for market in SUPPORTED_MARKETS if market in by_market]
    index = 0
    while any(by_market.get(market) for market in markets):
        market = markets[index % len(markets)]
        bucket = by_market.get(market)
        if bucket:
            ordered.append(bucket.pop(0))
        index += 1
    return ordered


def _target_key(target: Dict[str, str]) -> str:
    return f"{target['market']}|{target['symbol']}"


def _recent_analysis_keys(
    db: Session, targets: List[Dict[str, str]], freshness_hours: int
) -> set:
    """窗口内已分析过的标的键集合（一次分组查询，不要 N 次单查）。"""
    if freshness_hours <= 0 or not targets:
        return set()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=freshness_hours)
    wanted = {(target["symbol"], target["market"]) for target in targets}
    rows = (
        db.query(
            SecurityAnalysis.symbol,
            SecurityAnalysis.market,
            sa_func.max(SecurityAnalysis.created_at).label("latest"),
        )
        .group_by(SecurityAnalysis.symbol, SecurityAnalysis.market)
        .all()
    )
    fresh = set()
    for symbol, market, latest in rows:
        if (symbol, market) not in wanted or latest is None:
            continue
        stamp = latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)
        if stamp >= cutoff:
            fresh.add(f"{market}|{symbol}")
    return fresh


def start_batch_analysis_job(
    db: Session,
    user_id: int,
    *,
    include_report_digests: bool = False,
    force: bool = False,
    freshness_hours: Optional[int] = None,
) -> Dict[str, Any]:
    targets = get_batch_analysis_targets(db, user_id)
    if not targets:
        raise NoBatchTargetsError(
            "当前没有可分析的持仓标的（需持仓数量>0 且市场为 "
            f"{'/'.join(SUPPORTED_MARKETS)}）。"
        )
    if include_report_digests and len(targets) > FULL_MODE_MAX_SYMBOLS:
        raise NoBatchTargetsError(
            f"深度模式（含财报摘要）最多支持 {FULL_MODE_MAX_SYMBOLS} 只标的，"
            f"当前有 {len(targets)} 只。请改用快速模式，"
            "或在标的详情页逐个点「补齐历史摘要」。"
        )
    return create_or_get_active_job(
        JOB_TYPE,
        user_id,
        {
            "mode": "full" if include_report_digests else "fast",
            "force": bool(force),
            "freshness_hours": (
                0 if force
                else (
                    freshness_hours
                    if freshness_hours is not None
                    else settings.security_analysis_freshness_hours
                )
            ),
            # 目标固化：批量要跑几十分钟，期间用户可能导入新交易；不固化会让
            # 重试/接管时的范围漂移，进度条倒退
            "targets": targets,
            # 已完成键：接管/重试时精确续跑，不重复烧 LLM
            "completed_keys": [],
            "total": len(targets),
            "completed": 0,
            "progress_percent": 0,
            "success_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "current_symbol": None,
            "current_market": None,
            "current_stage": None,
            "results": [],
            "cancel_requested": False,
            "cancelled": False,
            # 不能叫 error：_serialize 展平后会盖掉 BackgroundJob.error 列
            "abort_reason": None,
        },
    )


def request_batch_cancel(job_id: str, user_id: int) -> Optional[Dict[str, Any]]:
    return request_job_cancel(job_id, JOB_TYPE, user_id)


def _is_cancel_requested(job_id: str, user_id: int) -> bool:
    return is_cancel_requested(job_id, JOB_TYPE, user_id)


def _classify_batch_failure(exc: Exception) -> str:
    """"abort"（整批等价的确定性失败）/ "symbol"（本标的失败，继续下一只）。"""
    if isinstance(exc, LLMNotConfiguredError):
        return "abort"
    if isinstance(exc, LLMClientError) and exc.status_code in (401, 402, 403, 429):
        return "abort"
    if classify_tushare_error(exc) == "fatal":
        return "abort"
    return "symbol"


def execute_batch_analysis_job(claimed: Dict[str, Any]) -> None:
    job_id = claimed["id"]
    attempt = claimed.get("attempt_count")
    user_id = claimed["user_id"]
    data = claimed["data"]
    targets: List[Dict[str, str]] = data.get("targets") or []
    done = set(data.get("completed_keys") or [])
    digest_max_new = FULL_MODE_DIGEST_MAX_NEW if data.get("mode") == "full" else 0
    pause = settings.security_analysis_batch_pause_seconds

    progress = make_batch_progress(job_id, JOB_TYPE, attempt)
    with batch_execution(
        job_id, JOB_TYPE, attempt=attempt,
        max_seconds=settings.security_analysis_batch_max_seconds,
        logger=logger, label="批量分析",
    ) as db:
        fresh = _recent_analysis_keys(db, targets, int(data.get("freshness_hours") or 0))
        counters = {
            "success_count": int(data.get("success_count") or 0),
            "failed_count": int(data.get("failed_count") or 0),
            "skipped_count": int(data.get("skipped_count") or 0),
        }
        results: List[Dict[str, Any]] = list(data.get("results") or [])
        consecutive = 0

        for index, target in enumerate(targets, start=1):
            key = _target_key(target)
            if key in done:
                continue  # 续跑：上次已处理

            if _is_cancel_requested(job_id, user_id):
                progress(
                    status="interrupted", cancelled=True,
                    current_symbol=None, current_market=None, current_stage=None,
                    abort_reason="用户终止；已生成的分析已保留，未开始的标的未分析。",
                    **counters,
                )
                return

            if key in fresh:
                counters["skipped_count"] += 1
                done.add(key)
                results.append({**target, "status": "skipped", "reason": "近期已分析"})
                progress(
                    completed=len(done), results=results[-RESULTS_KEPT:],
                    completed_keys=sorted(done), **counters,
                )
                continue

            progress(
                current_symbol=target["symbol"], current_market=target["market"],
                current_stage=None, completed=len(done), **counters,
            )
            started = time.monotonic()
            try:
                outcome = analyze_one(
                    db, target["symbol"], target["market"],
                    digest_max_new=digest_max_new,
                    on_stage=lambda stage, extra: progress(
                        current_stage=ANALYSIS_STAGE_LABELS.get(stage, stage)
                    ),
                )
            except JobOwnershipLostError:
                # 必须先于下面的兜底 except：否则失权异常会被
                # _classify_batch_failure 判成 "symbol"（本标的失败）而
                # 继续跑完整批。异常来自 analyze_one 内部的 on_stage 回调
                # ——analyze_one 的 stage() 包装器专门对本异常类型放行
                # （其余回调异常仍被它吞掉，不拖垮分析）。
                raise
            except Exception as exc:  # analyze_one 只上抛瞬时/意外失败
                if _classify_batch_failure(exc) == "abort":
                    logger.warning("批量分析中止（确定性失败）: %s", str(exc)[:200])
                    progress(
                        status="failed", error=str(exc)[:300],
                        abort_reason=f"遇到无法继续的错误：{str(exc)[:150]}",
                        current_symbol=None, current_market=None, current_stage=None,
                        completed=len(done), results=results[-RESULTS_KEPT:],
                        completed_keys=sorted(done), **counters,
                    )
                    return
                outcome = {
                    **target, "status": "failed", "analysis_id": None,
                    "error": str(exc)[:200], "degraded": [],
                }

            # 致命错误主要走 **outcome** 而非异常：analyze_one 把 LLM 4xx 与
            # 数据源 token/权限失效都转成 status=failed 返回。只看异常路径的话
            # 401 会一直请求到连续 3 只才停，Tushare 致命错误则永远不中止。
            if outcome.get("error_kind") in FATAL_ANALYSIS_ERROR_KINDS:
                counters["failed_count"] += 1
                results.append({
                    **target, "status": "failed", "error": outcome.get("error"),
                })
                progress(
                    status="failed", error=outcome.get("error"),
                    abort_reason=f"遇到无法继续的错误：{str(outcome.get('error'))[:150]}",
                    current_symbol=None, current_market=None, current_stage=None,
                    completed=len(done), results=results[-RESULTS_KEPT:],
                    completed_keys=sorted(done), **counters,
                )
                return

            done.add(key)
            elapsed = round(time.monotonic() - started, 1)
            if outcome["status"] == "succeeded":
                counters["success_count"] += 1
                consecutive = 0
                results.append({
                    **target, "status": "succeeded",
                    "analysis_id": outcome.get("analysis_id"),
                    "degraded": outcome.get("degraded") or [],
                    "elapsed_seconds": elapsed,
                })
            else:
                counters["failed_count"] += 1
                consecutive += 1
                results.append({
                    **target, "status": "failed",
                    "error": outcome.get("error"), "elapsed_seconds": elapsed,
                })

            progress(
                completed=len(done), results=results[-RESULTS_KEPT:],
                completed_keys=sorted(done), **counters,
            )

            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                progress(
                    status="failed",
                    error=f"连续 {consecutive} 只标的分析失败，已停止批量分析。",
                    abort_reason=f"连续 {consecutive} 只失败，停止以免继续消耗配额。",
                    current_symbol=None, current_market=None, current_stage=None,
                    **counters,
                )
                return

            if pause > 0 and index < len(targets):
                time.sleep(pause)

        progress(
            status="succeeded", completed=len(done),
            current_symbol=None, current_market=None, current_stage=None,
            results=results[-RESULTS_KEPT:], completed_keys=sorted(done), **counters,
        )


def run_batch_analysis_job(job_id: str) -> None:
    run_job_inline(job_id, JOB_TYPE, execute_batch_analysis_job, label="Batch analysis", logger=logger)

def get_batch_analysis_job(job_id: str, user_id: int) -> Optional[Dict[str, Any]]:
    return get_job(job_id, JOB_TYPE, user_id)


def ensure_no_conflicting_analysis_job(user_id: int, current_job_type: str) -> None:
    """分析类任务跨类型互斥：冲突时抛 AnalysisBusyError（API 映射 409）。

    残余竞态（两个并发请求都通过预检）只会造成外部 API 双倍消耗、无数据损坏；
    要收严可在建 job 的同一事务里加 pg_advisory_xact_lock。
    """
    from .background_job_store import find_active_job_of_types

    active = find_active_job_of_types(
        user_id, ANALYSIS_EXCLUSIVE_JOB_TYPES, exclude_job_type=current_job_type
    )
    if not active:
        return
    labels = {
        "security_analysis": "标的分析",
        "security_analysis_batch": "批量分析",
        "report_digest_backfill": "财报摘要回填",
        "report_digest_batch": "批量财报摘要回填",
    }
    label = labels.get(active.get("type"), active.get("type"))
    raise AnalysisBusyError(
        f"已有{label}任务进行中，请等待其完成后再发起——"
        "同时运行会对数据源产生双倍请求。",
        active_job=active,
    )


register_runner(JOB_TYPE, execute_batch_analysis_job)
