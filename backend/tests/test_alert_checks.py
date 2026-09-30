"""告警检查器：用构造的采集器状态 / scan_runs / Cookie 文件 / 后台任务行驱动，
断言每个检查器报出的告警键与严重度；以及一个检查器抛异常时不拖垮其他检查器。"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core.timeutil import local_today
from app.database import SessionLocal
from app.models.alert_state import AlertState
from app.models.background_job import BackgroundJob
from app.models.xueqiu_collector import (
    XueqiuArchiverScanRun,
    XueqiuCollectorAuthor,
    XueqiuCollectorState,
)
from app.services import alert_checks as checks
from app.services import alert_service, job_worker
from app.services.alert_service import Alert

A1, A2 = "900000001", "900000002"
STATE_COLUMNS = [column.name for column in XueqiuCollectorState.__table__.columns]


def _now():
    return datetime.now(timezone.utc)


def _keys(alerts):
    return {alert.key: alert.severity for alert in alerts}


@pytest.fixture
def db(monkeypatch):
    for name, value in {
        "xueqiu_collector_enabled": True,
        "xueqiu_collector_symbols_enabled": True,
        "xueqiu_collector_health_max_age_minutes": 30,
        "xueqiu_collector_waf_cooldown_seconds": 1800,
        "notify_collector_stale_hours": 3,
        "xueqiu_cookies": "",
        "xueqiu_cookie_file": "",
        "xueqiu_cookie_warn_days": 7,
        "xueqiu_cookie_critical_days": 3,
        "notify_min_severity": "warning",
    }.items():
        monkeypatch.setattr(checks.settings, name, value)
    session = SessionLocal()
    session.execute(
        text("INSERT INTO xueqiu_collector_state (id) VALUES (1) ON CONFLICT DO NOTHING")
    )
    session.commit()
    state = session.get(XueqiuCollectorState, 1)
    saved_state = {name: getattr(state, name) for name in STATE_COLUMNS}
    saved_enabled = {a.xueqiu_user_id: a.enabled for a in session.query(XueqiuCollectorAuthor)}
    session.execute(text("TRUNCATE xueqiu_archiver_scan_runs RESTART IDENTITY"))
    session.query(XueqiuCollectorAuthor).update({"enabled": False})
    for author_id in (A1, A2):
        session.add(
            XueqiuCollectorAuthor(xueqiu_user_id=author_id, display_name=f"作者{author_id[-1]}")
        )
    for name in ("heartbeat_at", "last_waf_at", "symbols_last_business_date", "symbols_pending"):
        setattr(state, name, None)
    state.last_cycle_status = ""
    state.symbols_last_status = ""
    state.heartbeat_at = _now()
    session.query(AlertState).delete()
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text("TRUNCATE xueqiu_archiver_scan_runs RESTART IDENTITY"))
        session.query(XueqiuCollectorAuthor).filter(
            XueqiuCollectorAuthor.xueqiu_user_id.in_([A1, A2])
        ).delete(synchronize_session=False)
        for author_id, enabled in saved_enabled.items():
            session.query(XueqiuCollectorAuthor).filter_by(xueqiu_user_id=author_id).update(
                {"enabled": enabled}
            )
        state = session.get(XueqiuCollectorState, 1)
        for name, value in saved_state.items():
            setattr(state, name, value)
        session.query(AlertState).delete()
        session.query(BackgroundJob).filter(BackgroundJob.job_type.like("alert_test_%")).delete(
            synchronize_session=False
        )
        session.commit()
        session.close()


def _run(db, author_id, status, finished_ago, *, started_ago=None, error=""):
    now = _now()
    db.add(
        XueqiuArchiverScanRun(
            target_user_id=author_id,
            author_user_id=author_id,
            status=status,
            started_at=now - (started_ago or finished_ago + timedelta(minutes=1)),
            finished_at=now - finished_ago,
            error_message=error,
        )
    )
    db.commit()


OLD_GRACE = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _collector(db, grace=OLD_GRACE):
    db.expire_all()
    return checks.check_xueqiu_collector(db, _now(), grace_since=grace)


# --------------------------------------------------------------------------- #
# 采集器
# --------------------------------------------------------------------------- #
def test_disabled_collector_reports_nothing(db, monkeypatch):
    monkeypatch.setattr(checks.settings, "xueqiu_collector_enabled", False)
    db.get(XueqiuCollectorState, 1).heartbeat_at = None
    db.commit()
    assert _collector(db) == []


def test_healthy_collector_reports_nothing(db):
    _run(db, A1, "ok", timedelta(hours=1))
    _run(db, A2, "partial", timedelta(minutes=30))
    assert _collector(db) == []


def test_heartbeat_missing_respects_startup_grace(db):
    _run(db, A1, "ok", timedelta(hours=1))
    state = db.get(XueqiuCollectorState, 1)
    state.heartbeat_at = _now() - timedelta(minutes=45)
    db.commit()
    assert _keys(_collector(db)) == {"xueqiu:heartbeat": "warning"}
    # Web 进程刚启动（开启采集器要重建容器）：宽限期内不报
    assert _collector(db, grace=_now() - timedelta(minutes=10)) == []
    state.heartbeat_at = None
    db.commit()
    assert "xueqiu:heartbeat" in _keys(_collector(db))


def test_stale_collection_uses_last_live_run_or_grace(db):
    _run(db, A1, "ok", timedelta(hours=4))
    _run(db, A1, "error", timedelta(hours=1))
    assert _keys(_collector(db)) == {"xueqiu:collector_stale": "warning"}
    assert _collector(db, grace=_now() - timedelta(hours=1)) == []
    # 没有启用的作者：不产生 scan_runs 是预期，不报停摆
    db.query(XueqiuCollectorAuthor).update({"enabled": False})
    db.commit()
    assert _collector(db) == []


def test_never_ran_collector_is_stale_after_grace(db):
    assert _keys(_collector(db)) == {"xueqiu:collector_stale": "warning"}
    assert _collector(db, grace=_now() - timedelta(minutes=30)) == []


def test_author_failure_streak_ignores_waf_and_interrupted_runs(db):
    _run(db, A2, "ok", timedelta(minutes=5))
    _run(db, A1, "ok", timedelta(hours=2, minutes=30))
    _run(db, A1, "error", timedelta(hours=2), error="首页不是 JSON")
    _run(db, A1, "waf", timedelta(hours=1, minutes=30))
    _run(db, A1, "failed", timedelta(hours=1))
    _run(db, A1, "interrupted", timedelta(minutes=40))
    assert _collector(db) == []  # 只有两次失败
    _run(db, A1, "error", timedelta(minutes=20), error="登录页")
    alerts = {alert.key: alert for alert in _collector(db)}
    assert set(alerts) == {f"xueqiu:author_errors:{A1}"}
    assert alerts[f"xueqiu:author_errors:{A1}"].severity == "warning"
    assert "作者1" in alerts[f"xueqiu:author_errors:{A1}"].title
    assert "登录页" in alerts[f"xueqiu:author_errors:{A1}"].message


def test_all_authors_failing_is_critical(db):
    _run(db, A1, "ok", timedelta(hours=1))
    _run(db, A1, "error", timedelta(minutes=20))
    _run(db, A2, "error", timedelta(minutes=10))
    assert _keys(_collector(db)) == {"xueqiu:all_authors_failing": "critical"}


def test_waf_stays_active_until_a_later_successful_run(db):
    _run(db, A1, "ok", timedelta(hours=1))
    state = db.get(XueqiuCollectorState, 1)
    state.last_waf_at = _now() - timedelta(minutes=50)
    db.commit()
    assert _keys(_collector(db)) == {"xueqiu:waf": "critical"}
    _run(db, A2, "ok", timedelta(minutes=5), started_ago=timedelta(minutes=10))
    assert _collector(db) == []


def test_waf_recovered_by_symbols_round(db):
    _run(db, A1, "ok", timedelta(hours=1))
    state = db.get(XueqiuCollectorState, 1)
    state.last_waf_at = _now() - timedelta(minutes=50)
    state.symbols_last_status = "ok"
    state.symbols_last_started_at = _now() - timedelta(minutes=5)
    db.commit()
    assert _collector(db) == []


def test_symbols_round_exhausted_and_pending_retry(db):
    _run(db, A1, "ok", timedelta(hours=1))
    state = db.get(XueqiuCollectorState, 1)
    today = local_today()
    state.symbols_last_business_date = today
    state.symbols_last_status = "partial"
    state.symbols_last_message = "失败 2 项；当日已尝试 3 轮"
    db.commit()
    alerts = {alert.key: alert for alert in _collector(db)}
    assert alerts["xueqiu:symbols"].severity == "warning"
    assert "已尝试 3 轮" in alerts["xueqiu:symbols"].message
    # 当日还在自动重试：只记录 info
    state.symbols_last_business_date = today - timedelta(days=1)
    state.symbols_last_status = "failed"
    state.symbols_pending = {"date": today.isoformat(), "attempts": 1, "items": None}
    db.commit()
    assert _keys(_collector(db)) == {"xueqiu:symbols": "info"}
    # 昨天的待重试记录不算
    state.symbols_pending = {"date": (today - timedelta(days=1)).isoformat(), "attempts": 2}
    db.commit()
    assert _collector(db) == []
    state.symbols_last_business_date = today
    state.symbols_last_status = "ok"
    db.commit()
    assert _collector(db) == []


# --------------------------------------------------------------------------- #
# Cookie
# --------------------------------------------------------------------------- #
def _cookie_file(tmp_path, days_left, names=("xq_a_token", "xqat")):
    expires = _now().timestamp() + days_left * 86400
    path = tmp_path / "xueqiu.json"
    path.write_text(
        json.dumps(
            {"cookies": [{"name": name, "value": "v", "expirationDate": expires} for name in names]}
        )
    )
    return str(path)


def test_cookie_unconfigured_is_not_an_alert(db):
    assert checks.check_xueqiu_cookie(db, _now()) == []


@pytest.mark.parametrize(
    "days_left,expected",
    [
        (30, {}),
        (5, {"xueqiu:cookie": "warning"}),
        (2, {"xueqiu:cookie": "critical"}),
        (-1, {"xueqiu:cookie": "critical"}),
    ],
)
def test_cookie_expiry_thresholds(db, monkeypatch, tmp_path, days_left, expected):
    monkeypatch.setattr(checks.settings, "xueqiu_cookie_file", _cookie_file(tmp_path, days_left))
    alerts = checks.check_xueqiu_cookie(db, _now())
    assert _keys(alerts) == expected
    if days_left == -1:
        assert alerts[0].title == "雪球 Cookie 已过期"
    elif expected:
        assert alerts[0].title == "雪球 Cookie 即将过期"


def test_cookie_file_missing_primary_credential_or_file(db, monkeypatch, tmp_path):
    monkeypatch.setattr(
        checks.settings, "xueqiu_cookie_file", _cookie_file(tmp_path, 30, names=("xq_a_token",))
    )
    alerts = checks.check_xueqiu_cookie(db, _now())
    assert _keys(alerts) == {"xueqiu:cookie": "critical"} and "xqat" in alerts[0].message
    monkeypatch.setattr(checks.settings, "xueqiu_cookie_file", str(tmp_path / "missing.json"))
    assert _keys(checks.check_xueqiu_cookie(db, _now())) == {"xueqiu:cookie": "critical"}


def test_cookie_file_empty_primary_value_is_critical(db, monkeypatch, tmp_path):
    expires = _now().timestamp() + 30 * 86400
    path = tmp_path / "xueqiu.json"
    path.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "xq_a_token", "value": "v", "expirationDate": expires},
                    {"name": "xqat", "value": "v", "expirationDate": expires},
                    {"name": "xqat", "value": "", "expirationDate": expires},  # 后者覆盖前者
                ]
            }
        )
    )
    monkeypatch.setattr(checks.settings, "xueqiu_cookie_file", str(path))
    alerts = checks.check_xueqiu_cookie(db, _now())
    assert _keys(alerts) == {"xueqiu:cookie": "critical"} and "xqat" in alerts[0].message


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"xq_a_token": "a", "xqat": "b"}', {}),
        ('[{"name": "xq_a_token", "value": "a"}, {"name": "xqat", "value": "b"}]', {}),
        ('{"xq_a_token": "a"}', {"xueqiu:cookie": "critical"}),
        ("not json", {"xueqiu:cookie": "critical"}),
        # PR #255 评审：键在、值为空（字典 / 列表 / 空白 / null）同样不可用
        ('{"xq_a_token": "", "xqat": ""}', {"xueqiu:cookie": "critical"}),
        ('{"xq_a_token": "a", "xqat": "   "}', {"xueqiu:cookie": "critical"}),
        ('{"xq_a_token": "a", "xqat": null}', {"xueqiu:cookie": "critical"}),
        (
            '[{"name": "xq_a_token", "value": "a"}, {"name": "xqat", "value": ""}]',
            {"xueqiu:cookie": "critical"},
        ),
        (
            '{"cookies": [{"name": "xq_a_token", "value": "a"}, {"name": "xqat", "value": ""}]}',
            {"xueqiu:cookie": "critical"},
        ),
        # 同名后者覆盖前者（与加载器一致）
        (
            '[{"name": "xq_a_token", "value": "a"}, {"name": "xqat", "value": "b"},'
            ' {"name": "xqat", "value": ""}]',
            {"xueqiu:cookie": "critical"},
        ),
        (
            '[{"name": "xq_a_token", "value": "a"}, {"name": "xqat", "value": ""},'
            ' {"name": "xqat", "value": "b"}]',
            {},
        ),
        ('[{"name": "xq_a_token", "value": "a"}, "junk"]', {"xueqiu:cookie": "critical"}),
    ],
)
def test_raw_cookies_primary_credentials(db, monkeypatch, raw, expected):
    monkeypatch.setattr(checks.settings, "xueqiu_cookies", raw)
    assert _keys(checks.check_xueqiu_cookie(db, _now())) == expected


def test_collector_unavailable_cycle_is_critical(db, monkeypatch):
    monkeypatch.setattr(checks.settings, "xueqiu_cookies", '{"xq_a_token": "a", "xqat": "b"}')
    state = db.get(XueqiuCollectorState, 1)
    state.last_cycle_status = "unavailable"
    state.last_cycle_message = "采集器不可用：雪球 Cookie 为空"
    db.commit()
    alerts = checks.check_xueqiu_cookie(db, _now())
    assert _keys(alerts) == {"xueqiu:collector_unavailable": "critical"}
    monkeypatch.setattr(checks.settings, "xueqiu_collector_enabled", False)
    assert checks.check_xueqiu_cookie(db, _now()) == []


# --------------------------------------------------------------------------- #
# 汇率 / 周期任务 / 后台任务
# --------------------------------------------------------------------------- #
def test_fx_warnings(db, monkeypatch):
    from app.services import exchange_rate_service

    monkeypatch.setattr(exchange_rate_service, "fx_source_warnings", lambda _db: [])
    assert checks.check_fx(db, _now()) == []
    monkeypatch.setattr(
        exchange_rate_service, "fx_source_warnings", lambda _db: ["USD/CNY 当前使用第三方报价"]
    )
    alerts = checks.check_fx(db, _now())
    assert _keys(alerts) == {"fx:sources": "warning"} and "第三方" in alerts[0].message


def test_periodic_task_consecutive_failures(db):
    name = "alert_test_periodic"
    try:
        for _ in range(2):
            job_worker.record_periodic_result(name, RuntimeError("down"))
        assert f"periodic:{name}" not in _keys(checks.check_periodic_tasks(db, _now()))
        job_worker.record_periodic_result(name, RuntimeError("still down"))
        alerts = {a.key: a for a in checks.check_periodic_tasks(db, _now())}
        assert alerts[f"periodic:{name}"].severity == "warning"
        assert "still down" in alerts[f"periodic:{name}"].message
        job_worker.record_periodic_result(name)  # 成功一次清零
        assert f"periodic:{name}" not in _keys(checks.check_periodic_tasks(db, _now()))
    finally:
        job_worker.record_periodic_result(name)


def test_periodic_alert_survives_restart_until_the_task_succeeds(db, monkeypatch):
    """重启清零了进程内计数：还没跑到的任务不能被判成「已恢复」。"""
    name = "alert_test_restarted"
    alert_service.evaluate_alerts(
        db,
        checks.SOURCE_PERIODIC,
        [Alert(f"periodic:{name}", "warning", f"周期任务 {name} 连续失败 3 次", "最近一次：x")],
        sender=lambda *a, **k: {"ok": True, "status": "sent", "message": ""},
    )
    monkeypatch.setattr(job_worker, "_periodic_failures", {})
    monkeypatch.setattr(job_worker, "_periodic_succeeded", set())
    alerts = {a.key: a for a in checks.check_periodic_tasks(db, _now())}
    assert alerts[f"periodic:{name}"].title == f"周期任务 {name} 连续失败 3 次"
    job_worker.record_periodic_result(name, RuntimeError("again"))  # 又失败但未到阈值
    assert f"periodic:{name}" in _keys(checks.check_periodic_tasks(db, _now()))
    job_worker.record_periodic_result(name)
    assert f"periodic:{name}" not in _keys(checks.check_periodic_tasks(db, _now()))


def test_worker_counts_periodic_task_exceptions(monkeypatch):
    calls = {"n": 0}

    def alert_test_flaky():
        calls["n"] += 1
        if calls["n"] <= 3:
            raise ValueError("boom")

    monkeypatch.setattr(
        job_worker, "_periodic_tasks", [[alert_test_flaky, 0, 0.0, "alert_test_flaky"]]
    )
    worker = job_worker.JobWorker(poll_seconds=1)
    try:
        for _ in range(3):
            worker._run_due_periodic_tasks()
        assert job_worker.periodic_task_failures()["alert_test_flaky"]["consecutive_failures"] == 3
        worker._run_due_periodic_tasks()
        assert "alert_test_flaky" not in job_worker.periodic_task_failures()
    finally:
        job_worker.record_periodic_result("alert_test_flaky")


def _job(db, job_type, status, finished_ago, error=None, data=None, user_id=1):
    import uuid

    db.add(
        BackgroundJob(
            id=uuid.uuid4().hex,
            user_id=user_id,
            job_type=job_type,
            status=status,
            finished_at=_now() - finished_ago,
            error=error,
            data=data or {},
        )
    )
    db.commit()


def _job_alerts(db):
    return {
        a.key: a
        for a in checks.check_background_jobs(db, _now())
        if a.key.startswith("job_failed:alert_test_")
    }


def test_background_job_failures(db):
    _job(db, "alert_test_a", "failed", timedelta(hours=2), "Tushare 超时")
    assert {k: a.severity for k, a in _job_alerts(db).items()} == {
        "job_failed:alert_test_a": "info"
    }
    _job(db, "alert_test_a", "failed", timedelta(hours=1))
    _job(db, "alert_test_a", "failed", timedelta(minutes=30), "最近的错误")
    alert = _job_alerts(db)["job_failed:alert_test_a"]
    assert alert.severity == "warning" and "3 次" in alert.message and "最近的错误" in alert.message
    # 之后同类型成功一次即恢复；更早的成功不算
    _job(db, "alert_test_a", "succeeded", timedelta(hours=3))
    assert "job_failed:alert_test_a" in _job_alerts(db)
    _job(db, "alert_test_a", "succeeded", timedelta(minutes=5))
    assert _job_alerts(db) == {}
    # 滑出 24h 窗口也恢复
    _job(db, "alert_test_b", "failed", timedelta(hours=25))
    assert _job_alerts(db) == {}


def test_interrupted_jobs_alert_unless_the_user_cancelled(db):
    """#272：执行进程失联、排队过久被中断的任务同样没做完；用户主动终止不算。"""
    _job(
        db,
        "alert_test_c",
        "interrupted",
        timedelta(hours=1),
        "任务排队过久仍未开始执行",
        data={"cancelled": False},
    )
    _job(db, "alert_test_d", "interrupted", timedelta(hours=1), data={"cancelled": True})
    _job(
        db,
        "alert_test_e",
        "interrupted",
        timedelta(hours=1),
        data={"cancelled": False, "cancel_requested": True},
    )
    alerts = _job_alerts(db)
    assert set(alerts) == {"job_failed:alert_test_c"}
    assert "排队过久" in alerts["job_failed:alert_test_c"].message


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #
def test_raising_checker_is_reported_and_keeps_its_alerts(db):
    sent = []

    def sender(title, body, *, severity, kind):
        sent.append(title)
        return {"ok": True, "status": "sent", "message": ""}

    alert_service.evaluate_alerts(
        db, "boom_src", [Alert("boom:real", "warning", "真实告警")], sender=sender
    )

    def broken(_db, _now):
        raise RuntimeError("查询失败")

    def fine(_db, _now):
        return [Alert("fine:x", "warning", "另一个检查器的告警")]

    summary = checks.run_checks(
        db, checkers=[("boom", "boom_src", broken), ("fine", "fine_src", fine)], sender=sender
    )
    assert summary["checker_errors"] == ["boom"]
    db.expire_all()
    rows = {row.alert_key: row for row in db.query(AlertState)}
    assert rows["boom:real"].status == "active"  # 检查失败不等于恢复
    assert rows["fine:x"].status == "active"
    assert rows["checker:boom"].status == "active" and "查询失败" in rows["checker:boom"].message
    # 检查器恢复正常 → checker:boom 恢复，且它名下告警按新结果判定
    checks.run_checks(
        db,
        checkers=[("boom", "boom_src", lambda *_: []), ("fine", "fine_src", fine)],
        sender=sender,
    )
    db.expire_all()
    rows = {row.alert_key: row for row in db.query(AlertState)}
    assert rows["checker:boom"].status == "resolved"
    assert rows["boom:real"].status == "resolved"
    assert "【已恢复】告警检查器 boom 运行失败" in sent


