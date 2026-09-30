"""告警状态机：决定什么时候推送、什么时候提醒、什么时候发「已恢复」。

每个检查器（source）每轮给出**当前触发中**的告警列表，`evaluate_alerts` 与库里该
source 的状态逐键比对：

| 之前 | 本轮 | 动作 |
| --- | --- | --- |
| 无 / 已恢复 | 触发 | new：置 active 并推送 |
| active | 触发、严重度升高，或高于最近**送达**的严重度 | escalate：推送（warning → critical 必须让人再看一眼；升级推送失败下一轮重试，不等提醒间隔） |
| active | 触发、从未推送成功 | remind：重试推送（渠道故障 / 刚配置好渠道） |
| active | 触发、上次推送早于 notify_reminder_hours | remind：再提醒一次 |
| active | 触发、其他 | update：只刷新 last_seen/消息，不推送（防刷屏） |
| active | 未触发 | resolve：置 resolved，**推送过的**才发「已恢复」 |
| resolved、推送过、「已恢复」未送达 | — | `retry_recovery_notices` 在之后的检查里重试（提醒间隔内） |

严重度低于 `notify_min_severity`（默认 warning）的告警只记录不推送；严重度降低
（critical → warning）静默更新。判定是纯函数 `decide`，落库与推送在外层。

外部信号（`manage.py notify`，如 backup.sh）走 `raise_alert` / `resolve_alert`：
只动那一个键，不会把同 source 的其他告警判成恢复。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import settings
from ..core.logging import get_app_logger
from ..core.timeutil import business_timezone
from ..models.alert_state import AlertState
from . import notification_service as notifier

logger = get_app_logger(__name__)

STATUS_ACTIVE = "active"
STATUS_RESOLVED = "resolved"

ACTION_NEW = "new"
ACTION_ESCALATE = "escalate"
ACTION_REMIND = "remind"
ACTION_UPDATE = "update"
ACTION_RESOLVE = "resolve"
ACTION_NOOP = "noop"

SOURCE_EXTERNAL = "external"

# 所有写 alert_states 的路径（周期检查、manage.py notify、管理员「立即检查」）共用一把
# 事务级 advisory lock：同一个键不会被两个进程同时判成 new 而推送两次
ALERT_LOCK_KEY = 0x616C657274  # "alert"

Sender = Callable[..., Dict[str, Any]]


@dataclass(frozen=True)
class Alert:
    key: str
    severity: str
    title: str
    message: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StateView:
    status: str
    severity: str
    last_notified_at: Optional[datetime]
    notify_count: int = 0
    # 最近一次推送成功时的严重度：状态里的 severity 在升级时就改了，推送失败的话只有它
    # 记得用户实际知道的级别
    notified_severity: Optional[str] = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _rank(severity: str) -> int:
    return notifier.SEVERITY_RANK.get(severity, 0)


def decide(
    state: Optional[StateView],
    alert: Optional[Alert],
    *,
    now: datetime,
    reminder_hours: float,
) -> str:
    """纯函数：给定库里的状态与本轮是否触发，返回动作（见模块 docstring 的表）。"""
    if alert is None:
        if state is not None and state.status == STATUS_ACTIVE:
            return ACTION_RESOLVE
        return ACTION_NOOP
    if state is None or state.status != STATUS_ACTIVE:
        return ACTION_NEW
    if _rank(alert.severity) > _rank(state.severity):
        return ACTION_ESCALATE
    if state.notified_severity and _rank(alert.severity) > _rank(state.notified_severity):
        return ACTION_ESCALATE  # 上一轮的升级推送没送达
    if state.last_notified_at is None:
        return ACTION_REMIND
    if now - state.last_notified_at >= timedelta(hours=reminder_hours):
        return ACTION_REMIND
    return ACTION_UPDATE


def wants_push(action: str, severity: str, notify_count: int) -> bool:
    """动作是否推送：恢复只发给推送过的告警（info 级从未推送，恢复也不打扰）。"""
    if action == ACTION_RESOLVE:
        return notify_count > 0
    if action in (ACTION_NEW, ACTION_ESCALATE, ACTION_REMIND):
        return notifier.should_push(severity)
    return False


def fmt_time(value: Optional[datetime]) -> str:
    if value is None:
        return "—"
    return value.astimezone(business_timezone()).strftime("%m-%d %H:%M")


def _fmt_duration(delta: timedelta) -> str:
    minutes = max(int(delta.total_seconds() // 60), 0)
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} 小时 {minutes} 分钟" if minutes else f"{hours} 小时"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时" if hours else f"{days} 天"


def compose_message(
    action: str,
    *,
    title: str,
    severity: str,
    message: str,
    first_seen_at: Optional[datetime],
    now: datetime,
    notify_count: int,
) -> tuple:
    """推送的标题与正文（中文；标题带严重度，便于锁屏一眼分辨）。"""
    label = notifier.SEVERITY_LABELS.get(severity, severity)
    since = ""
    if first_seen_at is not None:
        since = f"首次发现 {fmt_time(first_seen_at)}，已持续 {_fmt_duration(now - first_seen_at)}"
    if action == ACTION_RESOLVE:
        body = f"已恢复。{since}。" if since else "已恢复。"
        if message:
            body += f"\n最后状态：{message}"
        return f"【已恢复】{title}", body
    if action == ACTION_ESCALATE:
        prefix = f"【升级·{label}】"
    elif action == ACTION_REMIND and notify_count > 0:
        prefix = f"【仍未恢复·{label}】"
    else:
        prefix = f"【{label}】"
    body = message or title
    if action in (ACTION_REMIND, ACTION_ESCALATE) and since:
        body += f"\n{since}"
    return f"{prefix}{title}", body


# 锁内认领的有效期：认领后、推送回执前进程若退出（部署重启、OOM），过期的认领在下一次加锁时
# 回滚，这条告警按「未送达」重试——否则乐观推进的送达字段会让它整个提醒间隔都不再推送
# （PR #303 评审：认领把语义从「至少一次」变成了「至多一次」）。远大于渠道超时之和。
CLAIM_TTL = timedelta(minutes=10)


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def _claim(row: AlertState) -> Optional[Dict[str, Any]]:
    return (row.payload or {}).get("sending")


def _claim_in_flight(row: AlertState, now: datetime) -> bool:
    claim = _claim(row)
    return bool(claim) and _parse_time(claim["until"]) > now


def _undo_claim(row: AlertState, claim: Dict[str, Any]) -> None:
    """回滚一次没送达的认领；只在行上仍是这次认领写下的值时才回滚（不改别的会话的结果）。"""
    claimed_at = _parse_time(claim["at"])
    if claim["action"] == ACTION_RESOLVE:
        if row.status == STATUS_RESOLVED and row.resolve_notified_at == claimed_at:
            row.resolve_notified_at = None
    elif row.last_notified_at == claimed_at and row.notified_severity == claim["severity"]:
        row.last_notified_at = _parse_time(claim["prev_last_notified_at"])
        row.notified_severity = claim["prev_notified_severity"]
        row.notify_count = claim["prev_notify_count"]


def _release_claim(row: AlertState, claimed_at: datetime) -> None:
    payload = dict(row.payload or {})
    if (payload.get("sending") or {}).get("at") == claimed_at.isoformat():
        payload.pop("sending")
        row.payload = payload


def _lock(db: Session, now: datetime) -> None:
    """取告警锁，并回滚过期未回执的认领（上一次推送前进程退出留下的）。"""
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ALERT_LOCK_KEY})
    for row in db.query(AlertState).filter(AlertState.payload.has_key("sending")):
        claim = _claim(row)
        if claim and _parse_time(claim["until"]) <= now:
            logger.warning("告警 %s 的推送认领已过期仍无回执，按未送达回滚", row.alert_key)
            _undo_claim(row, claim)
            _release_claim(row, _parse_time(claim["at"]))


def _view(row: Optional[AlertState]) -> Optional[StateView]:
    if row is None:
        return None
    return StateView(
        status=row.status,
        severity=row.severity,
        last_notified_at=row.last_notified_at,
        notify_count=row.notify_count or 0,
        notified_severity=row.notified_severity,
    )


def _apply(
    db: Session,
    row: Optional[AlertState],
    alert: Optional[Alert],
    *,
    source: str,
    now: datetime,
    pending: List["_PendingSend"],
) -> Dict[str, Any]:
    action = decide(_view(row), alert, now=now, reminder_hours=settings.notify_reminder_hours)
    key = alert.key if alert is not None else row.alert_key
    outcome: Dict[str, Any] = {"key": key, "action": action, "notified": False}
    if action == ACTION_NOOP:
        return outcome

    if action == ACTION_NEW:
        if row is None:
            row = AlertState(alert_key=alert.key)
            db.add(row)
        row.status = STATUS_ACTIVE
        row.first_seen_at = now
        row.notify_count = 0
        row.last_notified_at = None
        row.notified_severity = None
        row.resolved_at = None
        row.resolve_notified_at = None
    if alert is not None:
        row.source = source
        if row.notified_severity and _rank(alert.severity) < _rank(row.notified_severity):
            # 静默降级：再升回去时要重新推送
            row.notified_severity = alert.severity
        row.severity = alert.severity
        row.title = alert.title[:500]
        row.message = alert.message[:4000]
        row.last_seen_at = now
        previous = row.payload or {}
        row.payload = {
            **(alert.payload or {}),
            "last_notify": previous.get("last_notify"),
            # 别的会话在飞的推送认领要留着：它的回执与过期回滚都靠它
            **({"sending": previous["sending"]} if previous.get("sending") else {}),
        }
    if action == ACTION_RESOLVE:
        row.status = STATUS_RESOLVED
        row.resolved_at = now
        row.resolve_notified_at = None
    db.flush()

    if not wants_push(action, row.severity, row.notify_count or 0):
        return outcome
    if action == ACTION_RESOLVE and _claim_in_flight(row, now):
        # 告警本身的首推还在飞：先发「已恢复」可能让用户只收到恢复没收到告警（首推失败回滚后
        # notify_count 归零）。留给 retry_recovery_notices——首推送达才会补发（PR #303 评审）
        return outcome
    pending.append(_prepare_send(row, action, now=now, outcome=outcome))
    return outcome


@dataclass
class _PendingSend:
    """锁内判定出的一次待发推送；提交、释放锁之后才真正发出（#273）。

    锁内已**乐观认领**：推送类先推进 last_notified_at / notified_severity / notify_count，
    恢复类先写 resolve_notified_at（claimed_at 即写入的时刻）。推送期间别的会话拿到锁看到的
    就是「刚推过」，decide 判成 update、恢复重试也查不到它——否则锁外推送的那几秒里，
    周期检查与「立即检查」会对同一个键各推一次（PR #303 评审）。没送达再在锁内回滚。"""

    key: str
    action: str
    severity: str
    title: str
    body: str
    outcome: Dict[str, Any]
    claimed_at: datetime
    prev_last_notified_at: Optional[datetime] = None
    prev_notified_severity: Optional[str] = None
    prev_notify_count: int = 0


