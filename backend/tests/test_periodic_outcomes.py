"""周期任务结果契约（PR #255 评审 P1）：自吞错误的任务不得被记成成功。

用 **main.py 实际注册的入口** 经 `JobWorker._run_due_periodic_tasks()` 驱动，
把各任务内部的数据源打桩成失败，断言连续失败计数上涨、且不进入「成功过」集合；
合法的空跑/新鲜跳过不计失败、也不算恢复。
"""

from datetime import date, datetime, timezone

import pytest

import app.main  # noqa: F401  # 触发全部周期任务注册
from app.config import settings
from app.services import (
    alert_checks,
    benchmark_service,
    dividend_sync_jobs,
    exchange_rate_service,
    hkex_dayquot_source,
    job_worker,
    price_tail_sync,
    llm_report_scheduler,
    reference_rate_service,
    security_catalog_service,
    security_industry_service,
)
from app.services.job_worker import PeriodicOutcome

EXPECTED_TASKS = {
    "refresh_rates_if_stale",
    "refresh_reference_rates",
    "refresh_benchmark_tails",
    "refresh_hk_dayquot",
    "refresh_security_catalog",
    "refresh_security_industries",
    "run_alert_checks",
    "enqueue_periodic_dividend_sync",
    "enqueue_due_scheduled_reports",
    "refresh_quotes",
    "sync_price_tails",
    "refresh_daily_basic",
    "send_event_notifications",
    "enqueue_weekly_data_refresh",
    "sync_announcements",
}
REGISTERED = {entry[3]: entry[0] for entry in list(job_worker._periodic_tasks)}


def test_registry_is_the_single_registration_point_with_groups():
    """#273：全部周期任务由 periodic_registry 集中注册；告警检查与行情刷新各占独立分组。"""
    from app.services import periodic_registry

    assert set(periodic_registry.register_all()) == EXPECTED_TASKS
    groups = {entry[3]: job_worker.task_group(entry) for entry in job_worker._periodic_tasks}
    assert groups["run_alert_checks"] == job_worker.ALERTS_GROUP
    assert groups["refresh_quotes"] == job_worker.QUOTES_GROUP
    assert groups["refresh_security_catalog"] == job_worker.DEFAULT_GROUP
    # 幂等：重复注册不产生第二条
    periodic_registry.register_all()
    names = [entry[3] for entry in job_worker._periodic_tasks]
    assert len(names) == len(set(names))


def test_every_job_type_and_periodic_task_has_a_chinese_label():
    """#273：告警推送标题用中文名，不再把内部标识推到家人的锁屏上。"""
    from app.services.job_labels import JOB_TYPE_LABELS, PERIODIC_TASK_LABELS

    assert EXPECTED_TASKS <= set(PERIODIC_TASK_LABELS)
    assert set(job_worker._runners) <= set(JOB_TYPE_LABELS), (
        f"缺中文名的 job_type：{set(job_worker._runners) - set(JOB_TYPE_LABELS)}"
    )


def test_every_registered_task_follows_the_outcome_contract():
    assert EXPECTED_TASKS <= set(REGISTERED)
    for name in EXPECTED_TASKS:
        assert getattr(REGISTERED[name], "periodic_outcome_contract", False), (
            f"周期任务 {name} 没有按 PeriodicOutcome 契约报告结果：自吞的错误会被记成成功"
        )


def _clear_scheduled_state():
    from app.database import SessionLocal
    from app.models.scheduled_task_state import ScheduledTaskState

    session = SessionLocal()
    try:
        session.query(ScheduledTaskState).delete()
        session.commit()
    finally:
        session.close()


@pytest.fixture
def worker(monkeypatch):
    """只跑指定的一个注册任务；失败计数与成功集合用干净副本。

    按库内状态判到期的任务（scheduled_task_state）每个用例前清空，免得别的用例
    记下的「已运行」让本用例直接 skipped。"""
    monkeypatch.setattr(job_worker, "_periodic_failures", {})
    monkeypatch.setattr(job_worker, "_periodic_succeeded", set())
    _clear_scheduled_state()

    def run(name, times=1):
        monkeypatch.setattr(job_worker, "_periodic_tasks", [[REGISTERED[name], 0, 0.0, name]])
        runner = job_worker.JobWorker(poll_seconds=1)
        for _ in range(times):
            runner._run_due_periodic_tasks()
        return (
            job_worker.periodic_task_failures().get(name),
            name in job_worker.periodic_task_succeeded_since_start(),
        )

    return run


