"""长循环 job 被接管后必须立刻停手（失权哨兵）。

租约体系原本只挡住了 DB 状态回写：`required_attempt_count` 让僵尸线程的写入
变 no-op，但各个长循环把 set_job_progress 的返回值整个丢弃，于是照样往下
遍历剩余标的——继续调 Tushare/EDGAR/LLM，而 analyze_one 的分析产物、
ensure_report_digests 的摘要、逐标的行情都直接落全局表，接管者与僵尸各写一遍。

覆盖四条路径：
- 两个批量 job（分析 / 财报回填）
- 历史行情同步（PR #147 评审发现的遗漏：progress 已带 attempt 守卫，
  但循环仍继续请求剩余标的）
- 单标的分析——**刻意不 mock analyze_one**：它的 stage() 包装器有
  `except Exception` 兜底（"回调异常不得影响分析本身"），会把哨兵吞掉。
  此前的用例正是因为整个 mock 掉 analyze_one 才没暴露这一层。
"""

from datetime import date

import pytest

from app.database import SessionLocal
from app.models.background_job import BackgroundJob
from app.services import (
    performance_history_jobs,
    report_digest_batch_jobs,
    security_analysis_batch_jobs,
    security_analysis_jobs,
)
from app.services.background_job_store import (
    JobOwnershipLostError,
    claim_job,
    claim_next_runnable_job,
    create_or_get_active_job,
)

ANALYSIS_JOB_TYPE = security_analysis_batch_jobs.JOB_TYPE
DIGEST_JOB_TYPE = report_digest_batch_jobs.JOB_TYPE
HISTORY_JOB_TYPE = performance_history_jobs.JOB_TYPE

TARGETS = [
    {"symbol": "600000", "market": "A股"},
    {"symbol": "600001", "market": "A股"},
    {"symbol": "600002", "market": "A股"},
]


@pytest.fixture
def db():
    session = SessionLocal()

    def _clear():
        session.query(BackgroundJob).filter(
            BackgroundJob.job_type.in_(
                [ANALYSIS_JOB_TYPE, DIGEST_JOB_TYPE, HISTORY_JOB_TYPE]
            )
        ).delete(synchronize_session=False)
        session.commit()

    try:
        _clear()
        yield session
        _clear()
    finally:
        session.close()


def _claimed_then_taken_over(job_type: str, payload: dict):
    """认领一次拿到 claimed，再让租约过期被"另一个 worker"接管。

    返回原先那次（现已失权）的 claimed —— 模拟内联线程仍活着的僵尸。
    """
    job = create_or_get_active_job(job_type, 1, payload)
    stale_claim = claim_job(job["id"], job_type)
    assert stale_claim is not None

    session = SessionLocal()
    try:
        row = session.query(BackgroundJob).filter(BackgroundJob.id == job["id"]).one()
        row.lease_expires_at = row.created_at  # 租约立刻过期
        session.commit()
    finally:
        session.close()

    takeover = claim_next_runnable_job([job_type], owner="other-worker")
    assert takeover is not None and takeover["id"] == job["id"]
    assert takeover["attempt_count"] != stale_claim["attempt_count"], "接管应使 attempt 前进"
    return stale_claim


def test_batch_analysis_stops_after_takeover(db, monkeypatch):
    stale = _claimed_then_taken_over(
        ANALYSIS_JOB_TYPE,
        {"targets": TARGETS, "completed_keys": [], "total": len(TARGETS)},
    )

    calls = []
    monkeypatch.setattr(
        security_analysis_batch_jobs, "analyze_one",
        lambda db_, symbol, market, **kw: calls.append((symbol, market)) or {
            "status": "succeeded", "analysis_id": 1, "degraded": [],
        },
    )
    monkeypatch.setattr(
        security_analysis_batch_jobs, "_recent_analysis_keys", lambda *a, **k: set()
    )

    # 不得抛出：哨兵在 execute 顶层被安静接住
    security_analysis_batch_jobs.execute_batch_analysis_job(stale)

    assert calls == [], (
        f"失权后仍分析了 {calls} —— 僵尸线程会与接管者双倍消耗 LLM/外部 API"
    )


