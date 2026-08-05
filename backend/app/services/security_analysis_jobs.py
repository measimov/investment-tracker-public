"""LLM 标的分析 job：同步基本面 → 压缩输入 → JSON mode 生成 → 落分析行。

结构照抄 llm_report_jobs 五函数模板。分析是标的级全局产物（只依赖公开
数据），job 仍按用户入队（background_jobs 用户域 + 每用户单活跃任务的
天然去重）；data 携带 {symbol, market}。

错误分层与 llm_report 一致：未配置 key / 4xx / 输出解析失败 → 确定性
失败不烧重试；5xx/超时/意外 → 上抛走退避重试。周期任务不做——分析
token 成本高，由用户在详情页显式触发。
"""

import json
from typing import Any, Callable, Dict, Optional

from ..core.logging import get_app_logger
from ..database import SessionLocal
from ..models.security_profile import SecurityAnalysis
from .background_job_store import (
    claim_job,
    create_or_get_active_job,
    get_job,
    handle_job_failure,
    job_heartbeat,
    set_job_progress,
)
from .job_worker import register_runner
from .llm_client import LLMClientError, LLMNotConfiguredError, chat_completion
from .security_analysis_prompts import build_analysis_messages, parse_analysis_output
from .security_profile_service import (
    PROFILE_CAPS,
    SUPPORTED_MARKETS,
    load_security_events_for,
    load_symbol_profile,
    profile_fetched_date,
    sync_symbol_profile,
)

logger = get_app_logger(__name__)
JOB_TYPE = "security_analysis"

# 分析阶段（进度展示 + 续租的回写点）。顺序即执行顺序，completed 从 0 递增。
ANALYSIS_STAGES = (
    ("sync_profile", "同步基本面档案"),
    ("report_digests", "补齐财报摘要"),
    ("business_profile", "刷新商业画像与同业"),
    ("build_input", "组装分析输入"),
    ("llm_analysis", "生成分析（LLM）"),
    ("persist", "写入分析结果"),
)
ANALYSIS_STAGE_LABELS: Dict[str, str] = dict(ANALYSIS_STAGES)
STAGE_TOTAL = len(ANALYSIS_STAGES)

# LLM 侧对"换个标的重试"无济于事的状态码：鉴权失败、余额不足、限流
LLM_FATAL_STATUS_CODES = frozenset({401, 402, 403, 429})

# 整批等价的失败类型：批量调用方遇到即立即中止，不再逐只消耗配额
FATAL_ANALYSIS_ERROR_KINDS = frozenset(
    {"llm_not_configured", "llm_auth", "tushare_fatal"}
)


