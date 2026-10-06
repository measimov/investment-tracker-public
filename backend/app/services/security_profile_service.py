"""标的基本面数据同步（按市场路由）与档案读取。

- A股：Tushare 十一个数据集（2026-08-02 真实 token 逐一实测可用）
- 美股：SEC EDGAR companyfacts（官方 XBRL，科目兜底链透视为每期一行）
- 港股：Yahoo fundamentals-timeseries（PR-4 接入）

"合规污点"按拍板降级为客观风险信号；美股无审计/质押/增减持数据源，
风险信号由 10-K Risk Factors 摘要替代（capabilities 自述）。存储为通用
JSON 行（security_profile_data），按 (symbol, market, dataset, period_key)
原子 upsert。
"""

import time
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import literal_column
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from ..core.logging import get_app_logger
from ..core.timeutil import to_local_date
from ..models.security_profile import SecurityProfileData
from .edgar_facts import EDGAR_ANNUAL_KEEP, EDGAR_QUARTERLY_KEEP, fetch_edgar_companyfacts
from .stock_price_service import (
    TushareEmptyResult,
    classify_tushare_error,
    to_tushare_a_code,
    tushare_cooldown_remaining,
    tushare_query,
)

logger = get_app_logger(__name__)

SUPPORTED_MARKETS = ("A股", "美股", "港股")

# 详情页/分析按市场的能力位（前端条件渲染 + prompt 分支依据）
MARKET_CAPABILITIES: Dict[str, Dict[str, Any]] = {
    "A股": {"structured": True, "report_digest": True, "risk_signals": True},
    "美股": {"structured": True, "report_digest": True, "risk_signals": "risk_factors"},
    # 港股：年报/中报 PDF 三张表抽取（report_statements，官方一手、可达十年）+ Yahoo 年度
    # 科目补缺 + 披露易年报全文摘要；无审计意见/质押/增减持数据源，风险信号只能来自
    # 年报「主要風險」章节
    "港股": {
        "structured": True,
        "report_digest": True,
        "risk_signals": "risk_factors",
        "statements": "report_pdf",
    },
}

# dataset → (Tushare 接口, 额外参数, 自然键构造)。period_key 必须稳定：
# 同一行重同步得到同一键（幂等 upsert 判据）。
_KeyFn = Callable[[Dict[str, Any]], Optional[str]]


def _key_of(*fields: str) -> _KeyFn:
    def build(row: Dict[str, Any]) -> Optional[str]:
        parts = [str(row.get(field) or "") for field in fields]
        if not any(parts):
            return None
        return "|".join(parts)[:40]

    return build


def _merged_statement_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """三大报表行预处理：只留合并报表（report_type=1），同一报告期取最新披露。

    同一 end_date 会有多次披露/修正行（update_flag/ann_date 不同）；按
    (f_ann_date/ann_date, update_flag) 倒序排，配合 upsert 的"同批首见者
    胜"去重，落库的即最新修正版。
    """
    merged = [row for row in rows if str(row.get("report_type") or "1") == "1"]
    return sorted(
        merged,
        key=lambda row: (
            str(row.get("end_date") or ""),
            str(row.get("f_ann_date") or row.get("ann_date") or ""),
            str(row.get("update_flag") or ""),
        ),
        reverse=True,
    )


DATASETS: Dict[str, Dict[str, Any]] = {
    "fina_indicator": {"api": "fina_indicator", "params": {}, "key": _key_of("end_date")},
    "forecast": {"api": "forecast", "params": {}, "key": _key_of("end_date", "ann_date")},
    "express": {"api": "express", "params": {}, "key": _key_of("end_date")},
    "daily_basic": {"api": "daily_basic", "params": {}, "key": _key_of("trade_date")},
    "dividend_history": {
        "api": "dividend",
        "params": {},
        "key": _key_of("end_date", "div_proc", "ann_date"),
    },
    "fina_audit": {"api": "fina_audit", "params": {}, "key": _key_of("end_date", "ann_date")},
    "pledge_stat": {"api": "pledge_stat", "params": {}, "key": _key_of("end_date")},
    "stk_holdertrade": {
        "api": "stk_holdertrade",
        "params": {},
        "key": _key_of("ann_date", "holder_name", "in_de"),
    },
    # 三大报表（合并报表口径，同报告期取最新修正；2026-08-02 真实 token 实测可用）
    "income": {
        "api": "income",
        "params": {},
        "key": _key_of("end_date"),
        "prepare": _merged_statement_rows,
    },
    "balancesheet": {
        "api": "balancesheet",
        "params": {},
        "key": _key_of("end_date"),
        "prepare": _merged_statement_rows,
    },
    "cashflow": {
        "api": "cashflow",
        "params": {},
        "key": _key_of("end_date"),
        "prepare": _merged_statement_rows,
    },
}


def _fetch_yahoo_fundamentals(symbol: str, market: str) -> List[Dict[str, Any]]:
    from .report_fetchers import yahoo_hk_fundamentals

    return yahoo_hk_fundamentals(symbol)


def _fetch_xueqiu_income(symbol: str, market: str) -> List[Dict[str, Any]]:
    from .xueqiu_source import fetch_income_rows

    return fetch_income_rows(symbol, market)