def test_digest_batch_stops_after_takeover(db, monkeypatch):
    stale = _claimed_then_taken_over(
        DIGEST_JOB_TYPE,
        {"targets": TARGETS, "completed_keys": [], "total": len(TARGETS)},
    )

    calls = []
    monkeypatch.setattr(
        report_digest_batch_jobs, "ensure_report_digests",
        lambda db_, symbol, market, **kw: calls.append((symbol, market)) or {
            "generated": 1, "gaps": [], "failed": 0, "completed": 1,
        },
    )

    report_digest_batch_jobs.execute_digest_batch_job(stale)

    assert calls == [], (
        f"失权后仍回填了 {calls} —— 僵尸线程会重复下载年报并烧 LLM token"
    )


def test_takeover_mid_run_stops_before_next_symbol(db, monkeypatch):
    """跑到一半才被接管：已开始的那只允许跑完，但不得继续下一只。"""
    job = create_or_get_active_job(
        ANALYSIS_JOB_TYPE, 1,
        {"targets": TARGETS, "completed_keys": [], "total": len(TARGETS)},
    )
    claimed = claim_job(job["id"], ANALYSIS_JOB_TYPE)
    assert claimed is not None

    calls = []

    def fake_analyze(db_, symbol, market, **kw):
        calls.append((symbol, market))
        if len(calls) == 1:
            # 第一只跑完的瞬间发生接管
            session = SessionLocal()
            try:
                row = session.query(BackgroundJob).filter(BackgroundJob.id == job["id"]).one()
                row.attempt_count = (row.attempt_count or 1) + 1
                session.commit()
            finally:
                session.close()
        return {"status": "succeeded", "analysis_id": 1, "degraded": []}

    monkeypatch.setattr(security_analysis_batch_jobs, "analyze_one", fake_analyze)
    monkeypatch.setattr(
        security_analysis_batch_jobs, "_recent_analysis_keys", lambda *a, **k: set()
    )
    monkeypatch.setattr(
        security_analysis_batch_jobs.settings, "security_analysis_batch_pause_seconds", 0
    )

    security_analysis_batch_jobs.execute_batch_analysis_job(claimed)

    assert len(calls) == 1, f"接管后应停在标的边界，实际分析了 {calls}"


def test_owned_run_completes_all_targets(db, monkeypatch):
    """未被接管时哨兵不得误伤：整批正常跑完。"""
    job = create_or_get_active_job(
        ANALYSIS_JOB_TYPE, 1,
        {"targets": TARGETS, "completed_keys": [], "total": len(TARGETS)},
    )
    claimed = claim_job(job["id"], ANALYSIS_JOB_TYPE)

    calls = []
    monkeypatch.setattr(
        security_analysis_batch_jobs, "analyze_one",
        lambda db_, symbol, market, **kw: calls.append((symbol, market)) or {
            "status": "succeeded", "analysis_id": 1, "degraded": [],
        },
    )
    monkeypatch.setattr(
        security_analysis_batch_jobs, "_recent_analysis_keys", lambda *a, **k: set()
    )
    monkeypatch.setattr(
        security_analysis_batch_jobs.settings, "security_analysis_batch_pause_seconds", 0
    )

    security_analysis_batch_jobs.execute_batch_analysis_job(claimed)

    assert len(calls) == len(TARGETS)
    row = db.query(BackgroundJob).filter(BackgroundJob.id == job["id"]).one()
    assert row.status == "succeeded"


# ---------------------------------------------------------------------------
# 历史行情同步（PR #147 评审发现的遗漏）
# ---------------------------------------------------------------------------

HISTORY_TARGETS = [
    {"symbol": "600000", "market": "A股", "currency": "CNY"},
    {"symbol": "600001", "market": "A股", "currency": "CNY"},
    {"symbol": "600002", "market": "A股", "currency": "CNY"},
]


def _patch_history_targets(monkeypatch):
    monkeypatch.setattr(
        performance_history_jobs, "get_history_sync_targets",
        lambda *a, **k: {
            "targets": HISTORY_TARGETS,
            "start_date": date(2026, 1, 1),
            "end_date": date(2026, 1, 31),
        },
    )


