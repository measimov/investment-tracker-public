"""OpenAI-compatible completions with bounded, job-local endpoint failover."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import time
from typing import Any, Dict, List

import httpx

from ..config import settings
from ..core.logging import get_app_logger
from .background_job_store import JobOwnershipLostError, update_job

logger = get_app_logger(__name__)
_PROVIDER_ORDER = ("deepseek", "ark", "bailian", "openrouter")
_AUTH_FAILURES = frozenset({401, 402, 403})
_SWITCH_STATUSES = _AUTH_FAILURES | {408, 429}


class LLMNotConfiguredError(Exception):
    """All channel keys are empty/placeholders: do not make any request."""


class LLMClientError(Exception):
    """Sanitized failure; existing callers use status_code/finish_reason for retries."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        finish_reason: str | None = None,
        *,
        attempts: list[dict] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.finish_reason = finish_reason
        self.attempts = attempts or []


def llm_error_user_message(exc: "LLMClientError") -> str:
    """Stable UI copy without upstream bodies, credentials, or URLs."""
    if is_output_truncated(exc):
        return "模型输出超出长度上限，请缩短问题后重试"
    status = exc.status_code
    if status in (401, 403):
        return "LLM 的 API Key 无效或无权限，请管理员检查已配置的 LLM 渠道"
    if status == 402:
        return "LLM 账户余额不足，请管理员充值后重试"
    if status == 429:
        return "LLM 请求过于频繁或额度已用尽，请稍后重试"
    if status is not None and 400 <= status < 500:
        return f"LLM 拒绝了本次请求（HTTP {status}），请稍后重试或联系管理员"
    return "LLM 服务暂时不可用或超时，请稍后重试"


def is_output_truncated(exc: BaseException) -> bool:
    return isinstance(exc, LLMClientError) and exc.finish_reason == "length"


@dataclass(frozen=True)
class _Endpoint:
    provider: str
    base_url: str
    model: str
    api_key: str = field(repr=False)


def _endpoints() -> list[_Endpoint]:
    endpoints = []
    for provider in _PROVIDER_ORDER:
        prefix = "llm_report" if provider == "deepseek" else f"llm_{provider}"
        key = getattr(settings, f"{prefix}_api_key").strip()
        if not key or (key.startswith("<") and key.endswith(">")):
            continue
        endpoints.append(
            _Endpoint(
                provider,
                getattr(settings, f"{prefix}_base_url").strip().rstrip("/"),
                getattr(settings, f"{prefix}_model").strip(),
                key,
            )
        )
    return endpoints


def is_llm_configured() -> bool:
    return bool(_endpoints())


@dataclass
class _Route:
    claimed: dict | None = None
    preferred_provider: str = "deepseek"
    disabled_providers: set[str] = field(default_factory=set)
    last_attempts: list[dict] = field(default_factory=list)
    # Only permanent channel errors survive worker retries. Never persist a timeout blacklist.
    last_auth_status: int = 401

    def checkpoint(self) -> None:
        if self.claimed is None:
            return
        route = {
            "preferred_provider": self.preferred_provider,
            "disabled_providers": sorted(self.disabled_providers),
            "last_attempts": list(self.last_attempts),
            "last_auth_status": self.last_auth_status,
        }
        if (
            update_job(
                self.claimed["id"],
                self.claimed["job_type"],
                data_updates={"llm_route": route},
                required_status="running",
                required_attempt_count=self.claimed.get("attempt_count"),
            )
            is None
        ):
            raise JobOwnershipLostError(self.claimed["id"])


_job_route: ContextVar[_Route | None] = ContextVar("llm_job_route", default=None)