def _fetch_xueqiu_capital_flow(symbol: str, market: str) -> List[Dict[str, Any]]:
    from .xueqiu_source import fetch_capital_flow_rows

    return fetch_capital_flow_rows(symbol, market)


def _fetch_xueqiu_holders(symbol: str, market: str) -> List[Dict[str, Any]]:
    from .xueqiu_source import fetch_holder_rows

    return fetch_holder_rows(symbol, market)


# 市场 → 数据集注册表；DATASETS 保留为合并视图（向后兼容测试/键查找）
MARKET_DATASETS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "A股": {
        **DATASETS,
        # 雪球（需登录态 Cookie；未配置时逐集失败并如实记入 failed，不影响
        # Tushare 的十一个数据集）。period_key 由库的 adapter 填好：
        # 利润表=报告期 end_date，资金流=交易日。
        "xueqiu_income": {"fetch": _fetch_xueqiu_income, "key": _key_of("period_key")},
        "xueqiu_capital_flow": {
            "fetch": _fetch_xueqiu_capital_flow,
            "key": _key_of("period_key"),
        },
        # 十大流通股东：period_key = 报告期|名次（wrapper 构造），每期十行
        "xueqiu_holders": {"fetch": _fetch_xueqiu_holders, "key": _key_of("period_key")},
    },
    "美股": {
        "edgar_companyfacts": {
            "fetch": fetch_edgar_companyfacts,
            "key": _key_of("end_date", "fp"),
        },
    },
    "港股": {
        "yahoo_fundamentals": {
            "fetch": _fetch_yahoo_fundamentals,
            "key": _key_of("end_date"),
        },
    },
}

# daily_basic 是日度估值快照：只保留最近 N 行，避免逐日膨胀
DAILY_BASIC_KEEP_ROWS = 30


# 任务驱动的数据集：不在 sync_symbol_profile 里拉取（由 job 写入），但随档案一起加载。
# 港股 report_statements = 年报/中报 PDF 抽取的科目行（report_statement_service）
MARKET_JOB_DATASETS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "港股": {"report_statements": {"key": _key_of("end_date", "fp")}},
}


def _job_dataset_current(dataset: str):
    """任务驱动数据集的行有效性谓词（旧版本行在重算成功前不参与读取）。"""
    if dataset == "report_statements":
        from .report_statement_prompts import statement_row_current

        return statement_row_current
    return lambda payload: True


def _dataset_spec(market: str, dataset: str) -> Dict[str, Any]:
    spec = (MARKET_DATASETS.get(market) or {}).get(dataset)
    if spec:
        return spec
    job_spec = (MARKET_JOB_DATASETS.get(market) or {}).get(dataset)
    if job_spec:
        return job_spec
    # 不再跨市场兜底（#282）：市场与数据集不匹配时静默拿别的市场的 spec，读写键会错位
    raise KeyError(f"{market}/{dataset}")


def normalize_row(raw: Dict[str, Any]) -> Dict[str, Any]:
    """DataFrame 行 → JSON 安全 dict（NaN→None，numpy 标量→原生类型）。"""
    normalized: Dict[str, Any] = {}
    for key, value in raw.items():
        if value is None or value != value:  # NaN 自身不等
            normalized[key] = None
        elif isinstance(value, (int, float, str, bool)):
            normalized[key] = value
        else:
            item = getattr(value, "item", None)
            normalized[key] = item() if callable(item) else str(value)
    return normalized


def fetch_dataset_rows(dataset: str, symbol: str, market: str) -> List[Dict[str, Any]]:
    """拉取单数据集全部行（测试 monkeypatch 本函数；空数据归一为空列表）。

    按市场分发：spec 带 "fetch" 走自定义客户端（EDGAR/Yahoo），否则走
    Tushare 默认路径（A股）。
    """
    spec = _dataset_spec(market, dataset)
    custom_fetch = spec.get("fetch")
    if custom_fetch is not None:
        return custom_fetch(symbol, market)
    try:
        df = tushare_query(spec["api"], ts_code=to_tushare_a_code(symbol), **spec["params"])
    except TushareEmptyResult:
        return []
    rows = [normalize_row(row) for row in df.to_dict("records")]
    prepare = spec.get("prepare")
    return prepare(rows) if prepare else rows


def upsert_profile_rows(
    db: Session, symbol: str, market: str, dataset: str, rows: List[Dict[str, Any]]
) -> int:
    """原子 upsert（ON CONFLICT DO UPDATE）；返回新增行数（xmax=0 判定）。"""
    spec = _dataset_spec(market, dataset)
    values = []
    seen_keys: set[str] = set()
    for row in rows:
        period_key = spec["key"](row)
        if not period_key or period_key in seen_keys:
            continue  # 无自然键或同批重复：跳过（如 pledge_stat 罕见重复行）
        seen_keys.add(period_key)
        values.append(
            {
                "symbol": symbol,
                "market": market,
                "dataset": dataset,
                "period_key": period_key,
                "payload": row,
            }
        )
    if not values:
        return 0
    stmt = pg_insert(SecurityProfileData).values(values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_security_profile_identity",
        set_={"payload": stmt.excluded.payload, "fetched_at": func.now()},
    ).returning(literal_column("(xmax = 0)").label("inserted"))
    inserted = sum(1 for flag in db.execute(stmt).scalars() if flag)
    return inserted


