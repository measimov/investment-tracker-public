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
from .stock_price_service import (
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
    # 港股：Yahoo 年度科目（非官方端点，仅近 3-5 年）+ 披露易年报全文摘要；
    # 无审计意见/质押/增减持数据源，风险信号只能来自年报「主要風險」章节
    "港股": {"structured": True, "report_digest": True, "risk_signals": "risk_factors"},
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
        "api": "dividend", "params": {}, "key": _key_of("end_date", "div_proc", "ann_date"),
    },
    "fina_audit": {"api": "fina_audit", "params": {}, "key": _key_of("end_date", "ann_date")},
    "pledge_stat": {"api": "pledge_stat", "params": {}, "key": _key_of("end_date")},
    "stk_holdertrade": {
        "api": "stk_holdertrade", "params": {},
        "key": _key_of("ann_date", "holder_name", "in_de"),
    },
    # 三大报表（合并报表口径，同报告期取最新修正；2026-08-02 真实 token 实测可用）
    "income": {
        "api": "income", "params": {}, "key": _key_of("end_date"),
        "prepare": _merged_statement_rows,
    },
    "balancesheet": {
        "api": "balancesheet", "params": {}, "key": _key_of("end_date"),
        "prepare": _merged_statement_rows,
    },
    "cashflow": {
        "api": "cashflow", "params": {}, "key": _key_of("end_date"),
        "prepare": _merged_statement_rows,
    },
}


# EDGAR XBRL 概念兜底链：同一财务概念在不同公司/年份用不同 tag，
# 取链上首个有值者；银行等特殊行业末位取不到就留空（走"数据不足"）。
EDGAR_CONCEPT_CHAINS: Dict[str, tuple] = {
    "total_revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
        "SalesRevenueNet",
    ),
    "cost_of_revenue": ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"),
    "operating_income": ("OperatingIncomeLoss",),
    "n_income_attr_p": ("NetIncomeLoss",),
    "total_assets": ("Assets",),
    "total_liab": ("Liabilities",),
    "total_hldr_eqy_exc_min_int": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ),
    "money_cap": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ),
    # **只放贸易应收语义等价的概念**。应收-营收增速差与 Beneish DSRI 比的是
    # "销售形成的应收"与营收；贷款/票据/其他应收与营业收入没有同一经济含义，
    # 拿它们兜底会生成看似完整实则无效的风险信号（PDD 实测只有 NotesAndLoans
    # 与 OtherReceivables 口径 → 该项如实留空，走"数据不足"）。
    "accounts_receiv": (
        "AccountsReceivableNetCurrent",
        "AccountsReceivableGrossCurrent",
    ),
    "inventories": ("InventoryNet",),
    "total_cur_assets": ("AssetsCurrent",),
    "fix_assets": ("PropertyPlantAndEquipmentNet",),
    "n_cashflow_act": ("NetCashProvidedByUsedInOperatingActivities",),
    "n_cashflow_inv_act": ("NetCashProvidedByUsedInInvestingActivities",),
    "n_cash_flows_fnc_act": ("NetCashProvidedByUsedInFinancingActivities",),
    "sga_exp": ("SellingGeneralAndAdministrativeExpense",),
    "depr_fa_coga_dpba": (
        "DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
        "Depreciation",
    ),
    "basic_eps": ("EarningsPerShareBasic",),
    "diluted_eps": ("EarningsPerShareDiluted",),
}

# 拆分科目求和兜底：概念链整条落空时，把这些分项**相加**补上。
# 兜底链解决的是"同一概念不同 tag"，解决不了"一个概念被拆成两个 tag"——
# PDD/BABA 实测都不报 SellingGeneralAndAdministrativeExpense，只报
# SellingAndMarketingExpense + GeneralAndAdministrativeExpense 两条，
# 而 Beneish M-score 的 SGAI 因子要的是合计值。
EDGAR_CONCEPT_SUMS: Dict[str, tuple] = {
    "sga_exp": ("SellingAndMarketingExpense", "GeneralAndAdministrativeExpense"),
}


