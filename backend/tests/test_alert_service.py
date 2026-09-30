"""告警状态机：纯函数判定表 + 落库/推送的去重、提醒、升级、恢复语义。"""

from datetime import datetime, timedelta, timezone

import pytest

from app.database import SessionLocal
from app.models.alert_state import AlertState
from app.services import alert_service as svc
from app.services.alert_service import Alert, StateView

T0 = datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #
def _state(status="active", severity="warning", notified=T0, count=1):
    return StateView(
        status=status, severity=severity, last_notified_at=notified, notify_count=count
    )


A_WARN = Alert("k", "warning", "t")
A_CRIT = Alert("k", "critical", "t")
A_INFO = Alert("k", "info", "t")


@pytest.mark.parametrize(
    "state,alert,now,expected",
    [
        (None, A_WARN, T0, svc.ACTION_NEW),
        (_state(status="resolved"), A_WARN, T0, svc.ACTION_NEW),
        (_state(), A_WARN, T0 + timedelta(hours=1), svc.ACTION_UPDATE),
        (_state(), A_WARN, T0 + timedelta(hours=24), svc.ACTION_REMIND),
        (_state(notified=None, count=0), A_WARN, T0, svc.ACTION_REMIND),
        (_state(), A_CRIT, T0 + timedelta(minutes=10), svc.ACTION_ESCALATE),
        (_state(severity="info", notified=None, count=0), A_WARN, T0, svc.ACTION_ESCALATE),
        (_state(severity="critical"), A_WARN, T0 + timedelta(hours=1), svc.ACTION_UPDATE),
        (_state(), None, T0, svc.ACTION_RESOLVE),
        (_state(status="resolved"), None, T0, svc.ACTION_NOOP),
        (None, None, T0, svc.ACTION_NOOP),
    ],
)
def test_decide_table(state, alert, now, expected):
    assert svc.decide(state, alert, now=now, reminder_hours=24) == expected


def test_wants_push_respects_threshold_and_only_recovers_pushed_alerts(monkeypatch):
    monkeypatch.setattr(svc.notifier.settings, "notify_min_severity", "warning")
    assert svc.wants_push(svc.ACTION_NEW, "warning", 0)
    assert not svc.wants_push(svc.ACTION_NEW, "info", 0)
    assert not svc.wants_push(svc.ACTION_UPDATE, "critical", 1)
    assert svc.wants_push(svc.ACTION_RESOLVE, "info", 1)
    assert not svc.wants_push(svc.ACTION_RESOLVE, "warning", 0)


def test_compose_message_wording():
    title, body = svc.compose_message(
        svc.ACTION_NEW,
        title="雪球 Cookie 即将过期",
        severity="critical",
        message="还有 2 天",
        first_seen_at=T0,
        now=T0,
        notify_count=0,
    )
    assert title == "【严重】雪球 Cookie 即将过期" and body == "还有 2 天"
    title, body = svc.compose_message(
        svc.ACTION_REMIND,
        title="x",
        severity="warning",
        message="m",
        first_seen_at=T0,
        now=T0 + timedelta(hours=25),
        notify_count=1,
    )
    assert title == "【仍未恢复·警告】x" and "已持续 25 小时" in body
    title, body = svc.compose_message(
        svc.ACTION_RESOLVE,
        title="x",
        severity="warning",
        message="m",
        first_seen_at=T0,
        now=T0 + timedelta(days=3, hours=2),
        notify_count=2,
    )
    assert title == "【已恢复】x" and "3 天 2 小时" in body


# --------------------------------------------------------------------------- #
# 落库 + 推送
# --------------------------------------------------------------------------- #
class Sender:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def __call__(self, title, body, *, severity, kind):
        self.calls.append({"title": title, "body": body, "severity": severity, "kind": kind})
        return {"ok": self.ok, "status": "sent" if self.ok else "failed", "message": "m"}


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(svc.settings, "notify_min_severity", "warning")
    monkeypatch.setattr(svc.settings, "notify_reminder_hours", 24)
    session = SessionLocal()
    session.query(AlertState).delete()
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.query(AlertState).delete()
        session.commit()
        session.close()


def _row(db, key):
    db.expire_all()
    return db.query(AlertState).filter_by(alert_key=key).one_or_none()