def prune_daily_basic(db: Session, symbol: str, market: str) -> None:
    keep_ids = [
        row[0]
        for row in db.query(SecurityProfileData.id)
        .filter(
            SecurityProfileData.symbol == symbol,
            SecurityProfileData.market == market,
            SecurityProfileData.dataset == "daily_basic",
        )
        .order_by(SecurityProfileData.period_key.desc())
        .limit(DAILY_BASIC_KEEP_ROWS)
        .all()
    ]
    if keep_ids:
        db.query(SecurityProfileData).filter(
            SecurityProfileData.symbol == symbol,
            SecurityProfileData.market == market,
            SecurityProfileData.dataset == "daily_basic",
            SecurityProfileData.id.notin_(keep_ids),
        ).delete(synchronize_session=False)


def _prune_stale_edgar_rows(
    db: Session, symbol: str, market: str, dataset: str, rows: List[Dict[str, Any]]
) -> None:
    """删掉本次透视已不再产出的旧行。upsert 只覆盖本次产出的期间键，两类旧行会残留：

    - **别币种的行**：报告币种改按 CNY 取数后，只在旧 USD 透视里出现过的期间以 USD 行残留，
      与新的 CNY 行混在同一数据集里——跨行的增速/趋势会把两种币种当成同一序列；
    - **本次窗口内不再产出的行**（EDGAR v4，#351）：季度占位行（10-Q 比较资产负债表，只有时点
      事实）不再生成，但库里的旧占位行仍会占着季度额度。按年度/季度分类，期末不早于本次该类
      最早一行、却不在本次产出里的旧行一律删除；比本次窗口更早的历史行保留（upsert 从不删，
      那是更早同步留下的深历史）。窗口外的旧占位行（`edgar_facts.is_quarter_placeholder`：
      季度行却没有任何期间科目）同样删除——季度真实行少的发行人窗口很窄，占位行可能落在窗口前。

    本次无产出（空 fetch）时不动。"""
    from .edgar_facts import is_quarter_placeholder

    currencies = {row.get("currency") for row in rows}
    if not currencies:
        return

    def _class(fp: Any) -> str:
        return "FY" if fp == "FY" else "interim"

    produced = {(str(row.get("end_date")), str(row.get("fp"))) for row in rows}
    window_start: Dict[str, str] = {}
    for end_date, fp in produced:
        klass = _class(fp)
        window_start[klass] = min(window_start.get(klass, end_date), end_date)

    def _stale(payload: Dict[str, Any]) -> bool:
        if payload.get("currency") not in currencies:
            return True
        key = (str(payload.get("end_date")), str(payload.get("fp")))
        if key in produced:
            return False
        if is_quarter_placeholder(payload):
            return True
        start = window_start.get(_class(payload.get("fp")))
        return start is not None and key[0] >= start

    stale_ids = [
        row_id
        for row_id, payload in db.query(SecurityProfileData.id, SecurityProfileData.payload)
        .filter(
            SecurityProfileData.symbol == symbol,
            SecurityProfileData.market == market,
            SecurityProfileData.dataset == dataset,
        )
        .all()
        if _stale(payload or {})
    ]
    if stale_ids:
        db.query(SecurityProfileData).filter(SecurityProfileData.id.in_(stale_ids)).delete(
            synchronize_session=False
        )
        logger.info("%s/%s %s 删除 %d 行过期透视行", symbol, market, dataset, len(stale_ids))


# 接口冷却的分档阈值：不超过这个时长就地等一下，超过则跳过该数据集。
# 等待发生在这里（锁外），绝不能塞进 wait_for_tushare_rate_limit 的临界区。
TUSHARE_COOLDOWN_INLINE_WAIT_SECONDS = 20.0


