"""OpenAI 兼容 Chat Completions 薄客户端（DeepSeek 等，不引入 SDK）。"""

from typing import Any, Dict, List

import httpx

from ..config import settings
from ..core.logging import get_app_logger

logger = get_app_logger(__name__)


class LLMNotConfiguredError(Exception):
    """未配置 llm_report_api_key：功能性禁用，不发起任何网络请求。"""


class LLMClientError(Exception):
    """LLM 调用失败；status_code 供调用方区分确定性失败（4xx）与可重试失败。"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def is_llm_configured() -> bool:
    """key 为空或为 <占位符> 均视同未配置（供 API 409 检查与定期调度共用）。"""
    api_key = settings.llm_report_api_key.strip()
    return bool(api_key) and not (api_key.startswith("<") and api_key.endswith(">"))


def chat_completion(
    messages: List[Dict[str, str]],
    *,
    max_tokens: int | None = None,
    temperature: float = 0.3,
    response_format: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """单次对话补全。返回 {"content", "model", "usage"}。

    response_format={"type": "json_object"} 启用 JSON mode（DeepSeek/OpenAI
    兼容）：模型保证输出合法 JSON，供结构化产物（标的分析标签）使用。
    """
    if not is_llm_configured():
        # 形如 <deepseek-api-key> 的占位符视同未配置：照抄示例文件不应打真实请求
        raise LLMNotConfiguredError("未配置 LLM API Key（llm_report_api_key）")
    api_key = settings.llm_report_api_key.strip()

    url = f"{settings.llm_report_base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": settings.llm_report_model,
        "messages": messages,
        "max_tokens": max_tokens or settings.llm_report_max_output_tokens,
        "temperature": temperature,
        "stream": False,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    try:
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=httpx.Timeout(settings.llm_report_timeout_seconds, connect=10.0),
        )
    except httpx.HTTPError as exc:
        raise LLMClientError(f"LLM 请求失败: {exc}") from exc

    if response.status_code != 200:
        body = response.text[:300]
        logger.warning("LLM API %s 返回 %s: %s", url, response.status_code, body)
        raise LLMClientError(
            f"LLM API 返回 {response.status_code}: {body}",
            status_code=response.status_code,
        )

    try:
        data = response.json()
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (ValueError, KeyError, IndexError) as exc:
        raise LLMClientError(f"LLM 响应格式异常: {response.text[:300]}") from exc

    if not content:
        # 推理模型（如 deepseek-v4-pro）会先产生 reasoning_content；输出配额
        # 被推理耗尽时 content 为空——这是确定性截断，报清晰错误而非落空报告。
        raise LLMClientError(
            f"LLM 输出为空（finish_reason={choice.get('finish_reason')}），"
            "可能是 max_tokens 配额被推理消耗，可调大 llm_report_max_output_tokens"
        )

    return {
        "content": content,
        "model": data.get("model", settings.llm_report_model),
        "usage": data.get("usage", {}),
    }
