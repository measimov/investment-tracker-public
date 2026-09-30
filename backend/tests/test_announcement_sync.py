"""公告同步编排：幂等入库、水位只在成功时推进、首次回溯限量、不支持的标的跳过、重分类、
周期入口契约。外呼（announcement_sources.fetch_announcements）全部打桩。"""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.config import settings
from app.database import SessionLocal
from app.models.scheduled_task_state import ScheduledTaskState
from app.models.security_announcement import SecurityAnnouncement
from app.services import announcement_sources, announcement_sync, scheduled_state
from app.services.announcement_classifier import ANNOUNCEMENT_CLASSIFIER_VERSION

from .helpers import reset_tables

TODAY = date(2026, 9, 29)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        reset_tables(session, [SecurityAnnouncement, ScheduledTaskState])
        yield session
        session.rollback()
        reset_tables(session, [SecurityAnnouncement, ScheduledTaskState])
    finally:
        session.close()


def _record(symbol, market, source_id, title, *, day=TODAY, category_raw=""):
    return {
        "symbol": symbol,
        "market": market,
        "source": "cninfo",
        "source_id": source_id,
        "published_at": datetime(day.year, day.month, day.day, 10, tzinfo=timezone.utc),
        "title": title,
        "url": "",
        "category_raw": category_raw,
        "payload": {},
    }


def test_store_records_is_idempotent_and_classifies(db):
    records = [
        _record("600298", "A股", "1", "安琪酵母股份有限公司向不特定对象发行可转换公司债券预案"),
        _record("600298", "A股", "2", "关于召开2026年第四次临时股东会的通知"),
    ]
    assert announcement_sync.store_records(db, records) == 2
    db.commit()
    assert announcement_sync.store_records(db, records) == 0
    db.commit()
    rows = {r.source_id: r for r in db.query(SecurityAnnouncement).all()}
    assert (rows["1"].category, rows["1"].importance) == ("financing", "major")
    assert rows["1"].group_key == f"600298|A股|{TODAY.isoformat()}|financing"
    assert rows["1"].classifier_version == ANNOUNCEMENT_CLASSIFIER_VERSION
    assert rows["2"].importance == "minor"


def test_watermark_advances_only_on_success(db, monkeypatch):
    calls = []

    def fake_fetch(symbol, market, start, end):
        calls.append((symbol, start, end))
        if symbol == "00700":
            raise RuntimeError("披露易 502")
        if symbol == "159307":
            raise announcement_sources.UnsupportedSymbol("ETF 无 orgId")
        return [
            _record(symbol, market, f"{symbol}-1", "关于回购公司股份方案的公告暨回购股份报告书")
        ]

    monkeypatch.setattr(announcement_sources, "fetch_announcements", fake_fetch)
    keys = [("600298", "A股"), ("00700", "港股"), ("159307", "A股")]
    summary = announcement_sync.sync_announcements(db, keys=keys, backfill_days=365, today=TODAY)
    assert summary["synced"] == 1 and summary["new"] == 1
    assert [f["key"] for f in summary["failed"]] == ["00700|港股"]
    assert summary["unsupported"] == ["159307|A股"]
    # 首次同步回溯 backfill_days
    assert calls[0] == ("600298", TODAY - timedelta(days=365), TODAY)
    entries = scheduled_state.get_detail(db, announcement_sync.TASK_NAME)["keys"]
    assert entries["600298|A股"] == {"last_ok": TODAY.isoformat()}
    assert "00700|港股" not in entries  # 失败不推进水位（下个 tick 仍按首次回溯）
    assert (
        entries["159307|A股"]["unsupported"]
        and entries["159307|A股"]["checked"] == TODAY.isoformat()
    )

    # 次日：600298 增量回看 2 天；00700 仍首次回溯；ETF 不再外呼
    calls.clear()
    announcement_sync.sync_announcements(
        db, keys=keys, backfill_days=365, today=TODAY + timedelta(days=1)
    )
    by_symbol = {c[0]: c for c in calls}
    assert by_symbol["600298"][1] == TODAY - timedelta(days=2)
    assert by_symbol["00700"][1] == TODAY + timedelta(days=1) - timedelta(days=365)
    assert "159307" not in by_symbol


def test_incremental_window_capped_and_backfill_deferred(db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        announcement_sources,
        "fetch_announcements",
        lambda s, m, a, b: calls.append((s, a)) or [],
    )
    scheduled_state.mark_ran(
        db,
        announcement_sync.TASK_NAME,
        detail={"keys": {"600298|A股": {"last_ok": (TODAY - timedelta(days=90)).isoformat()}}},
    )
    keys = [("600298", "A股"), ("000651", "A股"), ("600690", "A股")]
    summary = announcement_sync.sync_announcements(db, keys=keys, max_backfill=1, today=TODAY)
    assert calls[0] == ("600298", TODAY - timedelta(days=announcement_sync.INCREMENTAL_MAX_DAYS))
    assert [c[0] for c in calls] == ["600298", "000651"]
    assert summary["deferred"] == 1


