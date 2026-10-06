"""真实 job 认领/重试保留 LLM 路由；外呼全部打桩，仅清理本文件创建的任务。"""

from datetime import datetime, timedelta, timezone
import logging
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest

from app.database import SessionLocal
from app.models.background_job import BackgroundJob
from app.services import background_job_store as store
from app.services import job_runtime, job_worker, llm_client
from app.services.background_job_store import JobOwnershipLostError


@pytest.fixture
def job_type():
    value = f"test_llm_route_{uuid4().hex[:12]}"
    try:
        yield value
    finally:
        with SessionLocal() as db:
            db.query(BackgroundJob).filter(BackgroundJob.job_type == value).delete(
                synchronize_session=False
            )
            db.commit()


@pytest.fixture
def provider_calls(monkeypatch):
    for provider in ("deepseek", "ark", "bailian", "openrouter"):
        prefix = "llm_report" if provider == "deepseek" else f"llm_{provider}"
        monkeypatch.setattr(
            llm_client.settings,
            f"{prefix}_api_key",
            f"test-key-{provider}" if provider in {"deepseek", "ark"} else "",
        )
        monkeypatch.setattr(
            llm_client.settings, f"{prefix}_base_url", f"https://{provider}.test.invalid/v1"
        )
        monkeypatch.setattr(llm_client.settings, f"{prefix}_model", f"test-{provider}")
    monkeypatch.setattr(llm_client.settings, "llm_fallback_budget_seconds", 240)
    monkeypatch.setattr(llm_client.settings, "llm_report_timeout_seconds", 120)
    calls = []

    async def post(url, **kwargs):
        provider = urlsplit(url).hostname.split(".", 1)[0]
        calls.append(provider)
        return httpx.Response(
            503 if provider == "deepseek" else 200,
            json={
                "model": f"actual-{provider}",
                "choices": [{"message": {"content": "完整报告"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(llm_client, "_post_completion", post)
    return calls


def _execute(entrypoint, job_id, job_type, runner):
    if entrypoint == "inline":
        job_runtime.run_job_inline(
            job_id,
            job_type,
            runner,
            label="LLM routing test",
            logger=logging.getLogger(__name__),
        )
    else:
        claimed = store.claim_next_runnable_job([job_type], owner="routing-test-worker")
        assert claimed is not None and claimed["id"] == job_id
        job_worker.execute_claimed_job(claimed)


@pytest.mark.parametrize("entrypoint", ["inline", "worker"])
def test_job_retry_restores_successful_provider_from_database(
    monkeypatch, job_type, provider_calls, entrypoint
):
    business_data = {"marker": "keep", "targets": ["item-1", "item-2"], "completed_keys": []}
    job = store.create_or_get_active_job(job_type, 1, business_data)
    observed_attempts = []

    def runner(claimed):
        attempt = claimed["attempt_count"]
        observed_attempts.append(attempt)
        if attempt == 2:
            assert claimed["data"]["llm_route"]["preferred_provider"] == "ark"
            assert claimed["data"]["completed_keys"] == ["item-1"]
        completion = llm_client.chat_completion([{"role": "user", "content": "测试输入"}])
        assert completion["generation_meta"]["provider"] == "ark"
        updated = store.update_job(
            claimed["id"],
            job_type,
            data_updates={
                "completed_keys": ["item-1"] if attempt == 1 else business_data["targets"]
            },
            status=None if attempt == 1 else "succeeded",
            required_status="running",
            required_attempt_count=attempt,
        )
        assert updated is not None
        if attempt == 1:
            raise RuntimeError("retry after saving the first result")

    monkeypatch.setattr(job_worker, "_runners", {job_type: runner})
    _execute(entrypoint, job["id"], job_type, runner)
    first = store.get_job(job["id"], job_type, 1)
    assert first["status"] == "queued"
    with SessionLocal() as db:
        assert db.get(BackgroundJob, job["id"]).attempt_count == 1
    assert first["llm_route"]["preferred_provider"] == "ark"
    assert (
        first["marker"] == business_data["marker"] and first["targets"] == business_data["targets"]
    )
    assert first["completed_keys"] == ["item-1"]
    assert provider_calls == ["deepseek", "ark"]

    # Make only this job's retry due; no sleep, global maintenance, or retry policy changes.
    with SessionLocal() as db:
        row = db.get(BackgroundJob, job["id"])
        row.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    _execute(entrypoint, job["id"], job_type, runner)
    second = store.get_job(job["id"], job_type, 1)
    assert second["status"] == "succeeded"
    with SessionLocal() as db:
        assert db.get(BackgroundJob, job["id"]).attempt_count == 2
    assert second["llm_route"]["preferred_provider"] == "ark"
    assert (
        second["marker"] == business_data["marker"]
        and second["targets"] == business_data["targets"]
    )
    assert second["completed_keys"] == ["item-1", "item-2"]
    assert observed_attempts == [1, 2]
    assert provider_calls == ["deepseek", "ark", "ark"]


@pytest.mark.parametrize("entrypoint", ["inline", "worker"])
def test_entrypoint_stops_quietly_on_ownership_loss_without_changing_replacement_attempt(
    monkeypatch, job_type, entrypoint
):
    job = store.create_or_get_active_job(job_type, 1, {"marker": "keep"})

    def unexpected_failure(*args, **kwargs):
        pytest.fail("失权退出不得触发 handle_job_failure")

    monkeypatch.setattr(store, "handle_job_failure", unexpected_failure)
    monkeypatch.setattr(job_runtime, "handle_job_failure", unexpected_failure)

    def runner(claimed):
        with SessionLocal() as db:
            row = db.get(BackgroundJob, claimed["id"])
            row.attempt_count = claimed["attempt_count"] + 1
            row.lease_owner = "replacement-worker"
            db.commit()
        raise JobOwnershipLostError(claimed["id"])

    monkeypatch.setattr(job_worker, "_runners", {job_type: runner})
    _execute(entrypoint, job["id"], job_type, runner)
    with SessionLocal() as db:
        replacement = db.get(BackgroundJob, job["id"])
        assert replacement.status == "running" and replacement.attempt_count == 2
        assert replacement.lease_owner == "replacement-worker"
        assert replacement.error is None and replacement.next_attempt_at is None
        assert replacement.data == {"marker": "keep"}