def test_history_sync_stops_when_taken_over_before_run(db, monkeypatch):
    """接管发生在开跑之前：一个标的都不该请求。"""
    stale = _claimed_then_taken_over(HISTORY_JOB_TYPE, {"total": 0, "completed": 0})
    _patch_history_targets(monkeypatch)

    fetched = []
    monkeypatch.setattr(
        performance_history_jobs, "fetch_and_store_security_price_history_incremental",
        lambda db_, **kw: fetched.append(kw["symbol"]) or {"success": True},
    )

    performance_history_jobs.execute_performance_history_sync_job(stale)

    assert fetched == [], (
        f"失权后仍同步了 {fetched} —— 僵尸线程会与接管者重复请求 Tushare 并重复入库"
    )


def test_history_sync_stops_mid_run_after_takeover(db, monkeypatch):
    """跑到一半被接管：已开始的那只允许跑完，但不得继续下一只。"""
    job = create_or_get_active_job(HISTORY_JOB_TYPE, 1, {"total": 0, "completed": 0})
    claimed = claim_job(job["id"], HISTORY_JOB_TYPE)
    assert claimed is not None
    _patch_history_targets(monkeypatch)

    fetched = []

    def fake_fetch(db_, **kw):
        fetched.append(kw["symbol"])
        if len(fetched) == 1:
            # 第一只刚跑完就被接管
            session = SessionLocal()
            try:
                row = session.query(BackgroundJob).filter(
                    BackgroundJob.id == job["id"]
                ).one()
                row.attempt_count = (row.attempt_count or 1) + 1
                session.commit()
            finally:
                session.close()
        return {"success": True}

    monkeypatch.setattr(
        performance_history_jobs,
        "fetch_and_store_security_price_history_incremental",
        fake_fetch,
    )

    performance_history_jobs.execute_performance_history_sync_job(claimed)

    assert len(fetched) == 1, f"接管后应停在标的边界，实际同步了 {fetched}"


def test_history_sync_completes_all_targets_when_owned(db, monkeypatch):
    """未被接管时哨兵不得误伤：整批正常跑完。"""
    job = create_or_get_active_job(HISTORY_JOB_TYPE, 1, {"total": 0, "completed": 0})
    claimed = claim_job(job["id"], HISTORY_JOB_TYPE)
    _patch_history_targets(monkeypatch)

    fetched = []
    monkeypatch.setattr(
        performance_history_jobs, "fetch_and_store_security_price_history_incremental",
        lambda db_, **kw: fetched.append(kw["symbol"]) or {"success": True},
    )

    performance_history_jobs.execute_performance_history_sync_job(claimed)

    assert len(fetched) == len(HISTORY_TARGETS)
    row = db.query(BackgroundJob).filter(BackgroundJob.id == job["id"]).one()
    assert row.status == "succeeded"


# ---------------------------------------------------------------------------
# 单标的分析：哨兵必须穿透 analyze_one 的回调兜底
# ---------------------------------------------------------------------------


def test_ownership_sentinel_survives_analyze_one_stage_wrapper(monkeypatch):
    """analyze_one 的 stage() 有 `except Exception` 兜底，必须对哨兵放行。

    被吞掉的话，僵尸线程会跑完剩余阶段并 commit 第二份 SecurityAnalysis。
    这条刻意不 mock analyze_one——正是那种 mock 让上一轮没发现这层吞异常。
    """
    called = []
    monkeypatch.setattr(
        security_analysis_jobs, "sync_symbol_profile",
        lambda *a, **k: called.append("sync") or {"supported": True},
    )

    def raiser(stage_name, extra):
        raise JobOwnershipLostError("job-1")

    with pytest.raises(JobOwnershipLostError):
        security_analysis_jobs.analyze_one(None, "600000", "A股", on_stage=raiser)

    # 第一个 stage 回调发生在任何外呼之前，所以外部数据源一次都不该被碰
    assert called == [], "哨兵被吞掉了：失权后仍继续执行分析阶段"


def test_analyze_one_still_swallows_ordinary_progress_errors(monkeypatch):
    """普通回写失败仍不得拖垮分析（原有语义不能被本次改动破坏）。"""
    monkeypatch.setattr(
        security_analysis_jobs, "sync_symbol_profile",
        lambda *a, **k: {"supported": False},
    )

    def flaky(stage_name, extra):
        raise RuntimeError("DB 抖动")

    outcome = security_analysis_jobs.analyze_one(
        None, "600000", "未知市场", on_stage=flaky
    )
    assert outcome["status"] == "failed"
    assert outcome["error_kind"] == "unsupported_market"
