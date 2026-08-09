from sqlalchemy import Column, Integer, String, Numeric, Date, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from ..database import Base


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    broker_account_id = Column(
        Integer,
        ForeignKey("broker_accounts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    import_batch_id = Column(
        Integer,
        ForeignKey("import_batches.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    symbol = Column(String(20), nullable=False, index=True)
    name = Column(String(100))
    market = Column(String(20), nullable=False, index=True)
    # BUY / SELL / TRANSFER_OUT / TRANSFER_IN（转仓对，成本基础跟随迁移）
    transaction_type = Column(String(20), nullable=False)
    # 转仓对互指：OUT.linked → IN.id，IN.linked → OUT.id。删除任一腿时 API 层
    # 同时删除另一腿；数据库侧 SET NULL 仅作兜底。
    linked_transaction_id = Column(
        Integer,
        ForeignKey("transactions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    quantity = Column(Numeric(18, 8), nullable=False)
    price = Column(Numeric(18, 8), nullable=False)
    # 服务层早已保证有值（真实账本零 NULL 存量），DB 侧同步收紧
    fee = Column(Numeric(18, 8), nullable=False, default=0, server_default="0")
    transaction_date = Column(Date, nullable=False, index=True)
    currency = Column(String(10), nullable=False, default="CNY", server_default="CNY")
    notes = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    broker_account = relationship("BrokerAccount", back_populates="transactions")
    import_batch = relationship("ImportBatch", back_populates="transactions")