# duration facts 的期间长度容差（日历天）：财年 52/53 周与季度长度都有浮动
_ANNUAL_DAYS = (330, 400)
_QUARTER_DAYS = (60, 115)

# 分类保留额度：年度对齐十年覆盖深度（与 A股 报表 8 期同量级），季度只留近两年
EDGAR_ANNUAL_KEEP = 12
EDGAR_QUARTERLY_KEEP = 8


def _fact_duration_days(item: Dict[str, Any]) -> Optional[int]:
    """duration fact 的期间长度；instant fact（无 start）返回 None。"""
    start, end = str(item.get("start") or ""), str(item.get("end") or "")
    if not start or not end:
        return None
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except ValueError:
        return None


def _matches_period(item: Dict[str, Any], fp: str) -> bool:
    """该 fact 的期间长度是否符合 fp 声明的口径。

    (end, fp) **不能**唯一标识 companyfacts 的期间值：同一 (end, fp, filed)
    下会并存单季与年初至今累计值，只有 start/期间长度不同（实测 AAPL
    ('2018-09-29','FY') 同时含全年 265.6B 与 Q4 单季 62.9B）。不按期间长度
    选口径，年度营收/利润就会在全年与单季之间漂移。
    """
    days = _fact_duration_days(item)
    if days is None:
        return True  # instant fact（资产/负债等时点科目）无期间概念
    low, high = _ANNUAL_DAYS if fp == "FY" else _QUARTER_DAYS
    return low <= days <= high


def _apply_concept_sums(
    facts: Dict[str, Any], rows: Dict[tuple, Dict[str, Any]], marks: Dict[tuple, tuple]
) -> None:
    """概念链落空的期间用分项求和补齐（只补空，不覆盖链上已有值）。

    逐期判断：同一家公司可能早年报合计、近年改拆分，整体判断会漏掉半段历史。

    **必须分项齐全才求和**。只披露营销费的年份若把营销费当成完整 SGA，
    SGAI/M-score 会得到一个数值正常但系统性偏低的结果——这比留空危险得多，
    因为下游无从知道它是不完整合计。缺分项时留空，走"数据不足"路径。
    """
    for field, components in EDGAR_CONCEPT_SUMS.items():
        per_component: Dict[str, Dict[tuple, float]] = {}
        for concept in components:
            concept_data = facts.get(concept)
            if not concept_data:
                continue
            units = (concept_data.get("units") or {}).get("USD") or []
            latest: Dict[tuple, tuple] = {}  # 期间键 → (filed, val)
            for item in units:
                end, fp, value = (
                    str(item.get("end") or ""), str(item.get("fp") or ""), item.get("val")
                )
                if not end or value is None or not _matches_period(item, fp):
                    continue
                key = (end, fp)
                filed = str(item.get("filed") or "")
                if key not in latest or filed >= latest[key][0]:
                    latest[key] = (filed, value)
            per_component[concept] = {key: value for key, (_, value) in latest.items()}
        if len(per_component) < len(components):
            continue  # 有分项该公司整体就没报 → 无从求和
        complete_keys = set.intersection(*(set(v) for v in per_component.values()))
        for key in complete_keys:
            if (key, field) in marks or key not in rows:
                continue  # 链上已有值，或该期间没有任何其他科目（不凭空造行）
            rows[key][field] = sum(v[key] for v in per_component.values())