def test_run_alert_checks_honours_switch(monkeypatch):
    monkeypatch.setattr(checks.settings, "alert_check_enabled", False)
    assert checks.run_alert_checks() is None


# --------------------------------------------------------------------------- #
# 调度线程心跳（#273）
# --------------------------------------------------------------------------- #
def test_scheduler_heartbeat_is_written_and_stall_alerts(db, monkeypatch):
    from datetime import timedelta as _td

    from app.models.scheduled_task_state import ScheduledTaskState

    def probe():
        return None

    monkeypatch.setattr(job_worker, "_periodic_tasks", [[probe, 0, 0.0, "hb_probe", "hbtest"]])
    monkeypatch.setattr(checks.settings, "periodic_tasks_enabled", True)
    monkeypatch.setattr(checks.settings, "background_worker_enabled", True)
    db.query(ScheduledTaskState).filter(ScheduledTaskState.name == "scheduler:hbtest").delete()
    db.commit()

    job_worker.JobWorker(poll_seconds=1)._run_due_periodic_tasks(group="hbtest")
    db.expire_all()
    state = db.get(ScheduledTaskState, "scheduler:hbtest")
    assert state is not None and state.detail.get("current_task") is None
    assert "hb_probe" in job_worker.periodic_task_durations()

    fresh = checks.check_scheduler_heartbeats(db, state.last_run_at + _td(minutes=5))
    assert fresh == []

    # 卡在某个任务里：心跳停在「开跑前」那一次
    state.detail = {"current_task": "hb_probe", "current_task_started_at": "2026-09-29T00:00:00"}
    db.commit()
    later = state.last_run_at + checks.SCHEDULER_STALL_AFTER + _td(minutes=1)
    monkeypatch.setattr(checks, "PROCESS_STARTED_AT", state.last_run_at - _td(hours=1))
    alerts = {a.key: a for a in checks.check_scheduler_heartbeats(db, later)}
    assert "scheduler:hbtest" in alerts
    assert "hb_probe" in alerts["scheduler:hbtest"].message
    db.query(ScheduledTaskState).filter(ScheduledTaskState.name == "scheduler:hbtest").delete()
    db.commit()


