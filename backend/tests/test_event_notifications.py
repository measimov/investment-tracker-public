"""事件提醒：新分红建议、除净日临近、价格异动的去重、合并、失败重试与 API。"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.core.security import get_password_hash
from app.core.timeutil import business_timezone
from app.database import SessionLocal
from app.main import app
from app.models.corporate_action_suggestion import CorporateActionSuggestion
from app.models.holding import Holding
from app.models.notification_event import NotificationEvent
from app.models.user import User
from app.services import event_notifications as en
from app.services import notification_service as ns

USER_ID = 2  # demo（种子用户）
PASSWORD = "event-notifications-password"


class FakeSend:
    """替身 notification_service.send：按预设状态返回，记录调用。"""

    def __init__(self, status="sent"):
        self.status = status
        self.calls = []

    def __call__(self, title, body, *, severity="warning", kind="alert", urls=None):
        self.calls.append({"title": title, "body": body, "severity": severity})
        ok = self.status in ("sent", "partial")
        return {
            "ok": ok,
            "status": self.status,
            "message": f"fake {self.status}",
            "configured": 0 if self.status == "unconfigured" else 1,
            "sent": 1 if ok else 0,
            "channels": [],
        }


@pytest.fixture
def fake_send(monkeypatch):
    fake = FakeSend()
    monkeypatch.setattr(ns, "send", fake)
    monkeypatch.setattr(en.settings, "event_notifications_enabled", True)
    monkeypatch.setattr(en.settings, "notify_price_move_pct", 7)
    monkeypatch.setattr(en.settings, "notify_ex_date_days_ahead", 3)
    # 重大公告推送另有 test_announcement_delivery 覆盖；这里关掉以免共享库里的公告行串入
    monkeypatch.setattr(en.settings, "announcement_notify_enabled", False)
    # 免打扰时段按真实时钟判定会让夜里跑的用例失败；专门的用例单独验证它
    monkeypatch.setattr(en, "in_quiet_hours", lambda moment: False)
    return fake


REAL_IN_QUIET_HOURS = en.in_quiet_hours


@pytest.fixture
def db():
    session = SessionLocal()

    def clean():
        session.rollback()
        session.query(NotificationEvent).delete()
        session.query(CorporateActionSuggestion).filter(
            CorporateActionSuggestion.user_id == USER_ID,
            CorporateActionSuggestion.symbol.like("EVT%"),
        ).delete(synchronize_session=False)
        session.query(Holding).filter(
            Holding.user_id == USER_ID, Holding.symbol.like("EVT%")
        ).delete(synchronize_session=False)
        session.commit()

    clean()
    try:
        yield session
    finally:
        clean()
        session.close()


def _suggestion(db, symbol, ex_date, *, status="NEW", market="港股", **extra):
    row = CorporateActionSuggestion(
        user_id=USER_ID,
        symbol=symbol,
        name=extra.pop("name", "测试公司"),
        market=market,
        action_type=extra.pop("action_type", "CASH_DIVIDEND"),
        ex_date=ex_date,
        pay_date=extra.pop("pay_date", None),
        currency=extra.pop("currency", "HKD"),
        cash_div_pre_tax=extra.pop("cash_div_pre_tax", Decimal("0.25")),
        status=status,
        **extra,
    )
    db.add(row)
    db.commit()
    return row


def _holding(db, symbol, quantity, market="港股"):
    db.add(
        Holding(
            user_id=USER_ID,
            symbol=symbol,
            market=market,
            quantity=Decimal(quantity),
            avg_cost=Decimal("1"),
            total_cost=Decimal(quantity),
            currency="HKD",
        )
    )
    db.commit()


def _events(db, kind=None):
    query = db.query(NotificationEvent)
    if kind:
        query = query.filter(NotificationEvent.kind == kind)
    return query.order_by(NotificationEvent.id).all()


# 业务时区 2026-09-28 10:00（09:00 之后，除净日提醒可发）
NOW = datetime(2026, 9, 28, 10, 0, tzinfo=business_timezone()).astimezone(timezone.utc)
TODAY = date(2026, 9, 28)


def test_new_dividend_suggestions_merged_and_sent_once(db, fake_send):
    _suggestion(db, "EVT01", TODAY + timedelta(days=20))
    _suggestion(db, "EVT02", TODAY + timedelta(days=30), cash_div_pre_tax=Decimal("1.5"))
    _suggestion(db, "EVT03", TODAY + timedelta(days=30), status="IGNORED")

    counts = en.run_event_notifications(db, now=NOW)
    assert counts["recorded"] == 2 and counts["sent"] == 2
    assert len(fake_send.calls) == 1  # 合并成一条
    call = fake_send.calls[0]
    assert "新分红建议待确认（2 条）" in call["title"]
    assert "EVT01" in call["body"] and "EVT02" in call["body"] and "EVT03" not in call["body"]
    assert "每股 HKD 1.5" in call["body"]
    assert all(e.status == "sent" and e.sent_at is not None for e in _events(db))

    # 再跑一轮：已记录的建议不重复推送
    counts = en.run_event_notifications(db, now=NOW)
    assert counts["recorded"] == 0 and len(fake_send.calls) == 1


def test_username_prefix_when_multiple_active_users(db, fake_send):
    # 种子里 admin 与 demo 都是活跃用户 → 消息带用户名前缀
    _suggestion(db, "EVT01", TODAY + timedelta(days=20))
    en.run_event_notifications(db, now=NOW)
    assert fake_send.calls[0]["title"].startswith("[demo] ")


def test_ex_date_only_for_held_and_within_window(db, fake_send):
    _holding(db, "EVT10", "1000")
    _holding(db, "EVT11", "0")  # 已清仓
    _suggestion(
        db,
        "EVT10",
        TODAY + timedelta(days=2),
        status="ACCEPTED",
        pay_date=TODAY + timedelta(days=20),
    )
    _suggestion(db, "EVT11", TODAY + timedelta(days=1), status="MATCHED")
    _holding(db, "EVT12", "500")
    _suggestion(db, "EVT12", TODAY + timedelta(days=5), status="MATCHED")  # 超出 3 天窗口

    en.run_event_notifications(db, now=NOW)
    ex_events = _events(db, en.KIND_EX_DATE)
    assert [e.payload["symbol"] for e in ex_events] == ["EVT10"]
    assert (
        ex_events[0].event_key
        == f"ex_date:{USER_ID}:EVT10:港股:{(TODAY + timedelta(days=2)).isoformat()}"
    )
    assert "2 天后除净" in ex_events[0].message and "派息" in ex_events[0].message


def test_ex_date_waits_until_nine(db, fake_send):
    _holding(db, "EVT10", "1000")
    _suggestion(db, "EVT10", TODAY + timedelta(days=1), status="ACCEPTED")
    early = datetime(2026, 9, 28, 8, 30, tzinfo=business_timezone()).astimezone(timezone.utc)
    en.run_event_notifications(db, now=early)
    assert _events(db, en.KIND_EX_DATE) == []
    en.run_event_notifications(db, now=NOW)
    assert len(_events(db, en.KIND_EX_DATE)) == 1


def test_ex_date_deduplicates_per_account_suggestions(db, fake_send):
    _holding(db, "EVT10", "1000")
    ex = TODAY + timedelta(days=1)
    _suggestion(db, "EVT10", ex, status="NEW")
    # 同一标的同一除净日的另一条建议（送转）：同一个除净日只提醒一次
    _suggestion(
        db,
        "EVT10",
        ex,
        status="MATCHED",
        action_type="STOCK_DIVIDEND",
        stk_div_per_share=Decimal("0.1"),
    )
    en.run_event_notifications(db, now=NOW)
    assert len(_events(db, en.KIND_EX_DATE)) == 1


def test_failed_send_keeps_retrying_within_the_window_then_gives_up(db, fake_send):
    """#273：此前固定 3 次（约 30 分钟）就永久放弃；现在 24 小时窗口内一直重试。"""
    fake_send.status = "failed"
    _suggestion(db, "EVT01", TODAY + timedelta(days=20))
    for attempt in range(1, 6):
        counts = en.run_event_notifications(db, now=NOW)
        event = _events(db)[0]
        assert event.status == "pending" and event.attempts == attempt
        assert counts["retry"] == 1

    event.created_at = NOW - en.RETRY_WINDOW - timedelta(minutes=1)
    db.commit()
    counts = en.run_event_notifications(db, now=NOW)
    event = _events(db)[0]
    assert event.status == "failed" and event.attempts == 6
    assert counts["failed"] == 1
    # 放弃后不再尝试
    en.run_event_notifications(db, now=NOW)
    assert len(fake_send.calls) == 6


