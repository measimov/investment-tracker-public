from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from ..database import Base


class SecurityProfileData(Base):
    """标的基本面数据表（全局，与 SecurityPrice 同定位，A股 only）。

    通用 JSON 存储：消费方只有 LLM（吃 JSON）与详情面板（展示表格），无
    关系查询需求，逐接口建表属过度设计。dataset 即 Tushare 接口名族，
    period_key 为报告期/公告日等自然键，幂等 upsert。
    """

    __tablename__ = "security_profile_data"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True, comment="股票代码")
    market = Column(String(20), nullable=False, comment="市场")
    dataset = Column(String(30), nullable=False, comment="数据集（Tushare 接口名）")
    period_key = Column(String(40), nullable=False, comment="报告期/公告日等自然键")
    payload = Column(JSON, nullable=False, comment="原始行（归一后的 JSON）")
    fetched_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "symbol", "market", "dataset", "period_key",
            name="uq_security_profile_identity",
        ),
        CheckConstraint(
            "dataset IN ("
            "'fina_indicator', 'forecast', 'express', 'daily_basic', "
            "'dividend_history', 'fina_audit', 'pledge_stat', 'stk_holdertrade'"
            ")",
            name="ck_security_profile_dataset",
        ),
    )


class SecurityAnalysis(Base):
    """LLM 标的分析（全局产物：只依赖公开数据，不依赖任何用户持仓）。

    结构化标签 + Markdown 全文一次生成（DeepSeek JSON mode）。持仓列表按
    (symbol, market) 取最新一条；历史行保留供追溯。guardrail：分析只基于
    input_payload 中的客观数据（审计意见/质押/减持/解禁等风险信号），
    不引入模型自身对该公司的知识。
    """

    __tablename__ = "security_analyses"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True, comment="股票代码")
    market = Column(String(20), nullable=False, comment="市场")
    name = Column(String(100), comment="资产名称")

    tags = Column(JSON, nullable=False, comment="结构化标签数组")
    risk_level = Column(String(10), nullable=False, comment="low / medium / high")
    summary = Column(String(300), nullable=False, comment="一句话摘要")
    content = Column(Text, nullable=False, comment="Markdown 全文分析")

    model = Column(String(50), nullable=False, comment="生成模型")
    prompt_tokens = Column(Integer)
    completion_tokens = Column(Integer)
    total_tokens = Column(Integer)
    input_payload = Column(JSON, nullable=False, comment="生成时的压缩输入（可复现）")
    data_fetched_at = Column(Date, comment="输入数据抓取日（非数据截止日：数据本身的报告期见各数据集 period）")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "risk_level IN ('low', 'medium', 'high')",
            name="ck_security_analyses_risk_level",
        ),
    )