def test_reclassify_updates_stale_rows(db):
    announcement_sync.store_records(db, [_record("600298", "A股", "9", "2026年半年度业绩预告")])
    db.query(SecurityAnnouncement).update(
        {"classifier_version": 0, "category": "other", "importance": "minor"}
    )
    db.commit()
    assert announcement_sync.reclassify_announcements(db) == 1
    row = db.query(SecurityAnnouncement).one()
    assert (row.category, row.importance) == ("earnings_alert", "major")
    assert announcement_sync.reclassify_announcements(db) == 0


def test_periodic_entry_contract(db, monkeypatch):
    monkeypatch.setattr(settings, "announcement_sync_enabled", False)
    assert announcement_sync.periodic_sync_announcements().status == "skipped"
    monkeypatch.setattr(settings, "announcement_sync_enabled", True)
    monkeypatch.setattr(
        announcement_sync,
        "sync_announcements",
        lambda db_, **kw: {
            "synced": 3,
            "new": 2,
            "failed": [{"key": "00700|港股", "error": "x"}],
            "unsupported": [],
            "deferred": 0,
        },
    )
    outcome = announcement_sync.periodic_sync_announcements()
    assert outcome.status == "failed" and "00700|港股" in outcome.reason
    monkeypatch.setattr(
        announcement_sync,
        "sync_announcements",
        lambda db_, **kw: {"synced": 3, "new": 2, "failed": [], "unsupported": [], "deferred": 0},
    )
    assert announcement_sync.periodic_sync_announcements().status == "succeeded"
    assert getattr(
        announcement_sync.periodic_sync_announcements, "periodic_outcome_contract", False
    )


def test_unsupported_is_rechecked_after_ttl_and_legacy_markers_reprobe(db, monkeypatch):
    """PR #309 评审 P2-1：unsupported 带检查日期，满 UNSUPPORTED_RECHECK_DAYS 天重新探测；
    旧格式（无 checked）视为过期。"""
    calls = []
    monkeypatch.setattr(
        announcement_sources,
        "fetch_announcements",
        lambda s, m, a, b: calls.append(s) or [],
    )
    scheduled_state.mark_ran(
        db,
        announcement_sync.TASK_NAME,
        detail={
            "keys": {
                "159307|A股": {
                    "unsupported": "ETF",
                    "checked": (TODAY - timedelta(days=3)).isoformat(),
                },
                "510300|A股": {
                    "unsupported": "ETF",
                    "checked": (TODAY - timedelta(days=7)).isoformat(),
                },
                "000001|A股": {"unsupported": "旧格式"},
            }
        },
    )
    keys = [("159307", "A股"), ("510300", "A股"), ("000001", "A股")]
    summary = announcement_sync.sync_announcements(db, keys=keys, today=TODAY)
    assert calls == ["510300", "000001"]
    assert summary["unsupported"] == ["159307|A股"]
    entries = scheduled_state.get_detail(db, announcement_sync.TASK_NAME)["keys"]
    assert entries["510300|A股"] == {"last_ok": TODAY.isoformat()}


def test_watermarks_are_merged_per_key_not_overwritten(db, monkeypatch):
    """PR #309 评审 P3：另一进程在本轮进行中写入的水位不得被本轮结束时覆盖。"""

    def fake_fetch(symbol, market, start, end):
        if symbol == "600298":
            # 模拟并发的手工命令：在本轮读完水位之后写入另一只标的
            other = SessionLocal()
            try:
                announcement_sync._write_key(other, ("00700", "港股"), {"last_ok": "2026-09-28"})
                other.commit()
            finally:
                other.close()
        return []

    monkeypatch.setattr(announcement_sources, "fetch_announcements", fake_fetch)
    announcement_sync.sync_announcements(db, keys=[("600298", "A股")], today=TODAY)
    detail = scheduled_state.get_detail(db, announcement_sync.TASK_NAME)
    assert detail["keys"]["00700|港股"] == {"last_ok": "2026-09-28"}
    assert detail["keys"]["600298|A股"] == {"last_ok": TODAY.isoformat()}
    assert detail["last_summary"]["synced"] == 1
    state = scheduled_state.get_state(db, announcement_sync.TASK_NAME)
    assert state.last_run_at is not None and state.last_success_at is not None


def test_same_source_id_is_stored_for_each_security(db):
    """PR #309 评审 P2-2：B 股按对应 A 股检索、GOOG/GOOGL 共用 accession——同一来源 ID
    必须能分别落在两只标的下。"""
    shared = [
        _record(sym, mkt, "1224567890", "关于回购公司股份方案的公告暨回购股份报告书")
        for sym, mkt in (("600845", "A股"), ("900926", "B股"))
    ]
    assert announcement_sync.store_records(db, shared) == 2
    db.commit()
    assert announcement_sync.store_records(db, shared) == 0
    assert {r.symbol for r in db.query(SecurityAnnouncement).all()} == {"600845", "900926"}
