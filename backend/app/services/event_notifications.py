"""一次性事件提醒：新分红建议待确认、除净日临近、持仓价格异动、重大公告（表 `notification_events`）。

与 `alert_service` 的区别：告警是「状态」（持续未恢复会定期再提醒、恢复时发「已恢复」），
事件是「发生过一次」——一个事件键只推送一次，没有恢复。流程：

1. 收集候选事件，按 `event_key` 唯一键 `ON CONFLICT DO NOTHING` 插入（status=pending）——
   已记录过的事件不会再产生新行，因此不会重复推送；
2. `send_pending` 把待发送的行按 (用户, 类型) 合并成**一条**消息发出：送达 → sent；
   未配置渠道 → skipped（不重试，管理员页仍可见）；发送失败 → 留在 pending、attempts+1，
   下一轮重试，满 `MAX_ATTEMPTS` 次仍失败 → failed。

推送渠道是全局的 `NOTIFY_URLS`：活跃用户多于一个时，消息前缀用户名以免混淆。
`EVENT_NOTIFICATIONS_ENABLED=false` 时不收集新事件也不发送。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..config import settings
from ..core.logging import get_app_logger
from ..core.timeutil import business_timezone
from ..models.corporate_action_suggestion import CorporateActionSuggestion
from ..models.holding import Holding
from ..models.notification_event import NotificationEvent
from ..models.user import User
from . import announcement_service, notification_service
from .job_worker import PeriodicOutcome, periodic_outcome_task

logger = get_app_logger(__name__)

PERIODIC_INTERVAL_SECONDS = 600

KIND_DIVIDEND_SUGGESTION = "dividend_suggestion"
KIND_EX_DATE = "ex_date"
KIND_PRICE_MOVE = "price_move"
KIND_ANNOUNCEMENT = "announcement"

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

# 发送失败按时间窗重试（#273）：此前固定 3 次（10 分钟一轮 = 约 30 分钟），推送渠道故障
# 半小时，除净日临近、新分红建议的提醒就永久丢失；告警状态机却会一直重试到恢复
RETRY_WINDOW = timedelta(hours=24)
# 除净日提醒只在业务时区这个时刻之后发（避免凌晨推送）
EX_DATE_NOTIFY_AFTER = time(9, 0)
# 一条合并消息最多列出的明细行，其余以「…另 N 条」收尾
MAX_LINES = 12
# 已处理（sent/skipped/failed）的事件保留天数。新分红建议的事件不清理：建议可能一直停在
# 「新」状态，删掉事件行下一轮就会当成新事件再推一次
RETENTION_DAYS = 180
# 免打扰时段（业务时区）：期间只记录不发送，结束后下一轮合并补发（美股盘中的异动不在
# 凌晨把人叫醒）
QUIET_START = time(23, 0)
QUIET_END = time(8, 0)


def in_quiet_hours(moment: datetime) -> bool:
    local = moment.astimezone(business_timezone()).time()
    return local >= QUIET_START or local < QUIET_END


_TITLES = {
    KIND_DIVIDEND_SUGGESTION: "新分红建议待确认",
    KIND_EX_DATE: "除净日临近",
    KIND_PRICE_MOVE: "持仓价格异动",
    KIND_ANNOUNCEMENT: "重大公告",
}

# 重大公告只推「新近发生且新近入库」的组：公告日与首次入库都在近 N 天内。
# 首次回溯（365 天）入库的旧公告公告日早于窗口，永远不推——无需记录「启用时间」。
ANNOUNCEMENT_NOTIFY_DAYS = 2
ANNOUNCEMENT_TITLE_MAX = 60

_ACTION_LABELS = {"CASH_DIVIDEND": "现金分红", "STOCK_DIVIDEND": "送转"}
_EX_DATE_STATUSES = ("NEW", "MATCHED", "ACCEPTED")


# ---------------------------------------------------------------------------
# 文案（纯函数）
# ---------------------------------------------------------------------------


def _fmt_decimal(value: Any, places: int = 4) -> str:
    """去尾零的定点数；非数值返回空串。"""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return ""
    if not number.is_finite():
        return ""
    text = f"{number:.{places}f}".rstrip("0").rstrip(".")
    return text or "0"


def _fmt_md(value: Optional[date]) -> str:
    return value.strftime("%m-%d") if value else "—"


def _label(symbol: str, name: Optional[str]) -> str:
    return f"{symbol} {name}".strip() if name else symbol


def suggestion_line(suggestion: CorporateActionSuggestion) -> str:
    """「02669 中海物业 现金分红 每股 HKD 0.1（除净 10-12）」"""
    parts = [_label(suggestion.symbol, suggestion.name)]
    if suggestion.action_type == "STOCK_DIVIDEND":
        ratio = _fmt_decimal(suggestion.stk_div_per_share)
        parts.append(f"送转 每股 {ratio}" if ratio else "送转")
    else:
        per_share = _fmt_decimal(suggestion.cash_div_pre_tax)
        label = _ACTION_LABELS.get(suggestion.action_type, suggestion.action_type)
        parts.append(f"{label} 每股 {suggestion.currency} {per_share}" if per_share else label)
    total = _fmt_decimal(suggestion.estimated_total_dividend, places=2)
    if total and suggestion.action_type != "STOCK_DIVIDEND":
        parts.append(f"约 {total}")
    return " ".join(parts) + f"（除净 {_fmt_md(suggestion.ex_date)}）"


def ex_date_line(suggestion: CorporateActionSuggestion, today: date) -> str:
    days = (suggestion.ex_date - today).days
    when = "今天" if days == 0 else f"{days} 天后"
    pay = f"，派息 {_fmt_md(suggestion.pay_date)}" if suggestion.pay_date else ""
    return f"{suggestion_line(suggestion)}：{when}除净{pay}"


def price_move_line(move: Dict[str, Any]) -> str:
    pct = float(move["pct"])
    sign = "+" if pct > 0 else ""
    price = _fmt_decimal(move.get("price"))
    prev = _fmt_decimal(move.get("prev_close"))
    # 带上行情日：渠道故障恢复后补发的是哪一天的涨跌要看得出来（PR #303 评审）
    as_of = move.get("as_of")
    as_of_text = as_of.isoformat() if isinstance(as_of, date) else str(as_of or "")[:10]
    day = f"{as_of_text[5:7]}-{as_of_text[8:10]} " if len(as_of_text) == 10 else ""
    return (
        f"{_label(move['symbol'], move.get('name'))}（{move['market']}）"
        f" {day}{sign}{pct:.2f}%：{prev} → {price}"
    )


def announcement_line(group: Dict[str, Any], name: Optional[str]) -> str:
    title = group["title"]
    if len(title) > ANNOUNCEMENT_TITLE_MAX:
        title = title[: ANNOUNCEMENT_TITLE_MAX - 1] + "…"
    line = f"{_label(group['symbol'], name or group.get('sec_name'))}：{group['category_label']} — {title}"
    if group["document_count"] > 1:
        line += f"（{group['document_count']} 份文件）"
    return line


def compose_message(lines: Sequence[str]) -> str:
    shown = list(lines[:MAX_LINES])
    if len(lines) > MAX_LINES:
        shown.append(f"…另 {len(lines) - MAX_LINES} 条")
    return "\n".join(shown)


def compose_title(kind: str, count: int, username: Optional[str]) -> str:
    title = _TITLES.get(kind, kind)
    if count > 1:
        title = f"{title}（{count} 条）"
    return f"[{username}] {title}" if username else title


# ---------------------------------------------------------------------------
# 事件落库
# ---------------------------------------------------------------------------


def record_events(db: Session, events: Iterable[Dict[str, Any]]) -> int:
    """按 event_key 幂等插入（已存在的键不动）；返回新插入行数。不提交。"""
    inserted = 0
    for event in events:
        stmt = (
            insert(NotificationEvent)
            .values(
                event_key=event["event_key"][:200],
                kind=event["kind"],
                user_id=event.get("user_id"),
                title=event.get("title") or _TITLES.get(event["kind"], event["kind"]),
                message=event.get("message") or "",
                payload=event.get("payload") or {},
                status=STATUS_PENDING,
            )
            .on_conflict_do_nothing(index_elements=[NotificationEvent.event_key])
        )
        result = db.execute(stmt)
        inserted += result.rowcount or 0
    return inserted


def _active_users(db: Session) -> List[Tuple[int, str]]:
    return [
        (row.id, row.username)
        for row in db.query(User.id, User.username).filter(User.is_active.is_(True)).all()
    ]


def collect_dividend_suggestion_events(db: Session) -> List[Dict[str, Any]]:
    """状态为 NEW 的分红建议，每条一个事件（键含建议 id：同一建议只提醒一次）。"""
    rows = (
        db.query(CorporateActionSuggestion)
        .join(User, User.id == CorporateActionSuggestion.user_id)
        .filter(User.is_active.is_(True), CorporateActionSuggestion.status == "NEW")
        .order_by(CorporateActionSuggestion.ex_date, CorporateActionSuggestion.id)
        .all()
    )
    return [
        {
            "event_key": f"{KIND_DIVIDEND_SUGGESTION}:{row.id}",
            "kind": KIND_DIVIDEND_SUGGESTION,
            "user_id": row.user_id,
            "message": suggestion_line(row),
            "payload": {
                "suggestion_id": row.id,
                "symbol": row.symbol,
                "market": row.market,
                "ex_date": row.ex_date.isoformat(),
            },
        }
        for row in rows
    ]


def collect_ex_date_events(db: Session, today: date) -> List[Dict[str, Any]]:
    """用户当前持有（数量>0）且未忽略的建议，除净日落在 [today, today+N] 内。

    同一用户同一标的同一除净日可能有多个账户各一条建议——只提醒一次（按键去重）。
    """
    horizon = today + timedelta(days=settings.notify_ex_date_days_ahead)
    held = {
        (row.user_id, row.symbol, row.market)
        for row in db.query(Holding.user_id, Holding.symbol, Holding.market)
        .filter(Holding.quantity > 0)
        .distinct()
        .all()
    }
    rows = (
        db.query(CorporateActionSuggestion)
        .join(User, User.id == CorporateActionSuggestion.user_id)
        .filter(
            User.is_active.is_(True),
            CorporateActionSuggestion.status.in_(_EX_DATE_STATUSES),
            CorporateActionSuggestion.ex_date >= today,
            CorporateActionSuggestion.ex_date <= horizon,
        )
        .order_by(CorporateActionSuggestion.ex_date, CorporateActionSuggestion.id)
        .all()
    )
    events: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if (row.user_id, row.symbol, row.market) not in held:
            continue
        key = f"{KIND_EX_DATE}:{row.user_id}:{row.symbol}:{row.market}:{row.ex_date.isoformat()}"
        if key in events:
            continue
        events[key] = {
            "event_key": key,
            "kind": KIND_EX_DATE,
            "user_id": row.user_id,
            "message": ex_date_line(row, today),
            "payload": {
                "suggestion_id": row.id,
                "symbol": row.symbol,
                "market": row.market,
                "ex_date": row.ex_date.isoformat(),
                "pay_date": row.pay_date.isoformat() if row.pay_date else None,
            },
        }
    return list(events.values())


def collect_announcement_events(db: Session, now: datetime) -> List[Dict[str, Any]]:
    """持仓（数量>0）∪ 自选标的的重大公告组，公告日与首次入库都在近 ANNOUNCEMENT_NOTIFY_DAYS 天内。

    键 = 用户 + 组（标的|市场|公告日|类别）：同一组后续补发的文件不会再推一次。"""
    if not settings.announcement_notify_enabled:
        return []
    scope = announcement_service.user_scope(db)
    all_keys = {key for entries in scope.values() for key in entries}
    if not all_keys:
        return []
    today = now.astimezone(business_timezone()).date()
    seen_after = now - timedelta(days=ANNOUNCEMENT_NOTIFY_DAYS)
    groups = announcement_service.load_groups(
        db,
        keys=sorted(all_keys),
        since=today - timedelta(days=ANNOUNCEMENT_NOTIFY_DAYS),
        importance="major",
        limit=500,
    )
    events = []
    for group in groups:
        if group["first_seen_at"] < seen_after:
            continue
        key = (group["symbol"], group["market"])
        for user_id, entries in scope.items():
            if key not in entries:
                continue
            events.append(
                {
                    "event_key": f"{KIND_ANNOUNCEMENT}:{user_id}:{group['group_key']}",
                    "kind": KIND_ANNOUNCEMENT,
                    "user_id": user_id,
                    "message": announcement_line(group, entries[key]),
                    "payload": {
                        "symbol": group["symbol"],
                        "market": group["market"],
                        "ann_date": group["ann_date"].isoformat(),
                        "category": group["category"],
                        "url": group["url"],
                        "document_count": group["document_count"],
                    },
                }
            )
    return events


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------


def send_pending(db: Session, *, now: Optional[datetime] = None) -> Dict[str, int]:
    """把 pending 事件按 (用户, 类型) 合并发送；返回各结果计数。提交事务。

    待发送行用 FOR UPDATE SKIP LOCKED 认领，行锁持有到本函数 commit：refresh_quotes（quotes
    组）与 send_event_notifications（default 组）两条调度线程会并发调用，没有认领时双方读到
    同一批 pending 行、同一条合并消息推两次、attempts 也记两次（PR #302 评审）。"""
    moment = now or datetime.now(timezone.utc)
    counts = {STATUS_SENT: 0, STATUS_SKIPPED: 0, STATUS_FAILED: 0, "retry": 0}
    if in_quiet_hours(moment):
        return counts
    pending = (
        db.query(NotificationEvent)
        .filter(NotificationEvent.status == STATUS_PENDING)
        .order_by(NotificationEvent.created_at, NotificationEvent.id)
        .with_for_update(skip_locked=True)
        .all()
    )
    if not pending:
        db.commit()  # 结束读事务（没有认领到行）
        return counts

    users = _active_users(db)
    names = dict(users)
    show_user = len(users) > 1
    groups: Dict[Tuple[Optional[int], str], List[NotificationEvent]] = {}
    for event in pending:
        groups.setdefault((event.user_id, event.kind), []).append(event)

    for (user_id, kind), events in groups.items():
        username = names.get(user_id) if show_user and user_id is not None else None
        title = compose_title(kind, len(events), username)
        body = compose_message([event.message or event.title for event in events])
        result = notification_service.send(title, body, severity="warning")
        status = result.get("status")
        for event in events:
            event.attempts = (event.attempts or 0) + 1
            if status in (notification_service.STATUS_SENT, notification_service.STATUS_PARTIAL):
                event.status = STATUS_SENT
                event.sent_at = moment
                event.last_error = None
            elif status == notification_service.STATUS_UNCONFIGURED:
                event.status = STATUS_SKIPPED
                event.last_error = result.get("message")
            else:
                event.last_error = result.get("message")
                if retry_expired(event, moment):
                    event.status = STATUS_FAILED
        if status in (notification_service.STATUS_SENT, notification_service.STATUS_PARTIAL):
            counts[STATUS_SENT] += len(events)
        elif status == notification_service.STATUS_UNCONFIGURED:
            counts[STATUS_SKIPPED] += len(events)
        else:
            exhausted = sum(1 for event in events if event.status == STATUS_FAILED)
            counts[STATUS_FAILED] += exhausted
            counts["retry"] += len(events) - exhausted
            logger.warning(
                "事件提醒发送失败（%s，%d 条）：%s", kind, len(events), result.get("message")
            )
    db.commit()
    return counts


