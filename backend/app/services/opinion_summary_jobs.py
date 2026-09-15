"""雪球观点摘要单标的 job（job_type="opinion_summary"）。

管线（远轻于 security_analysis：无档案同步、无外部数据抓取，唯一外呼是 LLM）：
读同库外部表的匹配发言 → 按作者分组、按近期/基线拆窗 → DeepSeek JSON mode
一次产出观点标签 + 逐作者立场 + Markdown → 落 security_opinion_summaries。

失败契约与 analyze_one 逐字同款：确定性失败返回 {"status":"failed",
"error_kind":...} 不抛；瞬时失败（LLM 5xx/超时）上抛由调用方退避重试。
零匹配（no_matches）是确定性失败——不烧 LLM，也不落一条"空摘要"。
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from ..config import settings
from ..core.logging import get_app_logger
from ..database import SessionLocal
from ..models.security_opinion import SecurityOpinionSummary
from .background_job_store import (
    JobOwnershipLostError,
    create_or_get_active_job,
    get_job,
    job_heartbeat,
    set_job_progress,
)
from .job_runtime import run_job_inline
from .job_worker import register_runner
from .llm_client import LLMClientError, LLMNotConfiguredError, chat_completion
from .opinion_summary_prompts import build_opinion_messages, parse_opinion_output
from .security_analysis_jobs import (
    LLM_FATAL_STATUS_CODES,
    AnalysisBusyError,
    resolve_public_security_name,
)
from .xueqiu_opinion_source import (
    KIND_LABELS,
    OpinionSourceUnavailable,
    build_wanted_map,
    scan_matched_utterances,
)

logger = get_app_logger(__name__)

JOB_TYPE = "opinion_summary"

OPINION_STAGES = (
    ("load_utterances", "读取雪球观点"),
    ("build_input", "组装观点输入"),
    ("llm_summary", "生成观点摘要（LLM）"),
    ("persist", "写入观点摘要"),
)
OPINION_STAGE_LABELS: Dict[str, str] = dict(OPINION_STAGES)
STAGE_TOTAL = len(OPINION_STAGES)

# 观点汇总只依赖发言 + LLM，不依赖行情/基本面数据源——刻意比档案分析的
# SUPPORTED_MARKETS 多 B股（持仓的 B股标的在关注作者发言里确有提及）
OPINION_MARKETS = ("A股", "B股", "港股", "美股")

# 整批等价的失败：批量调用方遇到即中止（source_unavailable = 表没了，
# 换标的重试同样失败）
FATAL_OPINION_ERROR_KINDS = frozenset(
    {"source_unavailable", "llm_not_configured", "llm_auth"}
)

# 输入字符预算：发言均长 80 字，几百条也远小于档案分析；30k 留足余量
OPINION_CHAR_BUDGET = 30_000
_TEXT_CAP = 300  # 一级收缩：单条正文截断（雪球长文尾部多为展开的转发链）
_CONTEXT_CAP = 120
_BASELINE_KEEP_PER_AUTHOR = 10  # 二级收缩：每作者 baseline 只留最新 N 条


def _payload_chars(payload: Dict[str, Any]) -> int:
    import json

    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str))


def build_opinion_input(
    matched: List[Dict[str, Any]],
    *,
    symbol: str,
    market: str,
    xq_symbol: str,
    recent_days: int,
    lookback_days: int,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """匹配发言 -> LLM 输入（纯函数，不吃 db）。

    按 author_name 分组、组内时间升序、按 recent/baseline 拆窗。两级收缩且
    级间复测；被截断的内容在 meta 里写明条数——"被截掉"与"作者没说"必须
    在输入里可区分，否则 LLM 的「讨论沉寂」就可能是收缩的伪影。
    """
    now = now or datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(days=recent_days)

    def to_row(item: Dict[str, Any], text_cap: int) -> Dict[str, Any]:
        created = item.get("created_at")
        row: Dict[str, Any] = {
            "date": created.date().isoformat() if created else None,
            "kind": KIND_LABELS.get(item.get("kind"), item.get("kind")),
            "text": (item.get("text") or "")[:text_cap],
        }
        context = (item.get("context_text") or "").strip()
        if context:
            context_author = item.get("context_author_name") or "原帖"
            row["context"] = f"{context_author}: {context[:_CONTEXT_CAP]}"
        return row

    authors: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    recent_count = 0
    latest_at: Optional[datetime] = None
    for item in sorted(matched, key=lambda entry: entry.get("created_at") or now):
        author = item.get("author_name") or "未知作者"
        bucket = authors.setdefault(author, {"recent": [], "baseline": []})
        created = item.get("created_at")
        if created and (latest_at is None or created > latest_at):
            latest_at = created
        if created and created >= recent_cutoff:
            bucket["recent"].append(to_row(item, _TEXT_CAP))
            recent_count += 1
        else:
            bucket["baseline"].append(to_row(item, _TEXT_CAP))

    # 逐作者统计必须在二级收缩**之前**取：收缩只保留 baseline 尾部 N 条，
    # 收缩后再数会把"作者有 160 条历史"错记成 10 条，接地校验随之失真
    author_stats = {
        author: {"recent": len(bucket["recent"]), "baseline": len(bucket["baseline"])}
        for author, bucket in authors.items()
    }
    payload: Dict[str, Any] = {
        "meta": {
            "symbol": symbol,
            "market": market,
            "xueqiu_symbol": xq_symbol,
            "recent_days": recent_days,
            "lookback_days": lookback_days,
            "recent_cutoff": recent_cutoff.date().isoformat(),
            "data_semantics": (
                "authors 下每位作者的发言分 recent（近期窗口内）与 baseline（更早）"
                "两组，组内按时间升序。kind：主帖=作者自己发帖；回复/评论回复=作者"
                "回复他人（context 是被回复的原帖节选）；转发=作者转发他人帖子。"
                "全部内容是雪球用户的个人公开发言，不是事实数据。"
            ),
        },
        "authors": authors,
        "stats": {
            "utterance_count": len(matched),
            "recent_count": recent_count,
            "author_count": len(authors),
            "author_stats": author_stats,
        },
    }
    if latest_at is not None:
        payload["stats"]["latest_utterance_at"] = latest_at.isoformat()

    # 二级收缩：超预算时每作者 baseline 只留最新 N 条，meta 写明省略量
    if _payload_chars(payload) > OPINION_CHAR_BUDGET:
        omitted = 0
        for bucket in authors.values():
            baseline = bucket["baseline"]
            if len(baseline) > _BASELINE_KEEP_PER_AUTHOR:
                omitted += len(baseline) - _BASELINE_KEEP_PER_AUTHOR
                bucket["baseline"] = baseline[-_BASELINE_KEEP_PER_AUTHOR:]
        if omitted:
            payload["meta"]["truncation_note"] = (
                f"因输入预算限制，另有 {omitted} 条更早的 baseline 发言未纳入"
            )
        if _payload_chars(payload) > OPINION_CHAR_BUDGET:
            logger.warning(
                "观点输入 %s/%s 收缩后仍超预算（%d 字符），按现状送出",
                symbol, market, _payload_chars(payload),
            )
    return payload


def start_opinion_summary_job(user_id: int, symbol: str, market: str) -> Dict[str, Any]:
    job = create_or_get_active_job(
        JOB_TYPE,
        user_id,
        {
            "symbol": symbol, "market": market, "summary_id": None,
            "stage": None, "stage_label": "排队中",
            "total": STAGE_TOTAL, "completed": 0, "progress_percent": 0,
        },
    )
    if job.get("symbol") != symbol or job.get("market") != market:
        raise AnalysisBusyError(
            f"已有针对 {job.get('symbol')}（{job.get('market')}）的观点摘要任务"
            "进行中，请等待其完成后再发起。",
            active_job=job,
        )
    return job


def summarize_one(
    db,
    symbol: str,
    market: str,
    *,
    matched: Optional[List[Dict[str, Any]]] = None,
    on_stage: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """执行一次观点摘要并落库；不做任何 job 记账（单标的与批量共用）。

    matched 允许由批量调用方预先传入（批量启动时已做过一次全扫，逐标的再扫
    是 N 倍浪费）；单标的路径传 None 自行扫描。
    """

    def stage(name: str, **extra: Any) -> None:
        if on_stage is None:
            return
        try:
            on_stage(name, extra)
        except JobOwnershipLostError:
            # 失权是"立刻停手"信号，必须穿透兜底（见 analyze_one 同处注释）
            raise
        except Exception as exc:
            logger.warning("观点进度回写失败 %s/%s: %s", symbol, market, str(exc)[:150])

    def failure(error: str, kind: str) -> Dict[str, Any]:
        return {
            "symbol": symbol, "market": market, "status": "failed",
            "summary_id": None, "error": error, "error_kind": kind,
        }

    if market not in OPINION_MARKETS:
        return failure(
            f"{market} 暂不支持观点摘要（支持：{'/'.join(OPINION_MARKETS)}）",
            "unsupported_market",
        )

    # 1/4 读取匹配发言
    stage("load_utterances", completed=0)
    recent_days = settings.xueqiu_opinion_recent_days
    lookback_days = settings.xueqiu_opinion_lookback_days
    try:
        wanted = build_wanted_map([(symbol, market)])
        if not wanted:
            return failure(f"标的 {symbol}/{market} 无法转换为雪球代码", "unsupported_market")
        xq_symbol = next(iter(wanted))
        if matched is None:
            since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
            matched = scan_matched_utterances(db, set(wanted), since=since).get(xq_symbol, [])
    except OpinionSourceUnavailable as exc:
        return failure(str(exc), "source_unavailable")
    if not matched:
        return failure(
            f"近 {lookback_days} 天内关注作者未提及该标的（cashtag/帖子链接口径），"
            "无内容可摘要",
            "no_matches",
        )

    # 2/4 组装输入
    stage("build_input", completed=1)
    input_payload = build_opinion_input(
        matched,
        symbol=symbol, market=market, xq_symbol=xq_symbol,
        recent_days=recent_days, lookback_days=lookback_days,
    )
    stats = input_payload["stats"]

    # 3/4 LLM
    stage("llm_summary", completed=2)
    try:
        completion = chat_completion(
            build_opinion_messages(input_payload),
            response_format={"type": "json_object"},
        )
        parsed = parse_opinion_output(
            completion["content"],
            author_stats=stats["author_stats"],
        )
    except LLMNotConfiguredError as exc:
        return failure(str(exc), "llm_not_configured")
    except ValueError as exc:  # 输出解析失败：确定性失败不烧重试
        return failure(f"LLM 输出解析失败：{exc}", "parse")
    except LLMClientError as exc:
        if exc.status_code in LLM_FATAL_STATUS_CODES:
            return failure(str(exc), "llm_auth")
        if exc.status_code is not None and 400 <= exc.status_code < 500:
            return failure(str(exc), "llm_4xx")
        raise  # 5xx/超时 → 调用方退避重试

    # 4/4 落库（全局产物：名称只从公共元数据解析，与 SecurityAnalysis 同规）
    stage("persist", completed=3)
    usage = completion.get("usage", {})
    latest_raw = stats.get("latest_utterance_at")
    summary_row = SecurityOpinionSummary(
        symbol=symbol,
        market=market,
        name=resolve_public_security_name(symbol, market),
        tags=parsed["tags"],
        author_stances=parsed["author_stances"],
        summary=parsed["summary"],
        content=parsed["report_markdown"],
        model=completion.get("model", ""),
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        total_tokens=usage.get("total_tokens"),
        input_payload=input_payload,
        recent_days=recent_days,
        lookback_days=lookback_days,
        utterance_count=stats["utterance_count"],
        recent_utterance_count=stats["recent_count"],
        latest_utterance_at=(
            datetime.fromisoformat(latest_raw) if latest_raw else None
        ),
    )
    db.add(summary_row)
    db.commit()
    db.refresh(summary_row)
    return {
        "symbol": symbol, "market": market, "status": "succeeded",
        "summary_id": summary_row.id, "error": None, "error_kind": None,
        "tags": parsed["tags"],
    }


def execute_opinion_summary_job(claimed: Dict[str, Any]) -> None:
    job_id = claimed["id"]
    attempt = claimed.get("attempt_count")
    symbol = claimed["data"]["symbol"]
    market = claimed["data"]["market"]

    def report(stage_name: str, extra: Dict[str, Any]) -> None:
        if set_job_progress(
            job_id, JOB_TYPE, required_attempt_count=attempt,
            stage=stage_name,
            stage_label=OPINION_STAGE_LABELS.get(stage_name, stage_name),
            total=STAGE_TOTAL, **extra,
        ) is None:
            raise JobOwnershipLostError(job_id)

    db = SessionLocal()
    try:
        with job_heartbeat(job_id, JOB_TYPE, attempt_count=attempt):
            outcome = summarize_one(db, symbol, market, on_stage=report)
        if outcome["status"] == "failed":
            set_job_progress(
                job_id, JOB_TYPE, required_attempt_count=attempt,
                status="failed", error=outcome["error"],
            )
            return
        set_job_progress(
            job_id, JOB_TYPE, required_attempt_count=attempt,
            status="succeeded", stage="done", stage_label="已完成",
            completed=STAGE_TOTAL, total=STAGE_TOTAL,
            summary_id=outcome["summary_id"],
        )
    except JobOwnershipLostError:
        logger.warning("观点摘要 job %s 已被接管或进入终态，本次执行停止", job_id)
    finally:
        db.close()


def run_opinion_summary_job(job_id: str) -> None:
    run_job_inline(
        job_id, JOB_TYPE, execute_opinion_summary_job,
        label="Opinion summary", logger=logger,
    )


def get_opinion_summary_job(job_id: str, user_id: int) -> Optional[Dict[str, Any]]:
    return get_job(job_id, JOB_TYPE, user_id)


register_runner(JOB_TYPE, execute_opinion_summary_job)
