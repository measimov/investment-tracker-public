"""LLM 路由的失败切换、总预算与跨任务隔离（全部外呼和 job 回写均模拟）。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import threading
import time
import traceback
from urllib.parse import urlsplit

import httpx
import pytest

from app.services import background_job_store, llm_client
from app.services.background_job_store import JobOwnershipLostError


PROVIDERS = ("deepseek", "ark", "bailian", "openrouter")
MESSAGES = [{"role": "user", "content": "基于输入生成报告"}]


def _provider(url):
    return urlsplit(url).hostname.split(".", 1)[0]


def _response(url, status=200, *, content="完整报告", finish_reason="stop", model=None):
    payload = {
        "model": model or f"actual-{_provider(url)}-model",
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }
    return httpx.Response(status, json=payload, request=httpx.Request("POST", url))


@pytest.fixture
def routes(monkeypatch):
    for provider in PROVIDERS:
        prefix = "llm_report" if provider == "deepseek" else f"llm_{provider}"
        monkeypatch.setattr(llm_client.settings, f"{prefix}_api_key", f"unit-key-{provider}")
        monkeypatch.setattr(
            llm_client.settings, f"{prefix}_base_url", f"https://{provider}.test.invalid/v1/"
        )
        monkeypatch.setattr(llm_client.settings, f"{prefix}_model", f"requested-{provider}")
    monkeypatch.setattr(llm_client.settings, "llm_fallback_budget_seconds", 240)
    monkeypatch.setattr(llm_client.settings, "llm_report_timeout_seconds", 120)
    return llm_client.settings


@pytest.fixture
def guarded_store(monkeypatch):
    writes = []

    def update(job_id, job_type, **kwargs):
        writes.append((job_id, job_type, copy.deepcopy(kwargs)))
        return {"id": job_id, "job_type": job_type, "data": kwargs.get("data_updates", {})}

    monkeypatch.setattr(background_job_store, "update_job", update)
    monkeypatch.setattr(llm_client, "update_job", update, raising=False)
    return writes


def _claimed(job_id="job-a", *, attempt=1, route=None):
    return {
        "id": job_id,
        "job_type": "security_analysis_batch",
        "attempt_count": attempt,
        "data": {"llm_route": route} if route is not None else {},
    }


def _plan(monkeypatch, statuses):
    calls = []

    async def post(url, **kwargs):
        calls.append((_provider(url), copy.deepcopy(kwargs)))
        status = statuses[_provider(url)]
        if isinstance(status, Exception):
            raise status
        return _response(url, status)

    monkeypatch.setattr(llm_client, "_post_completion", post)
    return calls


def test_priority_and_provider_specific_credentials(routes, monkeypatch):
    calls = _plan(monkeypatch, {"deepseek": 503, "ark": 429, "bailian": 503, "openrouter": 200})
    result = llm_client.chat_completion(MESSAGES)
    assert [provider for provider, _ in calls] == list(PROVIDERS)
    assert result["model"] == "actual-openrouter-model"
    assert result["usage"]["total_tokens"] == 10
    meta = result["generation_meta"]
    assert meta["provider"] == "openrouter"
    assert meta["requested_model"] == "requested-openrouter"
    assert meta["response_model"] == "actual-openrouter-model"
    assert [attempt["provider"] for attempt in meta["attempts"]] == list(PROVIDERS)
    assert meta["elapsed_seconds"] >= 0
    for provider, kwargs in calls:
        assert kwargs["json"]["model"] == f"requested-{provider}"
        assert kwargs["headers"]["Authorization"] == f"Bearer unit-key-{provider}"


def test_messages_and_generation_parameters_survive_provider_switch(routes, monkeypatch):
    calls = _plan(monkeypatch, {"deepseek": 502, "ark": 200})
    llm_client.chat_completion(
        MESSAGES, max_tokens=32768, temperature=0.25, response_format={"type": "json_object"}
    )
    for _, kwargs in calls:
        assert kwargs["json"]["messages"] == MESSAGES
        assert kwargs["json"]["max_tokens"] == 32768
        assert kwargs["json"]["temperature"] == 0.25
        assert kwargs["json"]["response_format"] == {"type": "json_object"}
        assert kwargs["json"]["stream"] is False
    assert MESSAGES == [{"role": "user", "content": "基于输入生成报告"}]


@pytest.mark.parametrize("placeholder", ["", "  ", "<key>", " <key> "])
def test_unconfigured_providers_are_skipped_and_backup_alone_enables_llm(
    routes, monkeypatch, placeholder
):
    monkeypatch.setattr(routes, "llm_report_api_key", placeholder)
    monkeypatch.setattr(routes, "llm_ark_api_key", placeholder)
    monkeypatch.setattr(routes, "llm_bailian_api_key", placeholder)
    calls = _plan(monkeypatch, {"openrouter": 200})
    assert llm_client.is_llm_configured()
    assert llm_client.chat_completion(MESSAGES)["generation_meta"]["provider"] == "openrouter"
    assert [provider for provider, _ in calls] == ["openrouter"]


def test_no_configured_provider_does_not_send_a_request(routes, monkeypatch):
    for provider in PROVIDERS:
        prefix = "llm_report" if provider == "deepseek" else f"llm_{provider}"
        monkeypatch.setattr(routes, f"{prefix}_api_key", "<key>")
    calls = _plan(monkeypatch, {})
    assert not llm_client.is_llm_configured()
    with pytest.raises(llm_client.LLMNotConfiguredError):
        llm_client.chat_completion(MESSAGES)
    assert not calls


@pytest.mark.parametrize("status", [401, 402, 403, 408, 429, 500, 502, 503, 504])
def test_provider_failures_use_the_next_endpoint(routes, monkeypatch, status):
    calls = _plan(monkeypatch, {"deepseek": status, "ark": 200})
    assert llm_client.chat_completion(MESSAGES)["generation_meta"]["provider"] == "ark"
    assert [provider for provider, _ in calls] == ["deepseek", "ark"]


@pytest.mark.parametrize("status", [400, 404, 413, 422])
def test_request_contract_errors_do_not_burn_backup_requests(routes, monkeypatch, status):
    calls = _plan(monkeypatch, {"deepseek": status})
    with pytest.raises(llm_client.LLMClientError) as caught:
        llm_client.chat_completion(MESSAGES)
    assert caught.value.status_code == status
    assert len(calls) == 1


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("connection lost"), httpx.ReadTimeout("slow")]
)
def test_transport_errors_use_the_next_endpoint(routes, monkeypatch, error):
    calls = _plan(monkeypatch, {"deepseek": error, "ark": 200})
    assert llm_client.chat_completion(MESSAGES)["generation_meta"]["provider"] == "ark"
    assert [provider for provider, _ in calls] == ["deepseek", "ark"]


@pytest.mark.parametrize(
    ("content", "finish_reason"),
    [
        ("半截报告", "length"),
        ("", "length"),
        ("", "content_filter"),
        ("", "stop"),
        ("", "insufficient_system_resource"),
        (None, "insufficient_system_resource"),
    ],
)
def test_200_unusable_output_does_not_switch_provider(routes, monkeypatch, content, finish_reason):
    calls = []

    async def post(url, **kwargs):
        calls.append(_provider(url))
        return _response(url, content=content, finish_reason=finish_reason)

    monkeypatch.setattr(llm_client, "_post_completion", post)
    with pytest.raises(llm_client.LLMClientError) as caught:
        llm_client.chat_completion(MESSAGES, max_tokens=1234)
    assert calls == ["deepseek"]
    assert caught.value.finish_reason == finish_reason
    assert llm_client.is_output_truncated(caught.value) == (finish_reason == "length")


def test_unknown_empty_output_finish_reason_is_not_leaked(routes, monkeypatch, caplog):
    upstream_reason = "unknown-finish-reason-with-private-upstream-text"

    async def post(url, **kwargs):
        return _response(url, content="", finish_reason=upstream_reason)

    monkeypatch.setattr(llm_client, "_post_completion", post)
    with pytest.raises(llm_client.LLMClientError) as caught:
        llm_client.chat_completion(MESSAGES)
    assert caught.value.finish_reason == "invalid"
    formatted = "".join(traceback.format_exception(type(caught.value), caught.value, caught.tb))
    assert upstream_reason not in formatted + caplog.text + json.dumps(caught.value.__dict__)


@pytest.mark.parametrize("payload", [None, {}, {"choices": []}, {"choices": [{"message": {}}]}])
def test_invalid_success_envelopes_are_not_endpoint_failover(routes, monkeypatch, payload):
    calls = []

    async def post(url, **kwargs):
        calls.append(_provider(url))
        if payload is None:
            return httpx.Response(200, text="not-json", request=httpx.Request("POST", url))
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(llm_client, "_post_completion", post)
    with pytest.raises(llm_client.LLMClientError):
        llm_client.chat_completion(MESSAGES)
    assert calls == ["deepseek"]


@pytest.mark.parametrize("finish_reason", [["stop"], {"reason": "stop"}, 123])
def test_malformed_finish_reason_is_a_sanitized_client_error(routes, monkeypatch, finish_reason):
    calls = []

    async def post(url, **kwargs):
        calls.append(_provider(url))
        return _response(url, finish_reason=finish_reason)

    monkeypatch.setattr(llm_client, "_post_completion", post)
    with pytest.raises(llm_client.LLMClientError):
        llm_client.chat_completion(MESSAGES)
    assert calls == ["deepseek"]


def test_explicit_refusal_does_not_return_content_or_spend_on_backup(routes, monkeypatch):
    calls = []
    refusal = "mock-refusal-with-sensitive-provider-content"

    async def post(url, **kwargs):
        calls.append(_provider(url))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "看似完整报告", "refusal": refusal},
                        "finish_reason": "stop",
                    }
                ]
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(llm_client, "_post_completion", post)
    with pytest.raises(llm_client.LLMClientError) as caught:
        llm_client.chat_completion(MESSAGES)
    assert caught.value.finish_reason == "content_filter"
    assert refusal not in str(caught.value)
    assert calls == ["deepseek"]


@pytest.mark.parametrize("status", [408, 429])
def test_exhausted_rate_or_timeout_status_stays_retryable_for_existing_jobs(
    routes, monkeypatch, status
):
    calls = _plan(monkeypatch, dict.fromkeys(PROVIDERS, status))
    with pytest.raises(llm_client.LLMClientError) as caught:
        llm_client.chat_completion(MESSAGES)
    assert len(calls) == 4
    assert caught.value.status_code is None
    assert [attempt["status_code"] for attempt in caught.value.attempts] == [status] * 4


def test_mixed_exhaustion_preserves_the_transient_failure(routes, monkeypatch):
    calls = _plan(monkeypatch, {"deepseek": 503, "ark": 401, "bailian": 402, "openrouter": 403})
    with pytest.raises(llm_client.LLMClientError) as caught:
        llm_client.chat_completion(MESSAGES)
    assert len(calls) == 4
    assert caught.value.status_code == 503


def test_all_auth_failures_remain_terminal(routes, monkeypatch):
    calls = _plan(monkeypatch, dict.fromkeys(PROVIDERS, 401))
    with pytest.raises(llm_client.LLMClientError) as caught:
        llm_client.chat_completion(MESSAGES)
    assert len(calls) == 4
    assert caught.value.status_code == 401


@pytest.mark.parametrize("transport_error", [False, True])
def test_errors_logs_and_metadata_do_not_contain_response_secrets(
    routes, monkeypatch, caplog, transport_error
):
    secret = "mock-provider-echoed-secret-do-not-log"

    async def post(url, **kwargs):
        if transport_error:
            raise httpx.ConnectError(f"{secret} https://example.invalid/?api_key={secret}")
        return httpx.Response(503, text=secret, request=httpx.Request("POST", url))

    monkeypatch.setattr(llm_client, "_post_completion", post)
    with pytest.raises(llm_client.LLMClientError) as caught:
        llm_client.chat_completion(MESSAGES)
    assert secret not in str(caught.value)
    assert secret not in caplog.text
    assert secret not in "".join(traceback.format_exception(caught.value))
    assert "unit-key-" not in caplog.text


def test_generation_metadata_never_contains_keys_or_full_urls(routes, monkeypatch):
    _plan(monkeypatch, {"deepseek": 401, "ark": 200})
    result = llm_client.chat_completion(MESSAGES)
    metadata = json.dumps(result["generation_meta"])
    assert "unit-key-" not in metadata
    assert "test.invalid" not in metadata
    assert "Bearer" not in metadata


def test_total_budget_cancels_a_running_request_and_stops_the_chain(routes, monkeypatch):
    monkeypatch.setattr(routes, "llm_fallback_budget_seconds", 0.05)
    monkeypatch.setattr(routes, "llm_report_timeout_seconds", 1)
    started = []
    cancelled = []

    async def post(url, **kwargs):
        provider = _provider(url)
        started.append(provider)
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.append(provider)
            raise
        return _response(url)

    monkeypatch.setattr(llm_client, "_post_completion", post)
    before = time.monotonic()
    with pytest.raises(llm_client.LLMClientError) as caught:
        llm_client.chat_completion(MESSAGES)
    assert time.monotonic() - before < 1
    assert started == cancelled == ["deepseek"]
    assert caught.value.status_code is None


def test_per_hop_deadline_cancels_the_primary_and_leaves_budget_for_fallback(routes, monkeypatch):
    monkeypatch.setattr(routes, "llm_fallback_budget_seconds", 0.5)
    monkeypatch.setattr(routes, "llm_report_timeout_seconds", 0.03)
    cancelled = []
    calls = []

    async def post(url, **kwargs):
        provider = _provider(url)
        calls.append(provider)
        if provider == "deepseek":
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.append(provider)
                raise
        return _response(url)

    monkeypatch.setattr(llm_client, "_post_completion", post)
    result = llm_client.chat_completion(MESSAGES)
    assert calls == ["deepseek", "ark"]
    assert cancelled == ["deepseek"]
    assert result["generation_meta"]["provider"] == "ark"


def test_successful_fallback_is_sticky_and_saved_with_an_ownership_guard(
    routes, monkeypatch, guarded_store
):
    calls = _plan(monkeypatch, {"deepseek": 503, "ark": 200})
    with llm_client.llm_job_context(_claimed(attempt=2)):
        first = llm_client.chat_completion(MESSAGES)
        second = llm_client.chat_completion(MESSAGES)
    assert first["generation_meta"]["provider"] == second["generation_meta"]["provider"] == "ark"
    assert [provider for provider, _ in calls] == ["deepseek", "ark", "ark"]
    assert guarded_store
    for _, _, kwargs in guarded_store:
        assert kwargs["required_status"] == "running"
        assert kwargs["required_attempt_count"] == 2
    saved = [kwargs["data_updates"]["llm_route"] for _, _, kwargs in guarded_store]
    assert saved[-1]["preferred_provider"] == "ark"
    assert "deepseek" not in saved[-1]["disabled_providers"]
    assert saved[-1]["last_attempts"]


def test_a_new_worker_attempt_restores_the_previous_successful_provider(
    routes, monkeypatch, guarded_store
):
    calls = _plan(monkeypatch, {"deepseek": 503, "ark": 200})
    with llm_client.llm_job_context(_claimed()):
        llm_client.chat_completion(MESSAGES)
    saved = copy.deepcopy(guarded_store[-1][2]["data_updates"]["llm_route"])
    with llm_client.llm_job_context(_claimed(attempt=2, route=saved)):
        llm_client.chat_completion(MESSAGES)
    assert [provider for provider, _ in calls] == ["deepseek", "ark", "ark"]


def test_job_auth_failures_are_saved_as_disabled_but_other_jobs_start_at_primary(
    routes, monkeypatch, guarded_store
):
    calls = _plan(monkeypatch, {"deepseek": 401, "ark": 200})
    with llm_client.llm_job_context(_claimed()):
        llm_client.chat_completion(MESSAGES)
    saved = guarded_store[-1][2]["data_updates"]["llm_route"]
    assert "deepseek" in saved["disabled_providers"]
    with llm_client.llm_job_context(_claimed("job-b")):
        llm_client.chat_completion(MESSAGES)
    assert [provider for provider, _ in calls] == ["deepseek", "ark", "deepseek", "ark"]


def test_restored_disabled_provider_is_skipped(routes, monkeypatch, guarded_store):
    calls = _plan(monkeypatch, {"ark": 200})
    route = {"preferred_provider": "deepseek", "disabled_providers": ["deepseek"]}
    with llm_client.llm_job_context(_claimed(route=route)):
        llm_client.chat_completion(MESSAGES)
    assert [provider for provider, _ in calls] == ["ark"]


def test_all_transient_failures_do_not_disable_every_channel_on_worker_retry(
    routes, monkeypatch, guarded_store
):
    statuses = dict.fromkeys(PROVIDERS, 503)
    calls = _plan(monkeypatch, statuses)
    with llm_client.llm_job_context(_claimed()):
        with pytest.raises(llm_client.LLMClientError):
            llm_client.chat_completion(MESSAGES)
    saved = copy.deepcopy(guarded_store[-1][2]["data_updates"]["llm_route"])
    assert not saved["disabled_providers"]
    statuses["deepseek"] = 200
    with llm_client.llm_job_context(_claimed(attempt=2, route=saved)):
        result = llm_client.chat_completion(MESSAGES)
    assert result["generation_meta"]["provider"] == "deepseek"
    assert [provider for provider, _ in calls] == [*PROVIDERS, "deepseek"]


@pytest.mark.parametrize("lose_after_call", [False, True])
def test_loss_of_job_ownership_stops_before_more_external_calls(
    routes, monkeypatch, lose_after_call
):
    calls = _plan(monkeypatch, {"deepseek": 503, "ark": 200})

    def guarded_update(*args, **kwargs):
        if not lose_after_call or calls:
            return None
        return {"data": kwargs.get("data_updates", {})}

    monkeypatch.setattr(background_job_store, "update_job", guarded_update)
    monkeypatch.setattr(llm_client, "update_job", guarded_update, raising=False)
    with llm_client.llm_job_context(_claimed()):
        with pytest.raises(JobOwnershipLostError):
            llm_client.chat_completion(MESSAGES)
    assert len(calls) == (1 if lose_after_call else 0)


def test_concurrent_jobs_and_a_later_unscoped_call_have_independent_routes(
    routes, monkeypatch, guarded_store
):
    barrier = threading.Barrier(2)
    seen = []

    async def post(url, **kwargs):
        provider = _provider(url)
        seen.append(provider)
        if provider != "deepseek":
            barrier.wait(timeout=3)
        return _response(url)

    monkeypatch.setattr(llm_client, "_post_completion", post)

    def run(job_id, preferred):
        route = {"preferred_provider": preferred, "disabled_providers": []}
        with llm_client.llm_job_context(_claimed(job_id, route=route)):
            return llm_client.chat_completion(MESSAGES)["generation_meta"]["provider"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        ark = pool.submit(run, "job-ark", "ark")
        router = pool.submit(run, "job-router", "openrouter")
        assert ark.result(timeout=5) == "ark"
        assert router.result(timeout=5) == "openrouter"
    assert llm_client.chat_completion(MESSAGES)["generation_meta"]["provider"] == "deepseek"
    assert sorted(seen) == ["ark", "deepseek", "openrouter"]
