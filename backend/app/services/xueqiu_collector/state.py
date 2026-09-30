"""采集器的运行记录：scan_runs（每作者每轮一行）、作者名单、进程状态单行表、心跳。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ...config import settings
from ...core.logging import get_app_logger
from ...core.timeutil import business_timezone
from ...models.xueqiu_collector import (
    XueqiuArchiverScanRun,
    XueqiuCollectorAuthor,
    XueqiuCollectorState,
)

logger = get_app_logger(__name__)

STATE_ID = 1
# scan_runs.status 取值
RUN_OK = "ok"
RUN_FAILED = "failed"
RUN_WAF = "waf"
RUN_INTERRUPTED = "interrupted"
RUN_RUNNING = "running"
# 时间线首页就拿不到合法响应（非 JSON/登录页/缺 statuses）：本轮对该作者一无所知
RUN_ERROR = "error"
# 首页成功但部分页/帖失败：数据仍在流动，error_message 写明失败处
RUN_PARTIAL = "partial"
# 观点页活性判据认的状态：都意味着本轮确实从雪球拿到了该作者的合法时间线
LIVE_RUN_STATUSES = frozenset({RUN_OK, RUN_PARTIAL})


@dataclass
class AuthorResult:
    author_id: str
    status: str = RUN_OK
    candidate_count: int = 0
    reply_count: int = 0
    utterance_count: int = 0
    waf: bool = False
    stopped_early: bool = False
    error: str = ""
    utterance_keys: List[str] = field(default_factory=list)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# 状态单行表
# --------------------------------------------------------------------------- #
def get_state(db: Session) -> XueqiuCollectorState:
    """取 id=1 的状态行；迁移已播种，缺失时（被手工删掉）就地补建。"""
    state = db.get(XueqiuCollectorState, STATE_ID)
    if state is None:
        db.execute(
            text("INSERT INTO xueqiu_collector_state (id) VALUES (:id) ON CONFLICT DO NOTHING"),
            {"id": STATE_ID},
        )
        db.commit()
        state = db.get(XueqiuCollectorState, STATE_ID)
    return state


def request_run(db: Session) -> XueqiuCollectorState:
    """管理员「立即运行」：只记请求时间，由采集器进程在下一个轮询点拾取。"""
    state = get_state(db)
    state.run_requested_at = utcnow()
    db.commit()
    db.refresh(state)
    return state


def run_request_pending(state: XueqiuCollectorState) -> bool:
    if state.run_requested_at is None:
        return False
    started = state.last_cycle_started_at
    return started is None or state.run_requested_at > started


def waf_cooldown_until(state: XueqiuCollectorState, cooldown_seconds: float) -> Optional[datetime]:
    if state.last_waf_at is None or cooldown_seconds <= 0:
        return None
    until = state.last_waf_at + timedelta(seconds=cooldown_seconds)
    return until


def cycle_due(
    state: XueqiuCollectorState,
    now: datetime,
    *,
    interval_minutes: float,
    waf_cooldown_seconds: float,
) -> Tuple[bool, str]:
    """是否该开新一轮：WAF 冷却期内一律不开（立即运行也等冷却结束）。"""
    until = waf_cooldown_until(state, waf_cooldown_seconds)
    if until is not None and now < until:
        return False, "waf_cooldown"
    if run_request_pending(state):
        return True, "requested"
    if state.last_cycle_started_at is None:
        return True, "first"
    if now - state.last_cycle_started_at >= timedelta(minutes=interval_minutes):
        return True, "scheduled"
    return False, "waiting"


# --------------------------------------------------------------------------- #
# 每日按标的采集（与作者轮次分开记；共用 WAF 冷却）
# --------------------------------------------------------------------------- #
DEFAULT_SYMBOLS_RUN_AFTER = dt_time(7, 30)


def parse_run_after(value: Optional[str]) -> dt_time:
    """ "HH:MM" → time；格式不对退回 07:30（并告警），不让一个配置笔误停掉每日采集。"""
    text_value = (value or "").strip()
    try:
        hour, minute = text_value.split(":")
        return dt_time(int(hour), int(minute))
    except (ValueError, TypeError):
        logger.warning("XUEQIU_COLLECTOR_SYMBOLS_RUN_AFTER=%r 无法解析，按 07:30 处理", value)
        return DEFAULT_SYMBOLS_RUN_AFTER


def request_symbols_run(db: Session) -> XueqiuCollectorState:
    state = get_state(db)
    state.symbols_run_requested_at = utcnow()
    db.commit()
    db.refresh(state)
    return state


def symbols_request_pending(state: XueqiuCollectorState) -> bool:
    if state.symbols_run_requested_at is None:
        return False
    started = state.symbols_last_started_at
    return started is None or state.symbols_run_requested_at > started


def todays_symbols_pending(state: XueqiuCollectorState, today: date) -> Optional[Dict[str, Any]]:
    """当天的待重试记录；别的业务日留下的一律视为无（新的一天从整轮开始）。"""
    pending = state.symbols_pending
    if isinstance(pending, dict) and pending.get("date") == today.isoformat():
        return pending
    return None


def symbols_cycle_due(
    state: XueqiuCollectorState,
    now: datetime,
    *,
    run_after: dt_time,
    waf_cooldown_seconds: float,
    retry_minutes: float = 60,
) -> Tuple[bool, str]:
    """每日一轮：业务时区 run_after 之后、且该业务日还没「完成」；WAF 冷却期内一律不开。

    「完成」= 一轮无失败，或当日尝试次数用尽（见 symbols.finish_semantics）。有失败时
    当天留一条待重试记录，距上一轮结束满 retry_minutes 才重试（reason=retry）。
    """
    until = waf_cooldown_until(state, waf_cooldown_seconds)
    if until is not None and now < until:
        return False, "waf_cooldown"
    if symbols_request_pending(state):
        return True, "requested"
    local = now.astimezone(business_timezone())
    if local.time() < run_after:
        return False, "before_window"
    if state.symbols_last_business_date == local.date():
        return False, "done_today"
    if todays_symbols_pending(state, local.date()) is not None:
        finished = state.symbols_last_finished_at
        if finished is not None and now - finished < timedelta(minutes=retry_minutes):
            return False, "retry_wait"
        return True, "retry"
    return True, "scheduled"


def mark_symbols_started(db: Session) -> None:
    state = get_state(db)
    state.symbols_last_started_at = utcnow()
    state.symbols_last_status = RUN_RUNNING
    state.symbols_last_message = ""
    db.commit()


def mark_symbols_finished(
    db: Session,
    status: str,
    message: str,
    *,
    stats: Optional[Dict[str, Any]] = None,
    business_date: Optional[date] = None,
    waf_at: Optional[datetime] = None,
    pending: Any = ...,
) -> None:
    """pending 省略 = 不动待重试记录；None = 清除；dict = 写入。"""
    state = get_state(db)
    state.symbols_last_finished_at = utcnow()
    state.symbols_last_status = status
    state.symbols_last_message = message[:2000]
    state.symbols_last_stats = stats or {}
    if business_date is not None:
        state.symbols_last_business_date = business_date
    if pending is not ...:
        state.symbols_pending = pending
    if waf_at is not None:
        state.last_waf_at = waf_at
    db.commit()


def mark_cycle_started(db: Session) -> None:
    state = get_state(db)
    state.last_cycle_started_at = utcnow()
    state.last_cycle_status = RUN_RUNNING
    state.last_cycle_message = ""
    db.commit()


def mark_cycle_finished(
    db: Session, status: str, message: str, *, waf_at: Optional[datetime] = None
) -> None:
    state = get_state(db)
    state.last_cycle_finished_at = utcnow()
    state.last_cycle_status = status
    state.last_cycle_message = message[:2000]
    if waf_at is not None:
        state.last_waf_at = waf_at
    db.commit()


# --------------------------------------------------------------------------- #
# 作者名单与 scan_runs
# --------------------------------------------------------------------------- #
def pick_authors(db: Session, limit: int) -> List[str]:
    """本轮要采的作者：已启用，最久没跑的优先（原脚本永远取名单前 N 个，超过 N 个作者
    时排在后面的永远轮不到）。"""
    rows = (
        db.query(XueqiuCollectorAuthor.xueqiu_user_id)
        .filter(XueqiuCollectorAuthor.enabled.is_(True))
        .order_by(
            XueqiuCollectorAuthor.last_run_at.asc().nullsfirst(),
            XueqiuCollectorAuthor.created_at.asc(),
            XueqiuCollectorAuthor.xueqiu_user_id.asc(),
        )
        .limit(max(1, limit))
        .all()
    )
    return [row[0] for row in rows]


def close_orphan_runs(db: Session) -> int:
    """持锁后调用：上一个进程被杀时留下的 running 行标成 interrupted（持锁即说明
    没有别的轮次在跑，它们不可能还活着）。"""
    result = db.execute(
        text(
            "UPDATE xueqiu_archiver_scan_runs SET status = :status, finished_at = now(), "
            "error_message = '采集进程在本轮结束前退出' WHERE status = :running"
        ),
        {"status": RUN_INTERRUPTED, "running": RUN_RUNNING},
    )
    db.commit()
    return int(result.rowcount or 0)


def start_scan_run(db: Session, author_id: str) -> int:
    run = XueqiuArchiverScanRun(
        target_user_id=author_id, author_user_id=author_id, status=RUN_RUNNING
    )
    db.add(run)
    db.commit()
    return int(run.run_id)


def finish_scan_run(db: Session, run_id: int, result: AuthorResult) -> None:
    run = db.get(XueqiuArchiverScanRun, run_id)
    if run is None:
        return
    run.finished_at = utcnow()
    run.status = result.status
    run.candidate_count = result.candidate_count
    run.reply_count = result.reply_count
    run.utterance_count = result.utterance_count
    run.stopped_early = result.stopped_early
    run.waf_hit = result.waf
    run.error_message = (result.error or "")[:2000]
    db.commit()


def mark_author(db: Session, result: AuthorResult) -> None:
    author = db.get(XueqiuCollectorAuthor, result.author_id)
    if author is None:  # --author 显式指定的名单外作者：只写 scan_runs
        return
    author.last_run_at = utcnow()
    author.last_status = result.status
    author.last_message = (
        result.error
        or f"候选帖 {result.candidate_count}，命中回复 {result.reply_count}，"
        f"主页发言 {result.utterance_count}"
    )[:500]
    db.commit()


def recent_scan_runs(db: Session, limit: int = 10) -> List[XueqiuArchiverScanRun]:
    return (
        db.query(XueqiuArchiverScanRun)
        .order_by(XueqiuArchiverScanRun.run_id.desc())
        .limit(limit)
        .all()
    )


# --------------------------------------------------------------------------- #
# 心跳：文件 mtime（healthcheck，免 DB）+ 状态行 heartbeat_at（状态卡）
# --------------------------------------------------------------------------- #
def heartbeat_path() -> Path:
    return Path(settings.xueqiu_collector_heartbeat_file)


def heartbeat_age_seconds(
    path: Optional[Path] = None, *, now: Optional[float] = None
) -> Optional[float]:
    target = path or heartbeat_path()
    try:
        mtime = target.stat().st_mtime
    except OSError:
        return None
    return (time.time() if now is None else now) - mtime


class Heartbeat:
    """每次请求/轮询点调用 `beat()`：文件每次 touch，状态行最多每 db_interval 秒写一次
    （独立短连接，不掺进采集会话的事务）。`enabled=False`（dry-run）时什么都不写。"""

    def __init__(
        self,
        engine: Optional[Engine],
        *,
        path: Optional[Path] = None,
        db_interval: float = 60.0,
        enabled: bool = True,
    ) -> None:
        self.engine = engine
        self.path = path or heartbeat_path()
        self.db_interval = db_interval
        self.enabled = enabled
        self._last_db = 0.0

    def beat(self, *, force_db: bool = False) -> None:
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch()
        except OSError as exc:
            logger.warning("采集器心跳文件写入失败: %s", exc)
        now = time.monotonic()
        if self.engine is None or (not force_db and now - self._last_db < self.db_interval):
            return
        self._last_db = now
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text("UPDATE xueqiu_collector_state SET heartbeat_at = now() WHERE id = :id"),
                    {"id": STATE_ID},
                )
        except Exception as exc:  # noqa: BLE001 - 心跳失败不该打断采集
            logger.warning("采集器心跳写库失败: %s", str(exc)[:200])


def scan_run_to_dict(run: XueqiuArchiverScanRun) -> Dict[str, Any]:
    return {
        "run_id": run.run_id,
        "author_user_id": run.author_user_id or run.target_user_id,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "status": run.status,
        "candidate_count": run.candidate_count,
        "reply_count": run.reply_count,
        "utterance_count": run.utterance_count,
        "stopped_early": run.stopped_early,
        "waf_hit": run.waf_hit,
        "error_message": run.error_message,
    }
