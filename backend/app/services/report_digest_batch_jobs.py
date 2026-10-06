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

**每周刷新模式**（`mode="weekly"`，由 `weekly_data_refresh` 周期任务凌晨入队，手动入口
不传）：目标 = 持仓 ∪ 自选；逐标的先同步基本面档案（非 LLM，6 天内同步过即跳过）、美股
顺带 ADS 换算比，再**只补最新一份年报/中报**的摘要（`newest_only`，不向更早年份加深）与
港股一份报表抽取。未配置 LLM 时只做档案同步。续跑、早停与致命错误中止的语义不变。
"""

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..core.logging import get_app_logger
from ..models.security_profile import SecurityProfileData
from .background_job_store import (
    JobOwnershipLostError,
    create_or_get_active_job,
    get_job,
)
from .job_runtime import (
    batch_execution,
    initial_batch_data,
    is_cancel_requested,
    make_batch_progress,
    request_job_cancel,
    run_job_inline,
)
from .job_worker import register_runner
from .llm_client import is_llm_configured
from .opinion_summary_batch_jobs import candidate_opinion_targets
from .report_digest_service import (
    REPORT_MARKETS,
    all_attempts_failed,
    digest_versions_current,
    ensure_report_digests,
)
from .report_statement_service import (
    STATEMENT_MARKETS,
    attach_statement_outcome,
    ensure_report_statements,
)
from .security_analysis_batch_jobs import (
    MAX_CONSECUTIVE_FAILURES,
    RESULTS_KEPT,
    NoBatchTargetsError,
    get_batch_analysis_targets,
)
from .security_profile_service import SUPPORTED_MARKETS as PROFILE_MARKETS
from .security_profile_service import sync_symbol_profile

logger = get_app_logger(__name__)
JOB_TYPE = "report_digest_batch"


def _attach_statement_outcome(db, target: Dict[str, Any], outcome: Dict[str, Any]) -> None:
    """港股顺带抽三张报表：实现在 report_statement_service.attach_statement_outcome（与单标的
    回填共用）；这里只把本模块命名空间里的 ensure_report_statements 传进去，保住测试的
    monkeypatch 口径。"""
    attach_statement_outcome(
        db,
        target["symbol"],
        target["market"],
        outcome,
        max_new=DIGEST_BATCH_PER_SYMBOL,
        ensure=ensure_report_statements,
    )


def _statement_counts(outcome: Optional[Dict[str, Any]]) -> Dict[str, int]:
    statements = (outcome or {}).get("statements") or {}
    return {
        "statements_generated": int(statements.get("generated") or 0),
        "statements_blocked": int(statements.get("permanently_failed") or 0),
        "statements_suspect": int(statements.get("suspect") or 0),
    }


def _bump_statement_counters(counters: Dict[str, int], outcome: Optional[Dict[str, Any]]) -> None:
    for key, value in _statement_counts(outcome).items():
        counters[key] += value


# 每标的每轮最多补几份（与单标的回填 BACKFILL_BATCH_SIZE 一致）。
# 33 标的 × 4 份 ≈ 130 份/轮：单轮 2-4 小时、约 6-7 元，商业画像与财报要点
# 全部点亮；owner 拍板"先近 4 期、可续跑加深"而非一次挂机 5-8 小时补满十年。
DIGEST_BATCH_PER_SYMBOL = 4

# 整批等价的致命 kind（与 security_analysis_jobs.FATAL_ANALYSIS_ERROR_KINDS
# 同一套词汇表）。**不匹配中文 gap 文案**：文案一改判据就静默失效，而无效
# Key/欠费/限流会被记成成功、整批继续空转、UI 最后还提示"完成"。
FATAL_DIGEST_ERROR_KINDS = frozenset({"llm_not_configured", "llm_auth", "llm_rate_limited"})


# 每周刷新模式
WEEKLY_MODE = "weekly"
# 每标的最多新补几份摘要：最新年报 + 最新中报
WEEKLY_DIGEST_MAX_NEW = 2
# 港股每周顺带抽几份报表
WEEKLY_STATEMENT_MAX_NEW = 1
# 档案在这么多天内同步过即跳过：多个用户持有同一标的时，同一周只同步一次
PROFILE_FRESH_DAYS = 6
# 判定档案新鲜度看的数据集：每市场的核心财务数据集。不能看全部数据集的最新
# fetched_at——A股 daily_basic 每天由估值快照任务刷新，那会让档案永远「刚同步过」
PROFILE_FRESHNESS_DATASET = {
    "A股": "fina_indicator",
    "美股": "edgar_companyfacts",
    "港股": "yahoo_fundamentals",
}


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
        db.query(
            SecurityProfileData.symbol, SecurityProfileData.market, SecurityProfileData.payload
        )
        .filter(SecurityProfileData.dataset == "report_digest")
        .all()
    ):
        payload = row.payload or {}
        if payload.get("status") != "ok" or not digest_versions_current(payload, row.market):
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


def get_weekly_refresh_targets(db: Session, user_id: int) -> List[Dict[str, str]]:
    """每周刷新目标 = 持仓 ∪ 自选 ∩ 档案支持的市场，去掉排除/现金管理规则命中的标的，
    按市场轮转排序（与观点摘要同一套候选口径）。"""
    return [
        {"symbol": target["symbol"], "market": target["market"]}
        for target in candidate_opinion_targets(db, user_id)
        if target["market"] in PROFILE_MARKETS
    ]


def start_digest_batch_job(db: Session, user_id: int) -> Dict[str, Any]:
    targets = get_digest_backfill_targets(db, user_id)
    if not targets:
        raise NoBatchTargetsError(
            f"当前没有可回填的持仓标的（需持仓数量>0 且市场为 {'/'.join(REPORT_MARKETS)}）。"
        )
    job = create_or_get_active_job(JOB_TYPE, user_id, _initial_job_data(targets))
    if job.get("mode") == WEEKLY_MODE:
        # 每周刷新与手动回填共用 job_type，活跃任务按 (用户, 类型) 去重：不拦的话手动的
        # 「补齐」会静默变成那个只补最新一期的每周任务，页面还在跟踪它
        from .security_analysis_jobs import AnalysisBusyError

        raise AnalysisBusyError("每周自动数据刷新正在运行，请等它完成后再补齐财报摘要。", job)
    return job


def start_weekly_refresh_job(db: Session, user_id: int) -> Dict[str, Any]:
    """每周数据刷新（周期任务入队，不内联执行：由 worker 慢车道接管）。"""
    targets = get_weekly_refresh_targets(db, user_id)
    if not targets:
        raise NoBatchTargetsError("当前没有需要刷新的持仓或自选标的。")
    return create_or_get_active_job(JOB_TYPE, user_id, _initial_job_data(targets, mode=WEEKLY_MODE))


def _initial_job_data(
    targets: List[Dict[str, str]], *, mode: Optional[str] = None
) -> Dict[str, Any]:
    return initial_batch_data(
        targets,
        digests_generated=0,
        # 抽取器升版后节选未变、沿用旧摘要的份数（零 LLM，不进 digests_generated 成本计数）
        digests_reused=0,
        # 已永久失败（封顶）的报告份数：混着缓存成功时标的仍算成功，
        # 但这个数必须在最终结果里可见——完成提示据此发警告而非绿色
        digests_blocked=0,
        symbols_with_remaining=0,
        # 港股顺带的三张报表抽取：新抽份数 / 永久失败份数 / 校验存疑的会计期数
        statements_generated=0,
        statements_blocked=0,
        statements_suspect=0,
        # None = 手动回填（续跑加深）；"weekly" = 每周刷新
        mode=mode,
        profiles_synced=0,
        profiles_failed=0,
    )


def _profile_recently_synced(
    db: Session, symbol: str, market: str, *, now: Optional[datetime] = None
) -> bool:
    """该标的核心财务数据集在 PROFILE_FRESH_DAYS 内同步过（多个用户持有同一标的时
    只同步一次）。没有该数据集的行 = 从未同步成功，不算新鲜。"""
    dataset = PROFILE_FRESHNESS_DATASET.get(market)
    if not dataset:
        return False
    from sqlalchemy import func as sa_func

    latest = (
        db.query(sa_func.max(SecurityProfileData.fetched_at))
        .filter(
            SecurityProfileData.symbol == symbol,
            SecurityProfileData.market == market,
            SecurityProfileData.dataset == dataset,
        )
        .scalar()
    )
    if latest is None:
        return False
    moment = now or datetime.now(timezone.utc)
    return moment - latest < timedelta(days=PROFILE_FRESH_DAYS)


def _weekly_profile_step(db: Session, target: Dict[str, str]) -> Dict[str, Any]:
    """档案同步（非 LLM）+ 美股 ADS 换算比。失败只记进结果行，不影响摘要步骤。"""
    symbol, market = target["symbol"], target["market"]
    extras: Dict[str, Any] = {}
    if _profile_recently_synced(db, symbol, market):
        extras["profile"] = {"status": "fresh"}
    else:
        try:
            synced = sync_symbol_profile(db, symbol, market)
            failed_sets = [item.get("dataset") for item in synced.get("failed") or []]
            skipped_sets = [item.get("dataset") for item in synced.get("skipped") or []]
            fatal = (synced.get("fatal") or {}).get("error")
            stored_sets = set((synced.get("datasets") or {}).keys())
            core = PROFILE_FRESHNESS_DATASET.get(market)
            core_missing = bool(core) and core not in stored_sets
            if not synced.get("supported"):
                status = "unsupported"
            elif fatal or core_missing:
                # 致命错误，或核心财务数据集（新鲜度判据）本轮没有真正刷新——失败或因
                # Tushare 冷却被跳过都算：只刷到 daily_basic 之类的次要数据集不代表基本面
                # 已更新，记成功会让调度器一周内不再重试
                status = "failed"
            elif failed_sets or skipped_sets:
                # 核心已刷新、个别次要数据集失败/跳过（如需 Cookie 的雪球数据集）：部分成功
                status = "partial"
            else:
                status = "synced"
            extras["profile"] = {
                "status": status,
                "datasets": len(stored_sets),
                "failed": failed_sets,
                "skipped": skipped_sets,
                "fatal": fatal,
            }
            if core_missing and not fatal:
                extras["profile"]["error"] = (
                    f"核心数据集 {core} 未刷新（"
                    + (
                        "失败"
                        if core in failed_sets
                        else "被跳过"
                        if core in skipped_sets
                        else "缺失"
                    )
                    + "）"
                )
        except Exception as exc:  # 档案失败不拖累摘要步骤
            db.rollback()
            logger.warning("每周刷新 %s/%s 档案同步失败: %s", market, symbol, str(exc)[:200])
            extras["profile"] = {"status": "error", "error": str(exc)[:200]}
    if market == "美股":
        from .ads_ratio_service import ensure_ads_ratio

        try:
            extras["ads_ratio"] = ensure_ads_ratio(db, symbol).get("status")
        except Exception as exc:
            db.rollback()
            logger.warning("每周刷新 %s ADS 换算比失败: %s", symbol, str(exc)[:200])
            extras["ads_ratio"] = "error"
    return extras


# ADS 换算比这几种结果是本轮真实失败（cover_unknown = 10-K 封面无法识别、本轮没得到结论；
# capped = 同一份年报已连续失败到上限，属于已知的永久缺口，不再每周重复计失败）
_ADS_FAILED_STATUSES = ("failed", "error", "cover_unknown")


def weekly_step_error(extras: Optional[Dict[str, Any]]) -> Optional[str]:
    """每周模式档案/ADS 步骤的真实失败原因；没有失败返回 None。"""
    if not extras:
        return None
    reasons = []
    profile = extras.get("profile") or {}
    if profile.get("status") in ("error", "failed"):
        detail = (
            profile.get("fatal") or profile.get("error") or "、".join(profile.get("failed") or [])
        )
        reasons.append(f"档案同步失败：{str(detail)[:120]}")
    if extras.get("ads_ratio") in _ADS_FAILED_STATUSES:
        reasons.append("ADS 换算比同步失败")
    return "；".join(reasons) or None


def _weekly_digest_step(db: Session, target: Dict[str, str], *, llm_ready: bool) -> Dict[str, Any]:
    """只补最新一份年报/中报的摘要 + 港股一份报表抽取；未配置 LLM 时返回零工作的成功结果。"""
    symbol, market = target["symbol"], target["market"]
    if not llm_ready or market not in REPORT_MARKETS:
        return {
            "total": 0,
            "completed": 0,
            "generated": 0,
            "attempted": 0,
            "failed": 0,
            "remaining": 0,
            "pending_periods": [],
            "permanently_failed": 0,
            "plan_incomplete": False,
            "fatal": None,
            "gaps": ["未配置 LLM，本周只同步档案、未补财报摘要"] if not llm_ready else [],
        }
    outcome = ensure_report_digests(
        db, symbol, market, max_new=WEEKLY_DIGEST_MAX_NEW, newest_only=True
    )
    if market in STATEMENT_MARKETS and not outcome.get("fatal"):
        attach_statement_outcome(
            db,
            symbol,
            market,
            outcome,
            max_new=WEEKLY_STATEMENT_MAX_NEW,
            ensure=ensure_report_statements,
        )
    return outcome


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
        job_id,
        JOB_TYPE,
        attempt=attempt,
        max_seconds=settings.security_analysis_batch_max_seconds,
        logger=logger,
        label="批量回填",
    ) as db:
        counters = {
            "success_count": int(data.get("success_count") or 0),
            "failed_count": int(data.get("failed_count") or 0),
            "digests_generated": int(data.get("digests_generated") or 0),
            "digests_reused": int(data.get("digests_reused") or 0),
            "digests_blocked": int(data.get("digests_blocked") or 0),
            "symbols_with_remaining": int(data.get("symbols_with_remaining") or 0),
            "statements_generated": int(data.get("statements_generated") or 0),
            "statements_blocked": int(data.get("statements_blocked") or 0),
            "statements_suspect": int(data.get("statements_suspect") or 0),
            "profiles_synced": int(data.get("profiles_synced") or 0),
            "profiles_failed": int(data.get("profiles_failed") or 0),
        }
        results: List[Dict[str, Any]] = list(data.get("results") or [])
        weekly = data.get("mode") == WEEKLY_MODE
        # 每周模式：LLM 未配置只做档案同步（摘要/报表映射都要 LLM）
        llm_ready = is_llm_configured() if weekly else True
        consecutive = 0

        for index, target in enumerate(targets, start=1):
            key = f"{target['market']}|{target['symbol']}"
            if key in done:
                continue  # 续跑：上次已处理

            if _is_cancel_requested(job_id, user_id):
                progress(
                    status="interrupted",
                    cancelled=True,
                    current_symbol=None,
                    current_market=None,
                    abort_reason="用户终止；已生成的摘要已保留，可再次触发续跑。",
                    **counters,
                )
                return

            progress(
                current_symbol=target["symbol"],
                current_market=target["market"],
                completed=len(done),
                **counters,
            )
            started = time.monotonic()
            weekly_extras: Optional[Dict[str, Any]] = None
            consecutive_before = consecutive
            try:
                if weekly:
                    weekly_extras = _weekly_profile_step(db, target)
                    if weekly_extras.get("profile", {}).get("status") in ("synced", "partial"):
                        counters["profiles_synced"] += 1
                    outcome = _weekly_digest_step(db, target, llm_ready=llm_ready)
                else:
                    outcome = ensure_report_digests(
                        db,
                        target["symbol"],
                        target["market"],
                        max_new=DIGEST_BATCH_PER_SYMBOL,
                    )
                    if target["market"] in STATEMENT_MARKETS and not outcome.get("fatal"):
                        _attach_statement_outcome(db, target, outcome)
            except JobOwnershipLostError:
                # LLM checkpoint 的失权哨兵必须交给 batch_execution 安静退出，
                # 不能记成本标的失败后继续下载/生成其余标的。
                raise
            except Exception as exc:
                # ensure_report_digests 把下载/抽取/LLM 失败都消化成 gaps，
                # 走到这里的是意外错误——记本标的失败，继续下一只
                logger.warning(
                    "批量回填 %s/%s 意外失败: %s",
                    target["market"],
                    target["symbol"],
                    str(exc)[:200],
                )
                outcome = None

            elapsed = round(time.monotonic() - started, 1)
            if outcome is None:
                counters["failed_count"] += 1
                consecutive += 1
                done.add(key)
                results.append(
                    {
                        **target,
                        "status": "failed",
                        "elapsed_seconds": elapsed,
                    }
                )
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
                    _bump_statement_counters(counters, outcome)
                    results.append(
                        {
                            **target,
                            "status": "failed",
                            "error": message[:200],
                            "generated": generated,
                            "blocked": blocked,
                            "gap_count": len(gaps),
                            "gaps_preview": gaps[:3],
                            "statements": outcome.get("statements"),
                            "elapsed_seconds": elapsed,
                        }
                    )
                    done.add(key)
                    progress(
                        status="failed",
                        error=message[:300],
                        abort_reason=f"遇到无法继续的错误：{message[:150]}",
                        current_symbol=None,
                        current_market=None,
                        completed=len(done),
                        results=results[-RESULTS_KEPT:],
                        completed_keys=sorted(done),
                        **counters,
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
                    _bump_statement_counters(counters, outcome)
                    consecutive += 1
                    results.append(
                        {
                            **target,
                            "status": "failed",
                            "error": (
                                "年报清单检索失败或不完整（数据源故障）"
                                + (f"；本轮已生成 {generated} 份仍保留" if generated else "")
                            ),
                            "generated": generated,
                            "blocked": blocked,
                            "gap_count": len(gaps),
                            "gaps_preview": gaps[:3],
                            "statements": outcome.get("statements"),
                            "elapsed_seconds": elapsed,
                        }
                    )
                elif all_attempts_failed(outcome):
                    # 判据与单标的回填同一函数：升版后沿用（digest_reused）算本轮成功的工作，
                    # 「1 份沿用 + 1 份失败」是部分成功，不得记失败、不得计入连败早停
                    counters["failed_count"] += 1
                    # blocked 在**每个**分支都要累计：ensure 先跳过封顶报告
                    # 再处理后续，failed>0 与 permanently_failed>0 完全可能
                    # 同时出现——本分支漏加的话前端就少报已知的永久失败
                    counters["digests_blocked"] += blocked
                    _bump_statement_counters(counters, outcome)
                    consecutive += 1
                    results.append(
                        {
                            **target,
                            "status": "failed",
                            "error": f"本轮 {failed_count} 份摘要全部生成失败",
                            "blocked": blocked,
                            "gap_count": len(gaps),
                            "gaps_preview": gaps[:3],
                            "statements": outcome.get("statements"),
                            "elapsed_seconds": elapsed,
                        }
                    )
                elif generated == 0 and completed_count == 0 and blocked > 0:
                    counters["failed_count"] += 1
                    counters["digests_blocked"] += blocked
                    # **不计入连败早停**：封顶是零成本的历史结果，说明不了
                    # 本轮环境的健康度——前三只恰好全封顶就终止整批，会跳过
                    # 后面所有仍可正常回填的持仓。早停只看本轮真实尝试的
                    # 失败；consecutive 保持原值（也不清零：它没提供任何
                    # "环境恢复了"的证据）
                    _bump_statement_counters(counters, outcome)
                    results.append(
                        {
                            **target,
                            "status": "failed",
                            "error": f"{blocked} 份报告均已永久失败（下载/抽取或摘要封顶）",
                            "blocked": blocked,
                            "gap_count": len(gaps),
                            "gaps_preview": gaps[:3],
                            "statements": outcome.get("statements"),
                            "elapsed_seconds": elapsed,
                        }
                    )
                else:
                    counters["success_count"] += 1
                    counters["digests_generated"] += generated
                    counters["digests_reused"] += int(outcome.get("digest_reused") or 0)
                    counters["digests_blocked"] += blocked
                    _bump_statement_counters(counters, outcome)
                    if int(outcome.get("remaining") or 0) > 0:
                        counters["symbols_with_remaining"] += 1
                    consecutive = 0
                    results.append(
                        {
                            **target,
                            "status": "ok",
                            "total": outcome.get("total"),
                            "completed": completed_count,
                            "generated": generated,
                            "reused": int(outcome.get("digest_reused") or 0),
                            "failed": failed_count,
                            "blocked": blocked,
                            "remaining": outcome.get("remaining"),
                            "gap_count": len(gaps),
                            # 缺口前三条原文：只给计数的话"缺什么"在结果卡片里看不到
                            "gaps_preview": gaps[:3],
                            # 港股顺带的三张报表抽取结果（其他市场为 None）
                            "statements": outcome.get("statements"),
                            "elapsed_seconds": elapsed,
                        }
                    )

            if weekly_extras and results and results[-1].get("symbol") == target["symbol"]:
                # 档案同步与 ADS 的结果并进本标的的结果行（各分支统一在这里补，不逐分支改）
                results[-1].update(weekly_extras)
                step_error = weekly_step_error(weekly_extras)
                if step_error:
                    # 档案/ADS 是每周刷新的主体：失败即本标的失败（摘要照常做完、已生成的保留），
                    # 计入连败早停与任务终态，否则基本面静默一周不更新
                    counters["profiles_failed"] += 1
                    if results[-1].get("status") == "ok":
                        results[-1]["status"] = "failed"
                        results[-1]["error"] = step_error
                        counters["success_count"] -= 1
                        counters["failed_count"] += 1
                        consecutive = consecutive_before + 1
                    else:
                        results[-1]["error"] = (
                            f"{results[-1].get('error') or ''}；{step_error}".lstrip("；")
                        )

            progress(
                completed=len(done),
                results=results[-RESULTS_KEPT:],
                completed_keys=sorted(done),
                **counters,
            )

            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                progress(
                    status="failed",
                    error=f"连续 {consecutive} 只标的回填失败，已停止。",
                    abort_reason=f"连续 {consecutive} 只失败，停止以免继续消耗配额。",
                    current_symbol=None,
                    current_market=None,
                    **counters,
                )
                return

            if pause > 0 and index < len(targets):
                time.sleep(pause)

        if weekly and counters["failed_count"] > 0:
            # 每周刷新无人盯着看：有标的失败就以 failed 结束，由后台任务告警推送出来；
            # 调度器按任务终态在之后的凌晨窗口重试（weekly_data_refresh）
            message = f"每周数据刷新有 {counters['failed_count']} 只标的失败"
            progress(
                status="failed",
                error=message,
                abort_reason=message,
                completed=len(done),
                current_symbol=None,
                current_market=None,
                results=results[-RESULTS_KEPT:],
                completed_keys=sorted(done),
                **counters,
            )
            return
        progress(
            status="succeeded",
            completed=len(done),
            current_symbol=None,
            current_market=None,
            results=results[-RESULTS_KEPT:],
            completed_keys=sorted(done),
            **counters,
        )


def run_digest_batch_job(job_id: str) -> None:
    run_job_inline(job_id, JOB_TYPE, execute_digest_batch_job, label="Digest batch", logger=logger)


def get_digest_batch_job(job_id: str, user_id: int) -> Optional[Dict[str, Any]]:
    return get_job(job_id, JOB_TYPE, user_id)


register_runner(JOB_TYPE, execute_digest_batch_job)