def _boom(*_args, **_kwargs):
    raise RuntimeError("upstream down")


# --------------------------------------------------------------------------- #
# 港交所日报（评审复现：sync 抛错被 refresh_hk_dayquot 自吞）
# --------------------------------------------------------------------------- #
def test_hk_dayquot_swallowed_error_counts_as_failure(worker, monkeypatch, db_session_alerts):
    monkeypatch.setattr(settings, "hkex_dayquot_sync_enabled", True)
    monkeypatch.setattr(hkex_dayquot_source, "sync_recent_dayquots", _boom)
    failures, succeeded = worker("refresh_hk_dayquot", times=3)
    assert failures["consecutive_failures"] == 3 and not succeeded
    assert "upstream down" in failures["last_error"]
    # 进了告警检查器
    alerts = {a.key for a in alert_checks.check_periodic_tasks(db_session_alerts, _now())}
    assert "periodic:refresh_hk_dayquot" in alerts
    # 兼容入口语义不变：仍返回 0、不上抛
    assert hkex_dayquot_source.refresh_hk_dayquot() == 0


def _dayquot_result(**overrides):
    base = {"universe": 1, "processed": [], "skipped_done": [], "no_report": [], "errors": []}
    base.update(overrides)
    return base


def test_hk_dayquot_parse_errors_fail_and_idle_runs_are_skipped(worker, monkeypatch):
    monkeypatch.setattr(settings, "hkex_dayquot_sync_enabled", True)
    monkeypatch.setattr(
        hkex_dayquot_source,
        "sync_recent_dayquots",
        lambda db: _dayquot_result(
            errors=[{"report_date": date(2026, 9, 25), "error": "格式变了"}]
        ),
    )
    failures, _ = worker("refresh_hk_dayquot")
    assert failures["consecutive_failures"] == 1 and "格式变了" in failures["last_error"]
    # 空跑（都处理过 / 休市）：不计失败也不算恢复
    monkeypatch.setattr(
        hkex_dayquot_source,
        "sync_recent_dayquots",
        lambda db: _dayquot_result(no_report=[date(2026, 9, 27)]),
    )
    failures, succeeded = worker("refresh_hk_dayquot")
    assert failures["consecutive_failures"] == 1 and not succeeded
    processed = {
        "report_date": date(2026, 9, 26),
        "stored": 3,
        "missing": 0,
        "suspended": 0,
        "unpriced": 0,
        "conflicts": [],
    }
    monkeypatch.setattr(
        hkex_dayquot_source,
        "sync_recent_dayquots",
        lambda db: _dayquot_result(processed=[processed]),
    )
    failures, succeeded = worker("refresh_hk_dayquot")
    assert failures is None and succeeded


def test_hk_dayquot_disabled_is_skipped(worker, monkeypatch):
    monkeypatch.setattr(settings, "hkex_dayquot_sync_enabled", False)
    monkeypatch.setattr(hkex_dayquot_source, "sync_recent_dayquots", _boom)
    assert worker("refresh_hk_dayquot", times=3) == (None, False)


# --------------------------------------------------------------------------- #
# 标的全集 / 行业分类：按来源状态判定
# --------------------------------------------------------------------------- #
def test_security_catalog_failed_source_counts(worker, monkeypatch):
    monkeypatch.setattr(settings, "security_catalog_sync_enabled", True)
    monkeypatch.setattr(security_catalog_service, "is_sync_running", lambda: False)
    monkeypatch.setattr(
        security_catalog_service,
        "sync_security_catalog",
        lambda db, force: {
            "sources": [
                {"source": "hkex_list", "status": "failed", "error": "HTTP 503"},
                {"source": "us_basic", "status": "ok", "rows_upserted": 10},
            ]
        },
    )
    failures, succeeded = worker("refresh_security_catalog", times=3)
    assert failures["consecutive_failures"] == 3 and "hkex_list" in failures["last_error"]
    assert not succeeded
    assert security_catalog_service.refresh_security_catalog() == 1  # 兼容入口：成功来源数

    monkeypatch.setattr(security_catalog_service, "sync_security_catalog", _boom)
    failures, _ = worker("refresh_security_catalog")
    assert failures["consecutive_failures"] == 4

    monkeypatch.setattr(
        security_catalog_service,
        "sync_security_catalog",
        lambda db, force: {
            "sources": [{"source": "hkex_list", "status": "skipped", "reason": "fresh"}]
        },
    )
    failures, succeeded = worker("refresh_security_catalog")
    assert failures["consecutive_failures"] == 4 and not succeeded  # 新鲜跳过 ≠ 恢复


