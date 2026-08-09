"""批量财报摘要回填 job：对全部持仓标的逐个补 report_digest。

为什么需要它（2026-08-04 全量分析后的诊断）：商业画像的输入 = 财报摘要切片 +
业务概要节选，而系统里此前**不存在"给全部持仓补摘要"的路径**——批量分析 fast
模式 `digest_max_new=0` 永不补，deep 模式限 5 只×每轮 2 份，单标的回填按钮要在
几十个详情页反复点。结果 33/36 个标的的分析写着「暂无商业画像」。

骨架克隆 `security_analysis_batch_jobs`（同一套久经检视的可靠性模式）：
targets 固化 + completed_keys 续跑、单标的失败继续/连续失败早停、取消在标的
边界生效、进度回写续租 + heartbeat 覆盖长抽取（港股 400 页 PDF 的 pdfplumber
可达数分钟）。

**续跑加深**语义：每标的每轮最多补 `DIGEST_BATCH_PER_SYMBOL` 份；再次触发
同一按钮时已缓存的期数直接命中、自然向更早年份推进，直至十年补满。
"""

import time
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..core.logging import get_app_logger
from ..models.security_profile import SecurityProfileData
from .background_job_store import (
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
from .report_digest_service import (
    REPORT_MARKETS,
    digest_versions_current,
    ensure_report_digests,
)
from .security_analysis_batch_jobs import (
    MAX_CONSECUTIVE_FAILURES,
    RESULTS_KEPT,
    NoBatchTargetsError,
    get_batch_analysis_targets,
)

logger = get_app_logger(__name__)
JOB_TYPE = "report_digest_batch"

# 每标的每轮最多补几份（与单标的回填 BACKFILL_BATCH_SIZE 一致）。
# 33 标的 × 4 份 ≈ 130 份/轮：单轮 2-4 小时、约 6-7 元，商业画像与财报要点
# 全部点亮；owner 拍板"先近 4 期、可续跑加深"而非一次挂机 5-8 小时补满十年。
DIGEST_BATCH_PER_SYMBOL = 4

# 整批等价的致命 kind（与 security_analysis_jobs.FATAL_ANALYSIS_ERROR_KINDS
# 同一套词汇表）。**不匹配中文 gap 文案**：文案一改判据就静默失效，而无效
# Key/欠费/限流会被记成成功、整批继续空转、UI 最后还提示"完成"。
FATAL_DIGEST_ERROR_KINDS = frozenset(
    {"llm_not_configured", "llm_auth", "llm_rate_limited"}
)


def get_digest_backfill_targets(db: Session, user_id: int) -> List[Dict[str, str]]:
    """回填目标 = 批量分析目标 ∩ 支持财报摘要的市场（当前两者相等，
    交集是防御：将来支持结构化但不支持全文的市场加入时不静默跑空。"""
    return [
        target
        for target in get_batch_analysis_targets(db, user_id)
        if target["market"] in REPORT_MARKETS
    ]


def preview_digest_backfill(db: Session, user_id: int) -> Dict[str, Any]:
    """确认框数据：**纯 DB 统计不打外网**。

    只能诚实地报告"库内现状"（多少标的一份摘要都没有、已有多少份）——每标的
    的确切总期数要打 cninfo/披露易才知道，预览不该发起外呼。
    """
    targets = get_digest_backfill_targets(db, user_id)
    counts: Dict[tuple, int] = {}
    # 计数口径必须与读取路径（load_report_digests / digest_progress）一致：
    # status == "ok" 且双版本为当前。只按 dataset 数行的话，封顶失败行
    # （attempts 用尽的 status=failed）与版本过期行都被算成"已有摘要"——
    # 版本 bump 后几乎全部行过期，预览却显示"已有上百份"，而回填与分析
    # 实际一份都用不上（#133）。
    for row in (
        db.query(SecurityProfileData.symbol, SecurityProfileData.market, SecurityProfileData.payload)
        .filter(SecurityProfileData.dataset == "report_digest")
        .all()
    ):
        payload = row.payload or {}
        if payload.get("status") != "ok" or not digest_versions_current(payload):
            continue
        counts[(row.symbol, row.market)] = counts.get((row.symbol, row.market), 0) + 1
    existing = 0
    without = 0
    for target in targets:
        n = counts.get((target["symbol"], target["market"]), 0)
        existing += n
        if n == 0:
            without += 1
    return {
        "targets_total": len(targets),
        "targets_without_digest": without,
        "digests_existing": existing,
        "per_symbol_budget": DIGEST_BATCH_PER_SYMBOL,
    }


def start_digest_batch_job(db: Session, user_id: int) -> Dict[str, Any]:
    targets = get_digest_backfill_targets(db, user_id)
    if not targets:
        raise NoBatchTargetsError(
            "当前没有可回填的持仓标的（需持仓数量>0 且市场为 "
            f"{'/'.join(REPORT_MARKETS)}）。"
        )
    return create_or_get_active_job(
        JOB_TYPE,
        user_id,
        {
            # 目标固化 + completed_keys：接管/重试时精确续跑（语义同批量分析）
            "targets": targets,
            "completed_keys": [],
            "total": len(targets),
            "completed": 0,
            "progress_percent": 0,
            "success_count": 0,
            "failed_count": 0,
            "digests_generated": 0,
            # 已永久失败（封顶）的报告份数：混着缓存成功时标的仍算成功，
            # 但这个数必须在最终结果里可见——完成提示据此发警告而非绿色
            "digests_blocked": 0,
            "symbols_with_remaining": 0,
            "current_symbol": None,
            "current_market": None,
            "results": [],
            "cancel_requested": False,
            "cancelled": False,
            # 不能叫 error：_serialize 展平后会盖掉 BackgroundJob.error 列
            "abort_reason": None,
        },
    )


def request_digest_batch_cancel(job_id: str, user_id: int) -> Optional[Dict[str, Any]]:
    return request_job_cancel(job_id, JOB_TYPE, user_id)


def _is_cancel_requested(job_id: str, user_id: int) -> bool:
    return is_cancel_requested(job_id, JOB_TYPE, user_id)


def execute_digest_batch_job(claimed: Dict[str, Any]) -> None:
    job_id = claimed["id"]
    attempt = claimed.get("attempt_count")
    user_id = claimed["user_id"]
    data = claimed["data"]
    targets: List[Dict[str, str]] = data.get("targets") or []
    done = set(data.get("completed_keys") or [])
    pause = settings.security_analysis_batch_pause_seconds

    progress = make_batch_progress(job_id, JOB_TYPE, attempt)
    with batch_execution(
        job_id, JOB_TYPE, attempt=attempt,
        max_seconds=settings.security_analysis_batch_max_seconds,
        logger=logger, label="批量回填",
    ) as db:
        counters = {
            "success_count": int(data.get("success_count") or 0),
            "failed_count": int(data.get("failed_count") or 0),
            "digests_generated": int(data.get("digests_generated") or 0),
            "digests_blocked": int(data.get("digests_blocked") or 0),
            "symbols_with_remaining": int(data.get("symbols_with_remaining") or 0),
        }
        results: List[Dict[str, Any]] = list(data.get("results") or [])
        consecutive = 0

        for index, target in enumerate(targets, start=1):
            key = f"{target['market']}|{target['symbol']}"
            if key in done:
                continue  # 续跑：上次已处理

            if _is_cancel_requested(job_id, user_id):
                progress(
                    status="interrupted", cancelled=True,
                    current_symbol=None, current_market=None,
                    abort_reason="用户终止；已生成的摘要已保留，可再次触发续跑。",
                    **counters,
                )
                return

            progress(
                current_symbol=target["symbol"], current_market=target["market"],
                completed=len(done), **counters,
            )
            started = time.monotonic()
            try:
                outcome = ensure_report_digests(
                    db, target["symbol"], target["market"],
                    max_new=DIGEST_BATCH_PER_SYMBOL,
                )
            except Exception as exc:
                # ensure_report_digests 把下载/抽取/LLM 失败都消化成 gaps，
                # 走到这里的是意外错误——记本标的失败，继续下一只
                logger.warning(
                    "批量回填 %s/%s 意外失败: %s",
                    target["market"], target["symbol"], str(exc)[:200],
                )
                outcome = None

            elapsed = round(time.monotonic() - started, 1)
            if outcome is None:
                counters["failed_count"] += 1
                consecutive += 1
                done.add(key)
                results.append({
                    **target, "status": "failed", "elapsed_seconds": elapsed,
                })
            else:
                gaps = outcome.get("gaps") or []
                generated = int(outcome.get("generated") or 0)
                failed_count = int(outcome.get("failed") or 0)
                completed_count = int(outcome.get("completed") or 0)
                blocked = int(outcome.get("permanently_failed") or 0)
                plan_incomplete = bool(outcome.get("plan_incomplete"))
                fatal = outcome.get("fatal") or None
                if fatal and fatal.get("kind") in FATAL_DIGEST_ERROR_KINDS:
                    # 无效 Key / 欠费 / 限流：换个标的照样失败，继续跑只是
                    # 把整批拖成"看起来在跑"的空转，最后还谎报成功。
                    # ensure 是逐报告循环——fatal 之前可能已生成若干份、
                    # 跳过若干封顶行，这些**已落库的工作**必须先入账，
                    # 否则总数与结果行把它们记成 0
                    message = str(fatal.get("message") or fatal.get("kind"))
                    counters["failed_count"] += 1
                    counters["digests_generated"] += generated
                    counters["digests_blocked"] += blocked
                    results.append({
                        **target, "status": "failed", "error": message[:200],
                        "generated": generated, "blocked": blocked,
                        "elapsed_seconds": elapsed,
                    })
                    done.add(key)
                    progress(
                        status="failed", error=message[:300],
                        abort_reason=f"遇到无法继续的错误：{message[:150]}",
                        current_symbol=None, current_market=None,
                        completed=len(done), results=results[-RESULTS_KEPT:],
                        completed_keys=sorted(done), **counters,
                    )
                    return
                done.add(key)
                # 标的级判定（成本维度与结果维度分开）：
                # - 本轮有尝试且全失败（failed>0 且 generated=0）→ 失败
                # - 零尝试、零成品、但存在封顶失败（blocked>0）→ 同样是失败：
                #   这只标的所有可回填报告都已**永久**失败，记成功会让前端
                #   弹绿色"新生成 0 份"，用户看不到任何异常
                # - 零尝试且有 completed（缓存命中）→ 成功（即便同时有 blocked，
                #   部分年份封顶属于"有缺口的成功"，靠 blocked 计数外显）
                if plan_incomplete:
                    # 清单检索失败/不完整："该标的这轮补齐了什么"这个结论
                    # 本身不可信——annual 检索失败而 semi 成功时会生成 1 份
                    # 半年报，产出非零，但十年年报缺口被完全隐藏。**有产出
                    # 也不能记成功**（绿色完成 = 静默缺口）；已生成的照常
                    # 入账保留。计连败：源站故障时连续三只即早停。
                    counters["failed_count"] += 1
                    counters["digests_generated"] += generated
                    counters["digests_blocked"] += blocked
                    consecutive += 1
                    results.append({
                        **target, "status": "failed",
                        "error": (
                            "年报清单检索失败或不完整（数据源故障）"
                            + (f"；本轮已生成 {generated} 份仍保留" if generated else "")
                        ),
                        "generated": generated, "blocked": blocked,
                        "gap_count": len(gaps), "elapsed_seconds": elapsed,
                    })
                elif generated == 0 and failed_count > 0:
                    counters["failed_count"] += 1
                    # blocked 在**每个**分支都要累计：ensure 先跳过封顶报告
                    # 再处理后续，failed>0 与 permanently_failed>0 完全可能
                    # 同时出现——本分支漏加的话前端就少报已知的永久失败
                    counters["digests_blocked"] += blocked
                    consecutive += 1
                    results.append({
                        **target, "status": "failed",
                        "error": f"本轮 {failed_count} 份摘要全部生成失败",
                        "blocked": blocked,
                        "gap_count": len(gaps), "elapsed_seconds": elapsed,
                    })
                elif (
                    generated == 0 and completed_count == 0 and blocked > 0
                ):
                    counters["failed_count"] += 1
                    counters["digests_blocked"] += blocked
                    # **不计入连败早停**：封顶是零成本的历史结果，说明不了
                    # 本轮环境的健康度——前三只恰好全封顶就终止整批，会跳过
                    # 后面所有仍可正常回填的持仓。早停只看本轮真实尝试的
                    # 失败；consecutive 保持原值（也不清零：它没提供任何
                    # "环境恢复了"的证据）
                    results.append({
                        **target, "status": "failed",
                        "error": f"{blocked} 份报告均已永久失败（下载/抽取或摘要封顶）",
                        "blocked": blocked,
                        "gap_count": len(gaps), "elapsed_seconds": elapsed,
                    })
                else:
                    counters["success_count"] += 1
                    counters["digests_generated"] += generated
                    counters["digests_blocked"] += blocked
                    if int(outcome.get("remaining") or 0) > 0:
                        counters["symbols_with_remaining"] += 1
                    consecutive = 0
                    results.append({
                        **target, "status": "ok",
                        "total": outcome.get("total"),
                        "completed": completed_count,
                        "generated": generated,
                        "failed": failed_count,
                        "blocked": blocked,
                        "remaining": outcome.get("remaining"),
                        "gap_count": len(gaps),
                        "elapsed_seconds": elapsed,
                    })

            progress(
                completed=len(done), results=results[-RESULTS_KEPT:],
                completed_keys=sorted(done), **counters,
            )

            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                progress(
                    status="failed",
                    error=f"连续 {consecutive} 只标的回填失败，已停止。",
                    abort_reason=f"连续 {consecutive} 只失败，停止以免继续消耗配额。",
                    current_symbol=None, current_market=None,
                    **counters,
                )
                return

            if pause > 0 and index < len(targets):
                time.sleep(pause)

        progress(
            status="succeeded", completed=len(done),
            current_symbol=None, current_market=None,
            results=results[-RESULTS_KEPT:], completed_keys=sorted(done), **counters,
        )


def run_digest_batch_job(job_id: str) -> None:
    run_job_inline(job_id, JOB_TYPE, execute_digest_batch_job, label="Digest batch", logger=logger)

def get_digest_batch_job(job_id: str, user_id: int) -> Optional[Dict[str, Any]]:
    return get_job(job_id, JOB_TYPE, user_id)


register_runner(JOB_TYPE, execute_digest_batch_job)