def _prepare_send(
    row: AlertState, action: str, *, now: datetime, outcome: Dict[str, Any]
) -> _PendingSend:
    title, body = compose_message(
        action,
        title=row.title,
        severity=row.severity,
        message=row.message,
        first_seen_at=row.first_seen_at,
        # 恢复通知的时长以恢复时刻为准（重试晚到也不把之后的时间算进去）
        now=row.resolved_at if action == ACTION_RESOLVE and row.resolved_at else now,
        notify_count=row.notify_count or 0,
    )
    item = _PendingSend(
        row.alert_key,
        action,
        row.severity,
        title,
        body,
        outcome,
        claimed_at=now,
        prev_last_notified_at=row.last_notified_at,
        prev_notified_severity=row.notified_severity,
        prev_notify_count=row.notify_count or 0,
    )
    payload = dict(row.payload or {})
    payload["sending"] = {
        "at": now.isoformat(),
        "until": (now + CLAIM_TTL).isoformat(),
        "action": action,
        "severity": row.severity,
        "prev_last_notified_at": (
            row.last_notified_at.isoformat() if row.last_notified_at else None
        ),
        "prev_notified_severity": row.notified_severity,
        "prev_notify_count": row.notify_count or 0,
    }
    row.payload = payload
    if action == ACTION_RESOLVE:
        row.resolve_notified_at = now
    else:
        row.last_notified_at = now
        row.notified_severity = row.severity
        row.notify_count = (row.notify_count or 0) + 1
    return item


