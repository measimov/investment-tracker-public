"""标的分析测试用的完整报告正文：解析层会校验必需章节与免责声明（#287），
LLM 桩输出须带完整章节，否则会被判为半截报告。"""

import json

from app.services.security_analysis_prompts import ANALYSIS_DISCLAIMER, REPORT_SECTIONS

FULL_REPORT = (
    "\n\n".join(f"## {section}\n内容。" for section in REPORT_SECTIONS)
    + f"\n\n{ANALYSIS_DISCLAIMER}"
)
# 可直接拼进 JSON 字符串字面量（已转义，不含首尾引号）
FULL_REPORT_JSON = json.dumps(FULL_REPORT, ensure_ascii=False)[1:-1]


def analysis_output(tags, risk_level="medium", summary="s", report=FULL_REPORT) -> str:
    return json.dumps(
        {"tags": tags, "risk_level": risk_level, "summary": summary, "report_markdown": report},
        ensure_ascii=False,
    )
