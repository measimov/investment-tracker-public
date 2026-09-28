"""告警检查器：周期任务 `run_alert_checks`（每 10 分钟）逐个运行，结果交给状态机。

每个检查器只读库（与进程内状态），返回**当前触发中**的告警；一个检查器抛异常不影响
其他检查器——它自己被报成一条 `checker:<名字>` 告警，且它名下已有的告警本轮不动
（不能因为检查失败就把真实告警判成「已恢复」）。

检查器一览（告警键 → 严重度）见 DEPLOYMENT.md「告警通知」。

**宽限期**：Web 进程启动后的前若干时间内，「采集器无心跳 / 长时间没有成功采集」不报——
开启采集器（XUEQIU_COLLECTOR_ENABLED）要重建容器，backend 与采集器同时重启，此时
采集器还没来得及跑第一轮；把进程启动时刻当作「启用时刻」的代理，比任何库内痕迹都可靠
（从未跑过的采集器没有 scan_runs，停用数周后重新启用的采集器只有数周前的 scan_runs）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import settings
from ..core.logging import get_app_logger
from ..core.timeutil import local_today
from ..models.alert_state import AlertState
from ..models.background_job import BackgroundJob
from ..models.xueqiu_collector import (
    XueqiuArchiverScanRun,
    XueqiuCollectorAuthor,
    XueqiuCollectorState,
)
from . import alert_service
from .alert_service import Alert, utcnow
from .job_worker import PeriodicOutcome, periodic_outcome_task

logger = get_app_logger(__name__)

PERIODIC_INTERVAL_SECONDS = 600

SOURCE_COLLECTOR = "xueqiu_collector"
SOURCE_COOKIE = "xueqiu_cookie"
SOURCE_FX = "fx"
SOURCE_PERIODIC = "periodic_tasks"
SOURCE_JOBS = "background_jobs"
SOURCE_CHECKERS = "alert_checks"

# 同一作者连续多少次采集失败（error/failed）才告警：单次失败多是临时网络问题
AUTHOR_FAILURE_STREAK = 3
AUTHOR_FAILURE_STATUSES = frozenset({"error", "failed"})
# 只有这几种状态参与「连续失败」的判定：waf / interrupted 不是作者本身的问题
AUTHOR_STREAK_STATUSES = frozenset({"ok", "partial", "error", "failed"})
# 周期任务连续抛异常多少次告警
PERIODIC_FAILURE_THRESHOLD = 3
# 后台任务失败的观察窗口，以及升级成 warning 的失败次数
JOB_FAILURE_WINDOW = timedelta(hours=24)
JOB_FAILURE_WARNING_COUNT = 3

# Web 进程启动时刻：采集器相关检查的宽限起点（见模块 docstring）
PROCESS_STARTED_AT = utcnow()

Checker = Callable[[Session, datetime], List[Alert]]


def _latest(values) -> Optional[datetime]:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _fmt(value: Optional[datetime]) -> str:
    return alert_service.fmt_time(value)


# --------------------------------------------------------------------------- #
# 雪球采集器
# --------------------------------------------------------------------------- #
def _collector_state(db: Session) -> Optional[XueqiuCollectorState]:
    return db.get(XueqiuCollectorState, 1)


def _author_streaks(db: Session) -> List[Tuple[XueqiuCollectorAuthor, List[XueqiuArchiverScanRun]]]:
    authors = (
        db.query(XueqiuCollectorAuthor)
        .filter(XueqiuCollectorAuthor.enabled.is_(True))
        .order_by(XueqiuCollectorAuthor.xueqiu_user_id)
        .all()
    )
    result = []
    for author in authors:
        runs = (
            db.query(XueqiuArchiverScanRun)
            .filter(
                XueqiuArchiverScanRun.target_user_id == author.xueqiu_user_id,
                XueqiuArchiverScanRun.status.in_(AUTHOR_STREAK_STATUSES),
                XueqiuArchiverScanRun.finished_at.isnot(None),
            )
            .order_by(XueqiuArchiverScanRun.run_id.desc())
            .limit(AUTHOR_FAILURE_STREAK)
            .all()
        )
        result.append((author, runs))
    return result


def check_xueqiu_collector(
    db: Session, now: datetime, *, grace_since: Optional[datetime] = None
) -> List[Alert]:
    if not settings.xueqiu_collector_enabled:
        return []  # 未启用：名下全部告警随之恢复
    grace_since = grace_since or PROCESS_STARTED_AT
    state = _collector_state(db)
    alerts: List[Alert] = []

    # 1) 心跳：采集器进程每 30s 写一次（空转也写）
    heartbeat_at = state.heartbeat_at if state is not None else None
    limit = timedelta(minutes=settings.xueqiu_collector_health_max_age_minutes)
    reference = _latest([heartbeat_at, grace_since])
    if now - reference > limit:
        alerts.append(Alert(
            "xueqiu:heartbeat", "warning", "雪球采集器进程无心跳",
            f"最近一次心跳：{_fmt(heartbeat_at) if heartbeat_at else '从未'}（超过 "
            f"{settings.xueqiu_collector_health_max_age_minutes} 分钟）。采集器容器可能已退出，"
            "请检查 `docker compose ps xueqiu-collector` 与 xueqiu-collector.log。",
            {"heartbeat_at": heartbeat_at.isoformat() if heartbeat_at else None},
        ))

    # 2) 长时间没有一次成功的作者采集
    streaks = _author_streaks(db)
    last_live = (
        db.query(func.max(XueqiuArchiverScanRun.finished_at))
        .filter(XueqiuArchiverScanRun.status.in_(("ok", "partial")))
        .scalar()
    )
    stale_hours = settings.notify_collector_stale_hours
    if streaks and now - _latest([last_live, grace_since]) > timedelta(hours=stale_hours):
        alerts.append(Alert(
            "xueqiu:collector_stale", "warning", "雪球采集长时间没有成功",
            f"超过 {stale_hours:g} 小时没有一次成功的作者采集（最近一次成功："
            f"{_fmt(last_live) if last_live else '从未'}）。上一轮："
            f"{(state.last_cycle_status if state else '') or '—'} "
            f"{(state.last_cycle_message if state else '')[:200]}",
            {"last_live_at": last_live.isoformat() if last_live else None},
        ))

    # 3) 同一作者连续失败；全部作者最近一次都失败 → 疑似 Cookie 失效
    latest_statuses = []
    for author, runs in streaks:
        if runs:
            latest_statuses.append(runs[0].status)
        if len(runs) == AUTHOR_FAILURE_STREAK and all(
            run.status in AUTHOR_FAILURE_STATUSES for run in runs
        ):
            name = author.display_name or author.xueqiu_user_id
            alerts.append(Alert(
                f"xueqiu:author_errors:{author.xueqiu_user_id}", "warning",
                f"雪球作者 {name} 连续 {AUTHOR_FAILURE_STREAK} 次采集失败",
                f"最近一次（{_fmt(runs[0].finished_at)}）：{(runs[0].error_message or '')[:300]}",
                {"author_id": author.xueqiu_user_id, "run_ids": [run.run_id for run in runs]},
            ))
    if len(latest_statuses) >= 2 and all(
        status in AUTHOR_FAILURE_STATUSES for status in latest_statuses
    ):
        alerts.append(Alert(
            "xueqiu:all_authors_failing", "critical", "雪球采集全部失败（疑似 Cookie 失效）",
            f"{len(latest_statuses)} 位启用作者最近一次采集全部失败：时间线首页拿不到合法响应，"
            "最常见的原因是登录态被服务端注销。请重新导出 Cookie。",
        ))

    # 4) WAF：命中后直到出现一次成功的采集（作者或按标的）才算恢复
    if state is not None and state.last_waf_at is not None:
        recovered_by_authors = (
            db.query(XueqiuArchiverScanRun.run_id)
            .filter(
                XueqiuArchiverScanRun.status.in_(("ok", "partial")),
                XueqiuArchiverScanRun.started_at > state.last_waf_at,
            )
            .first()
            is not None
        )
        recovered_by_symbols = (
            state.symbols_last_status in ("ok", "partial")
            and state.symbols_last_started_at is not None
            and state.symbols_last_started_at > state.last_waf_at
        )
        if not (recovered_by_authors or recovered_by_symbols):
            cooldown_until = state.last_waf_at + timedelta(
                seconds=settings.xueqiu_collector_waf_cooldown_seconds
            )
            alerts.append(Alert(
                "xueqiu:waf", "critical", "雪球采集命中 WAF 挑战页",
                f"{_fmt(state.last_waf_at)} 命中阿里云 WAF，本轮已中止，冷却至 "
                f"{_fmt(cooldown_until)}。持续命中请降低采集频率或更换 Cookie；"
                "之后任一轮采集成功即自动恢复。",
                {"last_waf_at": state.last_waf_at.isoformat()},
            ))

    # 5) 每日按标的采集
    if settings.xueqiu_collector_symbols_enabled and state is not None:
        today = local_today()
        status = state.symbols_last_status or ""
        pending = state.symbols_pending if isinstance(state.symbols_pending, dict) else None
        todays_pending = pending if pending and pending.get("date") == today.isoformat() else None
        if state.symbols_last_business_date == today and status not in ("ok", "running", ""):
            alerts.append(Alert(
                "xueqiu:symbols", "warning", "今日雪球按标的采集未完成（重试已用尽）",
                (state.symbols_last_message or status)[:1000],
                {"status": status, "business_date": today.isoformat()},
            ))
        elif todays_pending is not None and status in ("failed", "unavailable", "waf", "partial"):
            alerts.append(Alert(
                "xueqiu:symbols", "info", "雪球按标的采集失败，等待自动重试",
                f"第 {todays_pending.get('attempts')} 轮：{(state.symbols_last_message or status)[:800]}",
                {"status": status, "attempts": todays_pending.get("attempts")},
            ))
    return alerts


# --------------------------------------------------------------------------- #
# 雪球 Cookie
# --------------------------------------------------------------------------- #
def check_xueqiu_cookie(db: Session, now: datetime) -> List[Alert]:
    from .xueqiu_collector import cookie_health

    raw = (settings.xueqiu_cookies or "").strip()
    cookie_file = (settings.xueqiu_cookie_file or "").strip()
    if not raw and not cookie_file:
        return []  # 雪球未配置：不是故障
    alerts: List[Alert] = []
    if raw:
        # XUEQIU_COOKIES 优先（与客户端同序）：没有到期日，只能检查主凭证的**最终值**
        # 是否非空——只有键、值为空串的登录态照样不可用（PR #255 评审）
        try:
            values = cookie_health.effective_cookie_values(json.loads(raw))
            missing = cookie_health.missing_primary_credentials(values)
            if missing:
                alerts.append(Alert(
                    "xueqiu:cookie", "critical", "雪球 Cookie 不可用",
                    f"XUEQIU_COOKIES 缺少登录凭证或值为空：{', '.join(missing)}"
                    "（请重新导出完整 Cookie）",
                ))
        except (ValueError, AttributeError, TypeError) as exc:
            alerts.append(Alert(
                "xueqiu:cookie", "critical", "雪球 Cookie 不可用",
                f"XUEQIU_COOKIES 无法解析：{type(exc).__name__}",
            ))
    else:
        result = cookie_health.check_expiry(
            cookie_file,
            warn_days=settings.xueqiu_cookie_warn_days,
            critical_days=settings.xueqiu_cookie_critical_days,
            now=now.timestamp(),
        )
        level = result.get("level")
        if level in ("warning", "critical"):
            days_left = result.get("days_left")
            title = "雪球 Cookie 即将过期" if days_left is not None and days_left > 0 else (
                "雪球 Cookie 已过期" if days_left is not None else "雪球 Cookie 不可用"
            )
            alerts.append(Alert(
                "xueqiu:cookie", level, title, result.get("message", ""),
                {"days_left": days_left, "cookie": result.get("cookie")},
            ))
    # 采集器自己的判定：上一轮因 Cookie 读不出/缺失而整轮不可用
    if settings.xueqiu_collector_enabled:
        state = _collector_state(db)
        if state is not None and state.last_cycle_status == "unavailable":
            alerts.append(Alert(
                "xueqiu:collector_unavailable", "critical", "雪球采集器不可用",
                (state.last_cycle_message or "")[:1000],
            ))
    return alerts


# --------------------------------------------------------------------------- #
# 汇率数据源
# --------------------------------------------------------------------------- #
def check_fx(db: Session, now: datetime) -> List[Alert]:
    from .exchange_rate_service import fx_source_warnings

    warnings = fx_source_warnings(db)
    if not warnings:
        return []
    return [Alert("fx:sources", "warning", "汇率数据源异常", "\n".join(warnings),
                  {"warnings": warnings})]


# --------------------------------------------------------------------------- #
# 周期任务连续失败（job_worker 在进程内计数）
# --------------------------------------------------------------------------- #
def check_periodic_tasks(db: Session, now: datetime) -> List[Alert]:
    """连续 N 次抛异常告警，成功一次恢复。

    计数在进程内：重启后计数清零，但「清零」不是「恢复」——重启后还没跑到（也没失败到阈值）
    的任务，已有的活动告警原样保留，等它真正成功一次才判恢复。
    """
    from .job_worker import periodic_task_failures, periodic_task_succeeded_since_start

    failures = periodic_task_failures()
    succeeded = periodic_task_succeeded_since_start()
    alerts = []
    for name, info in sorted(failures.items()):
        count = int(info.get("consecutive_failures") or 0)
        if count >= PERIODIC_FAILURE_THRESHOLD:
            alerts.append(Alert(
                f"periodic:{name}", "warning", f"周期任务 {name} 连续失败 {count} 次",
                f"最近一次：{info.get('last_error') or '—'}",
                dict(info),
            ))
    reported = {alert.key for alert in alerts}
    for row in db.query(AlertState).filter(
        AlertState.source == SOURCE_PERIODIC, AlertState.status == "active"
    ):
        name = row.alert_key.split(":", 1)[-1]
        # 重启后还没跑到，或又失败了但还没到阈值：都不能算恢复
        if row.alert_key in reported or name in succeeded:
            continue
        alerts.append(Alert(row.alert_key, row.severity, row.title, row.message,
                            dict(row.payload or {})))
    return alerts


# --------------------------------------------------------------------------- #
# 后台任务失败
# --------------------------------------------------------------------------- #
def check_background_jobs(db: Session, now: datetime) -> List[Alert]:
    """近 24h 有 failed 且之后没有同类型的 succeeded → 按类型一条告警。

    无状态判定：之后成功一次或失败滑出 24h 窗口即自动恢复。单次失败只记 info
    （用户触发的任务常因数据本身失败，不值得推送），窗口内达到 3 次升为 warning。
    """
    since = now - JOB_FAILURE_WINDOW
    failed = (
        db.query(
            BackgroundJob.job_type,
            func.count(BackgroundJob.id),
            func.max(BackgroundJob.finished_at),
        )
        .filter(BackgroundJob.status == "failed", BackgroundJob.finished_at >= since)
        .group_by(BackgroundJob.job_type)
        .all()
    )
    if not failed:
        return []
    succeeded = dict(
        db.query(BackgroundJob.job_type, func.max(BackgroundJob.finished_at))
        .filter(BackgroundJob.status == "succeeded", BackgroundJob.finished_at >= since)
        .group_by(BackgroundJob.job_type)
        .all()
    )
    alerts = []
    for job_type, count, last_failed_at in failed:
        last_success = succeeded.get(job_type)
        if last_success is not None and last_success > last_failed_at:
            continue
        latest = (
            db.query(BackgroundJob)
            .filter(BackgroundJob.job_type == job_type, BackgroundJob.status == "failed")
            .order_by(BackgroundJob.finished_at.desc())
            .first()
        )
        severity = "warning" if count >= JOB_FAILURE_WARNING_COUNT else "info"
        alerts.append(Alert(
            f"job_failed:{job_type}", severity, f"后台任务 {job_type} 失败",
            f"近 24 小时失败 {count} 次，最近一次 {_fmt(last_failed_at)}："
            f"{((latest.error if latest else '') or '—')[:300]}",
            {"count": int(count), "last_failed_at": last_failed_at.isoformat(),
             "job_id": latest.id if latest else None},
        ))
    return alerts


CHECKERS: List[Tuple[str, str, Checker]] = [
    ("xueqiu_collector", SOURCE_COLLECTOR, check_xueqiu_collector),
    ("xueqiu_cookie", SOURCE_COOKIE, check_xueqiu_cookie),
    ("fx", SOURCE_FX, check_fx),
    ("periodic_tasks", SOURCE_PERIODIC, check_periodic_tasks),
    ("background_jobs", SOURCE_JOBS, check_background_jobs),
]


def run_checks(
    db: Session,
    *,
    now: Optional[datetime] = None,
    checkers: Optional[List[Tuple[str, str, Checker]]] = None,
    sender=None,
) -> Dict[str, Any]:
    now = now or utcnow()
    summary: Dict[str, Any] = {"outcomes": [], "checker_errors": []}
    checker_failures: List[Alert] = []
    for name, source, checker in checkers or CHECKERS:
        try:
            triggered = checker(db, now)
            summary["outcomes"] += alert_service.evaluate_alerts(
                db, source, triggered, now=now, sender=sender
            )
        except Exception as exc:  # noqa: BLE001 - 一个检查器坏了不拖垮其他检查器
            db.rollback()
            logger.exception("告警检查器 %s 失败", name)
            summary["checker_errors"].append(name)
            checker_failures.append(Alert(
                f"checker:{name}", "warning", f"告警检查器 {name} 运行失败",
                f"{type(exc).__name__}: {str(exc)[:300]}",
            ))
    summary["outcomes"] += alert_service.evaluate_alerts(
        db, SOURCE_CHECKERS, checker_failures, now=now, sender=sender
    )
    summary["outcomes"] += alert_service.sweep_reminders(
        db, alert_service.SOURCE_EXTERNAL, now=now, sender=sender
    )
    summary["outcomes"] += alert_service.retry_recovery_notices(db, now=now, sender=sender)
    return summary


@periodic_outcome_task
def periodic_run_alert_checks() -> PeriodicOutcome:
    """周期任务入口（main.py 以名字 run_alert_checks 注册，每 10 分钟）。

    检查器自身的失败已由 `checker:<名字>` 告警报告；这里只在整轮编排出错时抛异常。"""
    if not settings.alert_check_enabled:
        return PeriodicOutcome.skipped("ALERT_CHECK_ENABLED=false")
    summary = run_alert_checks()
    return PeriodicOutcome.succeeded(len((summary or {}).get("outcomes", [])))


def run_alert_checks() -> Optional[Dict[str, Any]]:
    """跑一轮全部检查；开关关闭返回 None。"""
    if not settings.alert_check_enabled:
        return None
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        summary = run_checks(db)
    finally:
        db.close()
    changed = [item for item in summary["outcomes"] if item["action"] != alert_service.ACTION_UPDATE]
    if changed or summary["checker_errors"]:
        logger.info("告警检查：%s；检查器失败 %s", changed, summary["checker_errors"])
    return summary
