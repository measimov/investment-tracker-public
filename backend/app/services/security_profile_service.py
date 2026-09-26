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
    # 港股：年报/中报 PDF 三张表抽取（report_statements，官方一手、可达十年）+ Yahoo 年度
    # 科目补缺 + 披露易年报全文摘要；无审计意见/质押/增减持数据源，风险信号只能来自
    # 年报「主要風險」章节
    "港股": {
        "structured": True, "report_digest": True, "risk_signals": "risk_factors",
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
    # 格雷厄姆准则层（graham_screen）新增：流动负债/长期债务/利息支出。
    # 短期借款概念在各公司间过于分裂，刻意不收——净债务口径按"仅长期"
    # 标注方向性偏差，胜过用错概念拼出貌似完整的合计。
    "total_cur_liab": ("LiabilitiesCurrent",),
    "lt_debt": ("LongTermDebtNoncurrent", "LongTermDebt"),
    "int_exp": ("InterestExpense", "InterestExpenseDebt"),
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
            "fetch": _fetch_xueqiu_capital_flow, "key": _key_of("period_key"),
        },
        # 十大流通股东：period_key = 报告期|名次（wrapper 构造），每期十行
        "xueqiu_holders": {"fetch": _fetch_xueqiu_holders, "key": _key_of("period_key")},
    },
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


# 任务驱动的数据集：不在 sync_symbol_profile 里拉取（由 job 写入），但随档案一起加载。
# 港股 report_statements = 年报/中报 PDF 抽取的科目行（report_statement_service）
MARKET_JOB_DATASETS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "港股": {"report_statements": {"key": _key_of("end_date", "fp"), "current": "statements"}},
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
                    symbol, market, str(exc)[:150],
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
    db: Session, symbol: str, market: str, dataset: str,
    *, like: Optional[str] = None, limit: int = GRAHAM_ANNUAL_ROWS,
) -> List[Dict[str, Any]]:
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


def load_graham_inputs(db: Session, symbol: str, market: str) -> Optional[Dict[str, Any]]:
    """graham_screen 的取数口径：报表只取**年度行**（A股 period_key=末日 1231；
    美股 EDGAR 键带 |FY 后缀；港股 Yahoo 全为年度）、分红实施记录取全量、
    估值快照只要最新一行。库内无任何报表数据返回 None。

    全部消费点（详情页 profile / 分析输入 / 观察清单摘要）统一走这里，
    口径一处定义。
    """
    if market == "美股":
        statements_rows = {
            "edgar_companyfacts": _dataset_rows(
                db, symbol, market, "edgar_companyfacts", like="%|FY"
            )
        }
    elif market == "港股":
        statements_rows = {
            # PDF 抽取的年度行优先（period_key=末日|FY），Yahoo 补缺——由 market_statements 合并
            "report_statements": _dataset_rows(
                db, symbol, market, "report_statements", like="%|FY"
            ),
            "yahoo_fundamentals": _dataset_rows(db, symbol, market, "yahoo_fundamentals"),
        }
    elif market == "A股":
        statements_rows = {
            "income": _dataset_rows(db, symbol, market, "income", like="%1231"),
            "balancesheet": _dataset_rows(db, symbol, market, "balancesheet", like="%1231"),
        }
    else:
        return None
    if not any(statements_rows.values()):
        return None
    return {
        "statement_datasets": statements_rows,
        "daily_basic_rows": _dataset_rows(db, symbol, market, "daily_basic", limit=1),
        "dividend_rows": _dataset_rows(
            db, symbol, market, "dividend_history", limit=GRAHAM_DIVIDEND_ROWS
        ),
    }


def compute_graham_for(db: Session, symbol: str, market: str) -> Optional[Dict[str, Any]]:
    """load_graham_inputs + 纯函数计算；无数据返回 None。"""
    from .earnings_quality import market_statements
    from .graham_screen import compute_graham_screen

    inputs = load_graham_inputs(db, symbol, market)
    if inputs is None:
        return None
    result = compute_graham_screen(
        market,
        market_statements(market, inputs["statement_datasets"]),
        daily_basic_rows=inputs["daily_basic_rows"],
        dividend_rows=inputs["dividend_rows"],
    )
    return result if result["status"] == "ok" else None


def graham_summaries_for(
    db: Session, keys: List[tuple],
) -> Dict[tuple, Optional[Dict[str, Any]]]:
    """批量版 graham_summary_for：一次查询取全部标的的准则输入，避免列表页
    每条 3-4 次往返的 N 倍放大（评审 P2）。返回 {(symbol, market): summary|None}。

    仍按 (symbol, market) 逐标的计算纯函数；差别只在取数：一条 IN 查询把
    所需数据集全部拉回内存后按标的分桶，再按 load_graham_inputs 的同一口径
    （年度行/分红全量/最新估值）在内存中裁剪。
    """
    from .earnings_quality import market_statements
    from .graham_screen import compute_graham_screen

    if not keys:
        return {}
    wanted_datasets = (
        "income", "balancesheet", "daily_basic", "dividend_history",
        "edgar_companyfacts", "yahoo_fundamentals", "report_statements",
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

    def _pick(bucket, dataset, *, like_suffix=None, limit=GRAHAM_ANNUAL_ROWS):
        items = bucket.get(dataset, [])
        if like_suffix:
            items = [(k, v) for k, v in items if str(k).endswith(like_suffix)]
        if dataset in MARKET_JOB_DATASETS.get("港股", {}):
            current = _job_dataset_current(dataset)
            items = [(k, v) for k, v in items if current(v or {})]
        return [payload for _, payload in items[:limit]]

    result: Dict[tuple, Optional[Dict[str, Any]]] = {}
    for key in keys:
        symbol, market = key
        bucket = buckets.get(key, {})
        if market == "美股":
            statement_datasets = {
                "edgar_companyfacts": _pick(bucket, "edgar_companyfacts", like_suffix="|FY")
            }
        elif market == "港股":
            statement_datasets = {
                "report_statements": _pick(bucket, "report_statements", like_suffix="|FY"),
                "yahoo_fundamentals": _pick(bucket, "yahoo_fundamentals"),
            }
        elif market == "A股":
            statement_datasets = {
                "income": _pick(bucket, "income", like_suffix="1231"),
                "balancesheet": _pick(bucket, "balancesheet", like_suffix="1231"),
            }
        else:
            result[key] = None
            continue
        if not any(statement_datasets.values()):
            result[key] = None
            continue
        screen = compute_graham_screen(
            market,
            market_statements(market, statement_datasets),
            daily_basic_rows=_pick(bucket, "daily_basic", limit=1),
            dividend_rows=_pick(bucket, "dividend_history", limit=GRAHAM_DIVIDEND_ROWS),
        )
        if screen["status"] != "ok":
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


def graham_summary_for(db: Session, symbol: str, market: str) -> Optional[Dict[str, Any]]:
    """观察清单列表列用的准则摘要（计数 + 数据年度）；无档案数据返回 None
    ——前端显示"未同步档案"而不是 0/7 的误导计数。取数走 compute_graham_for
    的年度行专取口径（caps 窗口的旧实现会把分红记录截断成错误 fail）。"""
    result = compute_graham_for(db, symbol, market)
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
            rows = query.limit(cap).all() if not isinstance(cap, dict) else _cap_rows(query.all(), cap)
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