def retry_expired(event: NotificationEvent, now: datetime) -> bool:
    """发送失败的事件是否放弃重试：超过 RETRY_WINDOW；除净日提醒以除净日（业务时区）为截止
    ——除净日过了再提醒没有意义；价格异动以行情日次日为截止（美股行情日跨北京午夜，留一天），
    隔天补发的旧涨跌像是当前行情（PR #303 评审）。"""
    created = event.created_at or now
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if now - created > RETRY_WINDOW:
        return True
    if event.kind == KIND_EX_DATE:
        ex_date = (event.payload or {}).get("ex_date")
        if ex_date and now.astimezone(business_timezone()).date() > date.fromisoformat(ex_date):
            return True
    if event.kind == KIND_PRICE_MOVE:
        as_of = (event.payload or {}).get("as_of")
        if as_of and now.astimezone(business_timezone()).date() > (
            date.fromisoformat(as_of) + timedelta(days=1)
        ):
            return True
    return False


def cleanup_old_events(db: Session, *, now: Optional[datetime] = None) -> int:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=RETENTION_DAYS)
    deleted = (
        db.query(NotificationEvent)
        .filter(
            NotificationEvent.status != STATUS_PENDING,
            NotificationEvent.kind != KIND_DIVIDEND_SUGGESTION,
            NotificationEvent.created_at < cutoff,
        )
        .delete(synchronize_session=False)
    )
    db.commit()
    return deleted or 0