def _fetch_edgar_companyfacts(symbol: str, market: str) -> List[Dict[str, Any]]:
    """EDGAR XBRL → 每 (end, fp) 一行。

    口径选择：duration fact 必须与 fp 的期间长度相符（FY=全年、Qx=单季），
    不符者整条丢弃而不是任其覆盖；同口径下多 filing 取 filed 最新（重述胜）。
    """
    from .report_fetchers import edgar_companyfacts, edgar_lookup

    lookup = edgar_lookup(symbol)
    if not lookup:
        raise ValueError(f"美股代码 {symbol} 未在 SEC 注册表中找到")
    facts = (edgar_companyfacts(lookup["cik"]).get("facts") or {}).get("us-gaap") or {}

    rows: Dict[tuple, Dict[str, Any]] = {}
    # (期间键, 字段) → (概念优先级, filed)：优先级与重述判定都必须**逐期**做
    marks: Dict[tuple, tuple] = {}
    for field, chain in EDGAR_CONCEPT_CHAINS.items():
        # 整条链全扫，不在首个有值概念处 break：公司换 XBRL tag 后首选概念
        # 只覆盖近几年，旧年份的值只存在于备用概念里，一次性 break 会整段丢失
        for rank, concept in enumerate(chain):
            concept_data = facts.get(concept)
            if not concept_data:
                continue
            units = concept_data.get("units") or {}
            unit_rows = units.get("USD") or units.get("USD/shares") or []
            for item in unit_rows:
                end = str(item.get("end") or "")
                fp = str(item.get("fp") or "")
                value = item.get("val")
                if not end or value is None:
                    continue
                if not _matches_period(item, fp):
                    continue  # 口径不符（累计值混入单季期、单季混入年报期）
                key = (end, fp)
                filed = str(item.get("filed") or "")
                previous = marks.get((key, field))
                if previous is not None:
                    previous_rank, previous_filed = previous
                    if rank > previous_rank:
                        continue  # 该期已有更高优先级概念的值，备用概念不覆盖
                    if rank == previous_rank and filed < previous_filed:
                        continue  # 同概念多 filing：filed 最新者胜（重述）
                row = rows.setdefault(key, {
                    "end_date": end.replace("-", ""),
                    "fp": fp,
                    "form": item.get("form"),
                    "currency": "USD",
                })
                row[field] = value
                marks[(key, field)] = (rank, filed)

    _apply_concept_sums(facts, rows, marks)

    # 分类封顶：年度与季度各留各的额度。统一按 end_date 取前 N 会被数量占优的
    # 季度行占满——实测 AAPL 158 个期间里 FY 仅 20 个，一刀切 40 行只剩 4 个
    # 年度，十余年年报史（利润质量/趋势分析的输入）被静默丢弃。
    ordered = sorted(rows.values(), key=lambda r: r["end_date"], reverse=True)
    annual = [row for row in ordered if row["fp"] == "FY"][:EDGAR_ANNUAL_KEEP]
    quarterly = [row for row in ordered if row["fp"] != "FY"][:EDGAR_QUARTERLY_KEEP]
    return sorted(annual + quarterly, key=lambda r: r["end_date"], reverse=True)

def _fetch_yahoo_fundamentals(symbol: str, market: str) -> List[Dict[str, Any]]:
    from .report_fetchers import yahoo_hk_fundamentals

    return yahoo_hk_fundamentals(symbol)