def sync_symbol_profile(db: Session, symbol: str, market: str) -> Dict[str, Any]:
    """单标的全数据集同步；单数据集失败记录不中断（配额错误会逐集快速失败）。

    接口处于频率冷却中时：短冷却就地等待后照常同步，长冷却把该数据集记入
    `skipped` 并继续其余数据集——批量分析里一个受限接口不该毁掉整只标的。
    """
    if market not in SUPPORTED_MARKETS:
        return {
            "symbol": symbol,
            "market": market,
            "supported": False,
            "datasets": {},
            "failed": [],
            "skipped": [],
        }
    result: Dict[str, Any] = {
        "symbol": symbol,
        "market": market,
        "supported": True,
        "datasets": {},
        "failed": [],
        "skipped": [],
    }
    for dataset in MARKET_DATASETS.get(market, {}):
        api_name = _dataset_spec(market, dataset).get("api")
        if api_name:
            remaining = tushare_cooldown_remaining(api_name)
            if 0 < remaining <= TUSHARE_COOLDOWN_INLINE_WAIT_SECONDS:
                time.sleep(remaining)
            elif remaining > 0:
                logger.info(
                    "接口 %s 冷却中（剩余 %.0fs），本次跳过数据集 %s",
                    api_name,
                    remaining,
                    dataset,
                )
                result["skipped"].append(
                    {
                        "dataset": dataset,
                        "reason": "rate_cooldown",
                        "retry_after_seconds": round(remaining),
                    }
                )
                continue
        try:
            rows = fetch_dataset_rows(dataset, symbol, market)
            inserted = upsert_profile_rows(db, symbol, market, dataset, rows)
            if dataset == "daily_basic":
                prune_daily_basic(db, symbol, market)
            if dataset == "edgar_companyfacts":
                _prune_stale_edgar_rows(db, symbol, market, dataset, rows)
            db.commit()
            result["datasets"][dataset] = {"rows": len(rows), "inserted": inserted}
        except Exception as exc:  # 单数据集失败不中断
            db.rollback()
            logger.warning("同步 %s %s/%s 失败: %s", dataset, symbol, market, exc)
            result["failed"].append({"dataset": dataset, "error": str(exc)[:200]})
            # token 失效/无权限对所有 Tushare 数据集等价：继续逐集重试毫无意义，
            # 且会让调用方误以为只是"部分数据缺失"而生成一份没有依据的降级分析。
            #
            # 必须限定 api_name 存在（= 该数据集确实走 Tushare）：分类器是按中文
            # 子串匹配的（"权限"/"积分不足"/"抱歉，您"），EDGAR/Yahoo/雪球的上游
            # 报错里出现这几个字完全可能，那会让一个第三方源的失败连带中止整只
            # 标的的 Tushare 同步。
            if api_name and classify_tushare_error(exc) == "fatal":
                result["fatal"] = {"dataset": dataset, "error": str(exc)[:200]}
                logger.error(
                    "Tushare 致命错误（token/权限），中止 %s/%s 的档案同步: %s",
                    symbol,
                    market,
                    str(exc)[:150],
                )
                break
    return result


# 供 LLM 输入与详情面板使用的每数据集行数上限（按 period_key 倒序取最新）。
# 值可以是整数，也可以是按 fp 的字典（`{"FY": 12, "H1": 6}`）：report_statements 混着年度与
# 中报行，单一上限按 period_key 截断会让近几年的中报把十年年度行挤出窗口（16 期只剩约 8 年）
PROFILE_CAPS: Dict[str, Any] = {
    "fina_indicator": 12,
    "forecast": 8,
    "express": 8,
    "daily_basic": 1,
    "dividend_history": 24,
    "fina_audit": 8,
    "pledge_stat": 8,
    "stk_holdertrade": 20,
    "income": 8,
    "balancesheet": 8,
    "cashflow": 8,
    "edgar_companyfacts": EDGAR_ANNUAL_KEEP + EDGAR_QUARTERLY_KEEP,
    "yahoo_fundamentals": 8,
    # 年报/中报 PDF 抽取行：年度十二期（十年 + 比较列多出的年份）+ 近六期中报，分开封顶
    "report_statements": {"FY": 12, "H1": 6},
    "xueqiu_income": 8,
    "xueqiu_capital_flow": 20,
    # 十行一期，取两期便于看变动
    "xueqiu_holders": 20,
}


# 格雷厄姆准则的年度行专取窗口。**不能复用 load_symbol_profile 的 caps**：
# 那套窗口按 period_key 倒序取"最近 N 个报告期"，季报会把年度行挤到只剩
# 2-3 个——十年盈利稳定恒 indeterminate 还只是失灵，分红记录被截断后算出
# "连续 2 年"则是**错误 fail**（美的实测连续 20+ 年，真实账本冒烟逮住）。
GRAHAM_ANNUAL_ROWS = 12
GRAHAM_DIVIDEND_ROWS = 100  # 每年 1-2 行实施记录 → 覆盖 40+ 年，取全量


def _dataset_rows(
    db: Session,
    symbol: str,
    market: str,
    dataset: str,
    *,
    like: Optional[str] = None,
    limit: Optional[int] = GRAHAM_ANNUAL_ROWS,
) -> List[Dict[str, Any]]:
    """limit=None 取全部。"""
    query = db.query(SecurityProfileData).filter(
        SecurityProfileData.symbol == symbol,
        SecurityProfileData.market == market,
        SecurityProfileData.dataset == dataset,
    )
    if like:
        query = query.filter(SecurityProfileData.period_key.like(like))
    query = query.order_by(SecurityProfileData.period_key.desc())
    if dataset == "report_statements":
        current = _job_dataset_current(dataset)
        return [row.payload for row in query.all() if current(row.payload or {})][:limit]
    rows = query.limit(limit).all()
    return [row.payload for row in rows]


# 格雷厄姆估值的中报/季报行窗口（港股 H1 取近三期即可覆盖「最新中报 + 上年同期」；美股单季 8 行）
GRAHAM_INTERIM_ROWS = 6
GRAHAM_QUARTER_ROWS = 24

# 一个标的的格雷厄姆取数器：(dataset, 期间键后缀, 排除后缀, 行数上限) → payload 列表
_Pick = Callable[..., List[Dict[str, Any]]]