def notify_price_moves(db: Session, moves: Iterable[Dict[str, Any]]) -> int:
    """实时价刷新后调用。moves: [{symbol, market, name, price, prev_close, pct, as_of}]，
    调用方传入所有带昨收的持仓报价；这里按 |pct| ≥ NOTIFY_PRICE_MOVE_PCT 筛选。
    同一标的每个行情日只记一次（键含 as_of），同一轮的多只合并成一条消息立即发送。
    返回新记录的事件数。永不抛异常（不得拖垮价格刷新）。
    """
    if not settings.event_notifications_enabled:
        return 0
    try:
        threshold = float(settings.notify_price_move_pct)
        events = []
        seen = set()
        for move in moves:
            try:
                pct = float(move["pct"])
            except (KeyError, TypeError, ValueError):
                continue
            if abs(pct) < threshold:
                continue
            as_of = move.get("as_of")
            if not as_of:
                # 没有行情日期就无法按交易日去重（节假日的陈旧报价会天天报一次），不判
                continue
            as_of_text = as_of.isoformat() if isinstance(as_of, date) else str(as_of)[:10]
            key = f"{KIND_PRICE_MOVE}:{move['symbol']}:{move['market']}:{as_of_text}"
            if key in seen:
                continue
            seen.add(key)
            events.append(
                {
                    "event_key": key,
                    "kind": KIND_PRICE_MOVE,
                    "user_id": None,
                    "message": price_move_line(move),
                    "payload": {
                        "symbol": move["symbol"],
                        "market": move["market"],
                        "price": _fmt_decimal(move.get("price")),
                        "prev_close": _fmt_decimal(move.get("prev_close")),
                        "pct": round(pct, 4),
                        "as_of": as_of_text,
                    },
                }
            )
        if not events:
            return 0
        inserted = record_events(db, events)
        db.commit()
        if inserted:
            send_pending(db)
        return inserted
    except Exception:  # noqa: BLE001 - 提醒失败只记日志
        db.rollback()
        logger.exception("价格异动提醒处理失败")
        return 0