@contextmanager
def llm_job_context(claimed: dict):
    """Restore a job's successful channel and fence external calls against lost ownership."""
    saved = (claimed.get("data") or {}).get("llm_route") or {}
    preferred = saved.get("preferred_provider", "deepseek")
    route = _Route(
        claimed=claimed,
        preferred_provider=preferred if preferred in _PROVIDER_ORDER else "deepseek",
        disabled_providers=set(saved.get("disabled_providers") or []) & set(_PROVIDER_ORDER),
        last_auth_status=(
            saved.get("last_auth_status")
            if saved.get("last_auth_status") in _AUTH_FAILURES
            else 401
        ),
    )
    token = _job_route.set(route)
    try:
        yield
    finally:
        _job_route.reset(token)


async def _post_completion(url: str, **kwargs) -> httpx.Response:
    # wait_for wraps client creation, connection, full body read, and cancellation/close.
    async with httpx.AsyncClient() as client:
        return await client.post(url, **kwargs)


def _parse_completion(response: httpx.Response, endpoint: _Endpoint, max_tokens: int) -> dict:
    try:
        data = response.json()
        choice = data["choices"][0]
        message = choice["message"]
        content = message.get("content")
        finish_reason = choice.get("finish_reason")
        model = data.get("model") or endpoint.model
        usage = data.get("usage") or {}
        if (
            not isinstance(model, str)
            or not isinstance(usage, dict)
            or (finish_reason is not None and not isinstance(finish_reason, str))
        ):
            raise ValueError("invalid metadata")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise LLMClientError("LLM 响应格式异常") from None

    if finish_reason == "length":
        raise LLMClientError(
            f"LLM 输出被截断（finish_reason=length，max_tokens={max_tokens}），"
            "可调大输出额度（标的分析 SECURITY_ANALYSIS_MAX_OUTPUT_TOKENS，"
            "报表映射 STATEMENT_MAX_OUTPUT_TOKENS，其余 LLM_REPORT_MAX_OUTPUT_TOKENS）",
            finish_reason="length",
        )
    if finish_reason == "content_filter" or message.get("refusal"):
        raise LLMClientError("LLM 拒绝输出本次内容", finish_reason="content_filter")
    if not isinstance(content, str) or not content.strip():
        # Do not leak an arbitrary upstream finish_reason through errors/logs.
        # Keep the known resource interruption so report pipelines can retry without
        # consuming their permanent failure allowance.
        reason = (
            finish_reason
            if finish_reason in {None, "stop", "tool_calls", "insufficient_system_resource"}
            else "invalid"
        )
        raise LLMClientError(f"LLM 输出为空（finish_reason={reason}）", finish_reason=reason)
    return {"content": content, "model": model, "usage": usage}


