"""本地入口能力：实际历史/权限/只读；不探活，不暴露合成Cookie值或路径。"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.sql.selectable import Exists

from app.config import settings
from app.database import SessionLocal, get_db
from app.main import app
from app.models.auth_session import AuthSession
from app.models.security_opinion import SecurityOpinionSummary
from app.models.user import User
from app.models.xueqiu_collector import (
    XueqiuArchiverScanRun,
    XueqiuArchiverUtterance,
    XueqiuCollectorState,
    XueqiuSymbolPost,
)
from app.services.auth_session_service import issue_session


@pytest.fixture
def tokens():
    db = SessionLocal()
    sessions = []
    result = {}
    for name in ("admin", "demo"):
        token, session = issue_session(db, db.query(User).filter(User.username == name).one())
        result[name] = token
        sessions.append(session)
    try:
        yield result
    finally:
        db.query(AuthSession).filter(AuthSession.id.in_(sessions)).delete(synchronize_session=False)
        db.commit()
        db.close()


@pytest.fixture
def db(tokens, monkeypatch):
    monkeypatch.setattr(settings, "xueqiu_cookies", "")
    monkeypatch.setattr(settings, "xueqiu_cookie_file", "")
    db = SessionLocal()
    for model in (XueqiuArchiverUtterance, SecurityOpinionSummary, XueqiuSymbolPost):
        db.query(model).delete(synchronize_session=False)
    db.flush()
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield db
    finally:
        app.dependency_overrides.pop(get_db, None)
        db.rollback()
        db.close()


def get(token=None, *, raise_server_exceptions=True):
    with TestClient(app, raise_server_exceptions=raise_server_exceptions) as client:
        return client.get(
            "/api/capabilities", headers={"Authorization": f"Bearer {token}"} if token else {}
        )


def capability(available=False, reason="unconfigured"):
    return {"available": available, "reason": reason}


def test_capabilities_require_login_but_not_admin(db, tokens):
    assert get().status_code == 401
    expected = {"opinions": capability(), "xueqiu_symbol_feed": capability()}
    for name in ("admin", "demo"):
        response = get(tokens[name])
        assert response.status_code == 200
        assert response.json() == expected


def test_inactive_users_cannot_read_capabilities(db, tokens):
    db.query(User).filter(User.username == "demo").one().is_active = False
    db.flush()
    response = get(tokens["demo"])
    assert response.status_code == 400  # 沿原认证协议，不改停用用户状态码。
    assert response.json()["detail"] == "账号已被停用，请联系管理员"


@pytest.mark.parametrize("config", ["xueqiu_cookies", "xueqiu_cookie_file"])
def test_configured_is_only_local_presence_not_health(db, tokens, monkeypatch, config):
    synthetic = (
        "SYNTH_EXPIRED_COOKIE" if config == "xueqiu_cookies" else "/SYNTH/absent-cookie.json"
    )
    monkeypatch.setattr(settings, config, synthetic)
    response = get(tokens["demo"])
    assert response.status_code == 200
    assert response.json() == {
        "opinions": capability(True, "configured"),
        "xueqiu_symbol_feed": capability(True, "configured"),
    }
    assert synthetic not in response.text


@pytest.mark.parametrize("history", ["utterance", "summary", "announcement", "discussion"])
def test_global_readable_history_keeps_only_its_entry(db, tokens, history):
    if history == "utterance":
        db.add(
            XueqiuArchiverUtterance(
                utterance_key="UI_CAP_HISTORY",
                target_user_id="UI_AUTHOR",
                source="profile",
                kind="post",
            )
        )
    elif history == "summary":
        db.add(
            SecurityOpinionSummary(
                symbol="UICAP",
                market="A股",
                tags=[],
                author_stances=[],
                summary="UI虚构历史",
                content="UI虚构历史正文",
                model="UI",
                input_payload={},
                recent_days=7,
                lookback_days=30,
                utterance_count=1,
                recent_utterance_count=0,
            )
        )
    else:
        db.add(XueqiuSymbolPost(symbol="UICAP", market="A股", kind=history, post_id="UI_CAP_POST"))
    db.flush()
    response = get(tokens["demo"])
    assert response.status_code == 200
    opinions = history in {"utterance", "summary"}
    assert response.json() == {
        "opinions": capability(True, "history") if opinions else capability(),
        "xueqiu_symbol_feed": capability() if opinions else capability(True, "history"),
    }


def test_empty_successful_scan_is_not_readable_history(db, tokens):
    db.add(
        XueqiuArchiverScanRun(
            target_user_id="UI_CAP", status="ok", finished_at=datetime.now(timezone.utc)
        )
    )
    db.flush()
    assert get(tokens["demo"]).json() == {
        "opinions": capability(),
        "xueqiu_symbol_feed": capability(),
    }


def test_capability_request_only_selects_and_does_not_initialize_state(db, tokens):
    # 仅观测请求；应用lifespan本身会对原background_jobs做启动恢复。
    with TestClient(app) as client:
        before = db.query(XueqiuCollectorState).count()
        statements = []
        engine = db.get_bind()

        def record(_connection, _cursor, statement, _params, _context, _many):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            response = client.get(
                "/api/capabilities", headers={"Authorization": f"Bearer {tokens['demo']}"}
            )
            assert response.status_code == 200
            assert db.query(XueqiuCollectorState).count() == before
            assert statements and all(s.lstrip().upper().startswith("SELECT") for s in statements)
        finally:
            event.remove(engine, "before_cursor_execute", record)


def test_database_failure_is_not_an_unconfigured_response(db, tokens, monkeypatch):
    query = db.query

    def fail_capability_query(*entities):
        if all(isinstance(entity, Exists) for entity in entities):
            raise RuntimeError("UI明确虚构能力查询失败")
        return query(*entities)

    monkeypatch.setattr(db, "query", fail_capability_query)
    response = get(tokens["demo"], raise_server_exceptions=False)
    assert response.status_code == 500
    assert '"available":false' not in response.text