def test_ex_date_reminder_gives_up_once_the_ex_date_has_passed(db, fake_send):
    fake_send.status = "failed"
    _holding(db, "EVT10", "1000")
    _suggestion(db, "EVT10", TODAY + timedelta(days=1), status="ACCEPTED")
    en.run_event_notifications(db, now=NOW)
    assert _events(db, en.KIND_EX_DATE)[0].status == "pending"

    after_ex_date = NOW + timedelta(days=2)  # 仍在 24 小时窗口之外也无妨：除净日已过
    en.run_event_notifications(db, now=after_ex_date)
    assert _events(db, en.KIND_EX_DATE)[0].status == "failed"


def test_retry_succeeds_next_tick(db, fake_send):
    fake_send.status = "failed"
    _suggestion(db, "EVT01", TODAY + timedelta(days=20))
    en.run_event_notifications(db, now=NOW)
    fake_send.status = "sent"
    en.run_event_notifications(db, now=NOW)
    event = _events(db)[0]
    assert event.status == "sent" and event.attempts == 2


def test_unconfigured_marks_skipped(db, fake_send):
    fake_send.status = "unconfigured"
    _suggestion(db, "EVT01", TODAY + timedelta(days=20))
    counts = en.run_event_notifications(db, now=NOW)
    assert counts["skipped"] == 1
    assert _events(db)[0].status == "skipped"
    en.run_event_notifications(db, now=NOW)
    assert len(fake_send.calls) == 1  # skipped 不重试