def run_event_notifications(db: Session, *, now: Optional[datetime] = None) -> Dict[str, int]:
    """一轮：收集新分红建议、除净日临近与重大公告事件，发送全部 pending（含上轮失败待重试）。"""
    moment = now or datetime.now(timezone.utc)
    local_now = moment.astimezone(business_timezone())
    today = local_now.date()
    events = collect_dividend_suggestion_events(db)
    if local_now.time() >= EX_DATE_NOTIFY_AFTER:
        events.extend(collect_ex_date_events(db, today))
    events.extend(collect_announcement_events(db, moment))
    recorded = record_events(db, events)
    db.commit()
    counts = send_pending(db, now=moment)
    cleanup_old_events(db, now=moment)
    return {"recorded": recorded, **counts}


@periodic_outcome_task
def periodic_event_notifications() -> PeriodicOutcome:
    """周期入口（10 分钟）：开关关闭为 skipped；发送失败（待重试或重试用尽）计 failed。"""
    if not settings.event_notifications_enabled:
        return PeriodicOutcome.skipped("EVENT_NOTIFICATIONS_ENABLED=false")
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        counts = run_event_notifications(db)
    finally:
        db.close()
    failures = counts[STATUS_FAILED] + counts["retry"]
    if failures:
        return PeriodicOutcome.failed(f"{failures} 条事件提醒发送失败", count=counts[STATUS_SENT])
    return PeriodicOutcome.succeeded(count=counts[STATUS_SENT])


def list_events(db: Session, limit: int = 50) -> List[NotificationEvent]:
    return (
        db.query(NotificationEvent)
        .order_by(NotificationEvent.created_at.desc(), NotificationEvent.id.desc())
        .limit(limit)
        .all()
    )