def test_new_alert_notifies_once_then_stays_quiet(db):
    sender = Sender()
    svc.evaluate_alerts(
        db, "src", [Alert("a", "warning", "标题", "详情", {"x": 1})], now=T0, sender=sender
    )
    svc.evaluate_alerts(
        db,
        "src",
        [Alert("a", "warning", "标题", "详情2")],
        now=T0 + timedelta(minutes=10),
        sender=sender,
    )
    assert len(sender.calls) == 1 and sender.calls[0]["title"] == "【警告】标题"
    row = _row(db, "a")
    assert row.status == "active" and row.notify_count == 1
    assert row.first_seen_at == T0 and row.last_notified_at == T0
    assert row.last_seen_at == T0 + timedelta(minutes=10) and row.message == "详情2"
    assert row.payload["last_notify"]["status"] == "sent"


def test_reminder_after_interval_and_escalation(db):
    sender = Sender()
    alert = Alert("a", "warning", "t")
    svc.evaluate_alerts(db, "src", [alert], now=T0, sender=sender)
    svc.evaluate_alerts(db, "src", [alert], now=T0 + timedelta(hours=23), sender=sender)
    svc.evaluate_alerts(db, "src", [alert], now=T0 + timedelta(hours=24), sender=sender)
    assert [c["title"] for c in sender.calls] == ["【警告】t", "【仍未恢复·警告】t"]
    svc.evaluate_alerts(
        db,
        "src",
        [Alert("a", "critical", "t")],
        now=T0 + timedelta(hours=24, minutes=10),
        sender=sender,
    )
    assert sender.calls[-1]["title"] == "【升级·严重】t"
    assert sender.calls[-1]["severity"] == "critical"
    assert _row(db, "a").notify_count == 3


def test_resolve_within_source_only_and_recovery_is_pushed(db):
    sender = Sender()
    svc.evaluate_alerts(
        db, "src", [Alert("a", "warning", "t"), Alert("b", "warning", "u")], now=T0, sender=sender
    )
    svc.evaluate_alerts(db, "other", [Alert("c", "critical", "v")], now=T0, sender=sender)
    outcomes = svc.evaluate_alerts(
        db, "src", [Alert("b", "warning", "u")], now=T0 + timedelta(hours=2), sender=sender
    )
    assert {o["key"]: o["action"] for o in outcomes} == {"b": "update", "a": "resolve"}
    assert sender.calls[-1]["title"] == "【已恢复】t" and sender.calls[-1]["kind"] == "resolved"
    assert _row(db, "a").status == "resolved"
    assert _row(db, "a").resolved_at == T0 + timedelta(hours=2)
    assert _row(db, "c").status == "active"  # 别的检查器的告警不受影响
    # 再次触发 = 新事件：重置首次时间与计数，重新推送
    svc.evaluate_alerts(
        db,
        "src",
        [Alert("a", "warning", "t"), Alert("b", "warning", "u")],
        now=T0 + timedelta(hours=3),
        sender=sender,
    )
    row = _row(db, "a")
    assert row.status == "active" and row.first_seen_at == T0 + timedelta(hours=3)
    assert row.notify_count == 1 and row.resolved_at is None
    assert sender.calls[-1]["title"] == "【警告】t"


def test_info_alerts_are_recorded_but_not_pushed_nor_recovered(db):
    sender = Sender()
    svc.evaluate_alerts(db, "src", [Alert("i", "info", "t")], now=T0, sender=sender)
    svc.evaluate_alerts(db, "src", [], now=T0 + timedelta(hours=1), sender=sender)
    assert sender.calls == []
    assert _row(db, "i").status == "resolved"


def test_failed_push_is_retried_on_next_evaluation(db):
    failing = Sender(ok=False)
    svc.evaluate_alerts(db, "src", [Alert("a", "critical", "t")], now=T0, sender=failing)
    row = _row(db, "a")
    assert row.notify_count == 0 and row.last_notified_at is None
    assert row.payload["last_notify"]["status"] == "failed"
    ok = Sender()
    svc.evaluate_alerts(
        db, "src", [Alert("a", "critical", "t")], now=T0 + timedelta(minutes=10), sender=ok
    )
    assert [c["title"] for c in ok.calls] == ["【严重】t"]  # 首次送达，仍按「新告警」措辞
    assert _row(db, "a").notify_count == 1


def test_duplicate_keys_in_one_round_keep_highest_severity(db):
    sender = Sender()
    svc.evaluate_alerts(
        db, "src", [Alert("a", "warning", "t"), Alert("a", "critical", "t")], now=T0, sender=sender
    )
    assert _row(db, "a").severity == "critical" and len(sender.calls) == 1