def _deliver(db: Session, pending: List[_PendingSend], *, now: datetime, sender: Sender) -> None:
    """在锁外逐条推送，再各用一个短事务回写送达结果（#273）。

    此前在持有告警 advisory lock 的事务里直接发 Apprise 请求：渠道慢或超时时锁被持有
    「告警数 × 渠道超时」，manage.py notify（backup.sh）与管理员「立即检查」被同步阻塞。
    送达字段已在锁内乐观推进（见 _PendingSend）；没送达时回滚——只在行上仍是本次认领写下的
    值时才回滚，推送期间别的会话推进过（升级、重新触发）就不动，不把别人的送达结果改回去。
    回滚后下一轮按同一规则重试。"""
    for item in pending:
        kind = notifier.KIND_RESOLVED if item.action == ACTION_RESOLVE else notifier.KIND_ALERT
        result = sender(item.title, item.body, severity=item.severity, kind=kind)
        delivered = bool(result.get("ok"))
        item.outcome["notified"] = delivered
        item.outcome["notify_status"] = result.get("status")
        _lock(db, now)
        row = db.query(AlertState).filter(AlertState.alert_key == item.key).one_or_none()
        if row is not None:
            _release_claim(row, item.claimed_at)
            payload = dict(row.payload or {})
            payload["last_notify"] = {
                "at": now.isoformat(),
                "action": item.action,
                "status": result.get("status"),
                "message": result.get("message"),
            }
            row.payload = payload
            if not delivered:
                if item.action == ACTION_RESOLVE:
                    if row.status == STATUS_RESOLVED and row.resolve_notified_at == item.claimed_at:
                        row.resolve_notified_at = None
                elif (
                    row.last_notified_at == item.claimed_at
                    and row.notified_severity == item.severity
                ):
                    row.last_notified_at = item.prev_last_notified_at
                    row.notified_severity = item.prev_notified_severity
                    row.notify_count = item.prev_notify_count
        db.commit()