def test_price_moves_threshold_dedup_and_merge(db, fake_send):
    moves = [
        {
            "symbol": "EVT20",
            "market": "港股",
            "name": "甲",
            "price": Decimal("10.8"),
            "prev_close": Decimal("10"),
            "pct": 8.0,
            "as_of": TODAY,
        },
        {
            "symbol": "EVT21",
            "market": "A股",
            "name": "乙",
            "price": Decimal("9.2"),
            "prev_close": Decimal("10"),
            "pct": -8.0,
            "as_of": TODAY,
        },
        {
            "symbol": "EVT22",
            "market": "A股",
            "name": "丙",
            "price": Decimal("10.5"),
            "prev_close": Decimal("10"),
            "pct": 5.0,
            "as_of": TODAY,
        },
        {
            "symbol": "EVT23",
            "market": "A股",
            "name": "丁",
            "price": None,
            "prev_close": None,
            "pct": None,
            "as_of": TODAY,
        },
    ]
    assert en.notify_price_moves(db, moves) == 2
    assert len(fake_send.calls) == 1
    body = fake_send.calls[0]["body"]
    assert "EVT20 甲（港股） 09-28 +8.00%：10 → 10.8" in body  # 带行情日
    assert "EVT21 乙（A股） 09-28 -8.00%" in body and "EVT22" not in body
    # 同一行情日再次触发不重复
    assert en.notify_price_moves(db, moves) == 0
    assert len(fake_send.calls) == 1
    # 下一个行情日可以再推
    moves[0]["as_of"] = TODAY + timedelta(days=1)
    assert en.notify_price_moves(db, moves[:1]) == 1
    assert len(fake_send.calls) == 2


