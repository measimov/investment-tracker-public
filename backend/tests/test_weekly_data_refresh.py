"""每周数据刷新：weekly 模式的批量 job、newest_only 摘要、周期入队调度、持仓页分析过期判据。

全部 monkeypatch 外呼（档案同步/ADS/摘要/报表抽取/观点批量），不触发真实网络与 LLM。
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.background_job import BackgroundJob
from app.models.holding import Holding
from app.models.scheduled_task_state import ScheduledTaskState
from app.models.security_profile import SecurityAnalysis, SecurityProfileData
from app.models.user import User
from app.models.watchlist_item import WatchlistItem
from app.services import report_digest_batch_jobs as batch
from app.services import report_digest_service as digest_service
from app.services import weekly_data_refresh as weekly
from app.services.job_worker import PERIODIC_SKIPPED, PERIODIC_SUCCEEDED
from app.services.report_digest_service import (
    DIGEST_PROMPT_VERSION,
    SECTION_EXTRACTOR_VERSION,
    _newest_of_each_kind,
)
from app.services.profile_store import upsert_profile_row

from .helpers import reset_tables

JOB_TYPES = [
    "report_digest_batch",
    "security_analysis_batch",
    "security_analysis",
    "report_digest_backfill",
    "opinion_summary",
    "opinion_summary_batch",
]
RESET_MODELS = [SecurityAnalysis, SecurityProfileData, WatchlistItem, Holding, ScheduledTaskState]


def _clean(session):
    reset_tables(session, RESET_MODELS)
    session.query(BackgroundJob).filter(BackgroundJob.job_type.in_(JOB_TYPES)).delete(
        synchronize_session=False
    )
    session.commit()


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        _clean(session)
        yield session
        session.rollback()
        _clean(session)
    finally:
        session.close()


def _hold(db, symbol, market, user_id=1):
    db.add(
        Holding(
            user_id=user_id,
            symbol=symbol,
            name=symbol,
            market=market,
            quantity=Decimal("100"),
            avg_cost=Decimal("10"),
            total_cost=Decimal("1000"),
            currency="CNY",
        )
    )
    db.commit()


def _watch(db, symbol, market, user_id=1):
    db.add(WatchlistItem(user_id=user_id, symbol=symbol, market=market, name=symbol))
    db.commit()


def _ok(**overrides):
    base = {
        "total": 2,
        "completed": 1,
        "generated": 1,
        "attempted": 1,
        "failed": 0,
        "permanently_failed": 0,
        "plan_incomplete": False,
        "remaining": 0,
        "pending_periods": [],
        "gaps": [],
        "fatal": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# newest_only
# ---------------------------------------------------------------------------


def test_newest_of_each_kind_keeps_latest_annual_and_semi():
    targets = [
        {"period_key": "20250630|semi", "report_type": "semi", "end_date": "20250630"},
        {"period_key": "20241231|annual", "report_type": "annual", "end_date": "20241231"},
        {"period_key": "20231231|annual", "report_type": "annual", "end_date": "20231231"},
    ]
    assert [t["period_key"] for t in _newest_of_each_kind(targets)] == [
        "20250630|semi",
        "20241231|annual",
    ]


def test_newest_of_each_kind_treats_us_forms_as_one_annual_kind():
    """美股换过表单（10-K → 20-F）：按表单分组会把多年前的另一种表单也当「最新」补上。"""
    targets = [
        {"period_key": "20241231|20-F", "report_type": "20-F", "end_date": "20241231"},
        {"period_key": "20231231|20-F", "report_type": "20-F", "end_date": "20231231"},
        {"period_key": "20171231|10-K", "report_type": "10-K", "end_date": "20171231"},
    ]
    assert [t["period_key"] for t in _newest_of_each_kind(targets)] == ["20241231|20-F"]


def test_ensure_newest_only_does_not_deepen_into_older_years(db, monkeypatch):
    targets = [
        {
            "period_key": f"{year}1231|annual",
            "report_type": "annual",
            "end_date": f"{year}1231",
            "url": f"u{year}",
            "ann_date": f"{year + 1}0330",
        }
        for year in (2025, 2024, 2023, 2022)
    ]
    monkeypatch.setattr(
        digest_service,
        "cached_report_targets_detailed",
        lambda *a, **kw: {"targets": list(targets), "complete": True},
    )
    attempted = []

    def fake_section(db_, symbol, market, target):
        attempted.append(target["end_date"])
        return None  # 下载失败：只关心尝试了哪几份

    monkeypatch.setattr(digest_service, "_ensure_section", fake_section)
    result = digest_service.ensure_report_digests(db, "600036", "A股", max_new=4, newest_only=True)
    assert attempted == ["20251231"]
    assert result["total"] == 1
    assert result["remaining"] == 0

    attempted.clear()
    full = digest_service.ensure_report_digests(db, "600036", "A股", max_new=4)
    assert attempted == ["20251231", "20241231", "20231231", "20221231"]
    assert full["total"] == 4


# ---------------------------------------------------------------------------
# weekly 模式的批量 job
# ---------------------------------------------------------------------------


def _stub_weekly(monkeypatch, *, llm_ready=True):
    calls = {"profile": [], "ads": [], "digest": [], "statements": []}

    def fake_sync(db_, symbol, market):
        calls["profile"].append((symbol, market))
        core = batch.PROFILE_FRESHNESS_DATASET.get(market, "x")
        return {"supported": True, "datasets": {core: {}}, "failed": [], "skipped": []}

    def fake_ads(db_, symbol, force=False):
        calls["ads"].append(symbol)
        return {"status": "cached"}

    def fake_digest(db_, symbol, market, *, max_new, newest_only=False):
        calls["digest"].append({"symbol": symbol, "max_new": max_new, "newest_only": newest_only})
        return _ok()

    def fake_statements(db_, symbol, market, *, max_new):
        calls["statements"].append({"symbol": symbol, "max_new": max_new})
        return {
            "total": 0,
            "completed": 0,
            "generated": 0,
            "failed": 0,
            "permanently_failed": 0,
            "gaps": [],
            "fatal": None,
        }

    from app.services import ads_ratio_service

    monkeypatch.setattr(batch, "sync_symbol_profile", fake_sync)
    monkeypatch.setattr(ads_ratio_service, "ensure_ads_ratio", fake_ads)
    monkeypatch.setattr(batch, "ensure_report_digests", fake_digest)
    monkeypatch.setattr(batch, "ensure_report_statements", fake_statements)
    monkeypatch.setattr(batch, "is_llm_configured", lambda: llm_ready)
    monkeypatch.setattr(batch.settings, "security_analysis_batch_pause_seconds", 0)
    return calls


def test_weekly_job_targets_holdings_and_watchlist(db, monkeypatch):
    calls = _stub_weekly(monkeypatch)
    _hold(db, "600036", "A股")
    _watch(db, "00700", "港股")
    _watch(db, "PDD", "美股")
    _watch(db, "200596", "B股")  # 档案不支持的市场不进目标

    job = batch.start_weekly_refresh_job(db, 1)
    assert job["mode"] == batch.WEEKLY_MODE
    assert {(t["symbol"], t["market"]) for t in job["targets"]} == {
        ("600036", "A股"),
        ("00700", "港股"),
        ("PDD", "美股"),
    }
    batch.run_digest_batch_job(job["id"])
    done = batch.get_digest_batch_job(job["id"], 1)

    assert done["status"] == "succeeded"
    assert done["profiles_synced"] == 3
    assert sorted(calls["profile"]) == [("00700", "港股"), ("600036", "A股"), ("PDD", "美股")]
    assert calls["ads"] == ["PDD"]
    assert all(
        c["newest_only"] and c["max_new"] == batch.WEEKLY_DIGEST_MAX_NEW for c in calls["digest"]
    )
    assert calls["statements"] == [{"symbol": "00700", "max_new": batch.WEEKLY_STATEMENT_MAX_NEW}]
    by_symbol = {row["symbol"]: row for row in done["results"]}
    assert by_symbol["PDD"]["ads_ratio"] == "cached"
    assert by_symbol["600036"]["profile"]["status"] == "synced"


def test_weekly_job_skips_recently_synced_profiles(db, monkeypatch):
    calls = _stub_weekly(monkeypatch)
    _hold(db, "600036", "A股")
    _hold(db, "000001", "A股")
    upsert_profile_row(db, "600036", "A股", "fina_indicator", "20250630", {"x": 1})
    # daily_basic 每天刷新，不能让档案看起来「刚同步过」
    upsert_profile_row(db, "000001", "A股", "daily_basic", "20260928", {"x": 1})
    db.commit()

    job = batch.start_weekly_refresh_job(db, 1)
    batch.run_digest_batch_job(job["id"])
    done = batch.get_digest_batch_job(job["id"], 1)
    assert calls["profile"] == [("000001", "A股")]
    by_symbol = {row["symbol"]: row for row in done["results"]}
    assert by_symbol["600036"]["profile"]["status"] == "fresh"
    assert done["profiles_synced"] == 1


def test_weekly_job_without_llm_only_syncs_profiles(db, monkeypatch):
    calls = _stub_weekly(monkeypatch, llm_ready=False)
    _hold(db, "600036", "A股")
    _hold(db, "00700", "港股")

    job = batch.start_weekly_refresh_job(db, 1)
    batch.run_digest_batch_job(job["id"])
    done = batch.get_digest_batch_job(job["id"], 1)
    assert done["status"] == "succeeded"
    assert calls["digest"] == [] and calls["statements"] == []
    assert len(calls["profile"]) == 2
    assert done["success_count"] == 2


def test_weekly_profile_failure_does_not_block_digest(db, monkeypatch):
    calls = _stub_weekly(monkeypatch)

    def broken_sync(*args, **kwargs):
        raise RuntimeError("tushare down")

    monkeypatch.setattr(batch, "sync_symbol_profile", broken_sync)
    _hold(db, "600036", "A股")
    job = batch.start_weekly_refresh_job(db, 1)
    batch.run_digest_batch_job(job["id"])
    done = batch.get_digest_batch_job(job["id"], 1)
    # 摘要照常做完，但档案失败计入标的与任务终态（否则基本面静默一周不更新）
    assert len(calls["digest"]) == 1
    assert done["status"] == "failed"
    assert done["results"][0]["profile"]["status"] == "error"
    assert done["results"][0]["status"] == "failed"
    assert done["profiles_failed"] == 1 and done["success_count"] == 0


def test_weekly_all_profile_sources_failing_is_not_succeeded(db, monkeypatch):
    """档案源全部失败（返回值而非异常：supported 但一个数据集都没取到）不能得到 succeeded。"""
    _stub_weekly(monkeypatch)

    def empty_sync(db_, symbol, market):
        return {
            "supported": True,
            "datasets": {},
            "failed": [{"dataset": "fina_indicator", "error": "503"}],
            "skipped": [],
        }

    monkeypatch.setattr(batch, "sync_symbol_profile", empty_sync)
    _hold(db, "600036", "A股")
    _watch(db, "00700", "港股")
    job = batch.start_weekly_refresh_job(db, 1)
    batch.run_digest_batch_job(job["id"])
    done = batch.get_digest_batch_job(job["id"], 1)
    assert done["status"] == "failed"
    assert {row["profile"]["status"] for row in done["results"]} == {"failed"}
    assert done["profiles_failed"] == 2 and done["profiles_synced"] == 0


@pytest.mark.parametrize("core_outcome", ["failed", "skipped"])
def test_weekly_core_dataset_not_refreshed_fails_even_if_minor_ones_succeed(
    db, monkeypatch, core_outcome
):
    """核心数据集（fina_indicator）失败或因冷却跳过、只刷到 daily_basic：档案与任务都记失败。"""
    _stub_weekly(monkeypatch)

    def core_missing_sync(db_, symbol, market):
        entry = [{"dataset": "fina_indicator", "error": "Tushare 冷却中"}]
        return {
            "supported": True,
            "datasets": {"daily_basic": {}, "income": {}},
            "failed": entry if core_outcome == "failed" else [],
            "skipped": entry if core_outcome == "skipped" else [],
        }

    monkeypatch.setattr(batch, "sync_symbol_profile", core_missing_sync)
    _hold(db, "600036", "A股")
    job = batch.start_weekly_refresh_job(db, 1)
    batch.run_digest_batch_job(job["id"])
    done = batch.get_digest_batch_job(job["id"], 1)
    row = done["results"][0]
    assert row["profile"]["status"] == "failed"
    assert "fina_indicator" in row["profile"]["error"]
    assert row["status"] == "failed" and "fina_indicator" in row["error"]
    assert done["status"] == "failed" and done["profiles_failed"] == 1


def test_weekly_partial_dataset_failure_still_succeeds(db, monkeypatch):
    """个别数据集失败（其余已刷新）记 partial，不让整周任务失败。"""
    _stub_weekly(monkeypatch)

    def partial_sync(db_, symbol, market):
        return {
            "supported": True,
            "datasets": {"fina_indicator": {}},
            "failed": [{"dataset": "xueqiu_holders", "error": "cookie"}],
            "skipped": [{"dataset": "pledge_stat", "error": "冷却"}],
        }

    monkeypatch.setattr(batch, "sync_symbol_profile", partial_sync)
    _hold(db, "600036", "A股")
    job = batch.start_weekly_refresh_job(db, 1)
    batch.run_digest_batch_job(job["id"])
    done = batch.get_digest_batch_job(job["id"], 1)
    assert done["status"] == "succeeded"
    assert done["results"][0]["profile"]["status"] == "partial"


def test_weekly_fatal_llm_still_aborts(db, monkeypatch):
    calls = _stub_weekly(monkeypatch)
    monkeypatch.setattr(
        batch,
        "ensure_report_digests",
        lambda *a, **kw: _ok(generated=0, fatal={"kind": "llm_auth", "message": "Key 无效"}),
    )
    _hold(db, "600036", "A股")
    _hold(db, "000001", "A股")
    job = batch.start_weekly_refresh_job(db, 1)
    batch.run_digest_batch_job(job["id"])
    done = batch.get_digest_batch_job(job["id"], 1)
    assert done["status"] == "failed"
    assert len(calls["profile"]) == 1  # 第一只就中止


def test_manual_mode_is_unchanged(db, monkeypatch):
    calls = _stub_weekly(monkeypatch)
    _hold(db, "600036", "A股")
    _watch(db, "000001", "A股")  # 手动模式只看持仓
    job = batch.start_digest_batch_job(db, 1)
    assert job["mode"] is None
    batch.run_digest_batch_job(job["id"])
    assert calls["profile"] == []
    assert calls["digest"] == [
        {"symbol": "600036", "max_new": batch.DIGEST_BATCH_PER_SYMBOL, "newest_only": False}
    ]


# ---------------------------------------------------------------------------
# 周期入队调度
# ---------------------------------------------------------------------------

# 业务时区（Asia/Shanghai）03:00 = UTC 19:00 前一天
IN_WINDOW = datetime(2026, 9, 27, 19, 0, tzinfo=timezone.utc)


def _user_id(db, username):
    return db.query(User.id).filter(User.username == username).scalar()


@pytest.fixture
def scheduler(db, monkeypatch):
    opinion_calls = []

    def fake_opinion(db_, user_id, *, force=False):
        from app.services.background_job_store import create_or_get_active_job
        from app.services.opinion_summary_batch_jobs import candidate_opinion_targets
        from app.services.security_analysis_batch_jobs import NoBatchTargetsError

        if not candidate_opinion_targets(db_, user_id):
            raise NoBatchTargetsError("无目标")
        opinion_calls.append(user_id)
        return create_or_get_active_job("opinion_summary_batch", user_id, {"targets": []})

    monkeypatch.setattr(weekly, "start_opinion_batch_job", fake_opinion)
    monkeypatch.setattr(weekly, "is_llm_configured", lambda: True)
    return opinion_calls


def _active_types(db, user_id):
    db.expire_all()
    return sorted(
        row[0]
        for row in db.query(BackgroundJob.job_type).filter(
            BackgroundJob.user_id == user_id,
            BackgroundJob.status.in_(["queued", "running"]),
        )
    )


def _finish_jobs(db, user_id):
    db.query(BackgroundJob).filter(BackgroundJob.user_id == user_id).update(
        {"status": "succeeded"}, synchronize_session=False
    )
    db.commit()


def test_in_window_uses_business_timezone():
    assert weekly.in_window(IN_WINDOW)
    assert not weekly.in_window(IN_WINDOW + timedelta(hours=4))  # 07:00 北京时间
    assert not weekly.in_window(IN_WINDOW - timedelta(hours=2))  # 01:00 北京时间


def test_enqueue_data_then_opinion_then_not_due(db, scheduler):
    uid = _user_id(db, "demo")
    _hold(db, "600036", "A股", user_id=uid)

    first = weekly.enqueue_weekly_refresh(db, now=IN_WINDOW)
    assert first["counts"]["data_enqueued"] == 1
    assert _active_types(db, uid) == ["report_digest_batch"]
    assert scheduler == []  # 同一 tick 不入队观点（互斥）

    # 下一个 tick：数据刷新还在跑 → 观点忙，不记已跑
    second = weekly.enqueue_weekly_refresh(db, now=IN_WINDOW + timedelta(hours=1))
    assert second["counts"].get("opinion_busy") == 1
    assert "data_enqueued" not in second["counts"]

    _finish_jobs(db, uid)
    third = weekly.enqueue_weekly_refresh(db, now=IN_WINDOW + timedelta(hours=2))
    assert third["counts"].get("opinion_enqueued") == 1
    assert scheduler == [uid]

    _finish_jobs(db, uid)
    # 一周内都不再到期（持久化到期判定：换个进程/重启照样不重跑）
    fourth = weekly.enqueue_weekly_refresh(db, now=IN_WINDOW + timedelta(days=6))
    assert not any(k.endswith("_enqueued") for k in fourth["counts"])
    fifth = weekly.enqueue_weekly_refresh(db, now=IN_WINDOW + timedelta(days=7, hours=1))
    assert fifth["counts"].get("data_enqueued") == 1


def test_busy_user_is_retried_next_tick(db, scheduler):
    from app.services.background_job_store import create_or_get_active_job

    uid = _user_id(db, "demo")
    _hold(db, "600036", "A股", user_id=uid)
    create_or_get_active_job("security_analysis_batch", uid, {"targets": []})

    busy = weekly.enqueue_weekly_refresh(db, now=IN_WINDOW)
    assert busy["counts"].get("data_busy") == 1
    assert db.get(ScheduledTaskState, weekly.state_name(weekly.DATA_TASK, uid)) is None

    _finish_jobs(db, uid)
    retried = weekly.enqueue_weekly_refresh(db, now=IN_WINDOW + timedelta(hours=1))
    assert retried["counts"].get("data_enqueued") == 1


def test_same_type_manual_job_counts_as_busy(db, scheduler):
    """手动发起的批量回填在跑：create_or_get_active_job 会直接返回它——不能当成本周已入队。"""
    uid = _user_id(db, "demo")
    _hold(db, "600036", "A股", user_id=uid)
    batch.start_digest_batch_job(db, uid)
    result = weekly.enqueue_weekly_refresh(db, now=IN_WINDOW)
    assert result["counts"].get("data_busy") == 1


def test_user_without_targets_is_marked_and_inactive_users_ignored(db, scheduler):
    uid = _user_id(db, "demo")
    admin_id = _user_id(db, "admin")
    user = db.get(User, admin_id)
    user.is_active = False
    db.commit()
    try:
        result = weekly.enqueue_weekly_refresh(db, now=IN_WINDOW)
        assert result["users"] == 1
        assert result["counts"].get("data_no_targets") == 1
        state = db.get(ScheduledTaskState, weekly.state_name(weekly.DATA_TASK, uid))
        assert state is not None and state.detail["status"] == "no_targets"
    finally:
        user.is_active = True
        db.commit()


def test_opinion_source_unavailable_is_not_marked(db, scheduler, monkeypatch):
    from app.services.xueqiu_opinion_source import OpinionSourceUnavailable

    def unavailable(*args, **kwargs):
        raise OpinionSourceUnavailable("未接入")

    monkeypatch.setattr(weekly, "start_opinion_batch_job", unavailable)
    uid = _user_id(db, "demo")
    # 数据刷新本周已跑过：直接轮到观点摘要
    from app.services.scheduled_state import mark_ran

    mark_ran(db, weekly.state_name(weekly.DATA_TASK, uid), now=IN_WINDOW)
    result = weekly.enqueue_weekly_refresh(db, now=IN_WINDOW)
    assert result["counts"].get("opinion_unavailable", 0) >= 1
    assert db.get(ScheduledTaskState, weekly.state_name(weekly.OPINION_TASK, uid)) is None


def test_periodic_entry_skips_when_disabled_or_outside_window(monkeypatch):
    monkeypatch.setattr(weekly.settings, "weekly_data_refresh_enabled", False)
    assert weekly.periodic_enqueue_weekly_data_refresh().status == PERIODIC_SKIPPED

    monkeypatch.setattr(weekly.settings, "weekly_data_refresh_enabled", True)
    monkeypatch.setattr(weekly, "in_window", lambda now: False)
    assert weekly.periodic_enqueue_weekly_data_refresh().status == PERIODIC_SKIPPED


def test_periodic_entry_reports_enqueued(db, scheduler, monkeypatch):
    uid = _user_id(db, "demo")
    _hold(db, "600036", "A股", user_id=uid)
    monkeypatch.setattr(weekly.settings, "weekly_data_refresh_enabled", True)
    monkeypatch.setattr(weekly, "in_window", lambda now: True)
    outcome = weekly.periodic_enqueue_weekly_data_refresh()
    assert outcome.status == PERIODIC_SUCCEEDED
    assert outcome.count == 1


# ---------------------------------------------------------------------------
# 持仓页「可能过期」判据：列表端点的 latest_data_at 与详情页同一口径
# ---------------------------------------------------------------------------


def _digest_payload(**overrides):
    payload = {
        "status": "ok",
        "report_type": "annual",
        "end_date": "20251231",
        "extractor_version": SECTION_EXTRACTOR_VERSION,
        "prompt_version": DIGEST_PROMPT_VERSION,
        "digest": {},
    }
    payload.update(overrides)
    return payload


def _set_fetched(db, symbol, dataset, period_key, when):
    db.query(SecurityProfileData).filter(
        SecurityProfileData.symbol == symbol,
        SecurityProfileData.dataset == dataset,
        SecurityProfileData.period_key == period_key,
    ).update({"fetched_at": when}, synchronize_session=False)
    db.commit()


@pytest.mark.anyio
async def test_holding_analyses_carry_latest_data_at_matching_profile(db, monkeypatch):
    user = db.query(User).filter(User.username == "demo").first()
    user.hashed_password = get_password_hash("weekly-refresh-password")
    db.commit()
    _hold(db, "600036", "A股", user_id=user.id)
    _hold(db, "00700", "港股", user_id=user.id)
    _hold(db, "000001", "A股", user_id=user.id)

    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    for symbol, market in (("600036", "A股"), ("00700", "港股"), ("000001", "A股")):
        db.add(
            SecurityAnalysis(
                symbol=symbol,
                market=market,
                name=symbol,
                tags=[],
                risk_level="medium",
                summary="s",
                content="c",
                model="test",
                input_payload={},
                created_at=t0,
            )
        )
    db.commit()

    # 600036：一份当前版本摘要（晚于分析）+ 一份版本过期的更晚摘要（不算）
    upsert_profile_row(db, "600036", "A股", "report_digest", "20251231|annual", _digest_payload())
    upsert_profile_row(
        db,
        "600036",
        "A股",
        "report_digest",
        "20260630|semi",
        _digest_payload(prompt_version=0, report_type="semi"),
    )
    # 00700：报表抽取行（任何状态都算，与 statement_progress 同口径）
    upsert_profile_row(
        db, "00700", "港股", "report_statement_extract", "20251231|annual", {"status": "failed"}
    )
    db.commit()
    _set_fetched(db, "600036", "report_digest", "20251231|annual", t0 + timedelta(days=3))
    _set_fetched(db, "600036", "report_digest", "20260630|semi", t0 + timedelta(days=9))
    _set_fetched(db, "00700", "report_statement_extract", "20251231|annual", t0 + timedelta(days=5))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/api/auth/token",
            json={"username": "demo", "password": "weekly-refresh-password"},
        )
        auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
        rows = (await client.get("/api/securities/analyses", headers=auth)).json()
        by_symbol = {row["symbol"]: row for row in rows}
        assert datetime.fromisoformat(by_symbol["600036"]["latest_data_at"]) == t0 + timedelta(
            days=3
        )
        assert datetime.fromisoformat(by_symbol["00700"]["latest_data_at"]) == t0 + timedelta(
            days=5
        )
        assert by_symbol["000001"]["latest_data_at"] is None

        for symbol, market in (("600036", "A股"), ("00700", "港股"), ("000001", "A股")):
            profile = (
                await client.get(f"/api/securities/{market}/{symbol}/profile", headers=auth)
            ).json()
            assert profile["latest_data_at"] == by_symbol[symbol]["latest_data_at"], symbol


def test_manual_backfill_is_rejected_while_weekly_job_active(db, monkeypatch):
    """手动「补齐财报摘要」不能静默并进正在跑的每周任务（同一 job_type 按用户去重）。"""
    from app.services.security_analysis_jobs import AnalysisBusyError

    _stub_weekly(monkeypatch)
    _hold(db, "600036", "A股")
    weekly = batch.start_weekly_refresh_job(db, 1)
    assert weekly["status"] == "queued"
    with pytest.raises(AnalysisBusyError):
        batch.start_digest_batch_job(db, 1)


def test_failed_weekly_job_is_retried_in_a_later_window(db, scheduler):
    """每周任务以 failed 结束：之后的凌晨窗口重试（满 RETRY_AFTER），最多 MAX_RETRIES 次。"""
    uid = _user_id(db, "demo")
    _hold(db, "600036", "A股", user_id=uid)

    first = weekly.enqueue_weekly_refresh(db, now=IN_WINDOW)
    assert first["counts"]["data_enqueued"] == 1

    def fail_jobs():
        db.query(BackgroundJob).filter(
            BackgroundJob.user_id == uid, BackgroundJob.job_type == "report_digest_batch"
        ).update({"status": "failed"}, synchronize_session=False)
        db.query(BackgroundJob).filter(
            BackgroundJob.user_id == uid, BackgroundJob.job_type != "report_digest_batch"
        ).update({"status": "succeeded"}, synchronize_session=False)
        db.commit()

    fail_jobs()
    # 同一晚（未满 RETRY_AFTER）不重试
    same_night = weekly.enqueue_weekly_refresh(db, now=IN_WINDOW + timedelta(hours=2))
    assert "data_retry" not in same_night["counts"]
    _finish_jobs(db, uid)  # 让观点摘要不挡路
    fail_jobs()

    for attempt in range(1, weekly.MAX_RETRIES + 1):
        night = IN_WINDOW + timedelta(days=attempt)
        retried = weekly.enqueue_weekly_refresh(db, now=night)
        assert retried["counts"].get("data_retry") == 1, attempt
        state = db.get(ScheduledTaskState, weekly.state_name(weekly.DATA_TASK, uid))
        db.refresh(state)
        assert state.detail["retries"] == attempt
        fail_jobs()

    exhausted = weekly.enqueue_weekly_refresh(
        db, now=IN_WINDOW + timedelta(days=weekly.MAX_RETRIES + 1)
    )
    assert "data_retry" not in exhausted["counts"]


def test_succeeded_weekly_job_is_not_retried(db, scheduler):
    uid = _user_id(db, "demo")
    _hold(db, "600036", "A股", user_id=uid)
    weekly.enqueue_weekly_refresh(db, now=IN_WINDOW)
    _finish_jobs(db, uid)
    later = weekly.enqueue_weekly_refresh(db, now=IN_WINDOW + timedelta(days=1))
    assert "data_retry" not in later["counts"]


def _set_digest_status(db, uid, status, **data):
    job = (
        db.query(BackgroundJob)
        .filter(BackgroundJob.user_id == uid, BackgroundJob.job_type == "report_digest_batch")
        .order_by(BackgroundJob.created_at.desc())
        .first()
    )
    job.status = status
    job.data = {**(job.data or {}), **data}
    db.query(BackgroundJob).filter(
        BackgroundJob.user_id == uid, BackgroundJob.job_type != "report_digest_batch"
    ).update({"status": "succeeded"}, synchronize_session=False)
    db.commit()


def test_interrupted_weekly_job_is_retried_but_user_cancel_is_not(db, scheduler):
    """#272：排队/执行中被中断的每周刷新此前两边都看不到（只认 failed），当周数据静默丢失。
    用户主动终止同样写 interrupted，但带 cancelled=True，不能被自动重排。"""
    uid = _user_id(db, "demo")
    _hold(db, "600036", "A股", user_id=uid)
    weekly.enqueue_weekly_refresh(db, now=IN_WINDOW)

    _set_digest_status(db, uid, "interrupted", cancelled=True)
    cancelled = weekly.enqueue_weekly_refresh(db, now=IN_WINDOW + timedelta(days=1))
    assert "data_retry" not in cancelled["counts"]

    # 只写了终止请求、还没落 cancelled 就被中断：同样是用户的意思（PR #297 评审）
    _set_digest_status(db, uid, "interrupted", cancelled=False, cancel_requested=True)
    requested = weekly.enqueue_weekly_refresh(db, now=IN_WINDOW + timedelta(days=1, minutes=30))
    assert "data_retry" not in requested["counts"]

    _set_digest_status(db, uid, "interrupted", cancelled=False, cancel_requested=False)
    retried = weekly.enqueue_weekly_refresh(db, now=IN_WINDOW + timedelta(days=1, hours=1))
    assert retried["counts"].get("data_retry") == 1


def test_retry_does_not_reset_the_weekly_start_and_spacing_uses_enqueue_time(db, scheduler):
    uid = _user_id(db, "demo")
    _hold(db, "600036", "A股", user_id=uid)
    weekly.enqueue_weekly_refresh(db, now=IN_WINDOW)
    _set_digest_status(db, uid, "failed")

    first_retry = weekly.enqueue_weekly_refresh(db, now=IN_WINDOW + timedelta(days=1))
    assert first_retry["counts"].get("data_retry") == 1
    _set_digest_status(db, uid, "failed")

    # 第 2 次重试按第 1 次重试的入队时间计间隔：同一晚紧接着不得再重试
    too_soon = weekly.enqueue_weekly_refresh(db, now=IN_WINDOW + timedelta(days=1, hours=2))
    assert "data_retry" not in too_soon["counts"]

    _set_digest_status(db, uid, "succeeded")
    # 每周起点仍是首次入队：第 7 天按期入队，不因中间的重试顺延
    weekly_again = weekly.enqueue_weekly_refresh(db, now=IN_WINDOW + timedelta(days=7, hours=1))
    assert weekly_again["counts"].get("data_enqueued") == 1
    assert "data_retry" not in weekly_again["counts"]