async def _complete(messages, *, max_tokens, temperature, response_format, endpoints, route):
    started = time.monotonic()
    deadline = started + settings.llm_fallback_budget_seconds
    preferred_rank = _PROVIDER_ORDER.index(route.preferred_provider)
    candidates = [
        endpoint
        for endpoint in endpoints
        if _PROVIDER_ORDER.index(endpoint.provider) >= preferred_rank
        and endpoint.provider not in route.disabled_providers
    ]
    failures = []
    route.last_attempts = []
    for endpoint in candidates:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            failures.append(LLMClientError("LLM 渠道切换总预算已耗尽"))
            break
        # Guard before spending tokens, including each fallback hop.
        route.checkpoint()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            failures.append(LLMClientError("LLM 渠道切换总预算已耗尽"))
            break
        timeout = min(float(settings.llm_report_timeout_seconds), remaining)
        payload = {
            "model": endpoint.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if endpoint.provider == "openrouter":
            payload["provider"] = {"require_parameters": True}
        attempt = {"provider": endpoint.provider, "requested_model": endpoint.model}
        hop_started = time.monotonic()
        error = None
        try:
            response = await asyncio.wait_for(
                _post_completion(
                    f"{endpoint.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {endpoint.api_key}"},
                    json=payload,
                    timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
                ),
                timeout=timeout,
            )
        except (asyncio.TimeoutError, httpx.TimeoutException):
            error = LLMClientError("LLM 请求超时")
            attempt["reason"] = "timeout"
        except (httpx.InvalidURL, httpx.UnsupportedProtocol):
            error = LLMClientError("LLM 渠道地址配置无效", status_code=400)
            attempt["reason"] = "configuration"
        except httpx.HTTPError:
            error = LLMClientError("LLM 网络请求失败")
            attempt["reason"] = "network"
        if error is None:
            attempt["status_code"] = response.status_code
            if response.status_code != 200:
                error = LLMClientError(
                    f"LLM 渠道 {endpoint.provider} 返回 HTTP {response.status_code}",
                    status_code=response.status_code,
                )
                attempt["reason"] = "http_error"
            else:
                try:
                    result = _parse_completion(response, endpoint, max_tokens)
                except LLMClientError as exc:
                    error = exc
                    attempt["reason"] = "invalid_output"
        attempt["elapsed_seconds"] = round(time.monotonic() - hop_started, 3)
        attempt.setdefault("reason", "success")
        route.last_attempts.append(attempt)
        log = logger.info if error is None else logger.warning
        log(
            "LLM provider=%s model=%s status=%s reason=%s elapsed=%.3fs",
            endpoint.provider,
            endpoint.model,
            attempt.get("status_code"),
            attempt["reason"],
            attempt["elapsed_seconds"],
        )
        if error is None:
            route.preferred_provider = endpoint.provider
            route.checkpoint()
            result["generation_meta"] = {
                "provider": endpoint.provider,
                "requested_model": endpoint.model,
                "response_model": result["model"],
                "attempts": list(route.last_attempts),
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
            return result
        if error.status_code in _AUTH_FAILURES:
            route.disabled_providers.add(endpoint.provider)
            route.last_auth_status = error.status_code
        route.checkpoint()
        # Successful HTTP with unusable output, and request/configuration errors, stop here.
        if attempt["reason"] == "invalid_output" or (
            error.status_code is not None
            and error.status_code not in _SWITCH_STATUSES
            and not 500 <= error.status_code < 600
        ):
            error.attempts = list(route.last_attempts)
            raise error from None
        failures.append(error)

    # A later auth failure must not turn an earlier temporary outage into a permanent failure.
    transient = next(
        (
            error
            for error in failures
            if error.status_code is None
            or error.status_code in {408, 429}
            or 500 <= error.status_code < 600
        ),
        None,
    )
    if transient is not None:
        # Existing job callers treat all 4xx as terminal, so translate exhausted 408/429
        # into the transport retry category; exact upstream codes remain in attempts.
        status = transient.status_code
        raise LLMClientError(
            "LLM 可用渠道暂时不可用或切换预算已耗尽，请稍后重试",
            status_code=status if status is not None and status >= 500 else None,
            attempts=list(route.last_attempts),
        ) from None
    last = (
        failures[-1]
        if failures
        else LLMClientError(
            "LLM 本任务的可用渠道均因认证或余额问题被禁用", status_code=route.last_auth_status
        )
    )
    last.attempts = list(route.last_attempts)
    raise last from None


def chat_completion(
    messages: List[Dict[str, str]],
    *,
    max_tokens: int | None = None,
    temperature: float = 0.3,
    response_format: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """One bounded completion, trying each eligible endpoint at most once.

    Existing sync API/job callers keep their interface. Async HTTP allows a strict
    network deadline (httpx phase timeouts alone do not bound a slowly trickling body).
    """
    endpoints = _endpoints()
    if not endpoints:
        raise LLMNotConfiguredError("未配置任何 LLM 渠道的 API Key")
    route = _job_route.get() or _Route()
    return asyncio.run(
        _complete(
            messages,
            max_tokens=max_tokens or settings.llm_report_max_output_tokens,
            temperature=temperature,
            response_format=response_format,
            endpoints=endpoints,
            route=route,
        )
    )
