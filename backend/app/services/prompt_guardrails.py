"""LLM prompt 共享守则（issue #145：「禁止先验知识」三处独立维护已措辞分化）。

三条 prompt 管线（财报摘要 / 商业画像 / 标的分析）的输入类型不同（原文节选 /
摘要+数据 / 结构化数据集），"未提及写什么"的兜底词必须各自贴合语境，所以
共享的是**骨架**：核心禁令一份 + 参数化的来源与兜底子句。

改动警告：本模块的字节变化会传导进下游 prompt——
- 财报摘要侧要求**字节不变**（tests/test_llm_pipeline_misc.py 的金样钉住
  首段；变了必须 bump DIGEST_PROMPT_VERSION，代价是全部摘要缓存失效重烧）；
- 商业画像侧随 PROFILE_PROMPT_VERSION 管理（business_profile_prompts.py）；
- 标的分析无缓存版本机制（24h 新鲜度自然过期），字节变化零成本。
"""

# 核心禁令：三条管线共用的那一句
NO_PRIOR_KNOWLEDGE_CLAUSE = "禁止引入任何对该公司的先验知识"


def no_prior_knowledge_guardrail(source_clause: str, missing_clause: str) -> str:
    """组装完整守则句：只依据{来源}，{核心禁令}；{未提及兜底}，不得推测补全。"""
    return (
        f"只依据用户提供的{source_clause}，{NO_PRIOR_KNOWLEDGE_CLAUSE}；"
        f"{missing_clause}，不得推测补全。"
    )