def evaluate_alerts(
    db: Session,
    source: str,
    triggered: Iterable[Alert],
    *,
    now: Optional[datetime] = None,
    sender: Optional[Sender] = None,
) -> List[Dict[str, Any]]:
    """一个检查器一轮的结果落库并推送；该 source 本轮没再出现的活动告警判为恢复。"""
    now = now or utcnow()
    sender = sender or notifier.send
    by_key: Dict[str, Alert] = {}
    for alert in triggered:
        severity = notifier.normalize_severity(alert.severity)
        current = by_key.get(alert.key)
        if current is None or _rank(severity) > _rank(current.severity):
            by_key[alert.key] = Alert(
                alert.key, severity, alert.title, alert.message, alert.payload
            )
    _lock(db, now)
    rows = {
        row.alert_key: row
        for row in db.query(AlertState).filter(
            (AlertState.source == source) | AlertState.alert_key.in_(list(by_key) or [""])
        )
    }
    outcomes = []
    pending: List[_PendingSend] = []
    for key, alert in by_key.items():
        outcomes.append(_apply(db, rows.get(key), alert, source=source, now=now, pending=pending))
    for key, row in rows.items():
        if key not in by_key and row.source == source and row.status == STATUS_ACTIVE:
            outcomes.append(_apply(db, row, None, source=source, now=now, pending=pending))
    db.commit()
    _deliver(db, pending, now=now, sender=sender)
    return [item for item in outcomes if item["action"] != ACTION_NOOP]


def raise_alert(
    db: Session,
    alert: Alert,
    *,
    source: str = SOURCE_EXTERNAL,
    now: Optional[datetime] = None,
    sender: Optional[Sender] = None,
) -> Dict[str, Any]:
    """只触发这一个键（外部信号用）：不影响同 source 的其他告警。"""
    now = now or utcnow()
    alert = Alert(
        alert.key,
        notifier.normalize_severity(alert.severity),
        alert.title,
        alert.message,
        alert.payload,
    )
    _lock(db, now)
    row = db.query(AlertState).filter(AlertState.alert_key == alert.key).one_or_none()
    pending: List[_PendingSend] = []
    outcome = _apply(db, row, alert, source=source, now=now, pending=pending)
    db.commit()
    _deliver(db, pending, now=now, sender=sender or notifier.send)
    return outcome


def resolve_alert(
    db: Session,
    key: str,
    *,
    now: Optional[datetime] = None,
    sender: Optional[Sender] = None,
) -> Dict[str, Any]:
    now = now or utcnow()
    _lock(db, now)
    row = db.query(AlertState).filter(AlertState.alert_key == key).one_or_none()
    if row is None:
        db.commit()
        return {"key": key, "action": ACTION_NOOP, "notified": False}
    pending: List[_PendingSend] = []
    outcome = _apply(db, row, None, source=row.source, now=now, pending=pending)
    db.commit()
    _deliver(db, pending, now=now, sender=sender or notifier.send)
    return outcome


