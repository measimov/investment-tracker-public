"""官方公告同步编排（#306）：跟踪范围逐标的增量拉取 → 分类 → 幂等入库。

- 范围：`security_industry_service.scope_keys`（活跃用户持仓数量>0 ∪ 自选，A/B/港/美）。
- 水位：`scheduled_task_state` 的 announcement_sync.detail["keys"][symbol|market] =
  {"last_ok": 业务日} 或 {"unsupported": 原因, "checked": 业务日}，**只在该标的成功时推进**，
  每只标的成功后立即用 JSONB 合并单独写入该键（不在结束时整体回写——周期任务与手工命令
  并发时谁后结束谁覆盖对方，进程中途被杀则本轮水位全丢，PR #309 评审 P3）。unsupported
  只在来源映射表确认查无此码时记，UNSUPPORTED_RECHECK_DAYS 天后重新探测（新上市/新建档）；增量窗口
  [max(水位−2 天, 今天−30 天), 今天]（回看 2 天兜交易所补发/更正），无水位 = 首次回溯
  ANNOUNCEMENT_BACKFILL_DAYS 天。周期线程串行，单 tick 首次回溯最多 BACKFILL_PER_TICK 只，
  其余下个 tick 继续（首次上线建议先跑 `manage.py sync-announcements`）。
- 入库：(symbol, market, source, source_id) 唯一（B 股与对应 A 股、GOOG/GOOGL 共用公告 ID），ON CONFLICT DO NOTHING；分类/分组由纯函数给出，
  规则升版用 reclassify_announcements 零外呼重算。
"""

from __future__ import annotations

import json

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..config import settings
from ..core.logging import get_app_logger
from ..core.timeutil import local_today, to_local_date
from ..database import SessionLocal
from ..models.security_announcement import SecurityAnnouncement
from . import announcement_sources, scheduled_state
from .announcement_classifier import (
    ANNOUNCEMENT_CLASSIFIER_VERSION,
    classify,
    group_key,
)
from .job_worker import PeriodicOutcome, periodic_outcome_task

logger = get_app_logger(__name__)

TASK_NAME = "announcement_sync"
PERIODIC_INTERVAL_SECONDS = 1800
INCREMENTAL_LOOKBACK_DAYS = 2
INCREMENTAL_MAX_DAYS = 30
BACKFILL_PER_TICK = 5
UNSUPPORTED_RECHECK_DAYS = 7
SUPPORTED_MARKETS = ("A股", "B股", "港股", "美股")

Key = Tuple[str, str]


def _key_str(key: Key) -> str:
    return f"{key[0]}|{key[1]}"


