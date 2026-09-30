"""周期任务的持久化到期判定（表 `scheduled_task_state`）。

`job_worker.register_periodic_task` 的间隔只在进程内存里，重启后首个 tick 就会再跑——
「每周一次」「每个交易日一次」的任务注册成短 tick（如 1 小时），每次 tick 先用这里的
`is_due` 按库内的上次运行时间判定，到期才干活，干完 `mark_ran`。与
`llm_report_scheduler`（按库内最新报告时间判到期）同一思路。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..models.scheduled_task_state import ScheduledTaskState


def _now(now: Optional[datetime]) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def get_state(db: Session, name: str) -> Optional[ScheduledTaskState]:
    return db.get(ScheduledTaskState, name)


def get_detail(db: Session, name: str) -> Dict[str, Any]:
    state = get_state(db, name)
    return dict(state.detail or {}) if state is not None else {}


def is_due(
    db: Session,
    name: str,
    interval: timedelta,
    *,
    now: Optional[datetime] = None,
    basis: str = "run",
) -> bool:
    """距上次运行（basis="run"，含失败）或上次成功（basis="success"）满 interval 即到期。

    从未运行过的任务视为到期。按「运行」判定时失败也会推迟到下一个周期——适合
    入队类任务（失败原因在后台任务里，重复入队只会重复失败）；按「成功」判定适合
    失败后下个 tick 就该重试的同步类任务。
    """
    state = get_state(db, name)
    if state is None:
        return True
    last = state.last_success_at if basis == "success" else state.last_run_at
    if last is None:
        return True
    return _now(now) - last >= interval


def mark_ran(
    db: Session,
    name: str,
    *,
    success: bool = True,
    detail: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
    commit: bool = True,
) -> None:
    """记一次运行；detail 按键合并进已有明细（不传则不动）。"""
    moment = _now(now)
    merged = get_detail(db, name)
    if detail:
        merged.update(detail)
    values: Dict[str, Any] = {"name": name, "last_run_at": moment, "detail": merged}
    update: Dict[str, Any] = {"last_run_at": moment, "detail": merged}
    if success:
        values["last_success_at"] = moment
        update["last_success_at"] = moment
    stmt = insert(ScheduledTaskState).values(**values)
    stmt = stmt.on_conflict_do_update(index_elements=[ScheduledTaskState.name], set_=update)
    db.execute(stmt)
    # 同一会话里之后的 get_state 要看到新值
    db.expire_all()
    if commit:
        db.commit()


def set_detail(db: Session, name: str, detail: Dict[str, Any], *, commit: bool = True) -> None:
    """只合并明细、不推进 last_run_at / last_success_at（例如失败重试不该重置周期起点）。"""
    state = get_state(db, name)
    if state is None:
        mark_ran(db, name, success=False, detail=detail, commit=commit)
        return
    merged = dict(state.detail or {})
    merged.update(detail)
    state.detail = merged
    db.flush()
    db.expire_all()
    if commit:
        db.commit()
