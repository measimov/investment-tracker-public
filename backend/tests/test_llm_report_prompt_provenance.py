"""生成来源不进入后续追问的账本输入。"""

from app.services.llm_report_prompts import build_chat_messages


def test_follow_up_uses_the_same_ledger_snapshot_without_generation_metadata():
    ledger = {"meta": {"as_of": "2026-10-01"}, "accounts": [{"id": 1}]}
    stored = {**ledger, "generation_meta": {"provider": "ark", "attempts": []}}
    expected = build_chat_messages("报告", ledger, [], "为什么？")
    assert build_chat_messages("报告", stored, [], "为什么？") == expected
    assert stored["generation_meta"]["provider"] == "ark"
