"""系统告警 API（仅管理员）与 manage.py notify / notify-test。"""

import argparse
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.alert_state import AlertState
from app.models.user import User
from app.services import alert_service
from app.services import notification_service as ns
from app.services.alert_service import Alert

PASSWORD = "notifications-api-password"
KEY = "AbCdEfGhIjKlMnOpQrStUv"
MANAGE_PY = Path(__file__).resolve().parents[1] / "manage.py"


def _load_manage():
    spec = importlib.util.spec_from_file_location("_manage_under_test", MANAGE_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, title, body, *, severity="warning", kind="alert", urls=None):
        self.calls.append({"title": title, "severity": severity, "kind": kind})
        return {"ok": True, "status": "sent", "message": "已发送到 1 个渠道", "configured": 1,
                "sent": 1, "channels": [{"kind": "bark", "channel": "barks://api.day.app/Ab***",
                                         "ok": True, "error": None}]}


@pytest.fixture
def recorder(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(ns, "send", rec)
    monkeypatch.setattr(ns.settings, "notify_min_severity", "warning")
    return rec


@pytest.fixture
def clean_alerts():
    db = SessionLocal()
    db.query(AlertState).delete()
    db.commit()
    try:
        yield db
    finally:
        db.rollback()
        db.query(AlertState).delete()
        db.commit()
        db.close()


@pytest.fixture
def clients(clean_alerts):
    db = SessionLocal()
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
        db.close()


def test_endpoints_are_admin_only(clients):
    user = clients["demo"]
    assert user.get("/api/notifications/alerts").status_code == 403
    assert user.post("/api/notifications/test").status_code == 403
    assert user.post("/api/notifications/check").status_code == 403
    assert TestClient(app).get("/api/notifications/alerts").status_code == 401


def test_alert_list_shape_and_masked_channels(clients, clean_alerts, recorder, monkeypatch):
    monkeypatch.setattr(ns.settings, "notify_urls", f"https://api.day.app/{KEY}")
    alert_service.evaluate_alerts(
        clean_alerts, "src",
        [Alert("x:crit", "critical", "严重告警", "详情", {"n": 1}), Alert("x:info", "info", "提示")],
    )
    alert_service.evaluate_alerts(clean_alerts, "gone", [Alert("x:gone", "warning", "已恢复的")])
    alert_service.evaluate_alerts(clean_alerts, "gone", [])

    response = clients["admin"].get("/api/notifications/alerts")
    assert response.status_code == 200
    body = response.json()
    assert KEY not in response.text
    assert body["channels"]["configured"] is True and body["channels"]["count"] == 1
    assert body["channels"]["channels"][0]["channel"] == "barks://api.day.app/Ab***"
    assert body["check_interval_minutes"] == 10
    assert body["counts"] == {"active": 2, "info": 1, "warning": 0, "critical": 1,
                              "recent_resolved": 1}
    first = body["active"][0]
    assert first["alert_key"] == "x:crit" and first["notify_count"] == 1
    assert first["last_notify"]["status"] == "sent" and first["payload"] == {"n": 1}
    assert body["recent_resolved"][0]["alert_key"] == "x:gone"


def test_send_test_notification(clients, monkeypatch):
    monkeypatch.setattr(ns.settings, "notify_urls", "")
    response = clients["admin"].post("/api/notifications/test")
    assert response.status_code == 200
    assert response.json()["status"] == "unconfigured" and response.json()["ok"] is False


def test_check_now_runs_checkers(clients, recorder, monkeypatch):
    from app.services import alert_checks

    monkeypatch.setattr(alert_checks, "CHECKERS", [
        ("demo", "demo_src", lambda _db, _now: [Alert("demo:x", "warning", "演示告警")]),
    ])
    response = clients["admin"].post("/api/notifications/check")
    assert response.status_code == 200
    assert [a["alert_key"] for a in response.json()["active"]] == ["demo:x"]
    assert recorder.calls[0]["title"] == "【警告】演示告警"


class _FakeApprise:
    sent: list = []

    def add(self, url):
        return True

    def notify(self, title, body, notify_type):
        _FakeApprise.sent.append(title)
        return True


class _FakeApprisModule:
    Apprise = _FakeApprise

    class NotifyType:
        INFO = SUCCESS = WARNING = FAILURE = "x"


def test_malformed_notify_url_does_not_break_page_test_or_checks(clients, monkeypatch):
    """PR #255 评审：一个写坏的 URL 曾让告警页 500、状态机回滚、好渠道也收不到。"""
    from app.services import alert_checks

    _FakeApprise.sent = []
    monkeypatch.setattr(ns, "apprise_loader", lambda: _FakeApprisModule)
    monkeypatch.setattr(ns.settings, "notify_min_severity", "warning")
    monkeypatch.setattr(ns.settings, "notify_urls", f"barks://[bad/{KEY} https://api.day.app/{KEY}")
    admin = clients["admin"]

    page = admin.get("/api/notifications/alerts")
    assert page.status_code == 200 and KEY not in page.text
    channels = page.json()["channels"]
    assert channels["count"] == 2 and channels["valid_count"] == 1
    assert [c["valid"] for c in channels["channels"]] == [False, True]

    test = admin.post("/api/notifications/test")
    assert test.status_code == 200 and test.json()["status"] == "partial"
    assert KEY not in test.text

    monkeypatch.setattr(alert_checks, "CHECKERS", [
        ("demo", "demo_src", lambda _db, _now: [Alert("demo:bad-url", "critical", "坏渠道告警")]),
    ])
    checked = admin.post("/api/notifications/check")
    assert checked.status_code == 200
    [item] = checked.json()["active"]
    assert item["alert_key"] == "demo:bad-url" and item["notify_count"] == 1
    assert item["last_notify"]["status"] == "partial"
    assert "【严重】坏渠道告警" in _FakeApprise.sent  # 好渠道照常收到


# --------------------------------------------------------------------------- #
# manage.py
# --------------------------------------------------------------------------- #
def _notify_args(**kwargs):
    base = {"key": "backup", "severity": "critical", "title": None, "message": "", "resolve": False}
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_manage_notify_raise_and_resolve(clean_alerts, recorder, capsys):
    manage = _load_manage()
    assert manage.notify(_notify_args(title="数据库备份失败", message="退出码 1")) == 0
    assert "backup: new" in capsys.readouterr().out
    # 持续失败不重复推送（消息以最近一次为准）
    assert manage.notify(_notify_args(title="数据库备份失败", message="退出码 2")) == 0
    assert manage.notify(_notify_args(resolve=True)) == 0
    assert "backup: resolve" in capsys.readouterr().out
    assert [c["title"] for c in recorder.calls] == ["【严重】数据库备份失败", "【已恢复】数据库备份失败"]
    clean_alerts.expire_all()
    row = clean_alerts.query(AlertState).filter_by(alert_key="backup").one()
    assert row.status == "resolved" and row.source == "external" and row.message == "退出码 2"


def test_manage_notify_validates_input(clean_alerts, recorder):
    manage = _load_manage()
    assert manage.notify(_notify_args(title=None)) == 2
    assert manage.notify(_notify_args(key="bad key!", title="t")) == 2
    assert recorder.calls == []
    parser = manage.build_parser()
    args = parser.parse_args(["notify", "--key", "backup", "--resolve"])
    assert args.resolve and args.severity == "warning"
    assert parser.parse_args(["notify-test"]).command == "notify-test"


def test_manage_notify_test_prints_masked_channels(monkeypatch, capsys):
    manage = _load_manage()

    class FakeApprise:
        def add(self, url):
            return True

        def notify(self, **kwargs):
            return True

    class FakeModule:
        Apprise = FakeApprise

        class NotifyType:
            INFO = SUCCESS = WARNING = FAILURE = "x"

    monkeypatch.setattr(ns, "apprise_loader", lambda: FakeModule)
    monkeypatch.setattr(ns.settings, "notify_urls", f"https://api.day.app/{KEY}")
    assert manage.notify_test() == 0
    out = capsys.readouterr().out
    assert KEY not in out and "barks://api.day.app/Ab***" in out and "result: sent" in out
    monkeypatch.setattr(ns.settings, "notify_urls", "")
    assert manage.notify_test() == 1
    assert "no notification channel configured" in capsys.readouterr().out
