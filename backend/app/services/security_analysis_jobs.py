"""LLM 标的分析 job：同步基本面 → 压缩输入 → JSON mode 生成 → 落分析行。

结构照抄 llm_report_jobs 五函数模板。分析是标的级全局产物（只依赖公开
数据），job 仍按用户入队（background_jobs 用户域 + 每用户单活跃任务的
天然去重）；data 携带 {symbol, market}。

错误分层与 llm_report 一致：未配置 key / 4xx / 输出解析失败 → 确定性
失败不烧重试；5xx/超时/意外 → 上抛走退避重试。周期任务不做——分析
token 成本高，由用户在详情页显式触发。
"""

import json
from typing import Any, Dict, Optional

from ..core.logging import get_app_logger
from ..database import SessionLocal
from ..models.security_profile import SecurityAnalysis
from .background_job_store import (
    claim_job,
    create_or_get_active_job,
    get_job,
    handle_job_failure,
    update_job,
)
from .job_worker import register_runner
from .llm_client import LLMClientError, LLMNotConfiguredError, chat_completion
from .security_analysis_prompts import build_analysis_messages, parse_analysis_output
from .security_profile_service import (
    PROFILE_CAPS,
    load_security_events_for,
    load_symbol_profile,
    profile_fetched_date,
    sync_symbol_profile,
)

logger = get_app_logger(__name__)
JOB_TYPE = "security_analysis"


def resolve_public_security_name(symbol: str, market: str) -> str | None:
    """公共证券元数据名称（Tushare stock_basic）；失败/缺失留空。"""
    from .ibkr_activity_importer import lookup_tushare_security_name

    try:
        return lookup_tushare_security_name(symbol, market)
    except Exception as exc:  # 名称是锦上添花：查询失败不阻断分析
        logger.warning("解析 %s/%s 公共名称失败: %s", symbol, market, str(exc)[:120])
        return None


class AnalysisBusyError(Exception):
    """当前用户已有针对**另一标的**的活跃分析任务（API 层映射 409）。

    create_or_get_active_job 只按 (user, job_type) 去重，不比较 data——
    不校验会把 600036 的进行中任务当作 000001 请求的"成功"返回，前端轮询
    完成后却加载不到目标标的的结果。
    """

    def __init__(self, message: str, active_job: dict):
        super().__init__(message)
        self.active_job = active_job

# 输入字符预算：单标的档案远小于全组合复盘，30k 足够且省 token
CHAR_BUDGET = 30_000
SHRUNK_CAPS = {dataset: max(2, cap // 2) for dataset, cap in PROFILE_CAPS.items()}


def build_analysis_input(db, symbol: str, market: str) -> Dict[str, Any]:
    """压缩输入：分组档案数据（逐集封顶）+ 标的事件；超预算二级收缩。"""
    profile = load_symbol_profile(db, symbol, market)
    events = load_security_events_for(db, symbol, market)
    payload = {
        "meta": {
            "symbol": symbol,
            "market": market,
            "data_semantics": (
                "fina_indicator=财务指标(按报告期)；forecast=业绩预告；express=业绩快报；"
                "daily_basic=最新估值快照(pe/pb/股息率)；dividend_history=分红送股历史"
                "(div_proc=实施为已落地)；fina_audit=审计意见；pledge_stat=股权质押统计；"
                "stk_holdertrade=重要股东增减持；events=财报披露/分红预案/解禁事件"
            ),
        },
        "profile": profile["datasets"],
        "events": events,
    }
    if len(json.dumps(payload, ensure_ascii=False, default=str)) > CHAR_BUDGET:
        profile = load_symbol_profile(db, symbol, market, caps=SHRUNK_CAPS)
        payload["profile"] = profile["datasets"]
    return payload


def start_security_analysis_job(user_id: int, symbol: str, market: str) -> Dict[str, Any]:
    job = create_or_get_active_job(
        JOB_TYPE,
        user_id,
        {"symbol": symbol, "market": market, "analysis_id": None},
    )
    # _serialize 把 data 展平到顶层
    if job.get("symbol") != symbol or job.get("market") != market:
        raise AnalysisBusyError(
            f"已有针对 {job.get('symbol')}（{job.get('market')}）的分析任务"
            "进行中，请等待其完成后再发起新的标的分析。",
            active_job=job,
        )
    return job


def execute_security_analysis_job(claimed: Dict[str, Any]) -> None:
    job_id = claimed["id"]
    symbol = claimed["data"]["symbol"]
    market = claimed["data"]["market"]

    db = SessionLocal()
    try:
        # 先刷新档案数据（单数据集失败不阻断：LLM 会按"数据不足"处理）
        sync_result = sync_symbol_profile(db, symbol, market)
        if not sync_result["supported"]:
            update_job(
                job_id, JOB_TYPE, status="failed",
                error=f"{market} 暂不支持基本面数据（仅 A 股）",
                required_status="running",
            )
            return

        input_payload = build_analysis_input(db, symbol, market)
        try:
            completion = chat_completion(
                build_analysis_messages(input_payload),
                response_format={"type": "json_object"},
            )
            parsed = parse_analysis_output(completion["content"])
        except LLMNotConfiguredError as exc:
            update_job(
                job_id, JOB_TYPE, status="failed", error=str(exc),
                required_status="running",
            )
            return
        except ValueError as exc:  # 输出解析失败：确定性失败不烧重试
            update_job(
                job_id, JOB_TYPE, status="failed",
                error=f"LLM 输出解析失败：{exc}", required_status="running",
            )
            return
        except LLMClientError as exc:
            if exc.status_code is not None and 400 <= exc.status_code < 500:
                update_job(
                    job_id, JOB_TYPE, status="failed", error=str(exc),
                    required_status="running",
                )
                return
            raise  # 5xx/超时 → 退避重试

        # 全局产物不得读取任何用户的持仓字段（用户手工录入的名称会经此
        # 泄露给全部用户，且多用户不同名导致结果不确定）——名称只从公共
        # 证券元数据（Tushare stock_basic）解析，失败即留空由前端回退代码
        usage = completion.get("usage", {})
        analysis = SecurityAnalysis(
            symbol=symbol,
            market=market,
            name=resolve_public_security_name(symbol, market),
            tags=parsed["tags"],
            risk_level=parsed["risk_level"],
            summary=parsed["summary"],
            content=parsed["report_markdown"],
            model=completion.get("model", ""),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            input_payload=input_payload,
            data_fetched_at=profile_fetched_date(db, symbol, market),
        )
        db.add(analysis)
        db.commit()
        db.refresh(analysis)
        update_job(
            job_id, JOB_TYPE, status="succeeded",
            data_updates={"analysis_id": analysis.id},
            required_status="running",
        )
    finally:
        db.close()


def run_security_analysis_job(job_id: str) -> None:
    """Inline fast path：按 id 认领并执行；意外错误走重试/退避路径。"""
    claimed = claim_job(job_id, JOB_TYPE)
    if not claimed:
        logger.info("Security analysis job %s was already claimed or no longer queued", job_id)
        return
    try:
        execute_security_analysis_job(claimed)
    except Exception as exc:
        logger.exception("Security analysis job %s failed", job_id)
        handle_job_failure(job_id, JOB_TYPE, str(exc))


def get_security_analysis_job(job_id: str, user_id: int) -> Optional[Dict[str, Any]]:
    return get_job(job_id, JOB_TYPE, user_id)


register_runner(JOB_TYPE, execute_security_analysis_job)