# 市场 → 数据集注册表；DATASETS 保留为合并视图（向后兼容测试/键查找）
MARKET_DATASETS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "A股": DATASETS,
    "美股": {
        "edgar_companyfacts": {
            "fetch": _fetch_edgar_companyfacts,
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


def _dataset_spec(market: str, dataset: str) -> Dict[str, Any]:
    spec = (MARKET_DATASETS.get(market) or {}).get(dataset)
    if spec:
        return spec
    # 合并视图兜底（非注册表数据集如 report_* 不经此路径）
    for registry in MARKET_DATASETS.values():
        if dataset in registry:
            return registry[dataset]
    raise KeyError(dataset)


def _normalize_row(raw: Dict[str, Any]) -> Dict[str, Any]:
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
    except ValueError:
        return []
    rows = [_normalize_row(row) for row in df.to_dict("records")]
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
        values.append({
            "symbol": symbol,
            "market": market,
            "dataset": dataset,
            "period_key": period_key,
            "payload": row,
        })
    if not values:
        return 0
    stmt = pg_insert(SecurityProfileData).values(values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_security_profile_identity",
        set_={"payload": stmt.excluded.payload, "fetched_at": func.now()},
    ).returning(literal_column("(xmax = 0)").label("inserted"))
    inserted = sum(1 for flag in db.execute(stmt).scalars() if flag)
    return inserted


def upsert_profile_row(
    db: Session,
    symbol: str,
    market: str,
    dataset: str,
    period_key: str,
    payload: Dict[str, Any],
) -> None:
    """显式 period_key 的单行原子 upsert（报告节选/摘要等非注册表数据集用）。"""
    stmt = pg_insert(SecurityProfileData).values([{
        "symbol": symbol,
        "market": market,
        "dataset": dataset,
        "period_key": period_key[:40],
        "payload": payload,
    }])
    stmt = stmt.on_conflict_do_update(
        constraint="uq_security_profile_identity",
        set_={"payload": stmt.excluded.payload, "fetched_at": func.now()},
    )
    db.execute(stmt)


def _prune_daily_basic(db: Session, symbol: str, market: str) -> None:
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
            "symbol": symbol, "market": market, "supported": False,
            "datasets": {}, "failed": [], "skipped": [],
        }
    result: Dict[str, Any] = {
        "symbol": symbol, "market": market, "supported": True,
        "datasets": {}, "failed": [], "skipped": [],
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
                    api_name, remaining, dataset,
                )
                result["skipped"].append({
                    "dataset": dataset, "reason": "rate_cooldown",
                    "retry_after_seconds": round(remaining),
                })
                continue
        try:
            rows = fetch_dataset_rows(dataset, symbol, market)
            inserted = upsert_profile_rows(db, symbol, market, dataset, rows)
            if dataset == "daily_basic":
                _prune_daily_basic(db, symbol, market)
            db.commit()
            result["datasets"][dataset] = {"rows": len(rows), "inserted": inserted}
        except Exception as exc:  # 单数据集失败不中断
            db.rollback()
            logger.warning("同步 %s %s/%s 失败: %s", dataset, symbol, market, exc)
            result["failed"].append({"dataset": dataset, "error": str(exc)[:200]})
            # token 失效/无权限对所有数据集等价：继续逐集重试毫无意义，且会让
            # 调用方误以为只是"部分数据缺失"而生成一份没有依据的降级分析
            if classify_tushare_error(exc) == "fatal":
                result["fatal"] = {"dataset": dataset, "error": str(exc)[:200]}
                logger.error(
                    "Tushare 致命错误（token/权限），中止 %s/%s 的档案同步: %s",
                    symbol, market, str(exc)[:150],
                )
                break
    return result


# 供 LLM 输入与详情面板使用的每数据集行数上限（按 period_key 倒序取最新）
PROFILE_CAPS: Dict[str, int] = {
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
}


def load_symbol_profile(
    db: Session, symbol: str, market: str, *, caps: Optional[Dict[str, int]] = None
) -> Dict[str, Any]:
    """按数据集分组读取（period_key 倒序、逐集封顶），附数据截止信息。"""
    caps = caps or PROFILE_CAPS
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    latest_fetch: Optional[datetime] = None
    for dataset in MARKET_DATASETS.get(market, {}):
        rows = (
            db.query(SecurityProfileData)
            .filter(
                SecurityProfileData.symbol == symbol,
                SecurityProfileData.market == market,
                SecurityProfileData.dataset == dataset,
            )
            .order_by(SecurityProfileData.period_key.desc())
            .limit(caps.get(dataset, 10))
            .all()
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
            dataset: (
                _dataset_spec(market, dataset)["key"](rows[0]) if rows else None
            )
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