def test_disabled_switch(db, fake_send, monkeypatch):
    monkeypatch.setattr(en.settings, "event_notifications_enabled", False)
    _suggestion(db, "EVT01", TODAY + timedelta(days=20))
    outcome = en.periodic_event_notifications()
    assert outcome.status == "skipped"
    assert (
        en.notify_price_moves(
            db,
            [
                {
                    "symbol": "EVT20",
                    "market": "港股",
                    "pct": 20.0,
                    "price": 12,
                    "prev_close": 10,
                    "as_of": TODAY,
                }
            ],
        )
        == 0
    )
    assert _events(db) == [] and fake_send.calls == []


def test_periodic_outcome_reports_failure(db, fake_send):
    fake_send.status = "failed"
    _suggestion(db, "EVT01", TODAY + timedelta(days=20))
    outcome = en.periodic_event_notifications()
    assert outcome.status == "failed"
    fake_send.status = "sent"
    assert en.periodic_event_notifications().status == "succeeded"


def test_message_lines_truncated():
    lines = [f"line{i}" for i in range(en.MAX_LINES + 3)]
    text = en.compose_message(lines)
    assert text.endswith("…另 3 条") and "line0" in text


def test_suggestion_line_stock_dividend():
    row = CorporateActionSuggestion(
        symbol="600000",
        name="浦发银行",
        market="A股",
        action_type="STOCK_DIVIDEND",
        ex_date=TODAY,
        stk_div_per_share=Decimal("0.30000000"),
        currency="CNY",
    )
    assert en.suggestion_line(row) == "600000 浦发银行 送转 每股 0.3（除净 09-28）"


def test_cleanup_old_events(db, fake_send):
    db.add(
        NotificationEvent(
            event_key="old:1",
            kind="price_move",
            status="sent",
            created_at=NOW - timedelta(days=en.RETENTION_DAYS + 1),
        )
    )
    db.add(
        NotificationEvent(
            event_key="old:2",
            kind="price_move",
            status="pending",
            created_at=NOW - timedelta(days=en.RETENTION_DAYS + 1),
        )
    )
    db.commit()
    assert en.cleanup_old_events(db, now=NOW) == 1
    assert [e.event_key for e in _events(db)] == ["old:2"]


