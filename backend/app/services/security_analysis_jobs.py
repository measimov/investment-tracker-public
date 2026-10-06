"""LLM 标的分析 job：同步基本面 → 压缩输入 → JSON mode 生成 → 落分析行。

结构照抄 llm_report_jobs 五函数模板。分析是标的级全局产物（只依赖公开
数据），job 仍按用户入队（background_jobs 用户域 + 每用户单活跃任务的
天然去重）；data 携带 {symbol, market}。

错误分层与 llm_report 一致：未配置 key / 4xx / 输出解析失败 → 确定性
失败不烧重试；5xx/超时/意外 → 上抛走退避重试。周期任务不做——分析
token 成本高，由用户在详情页显式触发。
"""

import json
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional

from ..config import settings
from .analysis_signals import SIGNALS_SEMANTICS
from ..core.logging import get_app_logger
from ..database import SessionLocal
from ..models.security_profile import SecurityAnalysis
from .background_job_store import (
    JobOwnershipLostError,
    create_or_get_active_job,
    get_job,
    job_heartbeat,
    set_job_progress,
)
from .job_runtime import make_batch_progress, run_job_inline
from .job_worker import register_runner
from .llm_client import (
    LLMClientError,
    LLMNotConfiguredError,
    chat_completion,
    is_output_truncated,
)
from .security_analysis_prompts import (
    IncompleteReportError,
    build_analysis_messages,
    graham_for_llm,
    parse_analysis_output,
)
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
FATAL_ANALYSIS_ERROR_KINDS = frozenset({"llm_not_configured", "llm_auth", "tushare_fatal"})
# 报告缺章节（半截）时同一输入的重试次数
INCOMPLETE_REPORT_RETRIES = 1


def resolve_public_security_name(symbol: str, market: str) -> str | None:
    """公共证券元数据名称：先查标的全集（零外呼），未命中再走
    A股=Tushare stock_basic / 美股=EDGAR 注册名；失败/缺失留空。"""
    try:
        from .security_catalog_service import lookup_catalog_name

        cataloged = lookup_catalog_name(symbol, market)
        if cataloged:
            return cataloged
    except Exception as exc:  # 目录只是快路径
        logger.warning("查询标的全集名称失败 %s/%s: %s", symbol, market, str(exc)[:120])
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


# 输入字符预算（#332 离线评测，2026-10）：摘要补齐十年后，去噪音的完整输入 A股 6.5–8 万、港股 4.5–5.3 万、
# 美股 ≤4.1 万字符。4 万预算会把港股十年报表砍半、摘要压成核心字段——评测里「完整送出」相对「截断」
# 总分 +0.78、错误 −0.40/份（港股 +1.67、错误减半；A股 无差别），故提高到现有输入都不截断的水平，
# 逐级收缩只作超长时的安全网。结果见 ops/llm-eval/pr3/
CHAR_BUDGET = 100_000
# 分析输入的港股报表行窗口：年度十二期（十年 + 比较列多出的年份）；中报只送最新一期及其
# 比较列——中报科目与年度同名，多送几期只会让模型把半年数与全年数混算
ANALYSIS_STATEMENT_CAPS: Dict[str, int] = {"FY": 12, "H1": 2}
ANALYSIS_CAPS: Dict[str, Any] = {**PROFILE_CAPS, "report_statements": ANALYSIS_STATEMENT_CAPS}