def resolve_public_security_name(symbol: str, market: str) -> str | None:
    """公共证券元数据名称（A股=Tushare stock_basic；美股=EDGAR 注册名）；
    失败/缺失留空。"""
    try:
        if market == "美股":
            from .report_fetchers import edgar_lookup

            lookup = edgar_lookup(symbol)
            return lookup.get("title") if lookup else None
        from .ibkr_activity_importer import lookup_tushare_security_name

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
CHAR_BUDGET = 40_000  # 含十年财报摘要后上调（原 30k）
SHRUNK_CAPS = {dataset: max(2, cap // 2) for dataset, cap in PROFILE_CAPS.items()}

# 三大报表 85-152 列/行，全字段会撑爆预算且多为空值——LLM 输入只取核心科目
# （库内保留全量行供详情面板与追溯）
STATEMENT_LLM_FIELDS: Dict[str, tuple] = {
    "income": (
        "end_date", "total_revenue", "revenue", "operate_profit", "total_profit",
        "n_income", "n_income_attr_p", "basic_eps",
    ),
    "balancesheet": (
        "end_date", "total_assets", "total_liab", "total_hldr_eqy_exc_min_int",
        "money_cap", "goodwill", "inventories", "accounts_receiv",
    ),
    "cashflow": (
        "end_date", "n_cashflow_act", "n_cashflow_inv_act", "n_cash_flows_fnc_act",
        "free_cashflow", "c_pay_acq_const_fiolta",
    ),
}


def _compact_statement_rows(dataset: str, rows: list) -> list:
    fields = STATEMENT_LLM_FIELDS.get(dataset)
    if not fields:
        return rows
    return [
        {field: row.get(field) for field in fields if row.get(field) is not None}
        for row in rows
    ]


def _compact_profile(datasets: Dict[str, list]) -> Dict[str, list]:
    return {
        dataset: _compact_statement_rows(dataset, rows)
        for dataset, rows in datasets.items()
    }


# 缺口清单进 LLM 输入的条数上限（超出部分以计数如实告知）
MAX_DIGEST_GAPS = 6


def build_analysis_input(
    db, symbol: str, market: str, *,
    digest_gaps: list | None = None, data_gaps: list | None = None,
) -> Dict[str, Any]:
    """压缩输入：档案数据（逐集封顶+报表科目白名单）+ 事件 + 财报摘要
    + 商业画像/同业 + 利润质量指标；超预算逐级收缩。"""
    from .business_profile_service import load_business_profile
    from .earnings_quality import compute_earnings_quality, market_statements
    from .report_digest_service import load_report_digests, serialize_digest_for_analysis

    profile = load_symbol_profile(db, symbol, market)
    events = load_security_events_for(db, symbol, market)
    digests = serialize_digest_for_analysis(load_report_digests(db, symbol, market))
    business = load_business_profile(db, symbol, market)
    statements = market_statements(market, profile["datasets"])
    earnings_quality = compute_earnings_quality(
        statements["income"],
        statements["balancesheet"],
        statements["cashflow"],
        statements["fina_indicator"],
    )
    common_semantics = (
        "report_digests=财报关键章节的 AI 摘要(按报告期倒序，旧年份为压缩版；"
        "属公司自述口径，可引用)；business_profile=商业画像(商业模式/分部/上下游/"
        "估值因子)；peers=同行业名单(仅供提及可比公司，禁止对同业展开分析——"
        "同业数据不在输入中)；earnings_quality=预计算利润质量指标"
        "(红旗阈值见 metric_semantics)"
    )
    market_semantics = {
        "A股": (
            "fina_indicator=财务指标(按报告期)；forecast=业绩预告；express=业绩快报；"
            "daily_basic=最新估值快照(pe/pb/股息率)；dividend_history=分红送股历史"
            "(div_proc=实施为已落地)；fina_audit=审计意见；pledge_stat=股权质押统计；"
            "stk_holdertrade=重要股东增减持；income/balancesheet/cashflow=三大报表"
            "核心科目(合并报表口径，单位元)；events=财报披露/分红预案/解禁事件；"
        ),
        "美股": (
            "edgar_companyfacts=SEC XBRL 年度(FY)/季度核心科目(单位美元，"
            "科目缺失=该公司未按对应 us-gaap 概念披露——中概股常不披露贸易应收"
            "与存货，对应指标留空属正常)；report_digests 来自年报 10-K 或 20-F"
            "(外国私人发行人)；本市场无审计意见/质押/增减持/分红预案事件数据源；"
        ),
        "港股": (
            "yahoo_fundamentals=雅虎年度核心科目(报告币种见行内 currency 字段，"
            "公司间不一致；非官方接口、**仅近 3-5 年**)；report_digests=披露易"
            "年报全文的 AI 摘要；本市场无审计意见/质押/增减持/解禁数据源，风险"
            "只能来自年报摘要——年报未设「主要風險」章节时该项为空，须如实说明；"
        ),
    }
    payload = {
        "meta": {
            "symbol": symbol,
            "market": market,
            "data_semantics": market_semantics.get(market, "") + common_semantics,
        },
        "profile": _compact_profile(profile["datasets"]),
        "events": events,
        "report_digests": digests,
        "business_profile": business.get("profile"),
        "peers": {
            "industry": business.get("industry"),
            "list": [
                f"{peer.get('symbol')} {peer.get('name')}"
                for peer in (business.get("peers") or [])[:20]
            ],
        },
        "earnings_quality": earnings_quality,
    }
    if digest_gaps:
        # 截断本身也要可见：只留 6 条而不说"还有几条"，模型会把这 6 条当成
        # 缺口全集，把"没列出来的年份"读成"没有问题的年份"
        payload["report_digest_gaps"] = list(digest_gaps[:MAX_DIGEST_GAPS])
        if len(digest_gaps) > MAX_DIGEST_GAPS:
            payload["report_digest_gaps"].append(
                f"（另有 {len(digest_gaps) - MAX_DIGEST_GAPS} 条摘要缺口未列出）"
            )
    if data_gaps:
        # 数据集本次未取到（接口冷却/同步失败）。必须显式告知模型，否则"没数据"
        # 会被当成"没有质押/无风险信号"——把限流伪装成利好，比整体失败更危险。
        payload["profile_data_gaps"] = data_gaps[:8]
    if len(json.dumps(payload, ensure_ascii=False, default=str)) > CHAR_BUDGET:
        profile = load_symbol_profile(db, symbol, market, caps=SHRUNK_CAPS)
        payload["profile"] = _compact_profile(profile["datasets"])
        # 摘要侧二级收缩：全部压为核心四字段
        payload["report_digests"] = serialize_digest_for_analysis(
            load_report_digests(db, symbol, market), compact_older_than_years=0
        )
    return payload


def start_security_analysis_job(user_id: int, symbol: str, market: str) -> Dict[str, Any]:
    job = create_or_get_active_job(
        JOB_TYPE,
        user_id,
        {
            "symbol": symbol, "market": market, "analysis_id": None,
            # 进度字段（_serialize 展平到响应顶层，前端立即可读）
            "stage": None, "stage_label": "排队中",
            "total": STAGE_TOTAL, "completed": 0, "progress_percent": 0,
        },
    )
    # _serialize 把 data 展平到顶层
    if job.get("symbol") != symbol or job.get("market") != market:
        raise AnalysisBusyError(
            f"已有针对 {job.get('symbol')}（{job.get('market')}）的分析任务"
            "进行中，请等待其完成后再发起新的标的分析。",
            active_job=job,
        )
    return job


def analyze_one(
    db,
    symbol: str,
    market: str,
    *,
    digest_max_new: int = 2,
    on_stage: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """执行一次完整标的分析并落 security_analyses；**不做任何 job 记账**。

    单标的 job 与批量 job 共用本函数。失败语义与拆分前逐字一致：
    - 确定性失败（不支持市场 / LLM 未配置 / 输出解析失败 / LLM 4xx）
      → 返回 {"status": "failed", "error_kind": ...}，不抛。
    - 瞬时失败（LLM 5xx/超时、意外异常）→ **上抛**，由调用方决定重试。

    on_stage(stage, extra) 在每个阶段开始时回调（调用方用它回写进度并续租）；
    回调异常不得影响分析本身。
    """
    def stage(name: str, **extra: Any) -> None:
        if on_stage is None:
            return
        try:
            on_stage(name, extra)
        except Exception as exc:  # 进度回写失败不能拖垮分析
            logger.warning("分析进度回写失败 %s/%s: %s", symbol, market, str(exc)[:150])

    def failure(error: str, kind: str) -> Dict[str, Any]:
        return {
            "symbol": symbol, "market": market, "status": "failed",
            "analysis_id": None, "error": error, "error_kind": kind,
            "degraded": [], "digest_gaps": [],
        }

    # 1/6 先刷新档案数据（单数据集失败不阻断：LLM 会按"数据不足"处理）
    stage("sync_profile", completed=0)
    sync_result = sync_symbol_profile(db, symbol, market)
    if not sync_result["supported"]:
        return failure(
            f"{market} 暂不支持基本面数据（支持：{'/'.join(SUPPORTED_MARKETS)}）",
            "unsupported_market",
        )
    fatal = sync_result.get("fatal")
    if fatal:
        # token 失效/无权限：不能继续生成一份没有数据依据的"降级分析"，
        # 也必须让批量调用方看得出这是整批等价的失败
        return failure(
            f"数据源致命错误（{fatal['dataset']}）：{fatal['error']}", "tushare_fatal"
        )
    degraded = [
        f"{item['dataset']} 数据集本次获取失败"
        for item in sync_result.get("failed", [])
    ] + [
        f"{item['dataset']} 数据集因数据源频率限制本次跳过"
        f"（约 {item.get('retry_after_seconds')}s 后可重试）"
        for item in sync_result.get("skipped", [])
    ]

    # 2/6 财报摘要惰性保底：任何失败不阻断主分析，缺口进输入
    stage("report_digests", completed=1)
    digest_gaps: list = []
    if digest_max_new > 0:
        try:
            from .report_digest_service import ensure_report_digests

            digest_result = ensure_report_digests(db, symbol, market, max_new=digest_max_new)
            digest_gaps = digest_result.get("gaps", [])
        except Exception as exc:
            logger.warning("报告摘要保底失败 %s/%s: %s", symbol, market, str(exc)[:150])
            digest_gaps = ["报告摘要管线异常，本次分析未包含财报摘要"]

    # 3/6 商业画像与同业名单顺带刷新（内部吞错，失败降级为缓存/空）
    stage("business_profile", completed=2)
    try:
        from .business_profile_service import ensure_business_profile, ensure_peer_list

        ensure_peer_list(db, symbol, market)
        ensure_business_profile(db, symbol, market)
    except Exception as exc:
        logger.warning("商业画像刷新失败 %s/%s: %s", symbol, market, str(exc)[:150])

    # 4/6 组装输入
    stage("build_input", completed=3)
    input_payload = build_analysis_input(
        db, symbol, market, digest_gaps=digest_gaps, data_gaps=degraded,
    )

    # 5/6 生成（LLM）
    stage("llm_analysis", completed=4)
    try:
        completion = chat_completion(
            build_analysis_messages(input_payload),
            response_format={"type": "json_object"},
        )
        parsed = parse_analysis_output(completion["content"], market=market)
    except LLMNotConfiguredError as exc:
        return failure(str(exc), "llm_not_configured")
    except ValueError as exc:  # 输出解析失败：确定性失败不烧重试
        return failure(f"LLM 输出解析失败：{exc}", "parse")
    except LLMClientError as exc:
        if exc.status_code in LLM_FATAL_STATUS_CODES:
            # 鉴权/欠费/限流：换个标的重试同样会失败，批量必须立即中止
            return failure(str(exc), "llm_auth")
        if exc.status_code is not None and 400 <= exc.status_code < 500:
            return failure(str(exc), "llm_4xx")
        raise  # 5xx/超时 → 由调用方退避重试

    # 6/6 落库。全局产物不得读取任何用户的持仓字段（用户手工录入的名称会经此
    # 泄露给全部用户，且多用户不同名导致结果不确定）——名称只从公共证券元数据
    # 解析，失败即留空由前端回退代码
    stage("persist", completed=5)
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
    return {
        "symbol": symbol, "market": market, "status": "succeeded",
        "analysis_id": analysis.id, "error": None, "error_kind": None,
        "degraded": degraded, "digest_gaps": digest_gaps,
    }


def execute_security_analysis_job(claimed: Dict[str, Any]) -> None:
    job_id = claimed["id"]
    attempt = claimed.get("attempt_count")
    symbol = claimed["data"]["symbol"]
    market = claimed["data"]["market"]

    def report(stage_name: str, extra: Dict[str, Any]) -> None:
        set_job_progress(
            job_id, JOB_TYPE, required_attempt_count=attempt,
            stage=stage_name, stage_label=ANALYSIS_STAGE_LABELS.get(stage_name, stage_name),
            total=STAGE_TOTAL, **extra,
        )

    db = SessionLocal()
    try:
        # 心跳兜底：单次 LLM 调用或大年报解析本身就可能吃掉整个租约
        with job_heartbeat(job_id, JOB_TYPE, attempt_count=attempt):
            outcome = analyze_one(db, symbol, market, on_stage=report)
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
            analysis_id=outcome["analysis_id"], degraded=outcome["degraded"],
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
        handle_job_failure(
            job_id, JOB_TYPE, str(exc),
            required_attempt_count=claimed.get("attempt_count"),
        )


def get_security_analysis_job(job_id: str, user_id: int) -> Optional[Dict[str, Any]]:
    return get_job(job_id, JOB_TYPE, user_id)


register_runner(JOB_TYPE, execute_security_analysis_job)