@pytest.mark.parametrize("status", ["failed", "partial"])
def test_security_industries_source_errors_count(worker, monkeypatch, status):
    monkeypatch.setattr(settings, "security_industry_sync_enabled", True)
    monkeypatch.setattr(
        security_industry_service,
        "sync_security_industries",
        lambda db: {
            "scope": 3,
            "unresolved": [],
            "sources": {
                "eastmoney": {
                    "status": status,
                    "written": 1,
                    "missing": [],
                    "errors": ["A股 600000: EastmoneyError"],
                }
            },
        },
    )
    failures, succeeded = worker("refresh_security_industries", times=3)
    assert failures["consecutive_failures"] == 3 and "eastmoney" in failures["last_error"]
    assert not succeeded


def test_security_industries_ok_and_idle(worker, monkeypatch):
    monkeypatch.setattr(settings, "security_industry_sync_enabled", True)
    monkeypatch.setattr(
        security_industry_service,
        "sync_security_industries",
        lambda db: {
            "scope": 0,
            "unresolved": [],
            "sources": {
                "eastmoney": {"status": "skipped", "written": 0, "missing": [], "errors": []}
            },
        },
    )
    assert worker("refresh_security_industries") == (None, False)
    monkeypatch.setattr(
        security_industry_service,
        "sync_security_industries",
        lambda db: {
            "scope": 1,
            "unresolved": ["x"],  # 取不到不是失败
            "sources": {
                "eastmoney": {"status": "ok", "written": 0, "missing": ["x"], "errors": []}
            },
        },
    )
    assert worker("refresh_security_industries") == (None, True)


# --------------------------------------------------------------------------- #
# 参考利率 / 基准补尾 / 汇率
# --------------------------------------------------------------------------- #
def test_reference_rates_series_error_counts(worker, monkeypatch):
    monkeypatch.setattr(settings, "reference_rate_sync_enabled", True)
    monkeypatch.setattr(
        reference_rate_service,
        "sync_series",
        lambda db, series: {
            "series": series,
            "written": 0,
            "ranges": [],
            "error": "SHIBOR 3M 获取失败：timeout" if series == "SHIBOR_3M" else None,
        },
    )
    failures, succeeded = worker("refresh_reference_rates", times=3)
    assert failures["consecutive_failures"] == 3 and "SHIBOR" in failures["last_error"]
    assert not succeeded
    monkeypatch.setattr(
        reference_rate_service,
        "sync_series",
        lambda db, series: {
            "series": series,
            "written": 0,
            "ranges": [],
            "error": None,
        },
    )
    assert worker("refresh_reference_rates") == (None, True)  # 0 行的正常补尾是成功


class _FakeQuery:
    def __init__(self, first):
        self._first = first

    def filter(self, *_a):
        return self

    def order_by(self, *_a):
        return self

    def first(self):
        return self._first


class _FakeSession:
    def __init__(self, first):
        self._first = first

    def query(self, *_a):
        return _FakeQuery(self._first)

    def close(self):
        pass


def test_benchmark_tail_failures_count(worker, monkeypatch):
    monkeypatch.setattr(settings, "tushare_token", "token")
    monkeypatch.setattr(
        benchmark_service, "SessionLocal", lambda: _FakeSession((date(2024, 1, 2),))
    )
    monkeypatch.setattr(
        benchmark_service,
        "sync_benchmark_history",
        lambda db, code, start, end: {"success": False, "error": "抱歉，您每分钟最多访问"},
    )
    failures, succeeded = worker("refresh_benchmark_tails", times=3)
    assert failures["consecutive_failures"] == 3 and "每分钟" in failures["last_error"]
    assert not succeeded
    # 冷启动（没有任何已回填的基准）：跳过
    monkeypatch.setattr(benchmark_service, "SessionLocal", lambda: _FakeSession(None))
    failures, _ = worker("refresh_benchmark_tails")
    assert failures["consecutive_failures"] == 3