def _graham_statement_datasets(
    market: str, pick: _Pick
) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """报表只取**年度行**（A股 period_key=末日 1231；美股 EDGAR 键带 |FY 后缀；港股 PDF 年度行
    `末日|FY` 优先、Yahoo 全为年度）。"""
    if market == "美股":
        return {"edgar_companyfacts": pick("edgar_companyfacts", suffix="|FY")}
    if market == "港股":
        return {
            # PDF 抽取的年度行优先（period_key=末日|FY），Yahoo 补缺——由 market_statements 合并
            "report_statements": pick("report_statements", suffix="|FY"),
            "yahoo_fundamentals": pick("yahoo_fundamentals"),
        }
    if market == "A股":
        return {
            "income": pick("income", suffix="1231"),
            "balancesheet": pick("balancesheet", suffix="1231"),
        }
    return None


def load_annual_statement_datasets(
    db: Session, symbol: str, market: str
) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """利润质量指标与预计算信号的年度报表取数（与格雷厄姆同一口径：只取年度行）。

    此前两处调用方把档案的 caps 窗口（A股 8 个报告期）直接交给 market_statements，而那 8 期
    混着季报——只认 1231 年度行的利润质量指标实际只剩约 2 年。A股 另补现金流量表与财务
    指标的年度行（格雷厄姆不需要，利润质量需要）。无对应口径的市场返回 None，调用方退回
    档案数据集。"""
    pick = _single_symbol_picker(db, symbol, market)
    rows = _graham_statement_datasets(market, pick)
    if rows is None:
        return None
    if market == "A股":
        rows = {
            **rows,
            "cashflow": pick("cashflow", suffix="1231"),
            "fina_indicator": pick("fina_indicator", suffix="1231"),
        }
    return rows


SIGNAL_INTERIM_ROWS = 8  # A股 约两年的季度累计行；港股 4 份中报；美股 8 个单季


def load_signal_inputs(db: Session, symbol: str, market: str) -> Optional[Dict[str, Any]]:
    """预计算信号（analysis_signals）的取数：年度行（与利润质量同口径）+ 非年度行（中报/季报，
    用于同期对比与下半年推算）+ A股 分红实施记录与最新估值快照。无对应口径的市场返回 None。"""
    annual = load_annual_statement_datasets(db, symbol, market)
    if annual is None:
        return None
    pick = _single_symbol_picker(db, symbol, market)
    if market == "A股":
        interim = {
            kind: pick(kind, exclude_suffix="1231", limit=SIGNAL_INTERIM_ROWS)
            for kind in ("income", "balancesheet", "cashflow", "fina_indicator")
        }
    elif market == "港股":
        interim = {"report_statements": pick("report_statements", suffix="|H1", limit=4)}
    else:
        interim = {
            "edgar_companyfacts": pick(
                "edgar_companyfacts", exclude_suffix="|FY", limit=SIGNAL_INTERIM_ROWS
            )
        }
    return {
        "annual": annual,
        "interim": interim,
        # 取**全部**分红记录（只用于计算、不送模型）：判「从未派息」要以上市以来的完整历史为据，
        # 按 GRAHAM_DIVIDEND_ROWS 截断时，预案/股东大会/实施等过程行会把老公司最早的现金分派
        # 挤出窗口（PR #333 复审 P2）
        "dividend_rows": pick("dividend_history", limit=None) if market == "A股" else [],
        "daily_basic": pick("daily_basic", limit=1) if market == "A股" else [],
    }


def _graham_interim_rows(market: str, pick: _Pick) -> List[Dict[str, Any]]:
    """TTM 与 MRQ 用的非年度行：港股 = PDF 中报行（存疑科目先清洗）；美股 = EDGAR 单季行。"""
    if market == "港股":
        from .report_statement_checks import scrub_suspect_fields

        return [
            scrub_suspect_fields(row)
            for row in pick("report_statements", suffix="|H1", limit=GRAHAM_INTERIM_ROWS)
        ]
    if market == "美股":
        return pick("edgar_companyfacts", exclude_suffix="|FY", limit=GRAHAM_QUARTER_ROWS)
    return []


def load_graham_inputs(db: Session, symbol: str, market: str) -> Optional[Dict[str, Any]]:
    """graham_screen 的取数口径：报表只取**年度行**、分红实施记录取全量、估值快照只要最新
    一行；港股/美股另取中报/季报行（TTM 与 MRQ）。库内无任何报表数据返回 None。

    全部消费点（详情页 profile / 分析输入 / 观察清单摘要）统一走这里与
    graham_summaries_for 的同一组取数函数，口径一处定义。
    """
    pick = _single_symbol_picker(db, symbol, market)
    statements_rows = _graham_statement_datasets(market, pick)
    if statements_rows is None or not any(statements_rows.values()):
        return None
    return {
        "statement_datasets": statements_rows,
        "interim_rows": _graham_interim_rows(market, pick),
        "daily_basic_rows": pick("daily_basic", limit=1),
        "dividend_rows": pick("dividend_history", limit=GRAHAM_DIVIDEND_ROWS),
    }