def test_external_raise_and_resolve_do_not_touch_other_keys(db):
    sender = Sender()
    svc.raise_alert(db, Alert("backup", "critical", "数据库备份失败"), now=T0, sender=sender)
    svc.raise_alert(db, Alert("other", "warning", "x"), now=T0, sender=sender)
    svc.raise_alert(
        db,
        Alert("backup", "critical", "数据库备份失败"),
        now=T0 + timedelta(hours=1),
        sender=sender,
    )
    assert len(sender.calls) == 2  # 持续失败不重复推送
    outcome = svc.resolve_alert(db, "backup", now=T0 + timedelta(hours=2), sender=sender)
    assert (
        outcome["action"] == "resolve" and sender.calls[-1]["title"] == "【已恢复】数据库备份失败"
    )
    assert _row(db, "other").status == "active"
    assert svc.resolve_alert(db, "missing", sender=sender)["action"] == "noop"
    assert svc.resolve_alert(db, "backup", sender=sender)["action"] == "noop"


def test_sweep_reminders_for_external_alerts(db):
    sender = Sender()
    svc.raise_alert(db, Alert("backup", "critical", "t"), now=T0, sender=sender)
    assert svc.sweep_reminders(db, "external", now=T0 + timedelta(hours=1), sender=sender) == []
    outcomes = svc.sweep_reminders(db, "external", now=T0 + timedelta(hours=25), sender=sender)
    assert [o["action"] for o in outcomes] == ["remind"]
    assert sender.calls[-1]["title"] == "【仍未恢复·严重】t"


def test_list_alerts_orders_by_severity_and_counts(db):
    sender = Sender()
    now = svc.utcnow()
    svc.evaluate_alerts(
        db,
        "src",
        [
            Alert("w", "warning", "w"),
            Alert("c", "critical", "c"),
            Alert("i", "info", "i"),
            Alert("gone", "warning", "g"),
        ],
        now=now,
        sender=sender,
    )
    svc.evaluate_alerts(
        db,
        "src",
        [Alert("w", "warning", "w"), Alert("c", "critical", "c"), Alert("i", "info", "i")],
        now=now,
        sender=sender,
    )
    listing = svc.list_alerts(db)
    assert [a["alert_key"] for a in listing["active"]] == ["c", "w", "i"]
    assert [a["alert_key"] for a in listing["recent_resolved"]] == ["gone"]
    assert listing["counts"] == {
        "active": 3,
        "info": 1,
        "warning": 1,
        "critical": 1,
        "recent_resolved": 1,
    }
    assert "last_notify" not in listing["active"][0]["payload"]


# --------------------------------------------------------------------------- #
# PR #255 评审：未送达的升级 / 恢复通知必须重试
# --------------------------------------------------------------------------- #
class FlakySender(Sender):
    """按调用顺序返回预设的成败。"""

    def __init__(self, results):
        super().__init__()
        self.results = list(results)

    def __call__(self, title, body, *, severity, kind):
        self.ok = self.results.pop(0)
        return super().__call__(title, body, severity=severity, kind=kind)


def test_decide_retries_undelivered_escalation():
    # 已按 warning 送达，severity 已升到 critical 但升级推送失败
    state = StateView(
        status="active",
        severity="critical",
        last_notified_at=T0,
        notify_count=1,
        notified_severity="warning",
    )
    assert (
        svc.decide(state, A_CRIT, now=T0 + timedelta(minutes=10), reminder_hours=24)
        == svc.ACTION_ESCALATE
    )
    delivered = StateView(
        status="active",
        severity="critical",
        last_notified_at=T0,
        notify_count=2,
        notified_severity="critical",
    )
    assert (
        svc.decide(delivered, A_CRIT, now=T0 + timedelta(minutes=10), reminder_hours=24)
        == svc.ACTION_UPDATE
    )


def test_failed_escalation_is_retried_next_round(db):
    sender = FlakySender([True, False, True])
    svc.evaluate_alerts(db, "src", [Alert("a", "warning", "t")], now=T0, sender=sender)
    svc.evaluate_alerts(
        db, "src", [Alert("a", "critical", "t")], now=T0 + timedelta(minutes=10), sender=sender
    )
    row = _row(db, "a")
    assert row.severity == "critical" and row.notified_severity == "warning"
    assert row.notify_count == 1 and row.last_notified_at == T0
    outcomes = svc.evaluate_alerts(
        db, "src", [Alert("a", "critical", "t")], now=T0 + timedelta(minutes=20), sender=sender
    )
    assert outcomes[0]["action"] == "escalate" and outcomes[0]["notified"] is True
    assert [c["title"] for c in sender.calls] == ["【警告】t", "【升级·严重】t", "【升级·严重】t"]
    row = _row(db, "a")
    assert row.notified_severity == "critical" and row.notify_count == 2
    # 送达后不再重复
    svc.evaluate_alerts(
        db, "src", [Alert("a", "critical", "t")], now=T0 + timedelta(minutes=30), sender=sender
    )
    assert len(sender.calls) == 3


