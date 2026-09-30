"""#277：错误路径不回显内部异常原文（数据库错误里的 SQL/约束名、LLM 上游 URL 与响应片段）。"""

import pytest
from fastapi import HTTPException

from app.services.llm_client import LLMClientError, llm_error_user_message


@pytest.mark.parametrize(
    "exc, expected",
    [
        (LLMClientError("LLM API https://api.x/v1 返回 401: {...}", status_code=401), "API Key"),
        (LLMClientError("返回 402", status_code=402), "余额不足"),
        (LLMClientError("返回 429", status_code=429), "过于频繁"),
        (LLMClientError("返回 400: bad", status_code=400), "HTTP 400"),
        (LLMClientError("连接超时 https://api.x/v1"), "暂时不可用"),
        (LLMClientError("输出截断", finish_reason="length"), "超出长度上限"),
    ],
)
def test_llm_errors_map_to_stable_messages(exc, expected):
    message = llm_error_user_message(exc)
    assert expected in message
    assert "https://" not in message


def test_batch_price_update_hides_database_error_text(monkeypatch):
    from types import SimpleNamespace

    from app.api import holdings as holdings_api

    class ExplodingSession:
        def query(self, *args, **kwargs):
            return self

        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return []

        def commit(self):
            raise RuntimeError('violates constraint "uix_secret" SQL: UPDATE holdings ...')

        def rollback(self):
            pass

    with pytest.raises(HTTPException) as info:
        holdings_api.batch_update_prices(
            [], current_user=SimpleNamespace(id=1), db=ExplodingSession()
        )
    assert info.value.status_code == 500
    assert "uix_secret" not in info.value.detail
    assert info.value.detail == "批量更新价格失败，请稍后重试"
