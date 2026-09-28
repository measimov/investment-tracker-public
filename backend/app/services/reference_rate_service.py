"""参考利率日序列（#200）：无风险利率的采集、存储与读取。

- `SHIBOR_3M`：中国货币网 SHIBOR 3M（官方、免 token）——账本本币是 CNY，夏普/索提诺
  默认用它（`settings.risk_free_series`）；
- `UST_3M`：美国财政部 13 周国库券息票等价收益率——只采集与展示，不参与本币指标。
  （HKMA 的 HIBOR 开放 API 在接入时持续 502，未接；恢复后按同一形状加一个序列即可。）

同步口径：首次按全体用户最早交易日（再往前 15 天，留出向前填充的前值）回填，之后每次
只补尾部（最近一期往前 10 天到今天）；最早值晚于目标起点时补前段。按日期 upsert，幂等。
读取方拿到的是「发布日 → 年化 %」，非发布日由内核向前填充。
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..config import settings
from ..core.logging import get_app_logger
from ..core.timeutil import local_today
from ..models.reference_rate import ReferenceRate
from ..models.transaction import Transaction
from . import chinamoney_source, treasury_source
from .job_worker import PeriodicOutcome, periodic_outcome_task

logger = get_app_logger(__name__)

TAIL_LOOKBACK_DAYS = 10
HISTORY_PADDING_DAYS = 15
DEFAULT_HISTORY_DAYS = 400
PERIODIC_INTERVAL_SECONDS = 12 * 3600


@dataclass(frozen=True)
class SeriesSpec:
    label: str
    currency: str
    source: str
    fetch: Callable[[date, date], List[Tuple[date, Decimal]]]


SERIES: Dict[str, SeriesSpec] = {
    "SHIBOR_3M": SeriesSpec(
        label="SHIBOR 3M",
        currency="CNY",
        source=chinamoney_source.SOURCE_SHIBOR,
        fetch=lambda start, end: chinamoney_source.fetch_shibor_history(start, end, "3M"),
    ),
    "UST_3M": SeriesSpec(
        label="美国国库券 3M",
        currency="USD",
        source=treasury_source.SOURCE,
        fetch=treasury_source.fetch_bill_rates,
    ),
}


def history_start(db: Session, today: Optional[date] = None) -> date:
    """回填起点：全体用户最早交易日往前 15 天；没有交易则最近 400 天。"""
    today = today or local_today()
    earliest = db.query(func.min(Transaction.transaction_date)).scalar()
    if earliest is None:
        return today - timedelta(days=DEFAULT_HISTORY_DAYS)
    if hasattr(earliest, "date"):
        earliest = earliest.date()
    return earliest - timedelta(days=HISTORY_PADDING_DAYS)


def upsert_points(db: Session, series: str, source: str, points) -> int:
    rows = [
        {"series": series, "rate_date": rate_date, "value": value, "source": source}
        for rate_date, value in points
    ]
    if not rows:
        return 0
    statement = pg_insert(ReferenceRate).values(rows)
    statement = statement.on_conflict_do_update(
        constraint="uq_reference_rates_series_date",
        set_={"value": statement.excluded.value, "source": statement.excluded.source},
        where=ReferenceRate.value.is_distinct_from(statement.excluded.value),
    )
    result = db.execute(statement)
    db.commit()
    return result.rowcount or 0


def sync_series(
    db: Session, series: str, *, start: Optional[date] = None, today: Optional[date] = None
) -> Dict[str, object]:
    """补齐一个序列：缺前段则回填，再补尾部。返回 {series, written, ranges, error}。"""
    spec = SERIES[series]
    today = today or local_today()
    target_start = start or history_start(db, today)
    earliest, latest = db.query(
        func.min(ReferenceRate.rate_date), func.max(ReferenceRate.rate_date)
    ).filter(ReferenceRate.series == series).one()

    ranges: List[Tuple[date, date]] = []
    if earliest is None:
        ranges.append((target_start, today))
    else:
        if target_start < earliest:
            ranges.append((target_start, earliest - timedelta(days=1)))
        ranges.append((max(latest - timedelta(days=TAIL_LOOKBACK_DAYS), target_start), today))

    written = 0
    error = None
    for range_start, range_end in ranges:
        if range_start > range_end:
            continue
        try:
            points = spec.fetch(range_start, range_end)
        except Exception as exc:  # noqa: BLE001 - 单序列失败只记录，不影响其他序列
            error = f"{spec.label} 获取失败：{exc}"
            logger.warning("%s", error)
            break
        written += upsert_points(db, series, spec.source, points)
    return {
        "series": series,
        "written": written,
        "ranges": [(a.isoformat(), b.isoformat()) for a, b in ranges],
        "error": error,
    }


@periodic_outcome_task
def periodic_refresh_reference_rates() -> PeriodicOutcome:
    """周期任务入口（main.py 以名字 refresh_reference_rates 注册）：逐序列补齐（失败互不影响），
    任一序列获取失败 → failed（sync_series 只把错误放进 outcome，不上抛）。"""
    if not settings.reference_rate_sync_enabled:
        return PeriodicOutcome.skipped("REFERENCE_RATE_SYNC_ENABLED=false")
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        total = 0
        errors = []
        for series in SERIES:
            outcome = sync_series(db, series)
            total += int(outcome["written"])
            if outcome.get("error"):
                errors.append(str(outcome["error"])[:200])
        if total:
            logger.info("参考利率已同步: %s 行", total)
        if errors:
            return PeriodicOutcome.failed("；".join(errors), count=total)
        return PeriodicOutcome.succeeded(total)
    finally:
        db.close()


def refresh_reference_rates() -> int:
    """兼容入口：返回写入/改写行数。"""
    return periodic_refresh_reference_rates().count


def load_points(
    db: Session, series: str, start: date, end: date
) -> List[Tuple[date, Decimal]]:
    """[start, end] 内的发布值，外加 start 之前最近的一个（供向前填充区间首日）。"""
    prior = (
        db.query(ReferenceRate.rate_date, ReferenceRate.value)
        .filter(ReferenceRate.series == series, ReferenceRate.rate_date < start)
        .order_by(ReferenceRate.rate_date.desc())
        .first()
    )
    rows = (
        db.query(ReferenceRate.rate_date, ReferenceRate.value)
        .filter(
            ReferenceRate.series == series,
            ReferenceRate.rate_date >= start,
            ReferenceRate.rate_date <= end,
        )
        .order_by(ReferenceRate.rate_date)
        .all()
    )
    points = [(row[0], Decimal(str(row[1]))) for row in rows]
    if prior is not None:
        points.insert(0, (prior[0], Decimal(str(prior[1]))))
    return points


def latest_values(db: Session) -> List[Dict[str, object]]:
    """每个序列的最近一期（展示用）。"""
    result = []
    for series, spec in SERIES.items():
        row = (
            db.query(ReferenceRate)
            .filter(ReferenceRate.series == series)
            .order_by(ReferenceRate.rate_date.desc())
            .first()
        )
        result.append(
            {
                "series": series,
                "label": spec.label,
                "currency": spec.currency,
                "rate_date": row.rate_date if row else None,
                "value": Decimal(str(row.value)) if row else None,
                "source": row.source if row else None,
            }
        )
    return result