def test_exchange_rates_official_source_failure_counts(worker, monkeypatch):
    monkeypatch.setattr(
        exchange_rate_service, "expected_official_date", lambda now=None: date(2999, 1, 1)
    )  # 永远「缺最近一期」
    monkeypatch.setattr(exchange_rate_service.chinamoney_source, "fetch_ccpr_history", _boom)

    def third_party_down():
        raise RuntimeError("第三方汇率源均失败")

    monkeypatch.setattr(exchange_rate_service, "_fetch_third_party_quotes", third_party_down)
    failures, succeeded = worker("refresh_rates_if_stale", times=3)
    assert failures["consecutive_failures"] == 3 and not succeeded
    assert "中间价获取失败" in failures["last_error"]


def test_exchange_rates_holiday_and_fresh_are_not_failures(worker, monkeypatch):
    monkeypatch.setattr(
        exchange_rate_service, "expected_official_date", lambda now=None: date(2999, 1, 1)
    )
    # 节假日：官方接口正常、只是没有新值 → 成功
    monkeypatch.setattr(
        exchange_rate_service, "fetch_latest_rates_from_api", lambda db, errors=None: {"USD": 7}
    )
    assert worker("refresh_rates_if_stale") == (None, True)

    # 只有第三方比对源失败：不算失败
    def third_party_only(db, errors=None):
        errors.append("third_party: 超时")
        return {"USD": 7}

    monkeypatch.setattr(exchange_rate_service, "fetch_latest_rates_from_api", third_party_only)
    assert worker("refresh_rates_if_stale") == (None, True)
    monkeypatch.setattr(exchange_rate_service, "REQUIRED_RATE_CURRENCIES", ())
    monkeypatch.setattr(exchange_rate_service, "fetch_latest_rates_from_api", _boom)
    assert worker("refresh_rates_if_stale") == (None, True)  # 已是最新：不外呼


# --------------------------------------------------------------------------- #
# 入队类任务与告警检查本身
# --------------------------------------------------------------------------- #
def test_enqueuers_and_alert_checks(worker, monkeypatch):
    monkeypatch.setattr(settings, "dividend_sync_periodic_enabled", False)
    assert worker("enqueue_periodic_dividend_sync") == (None, False)
    monkeypatch.setattr(settings, "dividend_sync_periodic_enabled", True)
    monkeypatch.setattr(dividend_sync_jobs, "enqueue_periodic_dividend_sync", _boom)
    failures, _ = worker("enqueue_periodic_dividend_sync")
    assert failures["consecutive_failures"] == 1

    monkeypatch.setattr(llm_report_scheduler, "is_llm_configured", lambda: False)
    assert worker("enqueue_due_scheduled_reports") == (None, False)
    monkeypatch.setattr(llm_report_scheduler, "is_llm_configured", lambda: True)
    monkeypatch.setattr(llm_report_scheduler, "enqueue_due_scheduled_reports", lambda: 0)
    assert worker("enqueue_due_scheduled_reports") == (None, True)

    monkeypatch.setattr(settings, "alert_check_enabled", False)
    assert worker("run_alert_checks") == (None, False)
    monkeypatch.setattr(settings, "alert_check_enabled", True)
    monkeypatch.setattr(alert_checks, "run_alert_checks", _boom)
    failures, _ = worker("run_alert_checks")
    assert failures["consecutive_failures"] == 1


def test_price_tail_and_daily_basic_outcomes(worker, monkeypatch):
    monkeypatch.setattr(settings, "price_tail_sync_enabled", False)
    assert worker("sync_price_tails") == (None, False)
    monkeypatch.setattr(settings, "price_tail_sync_enabled", True)
    monkeypatch.setattr(price_tail_sync, "sync_price_tails", _boom)
    failures, succeeded = worker("sync_price_tails", times=3)
    assert failures["consecutive_failures"] == 3 and not succeeded

    monkeypatch.setattr(settings, "daily_basic_refresh_enabled", True)
    monkeypatch.setattr(settings, "tushare_token", "fake")
    monkeypatch.setattr(price_tail_sync, "refresh_daily_basic", _boom)
    failures, succeeded = worker("refresh_daily_basic", times=2)
    assert failures["consecutive_failures"] == 2 and not succeeded


def test_outcome_helpers():
    assert job_worker.as_periodic_outcome(3).status == "succeeded"  # 旧式返回值
    outcome = PeriodicOutcome.failed("x", count=2)
    assert (outcome.status, outcome.reason, outcome.count) == ("failed", "x", 2)


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture
def db_session_alerts():
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