def _single_symbol_picker(db: Session, symbol: str, market: str) -> _Pick:
    def pick(
        dataset: str,
        *,
        suffix: Optional[str] = None,
        exclude_suffix: Optional[str] = None,
        limit: Optional[int] = GRAHAM_ANNUAL_ROWS,
    ) -> List[Dict[str, Any]]:
        if exclude_suffix is None:
            return _dataset_rows(
                db,
                symbol,
                market,
                dataset,
                like=f"%{suffix}" if suffix else None,
                limit=limit,
            )
        query = (
            db.query(SecurityProfileData)
            .filter(
                SecurityProfileData.symbol == symbol,
                SecurityProfileData.market == market,
                SecurityProfileData.dataset == dataset,
                SecurityProfileData.period_key.notlike(f"%{exclude_suffix}"),
            )
            .order_by(SecurityProfileData.period_key.desc())
            .limit(limit)
        )
        return [row.payload for row in query.all()]

    return pick


def _latest_price_rows(db: Session, keys: List[tuple]) -> Dict[tuple, Any]:
    """(symbol, market) → 行情库最新一根收盘（DISTINCT ON，一次查询）。"""
    from ..models.security_price import SecurityPrice

    wanted = [key for key in keys if key[1] in ("港股", "美股")]
    if not wanted:
        return {}
    rows = (
        db.query(SecurityPrice)
        .filter(SecurityPrice.symbol.in_(sorted({symbol for symbol, _ in wanted})))
        .filter(SecurityPrice.market.in_(sorted({market for _, market in wanted})))
        .distinct(SecurityPrice.symbol, SecurityPrice.market)
        .order_by(SecurityPrice.symbol, SecurityPrice.market, SecurityPrice.price_date.desc())
        .all()
    )
    return {(row.symbol, row.market): row for row in rows}