def to_rows(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """来源记录 → 表行（补业务日、分类、分组；纯函数）。"""
    rows = []
    for record in records:
        ann_day = to_local_date(record["published_at"])
        result = classify(record["market"], record["title"], record.get("category_raw") or "")
        rows.append(
            {
                **record,
                "ann_date": ann_day,
                "category": result.category,
                "importance": result.importance,
                "rule_id": result.rule_id,
                "classifier_version": ANNOUNCEMENT_CLASSIFIER_VERSION,
                "group_key": group_key(
                    record["symbol"], record["market"], ann_day.isoformat(), result.category
                ),
            }
        )
    return rows


def store_records(db: Session, records: Sequence[Dict[str, Any]]) -> int:
    """幂等写入，返回新增行数（不提交）。"""
    rows = to_rows(records)
    if not rows:
        return 0
    stmt = (
        insert(SecurityAnnouncement)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_security_announcements_symbol_source_id")
        .returning(SecurityAnnouncement.id)
    )
    return len(db.execute(stmt).fetchall())


_WRITE_KEY_SQL = text(
    """
    INSERT INTO scheduled_task_state (name, detail)
    VALUES (:name, jsonb_build_object('keys', jsonb_build_object(:key, CAST(:entry AS jsonb))))
    ON CONFLICT (name) DO UPDATE SET detail = jsonb_set(
        scheduled_task_state.detail,
        '{keys}',
        COALESCE(scheduled_task_state.detail -> 'keys', '{}'::jsonb)
            || jsonb_build_object(:key, CAST(:entry AS jsonb))
    )
    """
)

_RECORD_RUN_SQL = text(
    """
    INSERT INTO scheduled_task_state (name, last_run_at, last_success_at, detail)
    VALUES (:name, :now, CASE WHEN :success THEN CAST(:now AS timestamptz) END,
            jsonb_build_object('last_summary', CAST(:summary AS jsonb)))
    ON CONFLICT (name) DO UPDATE SET
        last_run_at = EXCLUDED.last_run_at,
        last_success_at = COALESCE(EXCLUDED.last_success_at, scheduled_task_state.last_success_at),
        detail = scheduled_task_state.detail || EXCLUDED.detail
    """
)


def _write_key(db: Session, key: Key, entry: Dict[str, Any]) -> None:
    """单独合并写入一只标的的水位（不提交；与该标的的公告行同一事务）。"""
    db.execute(
        _WRITE_KEY_SQL,
        {"name": TASK_NAME, "key": _key_str(key), "entry": json.dumps(entry, ensure_ascii=False)},
    )


def _record_run(db: Session, *, success: bool, summary: Dict[str, Any]) -> None:
    db.execute(
        _RECORD_RUN_SQL,
        {
            "name": TASK_NAME,
            "now": datetime.now(timezone.utc),
            "success": success,
            "summary": json.dumps(summary, ensure_ascii=False, default=str),
        },
    )
    db.commit()
    db.expire_all()


def _unsupported_fresh(entry: Dict[str, Any], today: date) -> bool:
    """unsupported 标记仍在复查期内（旧格式无 checked 视为已过期，重新探测）。"""
    checked = entry.get("checked")
    if not entry.get("unsupported") or not checked:
        return False
    try:
        return (today - date.fromisoformat(checked)).days < UNSUPPORTED_RECHECK_DAYS
    except ValueError:
        return False


def sync_window(
    key: Key, detail_entry: Optional[Dict[str, Any]], today: date, backfill_days: int
) -> Tuple[date, date]:
    last_ok = (detail_entry or {}).get("last_ok")
    if last_ok:
        start = max(
            date.fromisoformat(last_ok) - timedelta(days=INCREMENTAL_LOOKBACK_DAYS),
            today - timedelta(days=INCREMENTAL_MAX_DAYS),
        )
    else:
        start = today - timedelta(days=backfill_days)
    return start, today


def sync_announcements(
    db: Session,
    *,
    keys: Optional[Sequence[Key]] = None,
    backfill_days: Optional[int] = None,
    force_window_days: Optional[int] = None,
    max_backfill: Optional[int] = None,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """逐标的同步；返回 {synced, new, failed[], unsupported[], deferred}。

    force_window_days：手动命令指定回看天数（忽略水位）；max_backfill：本轮最多首次回溯几只。"""
    from .security_industry_service import scope_keys

    today = today or local_today()
    backfill_days = backfill_days or settings.announcement_backfill_days
    targets = [
        key for key in (keys if keys is not None else scope_keys(db)) if key[1] in SUPPORTED_MARKETS
    ]
    state = scheduled_state.get_detail(db, TASK_NAME)
    entries: Dict[str, Any] = dict(state.get("keys") or {})
    summary: Dict[str, Any] = {
        "synced": 0,
        "new": 0,
        "failed": [],
        "unsupported": [],
        "deferred": 0,
    }
    backfilled = 0
    for key in targets:
        entry = entries.get(_key_str(key)) or {}
        if force_window_days is None and _unsupported_fresh(entry, today):
            summary["unsupported"].append(_key_str(key))
            continue
        if force_window_days is not None:
            start, end = today - timedelta(days=force_window_days), today
        else:
            if not entry.get("last_ok"):
                if max_backfill is not None and backfilled >= max_backfill:
                    summary["deferred"] += 1
                    continue
                backfilled += 1
            start, end = sync_window(key, entry, today, backfill_days)
        try:
            records = announcement_sources.fetch_announcements(key[0], key[1], start, end)
            new = store_records(db, records)
            _write_key(db, key, {"last_ok": today.isoformat()})
            db.commit()
        except announcement_sources.UnsupportedSymbol as exc:
            db.rollback()
            _write_key(db, key, {"unsupported": str(exc)[:200], "checked": today.isoformat()})
            db.commit()
            summary["unsupported"].append(_key_str(key))
            continue
        except Exception as exc:  # noqa: BLE001 - 单只失败不影响其余标的，水位不推进
            db.rollback()
            logger.warning("公告同步失败 %s: %s", _key_str(key), str(exc)[:200])
            summary["failed"].append({"key": _key_str(key), "error": str(exc)[:200]})
            continue
        summary["synced"] += 1
        summary["new"] += new
    _record_run(
        db,
        success=not summary["failed"],
        summary={k: v for k, v in summary.items() if k != "unsupported"},
    )
    return summary


def sync_status(db: Session, symbol: str, market: str) -> Dict[str, Any]:
    """单只标的的同步状态（只读水位，不外呼）：synced / unsupported / pending（尚未同步——
    不在任何用户的持仓∪自选里，或首次回溯还没轮到）。"""
    if market not in SUPPORTED_MARKETS:
        return {"status": "unsupported", "last_synced": None, "reason": f"{market} 无官方公告源"}
    entry = (scheduled_state.get_detail(db, TASK_NAME).get("keys") or {}).get(
        _key_str((symbol, market))
    ) or {}
    if entry.get("unsupported"):
        return {"status": "unsupported", "last_synced": None, "reason": entry["unsupported"]}
    if entry.get("last_ok"):
        return {
            "status": "synced",
            "last_synced": date.fromisoformat(entry["last_ok"]),
            "reason": None,
        }
    return {"status": "pending", "last_synced": None, "reason": None}


def reclassify_announcements(db: Session, *, all_rows: bool = False) -> int:
    """分类器升版后零外呼重算 category/importance/rule_id/group_key；返回更新行数。"""
    query = db.query(SecurityAnnouncement)
    if not all_rows:
        query = query.filter(
            SecurityAnnouncement.classifier_version < ANNOUNCEMENT_CLASSIFIER_VERSION
        )
    updated = 0
    for row in query.yield_per(500):
        result = classify(row.market, row.title, row.category_raw or "")
        row.category = result.category
        row.importance = result.importance
        row.rule_id = result.rule_id
        row.classifier_version = ANNOUNCEMENT_CLASSIFIER_VERSION
        row.group_key = group_key(row.symbol, row.market, row.ann_date.isoformat(), result.category)
        updated += 1
    db.commit()
    return updated


@periodic_outcome_task
def periodic_sync_announcements() -> PeriodicOutcome:
    if not settings.announcement_sync_enabled:
        return PeriodicOutcome.skipped("announcement_sync_enabled=false")
    db = SessionLocal()
    try:
        summary = sync_announcements(db, max_backfill=BACKFILL_PER_TICK)
    finally:
        db.close()
    if summary["failed"]:
        failed = ", ".join(item["key"] for item in summary["failed"][:5])
        return PeriodicOutcome.failed(
            f"{len(summary['failed'])} 只标的公告同步失败：{failed}", count=summary["synced"]
        )
    if not summary["synced"]:
        return PeriodicOutcome.skipped("无可同步标的", count=0)
    return PeriodicOutcome.succeeded(
        count=summary["new"], reason=f"同步 {summary['synced']} 只，新增 {summary['new']} 条"
    )


def utcnow() -> datetime:  # 测试可替换
    return datetime.now(timezone.utc)