@pytest.fixture
def clients(db):
    users = {u.username: u for u in db.query(User).filter(User.id.in_([1, 2])).all()}
    originals = {name: user.hashed_password for name, user in users.items()}
    for user in users.values():
        user.hashed_password = get_password_hash(PASSWORD)
    db.commit()
    result = {}
    for name in ("admin", "demo"):
        client = TestClient(app)
        token = client.post(
            "/api/auth/token", json={"username": name, "password": PASSWORD}
        ).json()["access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        result[name] = client
    try:
        yield result
    finally:
        for name, user in users.items():
            user.hashed_password = originals[name]
        db.commit()


def test_events_api_admin_only(db, fake_send, clients):
    _suggestion(db, "EVT01", TODAY + timedelta(days=20))
    en.run_event_notifications(db, now=NOW)

    assert clients["demo"].get("/api/notifications/events").status_code == 403
    response = clients["admin"].get("/api/notifications/events", params={"limit": 10})
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True and body["price_move_pct"] == 7
    assert body["ex_date_days_ahead"] == 3
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["kind"] == "dividend_suggestion" and item["status"] == "sent"
    assert "EVT01" in item["message"]


def test_quiet_hours_defer_then_send_merged(db, fake_send, monkeypatch):
    monkeypatch.setattr(en, "in_quiet_hours", REAL_IN_QUIET_HOURS)
    night = datetime(2026, 9, 28, 23, 30, tzinfo=business_timezone()).astimezone(timezone.utc)
    morning = datetime(2026, 9, 29, 8, 10, tzinfo=business_timezone()).astimezone(timezone.utc)
    _suggestion(db, "EVT01", TODAY + timedelta(days=20))
    en.run_event_notifications(db, now=night)
    assert fake_send.calls == []
    assert [e.status for e in _events(db)] == ["pending"]
    assert [e.attempts for e in _events(db)] == [0]  # 免打扰不计重试次数
    en.run_event_notifications(db, now=morning)
    assert len(fake_send.calls) == 1 and [e.status for e in _events(db)] == ["sent"]


def test_price_move_without_as_of_is_ignored(db, fake_send):
    move = {
        "symbol": "EVT20",
        "market": "港股",
        "name": "甲",
        "price": Decimal("12"),
        "prev_close": Decimal("10"),
        "pct": 20.0,
        "as_of": None,
    }
    assert en.notify_price_moves(db, [move]) == 0
    assert _events(db) == [] and fake_send.calls == []


def test_dividend_suggestion_events_are_never_cleaned_up(db, fake_send):
    db.add(
        NotificationEvent(
            event_key="dividend_suggestion:999999",
            kind="dividend_suggestion",
            status="sent",
            created_at=NOW - timedelta(days=en.RETENTION_DAYS + 1),
        )
    )
    db.commit()
    assert en.cleanup_old_events(db, now=NOW) == 0
    assert [e.event_key for e in _events(db)] == ["dividend_suggestion:999999"]


def test_concurrent_send_pending_pushes_each_event_once(db, fake_send, monkeypatch):
    """PR #302 评审：refresh_quotes（quotes 组）与 send_event_notifications（default 组）两条
    调度线程会并发调用 send_pending；待发送行按 FOR UPDATE SKIP LOCKED 认领，只推一次。"""
    import threading
    import time

    db.add(
        NotificationEvent(
            event_key="concurrent:1",
            kind="price_move",
            status="pending",
            user_id=USER_ID,
            title="t",
            message="EVT 异动",
        )
    )
    db.commit()

    first_sending = threading.Event()
    release = threading.Event()
    slow_calls = []

    def slow_send(title, body, **kwargs):
        slow_calls.append(title)
        first_sending.set()
        release.wait(timeout=10)  # 第一个调用方拿着行锁「推送」时，第二个调用方开跑
        return {
            "ok": True,
            "status": "sent",
            "message": "",
            "configured": 1,
            "sent": 1,
            "channels": [],
        }

    monkeypatch.setattr(ns, "send", slow_send)
    results = []

    def run():
        session = SessionLocal()
        try:
            results.append(en.send_pending(session, now=NOW))
        finally:
            session.close()

    first = threading.Thread(target=run)
    first.start()
    assert first_sending.wait(timeout=10)
    second = threading.Thread(target=run)
    second.start()
    time.sleep(0.3)
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert len(slow_calls) == 1
    assert sorted(r["sent"] for r in results) == [0, 1]
    db.expire_all()
    event = db.query(NotificationEvent).filter_by(event_key="concurrent:1").one()
    assert (event.status, event.attempts) == ("sent", 1)


def test_price_move_retry_stops_after_the_next_day():
    """PR #303 评审：价格异动按行情日次日截止，渠道恢复后不补发隔天的旧涨跌。"""
    # 事件在行情日当晚才建（美股收盘前后），24 小时窗口本身不会先截止
    created = datetime(2026, 9, 28, 23, 0, tzinfo=business_timezone())
    event = NotificationEvent(
        event_key="price_move:X:美股:2026-09-28",
        kind="price_move",
        payload={"as_of": "2026-09-28"},
        created_at=created,
    )
    next_day = datetime(2026, 9, 29, 20, 0, tzinfo=business_timezone())
    day_after = datetime(2026, 9, 30, 0, 30, tzinfo=business_timezone())
    assert en.retry_expired(event, next_day) is False  # 美股行情日跨北京午夜，留一天
    assert en.retry_expired(event, day_after) is True