def shrink_caps(caps: Dict[str, Any]) -> Dict[str, Any]:
    """超预算一级收缩：每集封顶减半（按 fp 的字典逐项减半），下限 2。"""
    shrunk: Dict[str, Any] = {}
    for dataset, cap in caps.items():
        if isinstance(cap, dict):
            shrunk[dataset] = {fp: max(2, int(value) // 2) for fp, value in cap.items()}
        else:
            shrunk[dataset] = max(2, int(cap) // 2)
    return shrunk


SHRUNK_CAPS = shrink_caps(ANALYSIS_CAPS)
# 超预算收缩时最后才动的**证据**数据集：报表与财务指标（#332：此前一级收缩先把它们减半，
# 噪音数据集反而原样保留）
CORE_PROFILE_DATASETS = frozenset(
    {
        "income",
        "balancesheet",
        "cashflow",
        "fina_indicator",
        "xueqiu_income",
        "report_statements",
        "yahoo_fundamentals",
        "edgar_companyfacts",
    }
)
# 只减半非核心数据集（业绩预告/快报、审计、质押、增减持、股东、资金流……）的封顶
NONCORE_SHRUNK_CAPS = {
    dataset: (ANALYSIS_CAPS[dataset] if dataset in CORE_PROFILE_DATASETS else cap)
    for dataset, cap in SHRUNK_CAPS.items()
}

# 三大报表 85-152 列/行，全字段会撑爆预算且多为空值——LLM 输入只取核心科目
# （库内保留全量行供详情面板与追溯）
STATEMENT_LLM_FIELDS: Dict[str, tuple] = {
    "income": (
        "end_date",
        "total_revenue",
        "revenue",
        "operate_profit",
        "total_profit",
        "n_income",
        "n_income_attr_p",
        "basic_eps",
    ),
    "balancesheet": (
        "end_date",
        "total_assets",
        "total_liab",
        "total_hldr_eqy_exc_min_int",
        "money_cap",
        "goodwill",
        "inventories",
        "accounts_receiv",
    ),
    "cashflow": (
        "end_date",
        "n_cashflow_act",
        "n_cashflow_inv_act",
        "n_cash_flows_fnc_act",
        "c_pay_acq_const_fiolta",
    ),
    # A股 Tushare 财务指标（#332）：整行 108 个字段、约 1.55 万字符，大多分析用不上，且有与正确字段
    # 长得像的干扰项（gross_margin 是毛利**额**，毛利率是 grossprofit_margin）。只送下列被代码或
    # 提示词实际引用的字段（tests/test_analysis_input_denoise.py 用 AST 扫描守护，新增引用漏加即红）
    "fina_indicator": (
        "end_date",
        # analysis_signals.roe_by_year（三种 ROE 口径）
        "roe",
        "roe_waa",
        "roe_dt",
        # earnings_quality（毛利率/净利率序列、扣非占比）
        "grossprofit_margin",
        "netprofit_margin",
        "profit_dedt",
        # graham_screen（流动比率、资产负债率、EPS、EBIT）
        "current_ratio",
        "debt_to_assets",
        "eps",
        "ebit",
        # 详情页基本面卡展示的增速（FundamentalsTab）：模型与用户看到同一组数
        "or_yoy",
        "tr_yoy",
        "netprofit_yoy",
    ),
    # A股 雪球利润表（#332）：年度行 85 个字段、每个科目还带 _yoy，约 1.35 万字符；与 Tushare 利润表
    # 只送核心科目，且只送 Tushare 利润表块里没有的期间（_compact_profile 去重）——
    # 它的价值是更长的年度历史，不是重复一遍最近两年
    "xueqiu_income": (
        "end_date",
        "total_revenue",
        "operating_cost",
        "sales_fee",
        "manage_fee",
        "rad_cost",
        "financing_expenses",
        "asset_impairment_loss",
        "credit_impairment_loss",
        "invest_income",
        "income_from_chg_in_fv",
        "op",
        "profit_total_amt",
        "income_tax_expenses",
        "net_profit",
        "net_profit_atsopc",
        "net_profit_after_nrgal_atsolc",
        "basic_eps",
    ),
    # 港股 PDF 抽取行：只送科目与期别，源 URL/页码/指纹等溯源元数据留在库里
    "report_statements": (
        "end_date",
        "fp",
        "currency",
        "is_comparative",
        "validation_status",
        # 已被后续报告重列的科目（值仍是首次披露数）/ 构建期修复过的资产小计
        "restated_fields",
        "repaired_fields",
        "total_revenue",
        "cost_of_revenue",
        "gross_profit",
        "operating_income",
        "n_income_attr_p",
        "total_profit",
        "income_tax",
        "ebitda",
        "sga_exp",
        "int_exp",
        "basic_eps",
        "diluted_eps",
        "total_assets",
        "total_nca",
        "total_cur_assets",
        "total_cur_liab",
        "total_ncl",
        "accounts_receiv",
        "inventories",
        "fix_assets",
        "money_cap",
        "total_liab",
        "total_hldr_eqy_exc_min_int",
        "total_equity",
        "minority_int",
        "total_debt",
        "lt_borr",
        "st_borr",
        "n_cashflow_act",
        "capex",
        "free_cashflow",
        "depr_fa_coga_dpba",
        "div_paid_owners",
    ),
}


# 原始分红记录只留最近几条作例证（#332）：逐年每股分红、股息总额、支付率由 signals 给出
DIVIDEND_LLM_ROWS = 4


DIVIDEND_LLM_FIELDS = (
    "end_date",
    "ann_date",
    "div_proc",
    "cash_div_tax",
    "stk_div",
    "ex_date",
    "pay_date",
)


def _compact_dividend_history(rows: list) -> list:
    """A股 分红记录送模型前去重（#289/#265）：同一次分配的多条「实施」按 signals 同一定义
    （analysis_signals.implemented_dividends）只留一条——两处口径一致，模型看到的金额与
    signals.shareholder_returns 相同；已有实施的报告期，其预案/股东大会通过等过程行不再送
    （未实施的新预案保留）。"""
    from .analysis_signals import implemented_dividends

    implemented = implemented_dividends(rows)
    done_periods = {
        period
        for item in implemented
        for period in (item.get("source_end_dates") or [item["end_date"]])
    }
    kept = [item["row"] for item in implemented] + [
        row
        for row in rows
        if row.get("div_proc") != "实施" and str(row.get("end_date") or "") not in done_periods
    ]
    kept.sort(
        key=lambda row: (str(row.get("end_date") or ""), str(row.get("ann_date") or "")),
        reverse=True,
    )
    return [
        {field: row.get(field) for field in DIVIDEND_LLM_FIELDS if row.get(field) is not None}
        for row in kept[:DIVIDEND_LLM_ROWS]
    ]


def _compact_statement_rows(dataset: str, rows: list) -> list:
    if dataset == "dividend_history":
        return _compact_dividend_history(rows)
    # FCF 统一引用预计算值及其口径；原始来源的另一种定义留在档案中供核对。
    if dataset == "yahoo_fundamentals":
        rows = [
            {key: value for key, value in row.items() if key != "free_cashflow"} for row in rows
        ]
    fields = STATEMENT_LLM_FIELDS.get(dataset)
    if not fields:
        return rows
    if dataset == "report_statements":
        from .report_statement_checks import restated_fields, scrub_suspect_fields

        # 校验存疑的科目不送模型（已置空），但把"这一期被清洗过"说出来；被后续报告重列的
        # 科目保留首次披露值，只标注（用户口径：分析用原值）
        rows = [
            {
                **scrub_suspect_fields(row),
                "validation_status": (row.get("validation") or {}).get("status"),
                "restated_fields": restated_fields(row) or None,
                "repaired_fields": sorted(row.get("repaired_fields") or {}) or None,
            }
            for row in rows
        ]
    return [
        {
            field: row.get(field)
            for field in fields
            if field != "free_cashflow" and row.get(field) is not None
        }
        for row in rows
    ]


def _compact_profile(datasets: Dict[str, list]) -> Dict[str, list]:
    compacted = {
        dataset: _compact_statement_rows(dataset, rows) for dataset, rows in datasets.items()
    }
    if compacted.get("report_statements") and compacted.get("yahoo_fundamentals"):
        # 与 merge_hk_statement_rows 同一规则：PDF 科目优先，Yahoo 仅送同币种的空缺科目。
        # 两份整行同时进入 prompt 会让模型混用相互矛盾的 CFO/资本开支（#289/#388）。
        pdf_rows = {
            (row.get("end_date"), row.get("fp") or "FY"): row
            for row in compacted["report_statements"]
        }
        fallback_rows = []
        meta_fields = ("end_date", "fp", "currency")
        for row in compacted["yahoo_fundamentals"]:
            pdf = pdf_rows.get((row.get("end_date"), row.get("fp") or "FY"))
            if pdf is None:
                fallback_rows.append(row)
                continue
            if not pdf.get("currency") or pdf["currency"] != row.get("currency"):
                continue
            missing = {
                key: value
                for key, value in row.items()
                if key not in meta_fields and value is not None and pdf.get(key) is None
            }
            if missing:
                fallback_rows.append(
                    {**{key: row[key] for key in meta_fields if key in row}, **missing}
                )
        compacted["yahoo_fundamentals"] = fallback_rows
    if compacted.get("xueqiu_income") and compacted.get("income"):
        # 同期按 Tushare 优先只送一份（#332）；两源可能存在修订/精度差异，不假设数值相等
        tushare_periods = {str(row.get("end_date") or "") for row in compacted["income"]}
        compacted["xueqiu_income"] = [
            row
            for row in compacted["xueqiu_income"]
            if str(row.get("end_date") or "") not in tushare_periods
        ]
    return compacted


# 缺口清单进 LLM 输入的条数上限（超出部分以计数如实告知）
MAX_DIGEST_GAPS = 6
# 二级收缩的封顶：一级（档案减半 + 摘要压缩）仍超预算时才动 events/peers
EVENTS_SHRUNK_CAP = 8
PEERS_SHRUNK_CAP = 8
# 官方公告块（#306）：近 180 天的重要/一般级别组，最多 30 组；二级收缩截到 10 组
ANNOUNCEMENT_WINDOW_DAYS = 180
ANNOUNCEMENTS_CAP = 30
ANNOUNCEMENTS_SHRUNK_CAP = 10
ANNOUNCEMENT_SEMANTICS = (
    "；announcements=交易所官方公告(近 180 天，重要/一般级别，同日同类文件合并为一组，"
    "按公告日倒序；importance=major 重要/normal 一般)：title 为代表文件的标题原文、"
    "documents 为该组文件数——"
    "只能说明「发布了该公告」，不得据标题推测正文内容、金额或结论；"
    "公告日均早于或等于数据日，属已发生事项，不得写进「未来事件提醒」，"
    "除非标题本身写明了之后的日期"
)


def _payload_chars(payload: Dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, default=str))


AS_OF_SEMANTICS = (
    "；as_of_date=本次分析的数据日（业务时区），判断事件已发生/未发生的基准；"
    "events[].status=past（早于数据日，已发生）/upcoming（数据日当天及之后），"
    "days_from_as_of=事件日距数据日的天数（负数为已过去）"
)


def annotate_event_status(events: list, as_of) -> list:
    """输入副本上给事件标注时态（#288/#265：模型曾把 09-09 已发生的解禁写进「未来事件提醒」）。
    不改 load_security_events_for 的返回结构——详情页展示共用它。"""
    from datetime import date as _date

    annotated = []
    for event in events:
        try:
            event_day = _date.fromisoformat(str(event.get("event_date"))[:10])
        except ValueError:
            annotated.append(event)
            continue
        delta = (event_day - as_of).days
        annotated.append(
            {
                **event,
                "status": "upcoming" if delta >= 0 else "past",
                "days_from_as_of": delta,
            }
        )
    return annotated


def _build_signals(db, symbol: str, market: str, graham: Dict[str, Any]) -> Dict[str, Any]:
    """预计算信号（#265）：取数 → analysis_signals.signals_from_inputs。辅助信息失败只降级为
    status=error，分析照常进行——但**数据库错误要隔离**（PR #333 后评审）：PostgreSQL 语句出错后
    当前事务作废，只捕获 Python 异常的话，后面照样调 LLM、到落库时才失败白烧一次额度。
    取数放在 SAVEPOINT 里，普通 SQL 错误只回滚到保存点；连接失效等不可恢复的错误直接上抛，
    不再往下走到 LLM。"""
    from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError

    from .analysis_signals import signals_from_inputs
    from .security_profile_service import load_signal_inputs

    try:
        with db.begin_nested():
            inputs = load_signal_inputs(db, symbol, market)
    except SQLAlchemyError as exc:
        if isinstance(exc, OperationalError) or (
            isinstance(exc, DBAPIError) and exc.connection_invalidated
        ):
            raise
        logger.warning("预计算信号取数失败 %s/%s: %s", symbol, market, str(exc)[:200])
        return {"status": "error"}
    except Exception as exc:  # noqa: BLE001 - 非数据库异常：降级，不影响分析
        logger.warning("预计算信号取数失败 %s/%s: %s", symbol, market, str(exc)[:200])
        return {"status": "error"}
    try:
        return signals_from_inputs(market, inputs, graham)
    except Exception as exc:  # noqa: BLE001 - 纯计算异常不影响分析（不碰数据库）
        logger.warning("预计算信号计算失败 %s/%s: %s", symbol, market, str(exc)[:200])
        return {"status": "error"}


def build_analysis_input(
    db,
    symbol: str,
    market: str,
    *,
    digest_gaps: list | None = None,
    data_gaps: list | None = None,
    user_id: int | None = None,
) -> Dict[str, Any]:
    """压缩输入：档案数据（逐集封顶+报表科目白名单）+ 事件 + 财报摘要
    + 商业画像/同业 + 利润质量指标。

    超预算按辅助语境、非核心档案、摘要、报表期数的顺序逐级收缩，级间复测。
    各级删减记入缺口；全部收缩后仍超预算按现状送出并记 warning（极端标的如实上报，
    胜过静默截断让模型把缺口读成"没有问题"）。"""
    from .business_profile_service import load_business_profile
    from .earnings_quality import compute_earnings_quality, market_statements
    from .report_digest_service import load_report_digests, serialize_digest_for_analysis
    from .security_profile_service import compute_graham_for, load_annual_statement_datasets
    from . import announcement_service, announcement_sync

    from ..core.timeutil import local_today

    as_of = local_today()
    profile = load_symbol_profile(db, symbol, market, caps=ANALYSIS_CAPS)
    events = annotate_event_status(load_security_events_for(db, symbol, market), as_of)
    announcement_status = announcement_sync.sync_status(db, symbol, market)
    announcements = announcement_service.compact_for_analysis(
        announcement_service.load_groups(
            db,
            keys=[(symbol, market)],
            since=as_of - timedelta(days=ANNOUNCEMENT_WINDOW_DAYS),
            importance="normal",
            limit=ANNOUNCEMENTS_CAP,
        )
    )
    digests = serialize_digest_for_analysis(load_report_digests(db, symbol, market))
    business = load_business_profile(db, symbol, market, for_analysis=True)
    # 利润质量按年度行取数（与详情页同口径；caps 窗口混着季报，A股 只剩约 2 个年度）
    annual = load_annual_statement_datasets(db, symbol, market)
    statements = market_statements(market, annual if annual is not None else profile["datasets"])
    earnings_quality = compute_earnings_quality(
        statements["income"],
        statements["balancesheet"],
        statements["cashflow"],
        statements["fina_indicator"],
        market=market,
    )
    # 准则取数走年度行专取口径（caps 窗口的季报会把年度行挤到 2-3 个，
    # 十年准则失灵、分红记录截断成错误 fail——真实账本冒烟实锤）
    # user_id = 发起分析的用户：其 ADS_RATIO 规则覆盖 20-F 封面解析值（美股估值口径）
    graham = compute_graham_for(db, symbol, market, user_id=user_id) or {"status": "no_data"}
    signals = _build_signals(db, symbol, market, graham)
    # 两种定义的 FCF 并列曾导致模型挑错金额甚至正负号；存储/计算保持完整，LLM 只取主口径。
    if "shareholder_returns" in signals:
        signals = {
            **signals,
            "shareholder_returns": {
                **(signals.get("shareholder_returns") or {}),
                "by_year": [
                    {key: value for key, value in row.items() if key != "fcf_alternative"}
                    for row in (signals.get("shareholder_returns") or {}).get("by_year", [])
                ],
            },
        }
    common_semantics = (
        "report_digests=财报关键章节的 AI 摘要(按报告期倒序，旧年份为压缩版；"
        "属公司自述口径，可引用)；business_profile=商业画像(商业模式/分部/上下游/"
        "估值因子)；peers=同行业名单(仅供提及可比公司，禁止对同业展开分析——"
        "同业数据不在输入中)；earnings_quality=预计算利润质量指标"
        "(红旗阈值见 metric_semantics)；graham_screen=预计算格雷厄姆防御型"
        "准则与脆弱性信号(逐项判定与依据见 criteria_semantics/"
        "fragility_semantics，禁止自行心算比率；正文用 criteria[].name_zh 与 "
        "verdict_zh 的中文说法)"
    )
    market_semantics = {
        "A股": (
            "fina_indicator=财务指标(按报告期，只送分析用到的字段；毛利率看 grossprofit_margin，"
            "单位 %)；forecast=业绩预告；express=业绩快报；"
            "daily_basic=最新估值快照(pe/pb/股息率)；dividend_history=最近几条分红送股记录"
            "(仅作例证：div_proc=实施为已落地，同一次分配已去重；cash_div_tax 为每股税前现金；"
            "逐年每股分红、股息总额与支付率以 signals.shareholder_returns 为准)；"
            "xueqiu_income=雪球利润表核心科目(只含 income 块没有的年度；同一期按 Tushare income 优先，"
            "单位元；net_profit_atsopc=归母净利润、net_profit_after_nrgal_atsolc=扣非归母净利润)；fina_audit=审计意见；pledge_stat=股权质押统计；"
            "stk_holdertrade=重要股东增减持；income/balancesheet/cashflow=三大报表"
            "核心科目(合并报表口径，单位元)；events=财报披露/分红预案/解禁事件；"
        ),
        "美股": (
            "edgar_companyfacts=SEC XBRL 年度(FY)/季度核心科目(金额单位为行内 currency 字段"
            "=发行人的报告币种：美国本土发行人为美元，中概 20-F 发行人多为人民币；"
            "basic_eps/diluted_eps 为每股普通股而非每股 ADS；"
            "科目缺失=该公司未按对应 us-gaap 概念披露——中概股常不披露贸易应收"
            "与存货，对应指标留空属正常)；report_digests 来自年报 10-K 或 20-F"
            "(外国私人发行人)；本市场无审计意见/质押/增减持/分红预案事件数据源；"
        ),
        "港股": (
            "report_statements=披露易年报/中报 PDF 原文抽取的三张报表核心科目(官方一手，"
            "**可达十年**；fp=FY 年度、fp=H1 中报——中报只送最新一期及其比较列；"
            "is_comparative=true 是下一期报告的比较列而非本期权威行；"
            "金额与每股盈利的币种见行内 currency 字段——公司可能中途更换报告币种(如 USD→HKD、"
            "HKD→CNY，切换年见 earnings_quality.currency_changes)，不同币种年份的金额不得直接"
            "比较或计算增速，跨年结论以预计算指标为准；"
            "validation_status=suspect 表示该期科目校验存疑、存疑科目已置空由雅虎补缺，"
            "对应期见 profile_data_gaps)；yahoo_fundamentals=雅虎年度核心科目(报告币种见"
            "行内 currency 字段，公司间不一致；非官方接口、仅近 3-5 年，只作补缺——同一期两源"
            "数值不一致时(如资本开支、自由现金流)以 report_statements 为准，不得混用两源)；"
            "report_digests=披露易年报全文的 AI 摘要；本市场无审计意见/质押/增减持/解禁"
            "数据源，风险依据为输入中各期摘要与预计算指标；某期风险字段缺失或写原文未提及，"
            "只代表所提供节选的覆盖不足，不证明年报没有风险章节或公司没有风险；"
        ),
    }
    payload = {
        "meta": {
            "symbol": symbol,
            "market": market,
            "as_of_date": as_of.isoformat(),
            "data_semantics": (
                market_semantics.get(market, "")
                + common_semantics
                + AS_OF_SEMANTICS
                + ANNOUNCEMENT_SEMANTICS
                + "；"
                + SIGNALS_SEMANTICS
            ),
        },
        "profile": _compact_profile(profile["datasets"]),
        "events": events,
        "announcements": announcements,
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
        "graham_screen": graham_for_llm(graham),
        "signals": signals,
    }
    if digest_gaps is None:
        from .report_digest_service import digest_gap_preview

        digest_gaps = digest_gap_preview(db, symbol, market)
    if digest_gaps:
        # 截断本身也要可见：只留 6 条而不说"还有几条"，模型会把这 6 条当成
        # 缺口全集，把"没列出来的年份"读成"没有问题的年份"
        payload["report_digest_gaps"] = list(digest_gaps[:MAX_DIGEST_GAPS])
        if len(digest_gaps) > MAX_DIGEST_GAPS:
            payload["report_digest_gaps"].append(
                f"（另有 {len(digest_gaps) - MAX_DIGEST_GAPS} 条摘要缺口未列出）"
            )
    if announcement_status["status"] != "synced":
        # 未同步与「近 180 天无公告」必须分得开，否则空列表会被读成「公司没有重大事项」
        reason = (
            "官方公告尚未同步"
            if announcement_status["status"] == "pending"
            else f"无官方公告源（{announcement_status['reason']}）"
        )
        announcement_gaps = [f"{reason}：announcements 为空不代表没有公告"]
    else:
        announcement_gaps = []
    if not business.get("profile"):
        announcement_gaps.append("商业画像缺失或其版本/输入已过期，本次未引用，不能视为公司未披露")
    suspect_periods = sorted(
        {
            f"{row.get('end_date')}|{row.get('fp') or 'FY'}"
            for row in profile["datasets"].get("report_statements") or []
            if (row.get("validation") or {}).get("status") == "suspect"
        },
        reverse=True,
    )
    if suspect_periods:
        # 校验存疑的会计期：存疑科目已置空由雅虎补缺（_compact_statement_rows），但"这些期
        # 被清洗过"本身要进缺口，否则模型会把空值读成"公司没披露"
        data_gaps = list(data_gaps or []) + [
            "港股报表科目校验存疑（存疑科目已置空，雅虎能补的已补）: "
            + "、".join(suspect_periods[:6])
            + (f"（另 {len(suspect_periods) - 6} 期）" if len(suspect_periods) > 6 else "")
        ]
    if data_gaps:
        # 数据集本次未取到（接口冷却/同步失败）。必须显式告知模型，否则"没数据"
        # 会被当成"没有质押/无风险信号"——把限流伪装成利好，比整体失败更危险。
        payload["profile_data_gaps"] = data_gaps[:8]
    if announcement_gaps:
        # 不计入上面的 8 条上限：Tushare 限流时数据集缺口常 ≥8 条，公告这条被截掉后模型会把
        # 空的 announcements 读成「近 180 天没有公告」（PR #311 评审 P3-1）
        payload["profile_data_gaps"] = payload.get("profile_data_gaps", []) + announcement_gaps
    return shrink_analysis_payload(db, symbol, market, payload)


# 超预算时按顺序截断，价值越低越先动（#332）；每一级截掉什么写进 profile_data_gaps，
# 触发到哪一级记在 meta.input_shrink（上线后监控用）
SHRINK_GAP_NOTES = {
    "aux": "输入超长：事件、公告、同业名单已截短（事件前 8 条、公告前 10 组、同业前 8 家）",
    "profile_noncore": "输入超长：业绩预告/快报、审计、质押、增减持、股东、资金流等只保留最近一半的记录",
    "digests": "输入超长：各期财报摘要只保留核心字段（主营收入结构、一次性项目、会计信号、关键数字）",
    "statements": "输入超长：报表与财务指标只保留最近一半的报告期",
}


def shrink_analysis_payload(
    db, symbol: str, market: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """超预算收缩（#332）：先砍辅助语境，再砍非核心档案，再压缩摘要，最后才减报表期数。
    此前的顺序相反——一级收缩就把报表减半、摘要压缩，噪音数据集原样保留。"""
    from .report_digest_service import load_report_digests, serialize_digest_for_analysis

    levels: List[str] = []

    def over() -> bool:
        return _payload_chars(payload) > CHAR_BUDGET

    if over():
        payload["events"] = (payload.get("events") or [])[:EVENTS_SHRUNK_CAP]
        payload["announcements"] = (payload.get("announcements") or [])[:ANNOUNCEMENTS_SHRUNK_CAP]
        payload["peers"]["list"] = (payload["peers"].get("list") or [])[:PEERS_SHRUNK_CAP]
        levels.append("aux")
    if over():
        profile = load_symbol_profile(db, symbol, market, caps=NONCORE_SHRUNK_CAPS)
        payload["profile"] = _compact_profile(profile["datasets"])
        levels.append("profile_noncore")
    if over():
        payload["report_digests"] = serialize_digest_for_analysis(
            load_report_digests(db, symbol, market), compact_older_than_years=0
        )
        levels.append("digests")
    if over():
        profile = load_symbol_profile(db, symbol, market, caps=SHRUNK_CAPS)
        payload["profile"] = _compact_profile(profile["datasets"])
        levels.append("statements")
    payload["meta"]["input_shrink"] = levels
    if levels:
        payload["profile_data_gaps"] = list(payload.get("profile_data_gaps") or []) + [
            SHRINK_GAP_NOTES[level] for level in levels
        ]
    final_chars = _payload_chars(payload)
    if final_chars > CHAR_BUDGET:
        logger.warning(
            "分析输入逐级收缩后仍超预算 %s/%s: %d > %d，按现状送出",
            symbol,
            market,
            final_chars,
            CHAR_BUDGET,
        )
    return payload


def start_security_analysis_job(user_id: int, symbol: str, market: str) -> Dict[str, Any]:
    job = create_or_get_active_job(
        JOB_TYPE,
        user_id,
        {
            "symbol": symbol,
            "market": market,
            "analysis_id": None,
            # 进度字段（_serialize 展平到响应顶层，前端立即可读）
            "stage": None,
            "stage_label": "排队中",
            "total": STAGE_TOTAL,
            "completed": 0,
            "progress_percent": 0,
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


def _ensure_ads_ratio_gap(db, symbol: str, market: str) -> list:
    """美股：顺带确保最新年报（20-F / 10-K）封面的 ADS 换算比已解析（缓存命中零下载）；
    失败不阻断分析，只进缺口（估值两项会如实 indeterminate）。"""
    if market != "美股":
        return []
    try:
        from .ads_ratio_service import ensure_ads_ratio

        outcome = ensure_ads_ratio(db, symbol)
    except Exception as exc:  # 网络/EDGAR 异常：不影响主分析
        db.rollback()
        logger.warning("ADS 换算比解析失败 %s: %s", symbol, str(exc)[:150])
        return ["ADS 换算比获取失败（年报封面），美股估值口径可能不可用"]
    if outcome.get("status") == "cover_unknown":
        return ["ADS 换算比无法确定（年报封面未识别出证券登记表），美股估值两项不可确定"]
    if outcome.get("status") in ("failed", "capped"):
        return ["ADS 换算比获取失败（年报封面），美股估值口径可能不可用"]
    return []


def analyze_one(
    db,
    symbol: str,
    market: str,
    *,
    digest_max_new: int = 2,
    on_stage: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    user_id: Optional[int] = None,
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
        except JobOwnershipLostError:
            # 失权不是"进度回写失败"而是"立刻停手"信号，必须穿透这层兜底：
            # 被吞掉的话，僵尸线程会跑完剩余阶段并 commit 第二份 SecurityAnalysis，
            # 与接管者重复调用 Tushare/EDGAR/LLM。
            raise
        except Exception as exc:  # 进度回写失败不能拖垮分析
            logger.warning("分析进度回写失败 %s/%s: %s", symbol, market, str(exc)[:150])

    def failure(error: str, kind: str) -> Dict[str, Any]:
        return {
            "symbol": symbol,
            "market": market,
            "status": "failed",
            "analysis_id": None,
            "error": error,
            "error_kind": kind,
            "degraded": [],
            "digest_gaps": [],
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
        return failure(f"数据源致命错误（{fatal['dataset']}）：{fatal['error']}", "tushare_fatal")
    degraded = (
        [f"{item['dataset']} 数据集本次获取失败" for item in sync_result.get("failed", [])]
        + _ensure_ads_ratio_gap(db, symbol, market)
        + [
            f"{item['dataset']} 数据集因数据源频率限制本次跳过"
            f"（约 {item.get('retry_after_seconds')}s 后可重试）"
            for item in sync_result.get("skipped", [])
        ]
    )

    # 2/6 财报摘要惰性保底：任何失败不阻断主分析，缺口进输入
    stage("report_digests", completed=1)
    digest_gaps: list = []
    if digest_max_new > 0:
        try:
            from .report_digest_service import ensure_report_digests

            digest_result = ensure_report_digests(db, symbol, market, max_new=digest_max_new)
            digest_gaps = digest_result.get("gaps", [])
        except JobOwnershipLostError:
            raise
        except Exception as exc:
            logger.warning("报告摘要保底失败 %s/%s: %s", symbol, market, str(exc)[:150])
            digest_gaps = ["报告摘要管线异常，本次分析未包含财报摘要"]
        # 港股结构化科目保底：年报/中报 PDF 三张表抽取（同一成本护栏；缺口一并进输入）。
        # 先再报一次进度续租：摘要 + 报表最多 4 次 LLM 调用（各约 30-60s）加下载，
        # 单个阶段可能逼近 300s 租约，不续租会被 worker 当成僵尸接管重跑
        try:
            from .report_statement_service import STATEMENT_MARKETS, ensure_report_statements

            if market in STATEMENT_MARKETS:
                stage("report_digests", completed=1)
                statement_result = ensure_report_statements(
                    db, symbol, market, max_new=digest_max_new
                )
                digest_gaps = digest_gaps + [
                    f"[报表抽取] {gap}" for gap in statement_result.get("gaps", [])
                ]
        except JobOwnershipLostError:
            raise
        except Exception as exc:
            logger.warning("报表抽取保底失败 %s/%s: %s", symbol, market, str(exc)[:150])
            digest_gaps = digest_gaps + ["[报表抽取] 管线异常，本次分析未包含 PDF 报表科目"]
    else:
        # 快速模式不补摘要，但缺口照样要让模型知道（只读库、不外呼，#289）
        try:
            from .report_digest_service import digest_gap_preview

            digest_gaps = digest_gap_preview(db, symbol, market)
        except Exception as exc:
            logger.warning("摘要缺口预览失败 %s/%s: %s", symbol, market, str(exc)[:150])
            digest_gaps = ["财报摘要的覆盖范围未能核对（本次为快速分析，未补齐摘要）"]

    # 3/6 商业画像与同业名单顺带刷新（内部吞错，失败降级为缓存/空）
    stage("business_profile", completed=2)
    try:
        from .business_profile_service import ensure_business_profile, ensure_peer_list

        ensure_peer_list(db, symbol, market)
        ensure_business_profile(db, symbol, market)
    except JobOwnershipLostError:
        raise
    except Exception as exc:
        logger.warning("商业画像刷新失败 %s/%s: %s", symbol, market, str(exc)[:150])

    # 4/6 组装输入
    stage("build_input", completed=3)
    input_payload = build_analysis_input(
        db,
        symbol,
        market,
        digest_gaps=digest_gaps,
        data_gaps=degraded,
        user_id=user_id,
    )

    # 5/6 生成（LLM）
    stage("llm_analysis", completed=4)
    messages = build_analysis_messages(input_payload)
    try:
        for attempt in range(1 + INCOMPLETE_REPORT_RETRIES):
            completion = chat_completion(
                messages,
                # 单独的输出额度：港股十年 PDF 报表行 + 摘要的输入让 JSON 结构化产物 + 全文
                # 报告在 16384（复盘默认）里被截断（00799）
                max_tokens=settings.security_analysis_max_output_tokens,
                response_format={"type": "json_object"},
            )
            try:
                parsed = parse_analysis_output(
                    completion["content"],
                    market=market,
                    graham_screen=input_payload.get("graham_screen"),
                )
                break
            except IncompleteReportError as exc:
                # 半截报告（约束解码在正文英文引号处提前收尾）是随机的：同一输入重试一次，
                # 仍不完整才记失败（#287）
                if attempt >= INCOMPLETE_REPORT_RETRIES:
                    return failure(f"LLM 输出不完整：{exc}", "incomplete")
                logger.warning("分析报告不完整，重试 %s/%s: %s", symbol, market, exc)
                stage("llm_analysis", completed=4)
    except LLMNotConfiguredError as exc:
        return failure(str(exc), "llm_not_configured")
    except ValueError as exc:  # 输出解析失败：确定性失败不烧重试
        return failure(f"LLM 输出解析失败：{exc}", "parse")
    except LLMClientError as exc:
        if is_output_truncated(exc):
            # 输出额度耗尽（空或半截）：同样的输入重试结果相同，确定性失败不烧重试；
            # 不是整批等价（输入长短因标的而异），批量继续下一只
            return failure(str(exc), "truncated")
        if exc.status_code in LLM_FATAL_STATUS_CODES:
            # 鉴权/欠费/限流：换个标的重试同样会失败，批量必须立即中止
            return failure(str(exc), "llm_auth")
        if exc.status_code is not None and 400 <= exc.status_code < 500:
            return failure(str(exc), "llm_4xx")
        raise  # 5xx/超时 → 由调用方退避重试

    # 标签兜底（#265）：与预计算信号明确矛盾的标签丢掉（只丢标签、不拒绝整份）
    from .analysis_signals import validate_tags

    tags, signal_adjustments = validate_tags(
        parsed["tags"], input_payload.get("signals"), input_payload.get("graham_screen")
    )
    if signal_adjustments:
        parsed = {
            **parsed,
            "tags": tags,
            "adjustments": list(parsed.get("adjustments") or []) + signal_adjustments,
        }

    # 6/6 落库。全局产物不得读取任何用户的持仓字段（用户手工录入的名称会经此
    # 泄露给全部用户，且多用户不同名导致结果不确定）——名称只从公共证券元数据
    # 解析，失败即留空由前端回退代码
    stage("persist", completed=5)
    usage = completion.get("usage", {})
    if "generation_meta" in completion:
        input_payload["generation_meta"] = completion["generation_meta"]
    analysis = SecurityAnalysis(
        symbol=symbol,
        market=market,
        name=resolve_public_security_name(symbol, market),
        tags=parsed["tags"],
        risk_level=parsed["risk_level"],
        risk_level_adjusted=parsed.get("risk_level_adjusted"),
        output_adjustments=parsed.get("adjustments") or None,
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
        "symbol": symbol,
        "market": market,
        "status": "succeeded",
        "analysis_id": analysis.id,
        "error": None,
        "error_kind": None,
        "degraded": degraded,
        "digest_gaps": digest_gaps,
    }


def execute_security_analysis_job(claimed: Dict[str, Any]) -> None:
    job_id = claimed["id"]
    attempt = claimed.get("attempt_count")
    symbol = claimed["data"]["symbol"]
    market = claimed["data"]["market"]

    progress = make_batch_progress(job_id, JOB_TYPE, attempt)

    def report(stage_name: str, extra: Dict[str, Any]) -> None:
        """阶段进度回写；失权即中断本次分析（analyze_one 的 stage 包装会放行）。"""
        progress(
            stage=stage_name,
            stage_label=ANALYSIS_STAGE_LABELS.get(stage_name, stage_name),
            total=STAGE_TOTAL,
            **extra,
        )

    db = SessionLocal()
    try:
        # 心跳兜底：单次 LLM 调用或大年报解析本身就可能吃掉整个租约
        with job_heartbeat(job_id, JOB_TYPE, attempt_count=attempt):
            outcome = analyze_one(
                db, symbol, market, on_stage=report, user_id=claimed.get("user_id")
            )
        if outcome["status"] == "failed":
            set_job_progress(
                job_id,
                JOB_TYPE,
                required_attempt_count=attempt,
                status="failed",
                error=outcome["error"],
            )
            return
        set_job_progress(
            job_id,
            JOB_TYPE,
            required_attempt_count=attempt,
            status="succeeded",
            stage="done",
            stage_label="已完成",
            completed=STAGE_TOTAL,
            total=STAGE_TOTAL,
            analysis_id=outcome["analysis_id"],
            degraded=outcome["degraded"],
        )
    except JobOwnershipLostError:
        # 安静退出：接管者正在跑同一个 job，这不是失败。
        logger.warning(
            "标的分析 job %s 已被接管或进入终态，本次执行停止",
            job_id,
        )
    finally:
        db.close()


def run_security_analysis_job(job_id: str) -> None:
    run_job_inline(
        job_id, JOB_TYPE, execute_security_analysis_job, label="Security analysis", logger=logger
    )


def get_security_analysis_job(job_id: str, user_id: int) -> Optional[Dict[str, Any]]:
    return get_job(job_id, JOB_TYPE, user_id)


register_runner(JOB_TYPE, execute_security_analysis_job)
