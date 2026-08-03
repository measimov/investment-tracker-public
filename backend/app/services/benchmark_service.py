"""基准指数目录与同步管线。

目录是产品目录（非账本事实，不入 security_rules）；指数日线复用
security_prices 表存储（market="指数"），免费获得增量抓取/缺口/upsert
管线。"指数"不在交易市场枚举内，持仓统计按用户交易键集合取价，不会
撞上指数行。三个接口已用真实 token 实测连通（2026-08-02）。
"""

from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..core.logging import get_app_logger
from ..database import SessionLocal
from ..models.security_price import SecurityPrice

logger = get_app_logger(__name__)

BENCHMARK_MARKET = "指数"

# code → 路由与展示元数据；calendar_market 决定尾部钳制用哪个交易日历
BENCHMARKS: Dict[str, Dict[str, str]] = {
    "000300.SH": {
        "name": "沪深300",
        "api": "index_daily",
        "currency": "CNY",
        "calendar_market": "A股",
    },
    "HSI": {
        "name": "恒生指数",
        "api": "index_global",
        "currency": "HKD",
        "calendar_market": "港股",
    },
    "SPX": {
        "name": "标普500",
        "api": "index_global",
        "currency": "USD",
        "calendar_market": "美股",
    },
}

# 周期补尾间隔（对照汇率 6h 先例；指数日线一天一根，24h 足够）
PERIODIC_INTERVAL_SECONDS = 24 * 3600


def benchmark_catalog() -> List[Dict[str, str]]:
    """前端选择器目录。"""
    return [
        {"code": code, "name": meta["name"], "currency": meta["currency"]}
        for code, meta in BENCHMARKS.items()
    ]


def is_valid_benchmark(code: str) -> bool:
    return code in BENCHMARKS


def sync_benchmark_history(
    db: Session, code: str, start_date: date, end_date: date
) -> Dict[str, Any]:
    """单基准增量同步（复用证券历史管线；尾部按目录日历市场钳制）。"""
    from .market_data_service import fetch_and_store_security_price_history_incremental

    meta = BENCHMARKS[code]
    return fetch_and_store_security_price_history_incremental(
        db,
        symbol=code,
        market=BENCHMARK_MARKET,
        start_date=start_date,
        end_date=end_date,
        currency=meta["currency"],
        calendar_market=meta["calendar_market"],
    )


def benchmark_targets(start_date: date, end_date: date) -> List[Dict[str, Any]]:
    """历史同步 job 的顺风车目标（形状与证券 target 一致 + calendar_market）。

    基准是全局数据：多用户重复跑由增量判重挡住（全覆盖时零外呼）。
    """
    return [
        {
            "symbol": code,
            "market": BENCHMARK_MARKET,
            "currency": meta["currency"],
            "calendar_market": meta["calendar_market"],
            "start_date": start_date,
            "end_date": end_date,
        }
        for code, meta in BENCHMARKS.items()
    ]


def refresh_benchmark_tails() -> int:
    """周期补尾：已有数据的基准把尾部推进到最近已完成交易日。

    冷启动（无任何数据）不主动回填全历史——首轮回填由用户区间驱动
    （history-sync job / analytics refresh_history），这里只维护日常尾部。
    无 token 时 fetch 内部失败并被记录，返回成功计数。
    """
    import os

    from ..config import settings

    if not (os.environ.get("TUSHARE_TOKEN") or settings.tushare_token):
        return 0

    refreshed = 0
    db = SessionLocal()
    try:
        for code in BENCHMARKS:
            coverage_start = (
                db.query(SecurityPrice.price_date)
                .filter(
                    SecurityPrice.symbol == code,
                    SecurityPrice.market == BENCHMARK_MARKET,
                )
                .order_by(SecurityPrice.price_date.asc())
                .first()
            )
            if coverage_start is None:
                continue  # 冷启动：等首轮用户区间回填
            result = sync_benchmark_history(
                db, code, coverage_start[0], date.today()
            )
            if result.get("success"):
                refreshed += 1
            else:
                logger.warning(
                    "基准 %s 周期补尾失败: %s", code, result.get("error")
                )
    finally:
        db.close()
    return refreshed


def load_benchmark_closes(
    db: Session, code: str, start_date: date, end_date: date
) -> Dict[date, Decimal]:
    """加载区间收盘价 + 起点前最近一行（供内核基点归一）。"""
    rows = (
        db.query(SecurityPrice.price_date, SecurityPrice.close_price)
        .filter(
            SecurityPrice.symbol == code,
            SecurityPrice.market == BENCHMARK_MARKET,
            SecurityPrice.price_date >= start_date,
            SecurityPrice.price_date <= end_date,
        )
        .all()
    )
    closes = {price_date: Decimal(str(close)) for price_date, close in rows if close}
    anchor = (
        db.query(SecurityPrice.price_date, SecurityPrice.close_price)
        .filter(
            SecurityPrice.symbol == code,
            SecurityPrice.market == BENCHMARK_MARKET,
            SecurityPrice.price_date < start_date,
        )
        .order_by(SecurityPrice.price_date.desc())
        .first()
    )
    if anchor and anchor[1]:
        closes[anchor[0]] = Decimal(str(anchor[1]))
    return closes


def resolve_benchmark_history_api(code: str) -> Optional[Dict[str, str]]:
    """market_data_service 路由分支的目录查询（避免双向 import）。"""
    meta = BENCHMARKS.get(code)
    if meta is None:
        return None
    return {"api": meta["api"], "adjust_api": "", "ts_code": code}