def test_another_users_success_does_not_resolve_this_users_failure(db):
    """#273：之后成功按 (job_type, user) 判断，别人同类任务成功不能把这里的失败判成恢复。"""
    _job(db, "alert_test_e", "failed", timedelta(hours=2), "只有用户 1 失败", user_id=1)
    _job(db, "alert_test_e", "succeeded", timedelta(hours=1), user_id=2)
    alerts = _job_alerts(db)
    assert "job_failed:alert_test_e" in alerts
    assert alerts["job_failed:alert_test_e"].payload["user_ids"] == [1]
    _job(db, "alert_test_e", "succeeded", timedelta(minutes=30), user_id=1)
    assert "job_failed:alert_test_e" not in _job_alerts(db)


def test_alert_titles_use_chinese_labels(db, monkeypatch):
    _job(db, "report_digest_batch", "failed", timedelta(minutes=10), "boom")
    alerts = {a.key: a for a in checks.check_background_jobs(db, _now())}
    assert alerts["job_failed:report_digest_batch"].title == "后台任务「批量财报摘要回填」失败"

    monkeypatch.setattr(
        job_worker,
        "_periodic_failures",
        {"enqueue_periodic_dividend_sync": {"consecutive_failures": 3, "last_error": "x"}},
    )
    periodic = {a.key: a for a in checks.check_periodic_tasks(db, _now())}
    assert periodic["periodic:enqueue_periodic_dividend_sync"].title == (
        "周期任务「分红公告定期同步」连续失败 3 次"
    )
    db.query(BackgroundJob).filter(BackgroundJob.job_type == "report_digest_batch").delete()
    db.commit()


def test_push_happens_outside_the_alert_lock(db):
    """#273：推送时不持有告警 advisory lock——渠道慢时 manage.py notify、立即检查不被阻塞。"""
    from sqlalchemy import text as sa_text

    from app.database import SessionLocal

    observed = []

    def sender(title, body, *, severity, kind):
        other = SessionLocal()
        try:
            got = other.execute(
                sa_text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": alert_service.ALERT_LOCK_KEY},
            ).scalar()
            observed.append(got)
            other.rollback()
        finally:
            other.close()
        return {"ok": True, "status": "sent", "message": ""}

    alert_service.evaluate_alerts(
        db, "lock_test_src", [Alert("lock_test:one", "warning", "测试告警")], sender=sender
    )
    assert observed == [True]
    row = db.query(AlertState).filter(AlertState.alert_key == "lock_test:one").one()
    assert row.notify_count == 1 and row.last_notified_at is not None
    db.delete(row)
    db.commit()
