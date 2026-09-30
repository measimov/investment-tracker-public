"""OpenAI 兼容 Chat Completions 薄客户端（DeepSeek 等，不引入 SDK）。"""

from typing import Any, Dict, List

import httpx

from ..config import settings
from ..core.logging import get_app_logger

logger = get_app_logger(__name__)


class LLMNotConfiguredError(Exception):
    """未配置 llm_report_api_key：功能性禁用，不发起任何网络请求。"""


class LLMClientError(Exception):
    """LLM 调用失败；status_code 供调用方区分确定性失败（4xx）与可重试失败。
    finish_reason 只在「200 但输出不可用」时有值：content 为空，或 finish_reason=length
    （推理/输出耗尽额度，content 为空或半截）。length 用 `is_output_truncated` 判定。"""

    def __init__(
        self, message: str, status_code: int | None = None, finish_reason: str | None = None
    ):
        super().__init__(message)
        self.status_code = status_code
        self.finish_reason = finish_reason


def llm_error_user_message(exc: "LLMClientError") -> str:
    """面向用户的稳定中文文案（#277）。异常原文含上游 URL 与响应片段，只进日志、不回显。"""
    if is_output_truncated(exc):
        return "模型输出超出长度上限，请缩短问题后重试"
    status = exc.status_code
    if status in (401, 403):
        return "LLM 的 API Key 无效或无权限，请管理员检查 LLM_REPORT_API_KEY"
    if status == 402:
        return "LLM 账户余额不足，请管理员充值后重试"
    if status == 429:
        return "LLM 请求过于频繁或额度已用尽，请稍后重试"
    if status is not None and 400 <= status < 500:
        return f"LLM 拒绝了本次请求（HTTP {status}），请稍后重试或联系管理员"
    return "LLM 服务暂时不可用或超时，请稍后重试"


def is_output_truncated(exc: BaseException) -> bool:
    """输出额度耗尽（finish_reason=length，空或半截输出）：同样的输入重试结果相同，
    调用方一律按确定性失败处理（status_code 为 None，不能落进「5xx/超时→重试」分支）。"""
    return isinstance(exc, LLMClientError) and exc.finish_reason == "length"


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

    finish_reason = choice.get("finish_reason")
    if content and finish_reason == "length":
        # 非空但被截断：半截内容绝不能当结果返回——JSON mode 调用方会报一个误导性的
        # 「不是合法 JSON」（00799 港股分析），Markdown 调用方会把半篇报告当成品落库
        logger.warning(
            "LLM 输出被截断（finish_reason=length，已输出 %s 字符，max_tokens=%s）",
            len(content),
            payload["max_tokens"],
        )
        raise LLMClientError(
            f"LLM 输出被截断（finish_reason=length，max_tokens={payload['max_tokens']}），"
            "可调大输出额度（标的分析 SECURITY_ANALYSIS_MAX_OUTPUT_TOKENS，"
            "报表映射 STATEMENT_MAX_OUTPUT_TOKENS，其余 LLM_REPORT_MAX_OUTPUT_TOKENS）",
            finish_reason=finish_reason,
        )

    if not content:
        # 推理模型（deepseek-flash 即 V4.1 Flash；deepseek-v4-pro 同理——DeepSeek 已撤回其
        # 09-14 下线决定，仍在服务）会先产生 reasoning_content；
        # 输出配额被推理耗尽时 content 为空（finish_reason=length）——同样的输入重试结果相同。
        # status_code 为 None：调用方用 `is_output_truncated` 判定 length 并按确定性失败处理。
        raise LLMClientError(
            f"LLM 输出为空（finish_reason={finish_reason}），"
            "可能是 max_tokens 配额被推理消耗，可调大输出额度（标的分析 "
            "SECURITY_ANALYSIS_MAX_OUTPUT_TOKENS，报表映射 STATEMENT_MAX_OUTPUT_TOKENS，"
            "其余 LLM_REPORT_MAX_OUTPUT_TOKENS）",
            finish_reason=finish_reason,
        )

    return {
        "content": content,
        "model": data.get("model", settings.llm_report_model),
        "usage": data.get("usage", {}),
    }
