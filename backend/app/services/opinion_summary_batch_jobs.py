"""批量观点摘要 job（job_type="opinion_summary_batch"）：对持仓∪自选逐标的
跑 summarize_one，串行 + 进度 + 可终止。

克隆 security_analysis_batch 的骨架，砍掉 deep 模式与 Tushare 分类——本管线
唯一外呼是 LLM（发言数据在同库表里）。两点刻意不同：

- **目标集 = 持仓(quantity>0) ∪ 自选股(watchlist_items)**，且市场集是
  OPINION_MARKETS（比档案分析多 B股：观点只依赖发言+LLM，不依赖行情/基本面
  数据源）。零匹配的标的在启动时剔除，不进 targets 不烧 LLM。
- **新鲜度双条件**：存在摘要且（created_at 在窗口内 **或** 该标的最新匹配
  发言 ≤ 摘要.latest_utterance_at）即跳过——"没新发言"的重跑零成本。
  latest_matched map 来自启动时那次全扫，随 job data 固化。
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from ..config import settings
from ..core.logging import get_app_logger
from ..models.holding import Holding
from ..models.security_opinion import SecurityOpinionSummary
from ..models.watchlist_item import WatchlistItem
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
from .opinion_summary_jobs import (
    FATAL_OPINION_ERROR_KINDS,
    OPINION_MARKETS,
    OPINION_STAGE_LABELS,
    summarize_one,
)
from .security_analysis_batch_jobs import NoBatchTargetsError
from .security_rule_service import get_cash_management_symbols, get_excluded_keys
from .xueqiu_opinion_source import (
    OpinionSourceUnavailable,
    build_wanted_map,
    scan_matched_utterances,
    source_freshness,
)

logger = get_app_logger(__name__)
JOB_TYPE = "opinion_summary_batch"

MAX_CONSECUTIVE_FAILURES = 3
RESULTS_KEPT = 50
# 每标的一次 LLM 调用（十几秒量级），几十只也就几十分钟；无外部数据源限速，
# 标的间只留 1s 安全阀
BATCH_MAX_SECONDS = 1800.0
PAUSE_SECONDS = 1.0


def candidate_opinion_targets(db: Session, user_id: int) -> List[Dict[str, str]]:
    """持仓∪自选的去重候选（未做匹配剔除），按市场轮转排序，带 origin。"""
    holding_rows = (
        db.query(Holding.symbol, Holding.market)
        .filter(Holding.user_id == user_id, Holding.quantity > 0)
        .distinct()
        .all()
    )
    watch_rows = (
        db.query(WatchlistItem.symbol, WatchlistItem.market)
        .filter(WatchlistItem.user_id == user_id)
        .all()
    )
    excluded = get_excluded_keys(db, user_id)
    cash_symbols = get_cash_management_symbols(db, user_id)

    origin: Dict[Tuple[str, str], str] = {}
    for symbol, market in holding_rows:
        origin[(symbol, market)] = "holding"
    for symbol, market in watch_rows:
        origin[(symbol, market)] = (
            "both" if (symbol, market) in origin else "watchlist"
        )

    by_market: Dict[str, List[Dict[str, str]]] = {}
    for (symbol, market), source in origin.items():
        if market not in OPINION_MARKETS:
            continue
        if (symbol, market) in excluded or symbol in cash_symbols:
            continue
        by_market.setdefault(market, []).append(
            {"symbol": symbol, "market": market, "origin": source}
        )

    for items in by_market.values():
        items.sort(key=lambda item: item["symbol"])
    ordered: List[Dict[str, str]] = []
    markets = [market for market in OPINION_MARKETS if market in by_market]
    index = 0
    while any(by_market.get(market) for market in markets):
        market = markets[index % len(markets)]
        bucket = by_market.get(market)
        if bucket:
            ordered.append(bucket.pop(0))
        index += 1
    return ordered


def get_opinion_batch_targets(db: Session, user_id: int) -> Dict[str, Any]:
    """批量目标预览（纯 DB 零外呼，预览端点直接复用）。

    候选 → to_xueqiu 建键 → 一次全扫 → 零匹配剔除；每目标带
    matched_count / recent_count / latest_matched_at。表不存在时
    source_available=False 且 targets 为空（不抛：预览要能如实展示状态）。
    """
    freshness = source_freshness(db)
    candidates = candidate_opinion_targets(db, user_id)
    if not freshness["available"] or not candidates:
        return {"targets": [], "source_available": freshness["available"], "freshness": freshness}

    wanted = build_wanted_map([(t["symbol"], t["market"]) for t in candidates])
    since = datetime.now(timezone.utc) - timedelta(
        days=settings.xueqiu_opinion_lookback_days
    )
    matched = scan_matched_utterances(db, set(wanted), since=since)
    recent_cutoff = datetime.now(timezone.utc) - timedelta(
        days=settings.xueqiu_opinion_recent_days
    )
    reverse = {pair: key for key, pair in wanted.items()}

    targets: List[Dict[str, Any]] = []
    for target in candidates:
        key = reverse.get((target["symbol"], target["market"]))
        rows = matched.get(key or "", [])
        if not rows:
            continue  # 零匹配：不进 targets，不烧 LLM
        latest = max(row["created_at"] for row in rows)
        targets.append({
            **target,
            "matched_count": len(rows),
            "recent_count": sum(1 for row in rows if row["created_at"] >= recent_cutoff),
            "latest_matched_at": latest.isoformat(),
        })
    return {"targets": targets, "source_available": True, "freshness": freshness}


def _target_key(target: Dict[str, str]) -> str:
    return f"{target['market']}|{target['symbol']}"


def _fresh_opinion_keys(
    db: Session,
    targets: List[Dict[str, Any]],
    freshness_hours: int,
) -> set:
    """可跳过的目标键：摘要在窗口内，或自摘要后无新匹配发言（双条件）。"""
    if not targets:
        return set()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=freshness_hours)
    wanted = {(target["symbol"], target["market"]): target for target in targets}
    rows = (
        db.query(SecurityOpinionSummary)
        .filter(
            SecurityOpinionSummary.symbol.in_({s for s, _ in wanted}),
        )
        .order_by(SecurityOpinionSummary.created_at.desc(), SecurityOpinionSummary.id.desc())
        .all()
    )
    latest_by_pair: Dict[Tuple[str, str], SecurityOpinionSummary] = {}
    for row in rows:
        pair = (row.symbol, row.market)
        if pair in wanted and pair not in latest_by_pair:
            latest_by_pair[pair] = row

    fresh = set()
    for pair, summary in latest_by_pair.items():
        target = wanted[pair]
        created = summary.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if freshness_hours > 0 and created is not None and created >= cutoff:
            fresh.add(_target_key(target))
            continue
        # 第二条件：自摘要以来没有新匹配发言——重新生成只会得到同一份结论
        latest_matched_raw = target.get("latest_matched_at")
        anchor = summary.latest_utterance_at
        if latest_matched_raw and anchor is not None:
            latest_matched = datetime.fromisoformat(latest_matched_raw)
            if anchor.tzinfo is None:
                anchor = anchor.replace(tzinfo=timezone.utc)
            if latest_matched <= anchor:
                fresh.add(_target_key(target))
    return fresh


def start_opinion_batch_job(
    db: Session, user_id: int, *, force: bool = False
) -> Dict[str, Any]:
    freshness = source_freshness(db)
    if not freshness["available"]:
        raise OpinionSourceUnavailable(
            "雪球观点数据源未接入（未找到 xueqiu_archiver_utterances 表，"
            "该表由 xueqiu-timeline-archiver 项目写入）"
        )
    preview = get_opinion_batch_targets(db, user_id)
    targets = preview["targets"]
    if not targets:
        raise NoBatchTargetsError(
            f"近 {settings.xueqiu_opinion_lookback_days} 天内关注作者未提及任何"
            "持仓/自选标的（cashtag 与帖子链接口径），没有可摘要的目标。"
        )
    return create_or_get_active_job(
        JOB_TYPE,
        user_id,
        {
            "force": bool(force),
            "freshness_hours": 0 if force else settings.security_analysis_freshness_hours,
            # 目标固化（含 latest_matched_at）：接管/重试范围不漂移
            "targets": targets,
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


def request_opinion_batch_cancel(job_id: str, user_id: int) -> Optional[Dict[str, Any]]:
    return request_job_cancel(job_id, JOB_TYPE, user_id)


def _classify_batch_failure(exc: Exception) -> str:
    """"abort"（整批等价）/ "symbol"（本标的失败，继续下一只）。"""
    if isinstance(exc, (LLMNotConfiguredError, OpinionSourceUnavailable)):
        return "abort"
    if isinstance(exc, LLMClientError) and exc.status_code in (401, 402, 403, 429):
        return "abort"
    return "symbol"


def execute_opinion_batch_job(claimed: Dict[str, Any]) -> None:
    job_id = claimed["id"]
    attempt = claimed.get("attempt_count")
    user_id = claimed["user_id"]
    data = claimed["data"]
    targets: List[Dict[str, Any]] = data.get("targets") or []
    done = set(data.get("completed_keys") or [])

    progress = make_batch_progress(job_id, JOB_TYPE, attempt)
    with batch_execution(
        job_id, JOB_TYPE, attempt=attempt,
        max_seconds=BATCH_MAX_SECONDS, logger=logger, label="批量观点摘要",
    ) as db:
        # force 绕过**两个**跳过条件（时间窗与"无新发言"）：用户点了强制重跑，
        # 任何一种"仍新鲜"都不该拦
        fresh = (
            set()
            if data.get("force")
            else _fresh_opinion_keys(db, targets, int(data.get("freshness_hours") or 0))
        )

        # 一次全扫供整批使用（逐标的自行扫描是 N 倍浪费）；接管/重试重扫一次，
        # 只会比启动时更新，不影响已固化的目标范围
        try:
            wanted = build_wanted_map(
                [(t["symbol"], t["market"]) for t in targets]
            )
            since = datetime.now(timezone.utc) - timedelta(
                days=settings.xueqiu_opinion_lookback_days
            )
            matched_all = scan_matched_utterances(db, set(wanted), since=since)
        except OpinionSourceUnavailable as exc:
            progress(
                status="failed", error=str(exc)[:300],
                abort_reason=f"遇到无法继续的错误：{str(exc)[:150]}",
            )
            return
        reverse = {pair: key for key, pair in wanted.items()}

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
                continue

            if is_cancel_requested(job_id, JOB_TYPE, user_id):
                progress(
                    status="interrupted", cancelled=True,
                    current_symbol=None, current_market=None, current_stage=None,
                    abort_reason="用户终止；已生成的摘要已保留，未开始的标的未处理。",
                    **counters,
                )
                return

            if key in fresh:
                counters["skipped_count"] += 1
                done.add(key)
                results.append({**target, "status": "skipped", "reason": "摘要仍新鲜（无新发言）"})
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
            xq_key = reverse.get((target["symbol"], target["market"]))
            try:
                outcome = summarize_one(
                    db, target["symbol"], target["market"],
                    matched=matched_all.get(xq_key or "", []),
                    on_stage=lambda stage, extra: progress(
                        current_stage=OPINION_STAGE_LABELS.get(stage, stage)
                    ),
                )
            except JobOwnershipLostError:
                # 必须先于泛捕获：失权异常被判成"本标的失败"会让僵尸线程
                # 跑完整批（见 security_analysis_batch 同处注释）
                raise
            except Exception as exc:  # summarize_one 只上抛瞬时/意外失败
                if _classify_batch_failure(exc) == "abort":
                    logger.warning("批量观点摘要中止（确定性失败）: %s", str(exc)[:200])
                    progress(
                        status="failed", error=str(exc)[:300],
                        abort_reason=f"遇到无法继续的错误：{str(exc)[:150]}",
                        current_symbol=None, current_market=None, current_stage=None,
                        completed=len(done), results=results[-RESULTS_KEPT:],
                        completed_keys=sorted(done), **counters,
                    )
                    return
                outcome = {
                    **target, "status": "failed", "summary_id": None,
                    "error": str(exc)[:200],
                }

            # 致命错误主要走 outcome 而非异常（LLM 4xx/未配置/表消失都是返回值）
            if outcome.get("error_kind") in FATAL_OPINION_ERROR_KINDS:
                counters["failed_count"] += 1
                results.append({**target, "status": "failed", "error": outcome.get("error")})
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
                    "summary_id": outcome.get("summary_id"),
                    "tags": outcome.get("tags") or [],
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
                    error=f"连续 {consecutive} 只标的观点摘要失败，已停止。",
                    abort_reason=f"连续 {consecutive} 只失败，停止以免继续消耗配额。",
                    current_symbol=None, current_market=None, current_stage=None,
                    **counters,
                )
                return

            if PAUSE_SECONDS > 0 and index < len(targets):
                time.sleep(PAUSE_SECONDS)

        progress(
            status="succeeded", completed=len(done),
            current_symbol=None, current_market=None, current_stage=None,
            results=results[-RESULTS_KEPT:], completed_keys=sorted(done), **counters,
        )


def run_opinion_batch_job(job_id: str) -> None:
    run_job_inline(
        job_id, JOB_TYPE, execute_opinion_batch_job,
        label="Opinion summary batch", logger=logger,
    )


def get_opinion_batch_job(job_id: str, user_id: int) -> Optional[Dict[str, Any]]:
    return get_job(job_id, JOB_TYPE, user_id)


register_runner(JOB_TYPE, execute_opinion_batch_job)
