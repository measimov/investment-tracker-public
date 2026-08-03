"""标的基本面数据同步（Tushare，A股 only）与档案读取。

八个数据集（2026-08-02 真实 token 逐一实测可用）：财务指标、业绩预告、
业绩快报、估值快照、分红历史、审计意见、股权质押、股东增减持。
"合规污点"按拍板降级为客观风险信号——审计意见/质押/减持/解禁，LLM 只
基于这些输入判断，不引入模型自身对公司的知识。

存储为通用 JSON 行（security_profile_data），按 (symbol, market, dataset,
period_key) 原子 upsert；港股/美股无对应接口，显式降级。
"""

from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import literal_column
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from ..core.logging import get_app_logger
from ..models.security_profile import SecurityProfileData
from .stock_price_service import to_tushare_a_code, tushare_query

logger = get_app_logger(__name__)

SUPPORTED_MARKETS = ("A股",)

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
}

# daily_basic 是日度估值快照：只保留最近 N 行，避免逐日膨胀
DAILY_BASIC_KEEP_ROWS = 30


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
    """拉取单数据集全部行（测试 monkeypatch 本函数；空数据归一为空列表）。"""
    spec = DATASETS[dataset]
    try:
        df = tushare_query(spec["api"], ts_code=to_tushare_a_code(symbol), **spec["params"])
    except ValueError:
        return []
    return [_normalize_row(row) for row in df.to_dict("records")]


def upsert_profile_rows(
    db: Session, symbol: str, market: str, dataset: str, rows: List[Dict[str, Any]]
) -> int:
    """原子 upsert（ON CONFLICT DO UPDATE）；返回新增行数（xmax=0 判定）。"""
    spec = DATASETS[dataset]
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


def sync_symbol_profile(db: Session, symbol: str, market: str) -> Dict[str, Any]:
    """单标的全数据集同步；单数据集失败记录不中断（配额错误会逐集快速失败）。"""
    if market not in SUPPORTED_MARKETS:
        return {
            "symbol": symbol, "market": market, "supported": False,
            "datasets": {}, "failed": [],
        }
    result: Dict[str, Any] = {
        "symbol": symbol, "market": market, "supported": True,
        "datasets": {}, "failed": [],
    }
    for dataset in DATASETS:
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
}


def load_symbol_profile(
    db: Session, symbol: str, market: str, *, caps: Optional[Dict[str, int]] = None
) -> Dict[str, Any]:
    """按数据集分组读取（period_key 倒序、逐集封顶），附数据截止信息。"""
    caps = caps or PROFILE_CAPS
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    latest_fetch: Optional[datetime] = None
    for dataset in DATASETS:
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
                DATASETS[dataset]["key"](rows[0]) if rows else None
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
    return latest.date() if latest else None
