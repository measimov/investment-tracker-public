import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(
    params=[
        None,
        {
            "provider": "ark",
            "requested_model": "deepseek-v4-1-flash-260910",
            "response_model": "deepseek-v4-1-flash-260910",
            "attempts": [
                {"provider": "deepseek", "status": "failed", "status_code": 503},
                {"provider": "ark", "status": "succeeded"},
            ],
            "elapsed_seconds": 1.5,
        },
    ],
    ids=["legacy-completion", "provider-metadata"],
)
def generation_meta(request):
    """生成来源在成功产物里保存，旧 completion 无此字段时保持原结构。"""
    return request.param


@pytest.fixture
def raw_llm_completions(monkeypatch):
    """仅模拟 HTTP 200 响应，保留真实客户端及报告管线的错误分类。"""
    import httpx

    from app.services import llm_client

    for provider in ("deepseek", "ark", "bailian", "openrouter"):
        prefix = "llm_report" if provider == "deepseek" else f"llm_{provider}"
        monkeypatch.setattr(llm_client.settings, f"{prefix}_api_key", "unit-test-key")
        monkeypatch.setattr(
            llm_client.settings, f"{prefix}_base_url", f"https://{provider}.test.invalid/v1"
        )
    state = {"responses": [], "calls": []}

    async def post(url, **kwargs):
        state["calls"].append(url)
        assert state["responses"], "本测试不应跨渠道重试或额外请求"
        content, finish_reason = state["responses"].pop(0)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(llm_client, "_post_completion", post)
    return state
