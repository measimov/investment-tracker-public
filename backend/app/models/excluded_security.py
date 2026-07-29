from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from ..database import Base


class ExcludedSecurity(Base):
    """现金管理标的排除清单（如货币基金 511880）。

    组合专注股票投资回报：清单内标的在券商对账单导入时只归档不入账，
    月末对账比对时双侧忽略。按用户配置，(symbol, market) 精确匹配比对键；
    导入侧按 symbol 匹配（对账单代码空间内无歧义）。
    """

    __tablename__ = "excluded_securities"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    symbol = Column(String(20), nullable=False)
    market = Column(String(20), nullable=False)
    note = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "symbol", "market", name="uq_excluded_securities_key"),
    )
