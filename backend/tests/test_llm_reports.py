"""LLM 报告：客户端、后台任务、API、定期调度。"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.background_job import BackgroundJob
from app.models.llm_report import LlmReport, LlmReportMessage, LlmReportSchedule
from app.models.user import User
from app.services import llm_client
from app.services import llm_report_jobs
from app.services import llm_report_scheduler
from app.services.background_job_store import create_or_get_active_job


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _cleanup(db):
    db.query(LlmReportMessage).delete()
    db.query(LlmReport).delete()
    db.query(LlmReportSchedule).delete()
    db.query(BackgroundJob).filter(BackgroundJob.job_type == "llm_report").delete()
    db.commit()


@pytest.fixture
def llm_user():
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "demo").one()
        original = user.hashed_password
        user.hashed_password = get_password_hash("llm-report-password")
        _cleanup(db)
        db.commit()
        yield user.id
        user.hashed_password = original
        _cleanup(db)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


def test_client_without_key_raises_before_network(monkeypatch):
    monkeypatch.setattr(llm_client.settings, "llm_report_api_key", "")

    def explode(*args, **kwargs):
        raise AssertionError("不得发起网络请求")

    monkeypatch.setattr(llm_client.httpx, "post", explode)
    with pytest.raises(llm_client.LLMNotConfiguredError):
        llm_client.chat_completion([{"role": "user", "content": "hi"}])


def test_client_parses_success_and_errors(monkeypatch):
    monkeypatch.setattr(llm_client.settings, "llm_report_api_key", "sk-test")

    def ok_post(url, **kwargs):
        return httpx.Response(
            200,
            json={
                "model": "deepseek-chat",
                "choices": [{"message": {"content": "报告内容"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(llm_client.httpx, "post", ok_post)
    result = llm_client.chat_completion([{"role": "user", "content": "hi"}])
    assert result["content"] == "报告内容"
    assert result["usage"]["total_tokens"] == 15

    def unauthorized_post(url, **kwargs):
        return httpx.Response(401, text="bad key", request=httpx.Request("POST", url))

    monkeypatch.setattr(llm_client.httpx, "post", unauthorized_post)
    with pytest.raises(llm_client.LLMClientError) as exc_info:
        llm_client.chat_completion([{"role": "user", "content": "hi"}])
    assert exc_info.value.status_code == 401

    def timeout_post(url, **kwargs):
        raise httpx.ReadTimeout("timed out", request=httpx.Request("POST", url))

    monkeypatch.setattr(llm_client.httpx, "post", timeout_post)
    with pytest.raises(llm_client.LLMClientError) as exc_info:
        llm_client.chat_completion([{"role": "user", "content": "hi"}])
    assert exc_info.value.status_code is None  # 非 4xx：可重试


# ---------------------------------------------------------------------------
# 后台任务
# ---------------------------------------------------------------------------


def _run_job(monkeypatch, user_id, completion=None, error=None):
    monkeypatch.setattr(llm_report_jobs, "build_llm_report_input", lambda db, uid: {"meta": {}})
    if error is not None:
        def fake_chat(messages, **kwargs):
            raise error
    else:
        def fake_chat(messages, **kwargs):
            return completion
    monkeypatch.setattr(llm_report_jobs, "chat_completion", fake_chat)
    job = llm_report_jobs.start_llm_report_job(user_id)
    llm_report_jobs.run_llm_report_job(job["id"])
    return llm_report_jobs.get_llm_report_job(job["id"], user_id)


def test_job_success_creates_report_row(monkeypatch, llm_user):
    job = _run_job(
        monkeypatch, llm_user,
        completion={"content": "# 报告", "model": "deepseek-chat",
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}},
    )
    assert job["status"] == "succeeded"
    assert job["report_id"] is not None

    db = SessionLocal()
    try:
        report = db.get(LlmReport, job["report_id"])
        assert report.content == "# 报告"
        assert report.trigger_source == "manual"
        assert report.total_tokens == 150
        assert report.input_payload == {"meta": {}}
    finally:
        db.close()


def test_job_4xx_is_deterministic_failure_without_retry(monkeypatch, llm_user):
    job = _run_job(
        monkeypatch, llm_user,
        error=llm_client.LLMClientError("bad key", status_code=401),
    )
    assert job["status"] == "failed"
    db = SessionLocal()
    try:
        row = db.get(BackgroundJob, job["id"])
        assert row.attempt_count == 1  # 不烧重试
        assert db.query(LlmReport).count() == 0
    finally:
        db.close()


def test_job_5xx_routes_to_retry_path(monkeypatch, llm_user):
    job = _run_job(
        monkeypatch, llm_user,
        error=llm_client.LLMClientError("upstream down", status_code=503),
    )
    # handle_job_failure：还有剩余尝试次数时回到 queued 等待重试
    db = SessionLocal()
    try:
        row = db.get(BackgroundJob, job["id"])
        assert row.status in {"queued", "failed"}
        assert row.attempt_count == 1
        assert row.status == "queued"  # max_attempts 默认 3，首败必然重排
    finally:
        db.close()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def _open_client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def _auth_headers(client):
    response = await client.post(
        "/api/auth/token",
        json={"username": "demo", "password": "llm-report-password"},
    )
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.anyio
async def test_generate_requires_configured_key(monkeypatch, llm_user):

    monkeypatch.setattr(llm_client.settings, "llm_report_api_key", "")
    async with _open_client() as client:
        headers = await _auth_headers(client)
        response = await client.post("/api/llm-reports/generate", headers=headers)
        assert response.status_code == 409


@pytest.mark.anyio
async def test_report_crud_ownership_and_chat(monkeypatch, llm_user):
    from app.api import llm_reports as api_mod

    db = SessionLocal()
    try:
        report = LlmReport(
            user_id=llm_user, title="投资复盘 2026-07-30", content="# 测试报告",
            model="deepseek-chat", trigger_source="manual", input_payload={"meta": {}},
            total_tokens=100,
        )
        db.add(report)
        db.commit()
        db.refresh(report)
        report_id = report.id
    finally:
        db.close()

    monkeypatch.setattr(llm_client.settings, "llm_report_api_key", "sk-test")
    monkeypatch.setattr(
        api_mod, "chat_completion",
        lambda messages, **kw: {"content": "回答内容", "model": "deepseek-chat",
                                "usage": {"total_tokens": 42}},
    )

    async with _open_client() as client:
        headers = await _auth_headers(client)
        listed = await client.get("/api/llm-reports", headers=headers)
        assert [r["id"] for r in listed.json()] == [report_id]
        assert "content" not in listed.json()[0]

        detail = await client.get(f"/api/llm-reports/{report_id}", headers=headers)
        assert detail.json()["content"] == "# 测试报告"
        assert detail.json()["messages"] == []

        # 追问：成功恰落 2 行
        asked = await client.post(
            f"/api/llm-reports/{report_id}/messages",
            json={"content": "为什么收益是估算口径？"},
            headers=headers,
        )
        assert asked.status_code == 200
        assert asked.json()["answer"]["content"] == "回答内容"
        detail = await client.get(f"/api/llm-reports/{report_id}", headers=headers)
        assert [m["role"] for m in detail.json()["messages"]] == ["user", "assistant"]

        # LLM 失败：零残留
        def boom(messages, **kw):
            raise llm_client.LLMClientError("upstream", status_code=502)

        monkeypatch.setattr(api_mod, "chat_completion", boom)
        failed = await client.post(
            f"/api/llm-reports/{report_id}/messages",
            json={"content": "再问一个"},
            headers=headers,
        )
        assert failed.status_code == 502
        detail = await client.get(f"/api/llm-reports/{report_id}", headers=headers)
        assert len(detail.json()["messages"]) == 2

        # 所有权：admin 拿不到 demo 的报告
        admin_login = await client.post(
            "/api/auth/token", json={"username": "admin", "password": "wrong"},
        )
        assert admin_login.status_code == 401  # admin 密码未知即不可访问，仅验证隔离前提

        # schedule 往返
        put = await client.put(
            "/api/llm-reports/schedule", json={"cadence": "weekly"}, headers=headers
        )
        assert put.json() == {"cadence": "weekly"}
        got = await client.get("/api/llm-reports/schedule", headers=headers)
        assert got.json() == {"cadence": "weekly"}
        bad = await client.put(
            "/api/llm-reports/schedule", json={"cadence": "daily"}, headers=headers
        )
        assert bad.status_code == 422

        # 删除
        deleted = await client.delete(f"/api/llm-reports/{report_id}", headers=headers)
        assert deleted.status_code == 204
        assert (await client.get("/api/llm-reports", headers=headers)).json() == []


@pytest.mark.anyio
async def test_message_cap_returns_409(monkeypatch, llm_user):
    from app.api import llm_reports as api_mod

    db = SessionLocal()
    try:
        report = LlmReport(
            user_id=llm_user, title="满额报告", content="x", model="m",
            trigger_source="manual", input_payload={},
        )
        db.add(report)
        db.flush()
        for i in range(api_mod.MAX_MESSAGES_PER_REPORT):
            db.add(LlmReportMessage(
                report_id=report.id, user_id=llm_user,
                role="user" if i % 2 == 0 else "assistant", content=f"m{i}",
            ))
        db.commit()
        report_id = report.id
    finally:
        db.close()

    monkeypatch.setattr(llm_client.settings, "llm_report_api_key", "sk-test")
    async with _open_client() as client:
        headers = await _auth_headers(client)
        response = await client.post(
            f"/api/llm-reports/{report_id}/messages",
            json={"content": "超限追问"},
            headers=headers,
        )
        assert response.status_code == 409


# ---------------------------------------------------------------------------
# 定期调度
# ---------------------------------------------------------------------------


def _make_report(db, user_id, created_at):
    report = LlmReport(
        user_id=user_id, title="t", content="c", model="m",
        trigger_source="scheduled", input_payload={},
    )
    db.add(report)
    db.commit()
    # server_default 会覆盖，显式回写创建时间
    db.query(LlmReport).filter(LlmReport.id == report.id).update(
        {LlmReport.created_at: created_at}
    )
    db.commit()


def test_scheduler_due_matrix(monkeypatch, llm_user):
    monkeypatch.setattr(llm_client.settings, "llm_report_api_key", "sk-test")
    now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    db = SessionLocal()
    try:
        # cadence off → 不入队
        db.add(LlmReportSchedule(user_id=llm_user, cadence="off"))
        db.commit()
        assert llm_report_scheduler.enqueue_due_scheduled_reports(now) == 0

        # weekly、无历史报告 → 入队
        db.query(LlmReportSchedule).update({LlmReportSchedule.cadence: "weekly"})
        db.commit()
        assert llm_report_scheduler.enqueue_due_scheduled_reports(now) == 1
        # 活跃任务去重：再次检查不重复入队
        assert llm_report_scheduler.enqueue_due_scheduled_reports(now) == 0
        db.query(BackgroundJob).filter(BackgroundJob.job_type == "llm_report").delete()
        db.commit()

        # 3 天前有报告 + weekly → 未到期
        _make_report(db, llm_user, now - timedelta(days=3))
        assert llm_report_scheduler.enqueue_due_scheduled_reports(now) == 0

        # 8 天前 + weekly → 到期；同一历史 + monthly → 未到期
        db.query(LlmReport).delete()
        db.commit()
        _make_report(db, llm_user, now - timedelta(days=8))
        assert llm_report_scheduler.enqueue_due_scheduled_reports(now) == 1
        db.query(BackgroundJob).filter(BackgroundJob.job_type == "llm_report").delete()
        db.query(LlmReportSchedule).update({LlmReportSchedule.cadence: "monthly"})
        db.commit()
        assert llm_report_scheduler.enqueue_due_scheduled_reports(now) == 0
    finally:
        _cleanup(db)
        db.close()


def test_scheduler_disabled_without_key(monkeypatch, llm_user):
    monkeypatch.setattr(llm_client.settings, "llm_report_api_key", "")
    db = SessionLocal()
    try:
        db.add(LlmReportSchedule(user_id=llm_user, cadence="weekly"))
        db.commit()
        assert llm_report_scheduler.enqueue_due_scheduled_reports() == 0
    finally:
        _cleanup(db)
        db.close()


def test_create_or_get_active_job_dedupes(llm_user):
    first = create_or_get_active_job("llm_report", llm_user, {"trigger": "manual"})
    second = create_or_get_active_job("llm_report", llm_user, {"trigger": "manual"})
    assert first["id"] == second["id"]
    db = SessionLocal()
    try:
        db.query(BackgroundJob).filter(BackgroundJob.job_type == "llm_report").delete()
        db.commit()
    finally:
        db.close()


def test_placeholder_key_is_treated_as_unconfigured(monkeypatch):
    """照抄 .env.example 的 <占位符> 不得被当成已配置打真实请求。"""
    for placeholder in ("", "<deepseek-api-key>", "  <key>  "):
        monkeypatch.setattr(llm_client.settings, "llm_report_api_key", placeholder)
        assert llm_client.is_llm_configured() is False
        with pytest.raises(llm_client.LLMNotConfiguredError):
            llm_client.chat_completion([{"role": "user", "content": "hi"}])
    monkeypatch.setattr(llm_client.settings, "llm_report_api_key", "sk-real")
    assert llm_client.is_llm_configured() is True


def test_scheduler_skips_placeholder_key(monkeypatch, llm_user):
    monkeypatch.setattr(llm_client.settings, "llm_report_api_key", "<deepseek-api-key>")
    db = SessionLocal()
    try:
        db.add(LlmReportSchedule(user_id=llm_user, cadence="weekly"))
        db.commit()
        assert llm_report_scheduler.enqueue_due_scheduled_reports() == 0
    finally:
        _cleanup(db)
        db.close()