def _graham_valuation(
    symbol: str,
    market: str,
    statements: Dict[str, List[Dict[str, Any]]],
    interim_rows: List[Dict[str, Any]],
    annual_rows: List[Dict[str, Any]],
    price_row: Any,
    rate_lookup: Any,
    ads_ratio: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """港股/美股估值输入：最新收盘价（陈旧照算、标注）+ 报表币种→价格币种汇率 + ADS 口径。

    ads_ratio = ads_ratio_service.resolve_ads_ratio(s) 的结果（用户规则 > 20-F 封面解析）：
    EDGAR 每股数据按普通股、行情价按 ADS；有换算比即用（不论表单），没有而年报是 20-F →
    估值两项 indeterminate（不猜比例）；10-K 发行人按 1:1。"""
    from ..core.timeutil import local_today
    from .graham_screen import resolve_fx_rates
    from .statistics.pricing import PRICE_STALE_DAYS

    if price_row is None:
        return {"price": None}
    age_days = (local_today() - price_row.price_date).days
    price = {
        "close": float(price_row.close_price),
        "currency": price_row.currency,
        "date": price_row.price_date.isoformat(),
        "stale": age_days > PRICE_STALE_DAYS,
        "age_days": age_days,
        "source": price_row.source,
    }
    currencies = {
        row.get("currency")
        for kind in ("income", "balancesheet")
        for row in statements.get(kind, [])
    } | {row.get("currency") for row in interim_rows}
    valuation: Dict[str, Any] = {
        "price": price,
        "fx_rates": resolve_fx_rates(
            currencies, price["currency"], price_row.price_date, rate_lookup
        ),
        "interim_rows": interim_rows,
    }
    if market == "美股":
        form = next((row.get("form") for row in annual_rows if row.get("form")), None)
        valuation["annual_form"] = form
        if ads_ratio and ads_ratio.get("ratio") is not None:
            valuation["share_ratio"] = float(ads_ratio["ratio"])
            valuation["share_ratio_note"] = ads_ratio["note"]
            valuation["share_ratio_source"] = ads_ratio["source"]
        elif str(form or "").startswith("20-F") or (ads_ratio or {}).get("missing"):
            # 20-F 发行人没有比例，或 10-K 封面登记了 ADS 却解析不出比例 / 封面无法识别
            # （#352，PR #358 评审 P2）：不按 1:1 猜
            valuation["share_ratio_missing"] = True
            if (ads_ratio or {}).get("unknown"):
                valuation["share_ratio_unknown"] = True
    return valuation


def _graham_history_confirmed(market: str, plan: Optional[Dict[str, Any]]) -> bool:
    """可得年度数据的起点是否即公司披露历史的起点。港股以披露易清单为准：完整清单里的年报
    不足十份才说「披露历史仅 N 年」；否则可能只是更早年报尚未抽取。其余市场的数据源覆盖全史。"""
    if market != "港股":
        return True
    from .report_statement_service import ANNUAL_YEARS

    return bool(plan) and int((plan or {}).get("planned_annual") or 0) < ANNUAL_YEARS


def _screen_symbol(
    symbol: str,
    market: str,
    pick: _Pick,
    *,
    price_row: Any,
    rate_lookup: Any,
    plan: Optional[Dict[str, Any]],
    ads_ratio: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """一个标的的格雷厄姆结果（单标的与批量共用）；无报表数据返回 None。"""
    from .earnings_quality import market_statements
    from .graham_screen import compute_graham_screen

    statement_datasets = _graham_statement_datasets(market, pick)
    if statement_datasets is None or not any(statement_datasets.values()):
        return None
    statements = market_statements(market, statement_datasets)
    valuation = None
    if market in ("港股", "美股"):
        interim = _graham_interim_rows(market, pick)
        annual_rows = statement_datasets.get("edgar_companyfacts") or []
        valuation = _graham_valuation(
            symbol,
            market,
            statements,
            interim,
            annual_rows,
            price_row,
            rate_lookup,
            ads_ratio=ads_ratio,
        )
    return compute_graham_screen(
        market,
        statements,
        daily_basic_rows=pick("daily_basic", limit=1),
        dividend_rows=pick("dividend_history", limit=GRAHAM_DIVIDEND_ROWS),
        valuation=valuation,
        history_confirmed=_graham_history_confirmed(market, plan),
    )


def _rate_lookup_for(db: Session, markets) -> Any:
    if not any(market in ("港股", "美股") for market in markets):
        return None
    from .statistics.fx import DbExchangeRateLookup

    return DbExchangeRateLookup.from_db(db)


def _ads_ratios_for(
    db: Session, keys: List[tuple], user_id: Optional[int]
) -> Dict[str, Dict[str, Any]]:
    """美股标的的生效 ADS 换算比；user_id=None（无用户上下文）只用 20-F 封面解析值。"""
    symbols = [symbol for symbol, market in keys if market == "美股"]
    if not symbols:
        return {}
    from .ads_ratio_service import resolve_ads_ratios

    return resolve_ads_ratios(db, symbols, user_id)


def compute_graham_for(
    db: Session, symbol: str, market: str, *, user_id: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """单标的格雷厄姆结果（详情页 profile / 分析输入）；无数据返回 None。

    user_id：请求方用户（其 ADS_RATIO 规则覆盖 20-F 解析值）；None = 只用解析值。"""
    plan = None
    if market == "港股":
        from .report_statement_service import load_statement_plan

        plan = load_statement_plan(db, symbol, market)
    result = _screen_symbol(
        symbol,
        market,
        _single_symbol_picker(db, symbol, market),
        price_row=_latest_price_rows(db, [(symbol, market)]).get((symbol, market)),
        rate_lookup=_rate_lookup_for(db, [market]),
        plan=plan,
        ads_ratio=_ads_ratios_for(db, [(symbol, market)], user_id).get(symbol),
    )
    return result if result is not None and result["status"] == "ok" else None


def graham_summaries_for(
    db: Session,
    keys: List[tuple],
    *,
    user_id: Optional[int] = None,
) -> Dict[tuple, Optional[Dict[str, Any]]]:
    """批量版 graham_summary_for：一次查询取全部标的的准则输入，避免列表页
    每条 3-4 次往返的 N 倍放大（评审 P2）。返回 {(symbol, market): summary|None}。

    仍按 (symbol, market) 逐标的计算纯函数；差别只在取数：一条 IN 查询把
    所需数据集全部拉回内存后按标的分桶，再按单标的的同一组取数函数
    （_graham_statement_datasets / _graham_interim_rows）在内存中裁剪；
    行情价一条 DISTINCT ON、汇率表整表装载一次。
    """
    from .report_statement_service import PLAN_DATASET

    if not keys:
        return {}
    wanted_datasets = (
        "income",
        "balancesheet",
        "daily_basic",
        "dividend_history",
        "edgar_companyfacts",
        "yahoo_fundamentals",
        "report_statements",
        PLAN_DATASET,
    )
    symbols = sorted({symbol for symbol, _ in keys})
    rows = (
        db.query(
            SecurityProfileData.symbol,
            SecurityProfileData.market,
            SecurityProfileData.dataset,
            SecurityProfileData.period_key,
            SecurityProfileData.payload,
        )
        .filter(
            SecurityProfileData.symbol.in_(symbols),
            SecurityProfileData.dataset.in_(wanted_datasets),
        )
        .order_by(SecurityProfileData.period_key.desc())
        .all()
    )
    buckets: Dict[tuple, Dict[str, List[tuple]]] = {}
    for symbol, market, dataset, period_key, payload in rows:
        buckets.setdefault((symbol, market), {}).setdefault(dataset, []).append(
            (period_key, payload)
        )
    prices = _latest_price_rows(db, keys)
    rate_lookup = _rate_lookup_for(db, [market for _, market in keys])
    ads_ratios = _ads_ratios_for(db, keys, user_id)

    def bucket_picker(bucket: Dict[str, List[tuple]]) -> _Pick:
        def pick(
            dataset: str,
            *,
            suffix: Optional[str] = None,
            exclude_suffix: Optional[str] = None,
            limit: int = GRAHAM_ANNUAL_ROWS,
        ) -> List[Dict[str, Any]]:
            items = bucket.get(dataset, [])
            if suffix:
                items = [(k, v) for k, v in items if str(k).endswith(suffix)]
            if exclude_suffix:
                items = [(k, v) for k, v in items if not str(k).endswith(exclude_suffix)]
            if dataset in MARKET_JOB_DATASETS.get("港股", {}):
                current = _job_dataset_current(dataset)
                items = [(k, v) for k, v in items if current(v or {})]
            return [payload for _, payload in items[:limit]]

        return pick

    result: Dict[tuple, Optional[Dict[str, Any]]] = {}
    for key in keys:
        symbol, market = key
        bucket = buckets.get(key, {})
        plan_items = bucket.get(PLAN_DATASET) or []
        screen = _screen_symbol(
            symbol,
            market,
            bucket_picker(bucket),
            price_row=prices.get(key),
            rate_lookup=rate_lookup,
            plan=plan_items[0][1] if plan_items else None,
            ads_ratio=ads_ratios.get(symbol) if market == "美股" else None,
        )
        if screen is None or screen["status"] != "ok":
            result[key] = None
            continue
        result[key] = {
            "passed": screen["passed"],
            "failed": screen["failed"],
            "indeterminate": screen["indeterminate"],
            "total": len(screen["criteria"]),
            "as_of_year": screen["as_of_year"],
        }
    return result


def graham_summary_for(
    db: Session, symbol: str, market: str, *, user_id: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """观察清单列表列用的准则摘要（计数 + 数据年度）；无档案数据返回 None
    ——前端显示"未同步档案"而不是 0/7 的误导计数。取数走 compute_graham_for
    的年度行专取口径（caps 窗口的旧实现会把分红记录截断成错误 fail）。"""
    result = compute_graham_for(db, symbol, market, user_id=user_id)
    if result is None:
        return None
    return {
        "passed": result["passed"],
        "failed": result["failed"],
        "indeterminate": result["indeterminate"],
        "total": len(result["criteria"]),
        "as_of_year": result["as_of_year"],
    }


def _cap_rows(rows: List[Any], cap: Any) -> List[Any]:
    """按上限截断（rows 已按 period_key 倒序）。cap 为字典时按行 payload 的 fp 分桶各自封顶
    （缺 fp 视为 FY），字典里没有的 fp 不保留。"""
    if not isinstance(cap, dict):
        return rows[: int(cap)]
    kept: List[Any] = []
    seen: Dict[str, int] = {}
    for row in rows:
        payload = row.payload if hasattr(row, "payload") else row
        fp = str((payload or {}).get("fp") or "FY")
        limit = cap.get(fp)
        if limit is None:
            continue
        if seen.get(fp, 0) >= int(limit):
            continue
        seen[fp] = seen.get(fp, 0) + 1
        kept.append(row)
    return kept


def load_symbol_profile(
    db: Session, symbol: str, market: str, *, caps: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """按数据集分组读取（period_key 倒序、逐集封顶），附数据截止信息。"""
    caps = caps or PROFILE_CAPS
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    latest_fetch: Optional[datetime] = None
    job_datasets = MARKET_JOB_DATASETS.get(market, {})
    datasets = list(MARKET_DATASETS.get(market, {})) + list(job_datasets)
    for dataset in datasets:
        query = (
            db.query(SecurityProfileData)
            .filter(
                SecurityProfileData.symbol == symbol,
                SecurityProfileData.market == market,
                SecurityProfileData.dataset == dataset,
            )
            .order_by(SecurityProfileData.period_key.desc())
        )
        if dataset in job_datasets:
            # 版本过滤后再截断：旧版本行不占 caps 名额也不进结果
            current = _job_dataset_current(dataset)
            rows = _cap_rows(
                [row for row in query.all() if current(row.payload or {})], caps.get(dataset, 10)
            )
        else:
            cap = caps.get(dataset, 10)
            rows = (
                query.limit(cap).all() if not isinstance(cap, dict) else _cap_rows(query.all(), cap)
            )
        grouped[dataset] = [row.payload for row in rows]
        for row in rows:
            if row.fetched_at and (latest_fetch is None or row.fetched_at > latest_fetch):
                latest_fetch = row.fetched_at
    return {
        "symbol": symbol,
        "market": market,
        "datasets": grouped,
        "fetched_at": latest_fetch.isoformat() if latest_fetch else None,
        "row_counts": {dataset: len(rows) for dataset, rows in grouped.items()},
        # 各数据集覆盖期（最新自然键）：数据时效以此为准，fetched_at 只是抓取时间
        "latest_periods": {
            dataset: (_dataset_spec(market, dataset)["key"](rows[0]) if rows else None)
            for dataset, rows in grouped.items()
        },
    }


def load_security_events_for(
    db: Session, symbol: str, market: str, *, limit: int = 20
) -> List[Dict[str, Any]]:
    """标的事件（含历史，倒序）：LLM 分析输入与详情面板共用。"""
    from ..models.security_event import SecurityEvent

    rows = (
        db.query(SecurityEvent)
        .filter(SecurityEvent.symbol == symbol, SecurityEvent.market == market)
        .order_by(SecurityEvent.event_date.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "event_type": row.event_type,
            "event_date": row.event_date.isoformat(),
            "payload": row.payload,
        }
        for row in rows
    ]


def profile_fetched_date(db: Session, symbol: str, market: str) -> Optional[date]:
    """输入数据的**抓取日**（非数据截止日：接口今天可能只取到旧报告期的数据，
    数据本身的时效以各数据集 period/latest_periods 为准）。"""
    latest = (
        db.query(func.max(SecurityProfileData.fetched_at))
        .filter(
            SecurityProfileData.symbol == symbol,
            SecurityProfileData.market == market,
        )
        .scalar()
    )
    # 必须换算到本地日：fetched_at 存 UTC，而调用方/展示侧的"今天"是 date.today()
    # （本地）。直接 .date() 会让本地 0-8 点触发的分析显示成前一天。
    return to_local_date(latest)
