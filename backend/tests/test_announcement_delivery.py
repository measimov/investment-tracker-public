"""公告触达（#306 后半）：分组读取、重大公告推送、读取 API、分析输入的公告块。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.core.security import get_password_hash
from app.core.timeutil import local_today
from app.database import SessionLocal
from app.main import app
from app.models.holding import Holding
from app.models.notification_event import NotificationEvent
from app.models.scheduled_task_state import ScheduledTaskState
from app.models.security_announcement import SecurityAnnouncement
from app.models.user import User
from app.models.watchlist_item import WatchlistItem
from app.services import announcement_service, announcement_sync, scheduled_state
from app.services import event_notifications as en
from app.services import notification_service as ns
from app.services import security_analysis_jobs as jobs

ADMIN_ID, USER_ID = 1, 2
PASSWORD = "announcement-delivery-password"
TODAY = local_today()


@pytest.fixture
def db():
    session = SessionLocal()

    def clean():
        session.rollback()
        session.query(NotificationEvent).delete()
        session.query(SecurityAnnouncement).delete()
        session.query(ScheduledTaskState).filter(
            ScheduledTaskState.name == announcement_sync.TASK_NAME
        ).delete()
        session.query(Holding).filter(Holding.symbol.like("ANN%")).delete(synchronize_session=False)
        session.query(WatchlistItem).filter(WatchlistItem.symbol.like("ANN%")).delete(
            synchronize_session=False
        )
        session.commit()

    clean()
    try:
        yield session
    finally:
        clean()
        session.close()


@pytest.fixture
def fake_send(monkeypatch):
    calls = []

    def send(title, body, *, severity="warning", kind="alert", urls=None):
        calls.append({"title": title, "body": body})
        return {
            "ok": True,
            "status": "sent",
            "message": "",
            "configured": 1,
            "sent": 1,
            "channels": [],
        }

    monkeypatch.setattr(ns, "send", send)
    monkeypatch.setattr(en.settings, "event_notifications_enabled", True)
    monkeypatch.setattr(en.settings, "announcement_notify_enabled", True)
    monkeypatch.setattr(en, "in_quiet_hours", lambda moment: False)
    return calls


def _store(db, symbol, source_id, title, *, day=TODAY, market="A股", hour=10):
    published = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
    announcement_sync.store_records(
        db,
        [
            {
                "symbol": symbol,
                "market": market,
                "source": "cninfo",
                "source_id": source_id,
                "published_at": published,
                "title": title,
                "url": f"https://example.invalid/{source_id}",
                "category_raw": "",
                "payload": {"sec_name": "安琪酵母"},
            }
        ],
    )
    db.commit()


def _hold(db, user_id, symbol, market="A股", name=None, quantity="100"):
    db.add(
        Holding(
            user_id=user_id,
            symbol=symbol,
            market=market,
            name=name,
            quantity=Decimal(quantity),
            avg_cost=Decimal("1"),
            total_cost=Decimal(quantity),
            currency="CNY",
        )
    )
    db.commit()


def _watch(db, user_id, symbol, market="A股", name=None):
    db.add(WatchlistItem(user_id=user_id, symbol=symbol, market=market, name=name))
    db.commit()


def _seed_financing_day(db, symbol="ANN01", day=TODAY):
    """安琪酵母式的一天：可转债预案 + 配套文件 + 一份同日的普通公告。"""
    _store(
        db,
        symbol,
        f"{symbol}-a",
        "关于向不特定对象发行可转换公司债券预案的提示性公告",
        day=day,
        hour=9,
    )
    _store(db, symbol, f"{symbol}-b", "向不特定对象发行可转换公司债券预案", day=day, hour=10)
    _store(
        db, symbol, f"{symbol}-c", "向不特定对象发行可转换公司债券的论证分析报告", day=day, hour=11
    )
    _store(db, symbol, f"{symbol}-d", "关于召开2026年第四次临时股东会的通知", day=day, hour=12)


# ---------------------------------------------------------------------------
# 分组读取
# ---------------------------------------------------------------------------


def test_groups_merge_same_day_category_with_representative_title(db):
    _seed_financing_day(db)
    groups = announcement_service.load_groups(db, keys=[("ANN01", "A股")])
    financing = next(g for g in groups if g["category"] == "financing")
    assert financing["document_count"] == 3
    assert financing["importance"] == "major"
    assert financing["title"] == "向不特定对象发行可转换公司债券预案"
    assert financing["category_label"]
    assert len(groups) == 2
    # 重要性过滤按组内最高：major 过滤只剩融资组，但组内文件取全
    majors = announcement_service.load_groups(db, keys=[("ANN01", "A股")], importance="major")
    assert [g["document_count"] for g in majors] == [3]


def test_representative_tie_takes_the_earliest_filing(db):
    """PR #311 评审 P3-3：同级（都落在「其余」档）时代表标题取最早发布的原公告，
    不是后发的更正/补充公告。"""
    _store(db, "ANN01", "orig", "关于公司董事长辞职的公告", hour=9)
    _store(db, "ANN01", "fix", "关于公司董事长辞职的更正公告", hour=15)
    (group,) = announcement_service.load_groups(db, keys=[("ANN01", "A股")])
    assert group["document_count"] == 2
    assert group["title"] == "关于公司董事长辞职的公告"


def test_pagination_completes_the_last_day(db):
    day1, day2 = TODAY - timedelta(days=1), TODAY - timedelta(days=5)
    _seed_financing_day(db, day=day1)
    _store(db, "ANN01", "old", "2026年半年度业绩预告", day=day2)
    groups, has_more = announcement_service.load_groups_page(
        db, keys=[("ANN01", "A股")], limit=1, complete_days=True
    )
    assert {g["ann_date"] for g in groups} == {day1} and len(groups) == 2  # 同日两组都取到
    assert has_more
    rest, has_more = announcement_service.load_groups_page(
        db, keys=[("ANN01", "A股")], before=day1, limit=1, complete_days=True
    )
    assert [g["ann_date"] for g in rest] == [day2] and not has_more


# ---------------------------------------------------------------------------
# 推送
# ---------------------------------------------------------------------------


def test_major_announcement_pushed_once_to_holders_and_watchers(db, fake_send):
    _seed_financing_day(db)
    _hold(db, USER_ID, "ANN01", name="安琪酵母")
    _watch(db, ADMIN_ID, "ANN01")
    _hold(db, USER_ID, "ANN02")  # 无公告
    en.run_event_notifications(db)
    events = (
        db.query(NotificationEvent).filter(NotificationEvent.kind == en.KIND_ANNOUNCEMENT).all()
    )
    assert sorted(e.user_id for e in events) == [ADMIN_ID, USER_ID]
    line = next(e.message for e in events if e.user_id == USER_ID)
    assert line.startswith("ANN01 安琪酵母：") and "可转换公司债券预案" in line
    assert "（3 份文件）" in line
    assert any("重大公告" in call["title"] for call in fake_send)
    # 同一组后续补发文件：键不变，不再推
    _store(db, "ANN01", "ANN01-e", "可转换公司债券持有人会议规则")
    sent_before = len(fake_send)
    en.run_event_notifications(db)
    assert len(fake_send) == sent_before


def test_backfilled_or_stale_announcements_are_not_pushed(db, fake_send):
    _hold(db, USER_ID, "ANN01")
    # 公告日在窗口外（首次回溯入库的旧公告）
    _seed_financing_day(db, day=TODAY - timedelta(days=10))
    # 公告日在窗口内但首次入库已久（不是新发现的）
    _store(db, "ANN01", "stale", "关于回购公司股份方案的公告暨回购股份报告书")
    db.query(SecurityAnnouncement).filter(SecurityAnnouncement.source_id == "stale").update(
        {"first_seen_at": datetime.now(timezone.utc) - timedelta(days=3)}
    )
    db.commit()
    assert en.collect_announcement_events(db, datetime.now(timezone.utc)) == []


def test_zero_quantity_holding_and_switch_off(db, fake_send, monkeypatch):
    _seed_financing_day(db)
    _hold(db, USER_ID, "ANN01", quantity="0")
    assert en.collect_announcement_events(db, datetime.now(timezone.utc)) == []
    _watch(db, USER_ID, "ANN01")
    assert len(en.collect_announcement_events(db, datetime.now(timezone.utc))) == 1
    monkeypatch.setattr(en.settings, "announcement_notify_enabled", False)
    assert en.collect_announcement_events(db, datetime.now(timezone.utc)) == []


def test_announcement_line_truncates_long_titles():
    group = {
        "symbol": "00700",
        "sec_name": "腾讯控股",
        "category_label": "回购",
        "title": "长" * 80,
        "document_count": 1,
    }
    line = en.announcement_line(group, None)
    assert line.startswith("00700 腾讯控股：回购 — ") and line.endswith("…")
    assert "份文件" not in line


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.fixture
def client(db):
    user = db.query(User).filter(User.id == USER_ID).one()
    original = user.hashed_password
    user.hashed_password = get_password_hash(PASSWORD)
    db.commit()
    client = TestClient(app)
    token = client.post(
        "/api/auth/token", json={"username": user.username, "password": PASSWORD}
    ).json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    try:
        yield client
    finally:
        user.hashed_password = original
        db.commit()


def test_security_announcements_api(db, client):
    _seed_financing_day(db)
    response = client.get("/api/securities/A股/ANN01/announcements")
    assert response.status_code == 200
    body = response.json()
    assert body["sync_status"] == "pending" and body["has_more"] is False
    assert len(body["groups"]) == 2
    group = body["groups"][0]
    assert group["name"] == "安琪酵母" and len(group["documents"]) == group["document_count"]

    scheduled_state.mark_ran(
        db,
        announcement_sync.TASK_NAME,
        detail={"keys": {"ANN01|A股": {"last_ok": TODAY.isoformat()}}},
    )
    body = client.get(
        "/api/securities/A股/ANN01/announcements", params={"importance": "major"}
    ).json()
    assert body["sync_status"] == "synced" and body["last_synced"] == TODAY.isoformat()
    assert [g["category"] for g in body["groups"]] == ["financing"]

    assert (
        client.get("/api/securities/新加坡股/D05/announcements").json()["sync_status"]
        == "unsupported"
    )
    assert client.get("/api/securities/火星/X/announcements").status_code == 422
    assert (
        client.get(
            "/api/securities/A股/ANN01/announcements", params={"category": "nope"}
        ).status_code
        == 422
    )


def test_recent_announcements_scoped_to_current_user(db, client):
    _seed_financing_day(db, "ANN01")
    _seed_financing_day(db, "ANN02")
    _hold(db, USER_ID, "ANN01", name="我的名字")
    _watch(db, ADMIN_ID, "ANN02")  # 别人的自选
    body = client.get("/api/announcements/recent").json()
    assert body["importance"] == "major" and body["days"] == 7
    assert [(g["symbol"], g["name"]) for g in body["groups"]] == [("ANN01", "我的名字")]
    body = client.get("/api/announcements/recent", params={"importance": "all"}).json()
    assert {g["symbol"] for g in body["groups"]} == {"ANN01"} and len(body["groups"]) == 2


# ---------------------------------------------------------------------------
# 分析输入
# ---------------------------------------------------------------------------


def test_analysis_input_announcements_block_and_gap(db):
    _seed_financing_day(db)
    _store(db, "ANN01", "minor", "关于董事会秘书取得资格证书的公告", day=TODAY - timedelta(days=2))
    _store(db, "ANN01", "old", "2025年年度报告", day=TODAY - timedelta(days=200))
    # 数据集缺口已达 8 条上限时，公告缺口也不得被截掉（PR #311 评审 P3-1）
    many_gaps = [f"数据集{i} 同步失败" for i in range(10)]
    payload = jobs.build_analysis_input(db, "ANN01", "A股", data_gaps=many_gaps)
    assert payload["profile_data_gaps"][:8] == many_gaps[:8]
    block = payload["announcements"]
    assert block and all(
        set(item) == {"date", "category", "importance", "title", "documents"} for item in block
    )
    assert all(item["importance"] in ("major", "normal") for item in block)
    assert "2025年年度报告" not in [item["title"] for item in block]  # 180 天窗口外
    assert "announcements=" in payload["meta"]["data_semantics"]
    # 未同步：空不代表没有公告，必须进缺口
    assert any("官方公告尚未同步" in gap for gap in payload.get("profile_data_gaps", []))

    scheduled_state.mark_ran(
        db,
        announcement_sync.TASK_NAME,
        detail={"keys": {"ANN01|A股": {"last_ok": TODAY.isoformat()}}},
    )
    payload = jobs.build_analysis_input(db, "ANN01", "A股")
    assert not any("官方公告" in gap for gap in payload.get("profile_data_gaps", []))