def sweep_reminders(
    db: Session,
    source: str,
    *,
    now: Optional[datetime] = None,
    sender: Optional[Sender] = None,
) -> List[Dict[str, Any]]:
    """没有检查器每轮重报的 source（外部信号）：活动告警按同一规则提醒/重试推送。"""
    now = now or utcnow()
    _lock(db, now)
    outcomes = []
    pending: List[_PendingSend] = []
    for row in db.query(AlertState).filter(
        AlertState.source == source, AlertState.status == STATUS_ACTIVE
    ):
        alert = Alert(row.alert_key, row.severity, row.title, row.message, row.payload or {})
        outcome = _apply(db, row, alert, source=source, now=now, pending=pending)
        if outcome["action"] != ACTION_UPDATE:
            outcomes.append(outcome)
    db.commit()
    _deliver(db, pending, now=now, sender=sender or notifier.send)
    return outcomes


def retry_recovery_notices(
    db: Session,
    *,
    now: Optional[datetime] = None,
    sender: Optional[Sender] = None,
) -> List[Dict[str, Any]]:
    """推送过的告警恢复了、但「已恢复」没送达：在提醒间隔内的后续检查里重试。

    业务状态（resolved）与待发的恢复通知分开记：状态在判定恢复时就落定，
    `resolve_notified_at` 为空即待发。超过提醒间隔仍发不出去就放弃（晚到数天的
    「已恢复」没有意义，页面上仍能看到恢复时间与最近一次推送结果）。
    """
    now = now or utcnow()
    _lock(db, now)
    cutoff = now - timedelta(hours=settings.notify_reminder_hours)
    outcomes = []
    pending: List[_PendingSend] = []
    for row in db.query(AlertState).filter(
        AlertState.status == STATUS_RESOLVED,
        AlertState.notify_count > 0,
        AlertState.resolve_notified_at.is_(None),
        AlertState.resolved_at >= cutoff,
        # 本轮刚判恢复（刚试过一次）的留给下一轮，免得一个坏渠道一轮被打两次
        AlertState.resolved_at < now,
    ):
        if _claim_in_flight(row, now):
            continue  # 这条告警的推送还在飞（首推或上一次恢复通知），等它有回执
        outcome: Dict[str, Any] = {
            "key": row.alert_key,
            "action": ACTION_RESOLVE,
            "notified": False,
            "retry": True,
        }
        pending.append(_prepare_send(row, ACTION_RESOLVE, now=now, outcome=outcome))
        outcomes.append(outcome)
    db.commit()
    _deliver(db, pending, now=now, sender=sender or notifier.send)
    return outcomes


def alert_to_dict(row: AlertState) -> Dict[str, Any]:
    payload = dict(row.payload or {})
    last_notify = payload.pop("last_notify", None)
    payload.pop("sending", None)
    return {
        "alert_key": row.alert_key,
        "source": row.source,
        "severity": row.severity,
        "status": row.status,
        "title": row.title,
        "message": row.message,
        "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at,
        "last_notified_at": row.last_notified_at,
        "notify_count": row.notify_count or 0,
        "resolved_at": row.resolved_at,
        "resolve_notified_at": row.resolve_notified_at,
        "last_notify": last_notify,
        "payload": payload,
    }


def list_alerts(db: Session, *, resolved_days: int = 7, resolved_limit: int = 50) -> Dict[str, Any]:
    active = db.query(AlertState).filter(AlertState.status == STATUS_ACTIVE).all()
    active.sort(key=lambda row: (-_rank(row.severity), row.first_seen_at), reverse=False)
    cutoff = utcnow() - timedelta(days=resolved_days)
    resolved = (
        db.query(AlertState)
        .filter(AlertState.status == STATUS_RESOLVED, AlertState.resolved_at >= cutoff)
        .order_by(AlertState.resolved_at.desc())
        .limit(resolved_limit)
        .all()
    )
    counts = {severity: 0 for severity in notifier.SEVERITIES}
    for row in active:
        counts[row.severity] = counts.get(row.severity, 0) + 1
    return {
        "active": [alert_to_dict(row) for row in active],
        "recent_resolved": [alert_to_dict(row) for row in resolved],
        "counts": {
            "active": len(active),
            **counts,
            "recent_resolved": len(resolved),
        },
    }
