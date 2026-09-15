from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from ..database import Base


class SecurityOpinionSummary(Base):
    """雪球观点摘要（全局追加产物，与 SecurityAnalysis 同定位）。

    输入是 xueqiu-timeline-archiver 写入同库的关注用户发言（外部表，本应用
    只读），LLM 按「近期窗口 vs 更早基线」归纳各作者立场与**近期观点变化**。
    追加式：最新 = created_at DESC 第一条；上一条天然可 diff（前端展示
    "较上次标签变化"）。不塞 security_profile_data——那是按自然键幂等 upsert
    的输入缓存，放不下"取最新 + 历史留痕"的产物语义。

    guardrail：内容是雪球用户个人观点的**转述**，不是对公司的事实判断；
    生成侧禁止引入模型先验知识（见 opinion_summary_prompts）。
    """

    __tablename__ = "security_opinion_summaries"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True, comment="股票代码")
    market = Column(String(20), nullable=False, comment="市场")
    name = Column(String(100), comment="资产名称（公共元数据，可空）")

    tags = Column(JSONB, nullable=False, comment="观点标签数组（白名单）")
    author_stances = Column(
        JSONB, nullable=False,
        comment="逐作者立场 [{author, stance, recent_change, evidence}]",
    )
    summary = Column(String(300), nullable=False, comment="一句话观点概括")
    content = Column(Text, nullable=False, comment="Markdown 全文")

    model = Column(String(50), nullable=False, comment="生成模型")
    prompt_tokens = Column(Integer)
    completion_tokens = Column(Integer)
    total_tokens = Column(Integer)
    input_payload = Column(JSONB, nullable=False, comment="生成时的压缩输入（可复现）")

    # 生成参数快照：窗口口径随配置漂移，回放/解读历史行不读 settings
    recent_days = Column(Integer, nullable=False, comment="近期窗口天数（生成时值）")
    lookback_days = Column(Integer, nullable=False, comment="回看深度天数（生成时值）")
    utterance_count = Column(Integer, nullable=False, comment="纳入的发言总数")
    recent_utterance_count = Column(Integer, nullable=False, comment="其中近期窗口内条数")
    # 增量新鲜度锚点：无新发言的重跑可零成本跳过
    latest_utterance_at = Column(DateTime(timezone=True), comment="纳入的最新发言时间")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