def test_deescalate_then_escalate_notifies_again(db):
    sender = Sender()
    svc.evaluate_alerts(db, "src", [Alert("a", "critical", "t")], now=T0, sender=sender)
    svc.evaluate_alerts(
        db, "src", [Alert("a", "warning", "t")], now=T0 + timedelta(minutes=10), sender=sender
    )
    svc.evaluate_alerts(
        db, "src", [Alert("a", "critical", "t")], now=T0 + timedelta(minutes=20), sender=sender
    )
    assert [c["title"] for c in sender.calls] == ["【严重】t", "【升级·严重】t"]


def test_failed_recovery_notice_is_retried(db):
    sender = FlakySender([True, False, True])
    svc.evaluate_alerts(db, "src", [Alert("a", "warning", "t")], now=T0, sender=sender)
    svc.evaluate_alerts(db, "src", [], now=T0 + timedelta(hours=1), sender=sender)
    row = _row(db, "a")
    assert row.status == "resolved" and row.resolve_notified_at is None
    assert row.payload["last_notify"]["status"] == "failed"
    # 下一轮检查：再次判定恢复是 noop，但待发的恢复通知会重试
    assert (
        svc.evaluate_alerts(db, "src", [], now=T0 + timedelta(hours=1, minutes=10), sender=sender)
        == []
    )
    retried = svc.retry_recovery_notices(db, now=T0 + timedelta(hours=1, minutes=10), sender=sender)
    assert [(o["key"], o["notified"]) for o in retried] == [("a", True)]
    assert sender.calls[-1]["title"] == "【已恢复】t"
    assert "已持续 1 小时" in sender.calls[-1]["body"]  # 时长算到恢复时刻，不是重试时刻
    assert _row(db, "a").resolve_notified_at == T0 + timedelta(hours=1, minutes=10)
    assert svc.retry_recovery_notices(db, now=T0 + timedelta(hours=2), sender=sender) == []
    assert len(sender.calls) == 3


def test_recovery_retry_gives_up_after_reminder_window_and_skips_unpushed(db):
    failing = Sender(ok=False)
    svc.evaluate_alerts(
        db, "src", [Alert("a", "warning", "t"), Alert("i", "info", "t")], now=T0, sender=Sender()
    )
    svc.evaluate_alerts(db, "src", [], now=T0 + timedelta(hours=1), sender=failing)
    # info 级从未推送：没有待发的恢复通知
    assert [
        o["key"]
        for o in svc.retry_recovery_notices(db, now=T0 + timedelta(hours=2), sender=failing)
    ] == ["a"]
    assert svc.retry_recovery_notices(db, now=T0 + timedelta(hours=26), sender=failing) == []


def test_run_checks_retries_recovery_notices(db):
    from app.services import alert_checks

    sender = FlakySender([True, False, True])
    checkers_on = [("x", "x_src", lambda *_: [Alert("x:1", "warning", "t")])]
    checkers_off = [("x", "x_src", lambda *_: [])]
    alert_checks.run_checks(db, checkers=checkers_on, sender=sender)
    alert_checks.run_checks(db, checkers=checkers_off, sender=sender)
    assert _row(db, "x:1").resolve_notified_at is None
    alert_checks.run_checks(db, checkers=checkers_off, sender=sender)
    assert _row(db, "x:1").resolve_notified_at is not None
    assert [c["title"] for c in sender.calls] == ["【警告】t", "【已恢复】t", "【已恢复】t"]


def test_concurrent_evaluation_during_a_slow_push_does_not_push_twice(db):
    """PR #303 评审：推送在锁外进行，推送期间另一个会话（周期检查 vs「立即检查」）评估同一个
    键时，锁内已乐观推进 last_notified_at，它判成 update 而不是再推一次 new/remind。"""
    calls = []

    def nested_sender(title, body, *, severity, kind):
        calls.append(title)
        if len(calls) == 1:
            other = SessionLocal()
            try:
                svc.evaluate_alerts(
                    other,
                    "src",
                    [Alert("a", "warning", "t")],
                    now=T0 + timedelta(seconds=5),
                    sender=nested_sender,
                )
            finally:
                other.close()
        return {"ok": True, "status": "sent", "message": "m"}

    svc.evaluate_alerts(db, "src", [Alert("a", "warning", "t")], now=T0, sender=nested_sender)
    assert calls == ["【警告】t"]
    row = _row(db, "a")
    assert row.notify_count == 1 and row.notified_severity == "warning"


def test_failed_push_rolls_back_its_claim_but_not_a_concurrent_escalation(db):
    """没送达：回滚本次认领，下一轮重推；推送期间别的会话已升级并送达，则不把它改回去。"""
    failing = Sender(ok=False)
    svc.evaluate_alerts(db, "src", [Alert("a", "warning", "t")], now=T0, sender=failing)
    row = _row(db, "a")
    assert row.last_notified_at is None and row.notify_count == 0 and row.notified_severity is None

    ok = Sender()
    svc.evaluate_alerts(
        db, "src", [Alert("a", "warning", "t")], now=T0 + timedelta(minutes=10), sender=ok
    )
    assert len(ok.calls) == 1  # 回滚后下一轮重推

    def failing_while_escalated(title, body, *, severity, kind):
        other = SessionLocal()
        try:
            svc.evaluate_alerts(
                other,
                "src",
                [Alert("a", "critical", "t")],
                now=T0 + timedelta(hours=30, seconds=5),
                sender=Sender(),
            )
        finally:
            other.close()
        return {"ok": False, "status": "failed", "message": "timeout"}

    svc.evaluate_alerts(
        db,
        "src",
        [Alert("a", "warning", "t")],
        now=T0 + timedelta(hours=30),
        sender=failing_while_escalated,
    )
    row = _row(db, "a")
    assert row.notified_severity == "critical"  # 别的会话送达的升级不被回滚
    assert row.last_notified_at == T0 + timedelta(hours=30, seconds=5)


def test_claim_left_by_a_crashed_push_expires_and_the_alert_is_pushed_again(db, monkeypatch):
    """PR #303 复审：认领后、推送回执前进程退出，乐观推进的送达字段不能让这条告警在整个
    提醒间隔里都不再推送——过期未回执的认领在下一次加锁时回滚。"""
    monkeypatch.setattr(
        svc, "_deliver", lambda db, pending, *, now, sender: None
    )  # 进程在推送前退出
    svc.evaluate_alerts(db, "src", [Alert("a", "critical", "t")], now=T0, sender=Sender())
    row = _row(db, "a")
    assert row.notify_count == 1 and row.payload.get("sending")
    monkeypatch.undo()

    sender = Sender()
    svc.evaluate_alerts(
        db, "src", [Alert("a", "critical", "t")], now=T0 + timedelta(minutes=5), sender=sender
    )
    assert sender.calls == []  # 认领未过期：仍按在飞处理，不重复推
    svc.evaluate_alerts(
        db,
        "src",
        [Alert("a", "critical", "t")],
        now=T0 + svc.CLAIM_TTL + timedelta(minutes=1),
        sender=sender,
    )
    assert len(sender.calls) == 1  # 认领过期回滚，按未送达重推
    row = _row(db, "a")
    assert row.notify_count == 1 and "sending" not in row.payload


def test_recovery_waits_for_the_in_flight_first_push(db):
    """PR #303 复审：首推在飞时另一会话判恢复，不能先发「已恢复」——首推失败的话用户只会
    收到恢复。恢复通知交给 retry_recovery_notices，首推送达后才补发。"""
    nested_calls = []

    def nested_sender(title, body, *, severity, kind):
        nested_calls.append(title)
        return {"ok": True, "status": "sent", "message": "m"}

    def first_push(outcome_ok):
        def sender(title, body, *, severity, kind):
            other = SessionLocal()
            try:  # 推送期间告警消失：另一会话判恢复
                svc.evaluate_alerts(
                    other, "src", [], now=T0 + timedelta(seconds=5), sender=nested_sender
                )
            finally:
                other.close()
            return {"ok": outcome_ok, "status": "sent" if outcome_ok else "failed", "message": "m"}

        return sender

    svc.evaluate_alerts(db, "src", [Alert("a", "warning", "t")], now=T0, sender=first_push(False))
    assert nested_calls == []
    row = _row(db, "a")
    assert row.status == "resolved" and row.notify_count == 0  # 首推失败回滚
    svc.retry_recovery_notices(db, now=T0 + timedelta(minutes=1), sender=nested_sender)
    assert nested_calls == []  # 告警从没送达过，不补发「已恢复」

    db.query(AlertState).delete()
    db.commit()
    svc.evaluate_alerts(db, "src", [Alert("a", "warning", "t")], now=T0, sender=first_push(True))
    assert nested_calls == []
    svc.retry_recovery_notices(db, now=T0 + timedelta(minutes=1), sender=nested_sender)
    assert nested_calls == ["【已恢复】t"]  # 首推送达后补发
