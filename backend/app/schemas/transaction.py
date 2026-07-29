from pydantic import BaseModel, Field, ConfigDict
from decimal import Decimal
from datetime import date, datetime
from typing import Optional


class TransactionBase(BaseModel):
    broker_account_id: Optional[int] = None
    symbol: str = Field(..., max_length=20, description="Stock/Asset symbol")
    name: Optional[str] = Field(None, max_length=100, description="Asset name")
    market: str = Field(..., max_length=20, description="Market (A股, 港股, 美股, 加密货币, etc.)")
    transaction_type: str = Field(..., pattern="^(BUY|SELL)$", description="Transaction type: BUY or SELL")
    quantity: Decimal = Field(..., gt=0, description="Quantity")
    price: Decimal = Field(..., gt=0, description="Price per unit")
    fee: Decimal = Field(default=Decimal("0"), ge=0, description="Transaction fee")
    transaction_date: date = Field(..., description="Transaction date")
    currency: str = Field(default="CNY", max_length=10, description="Currency")
    notes: Optional[str] = Field(None, description="Notes")


class TransactionCreate(TransactionBase):
    model_config = ConfigDict(extra="forbid")


class TransactionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    broker_account_id: Optional[int] = None
    symbol: Optional[str] = Field(None, max_length=20)
    name: Optional[str] = Field(None, max_length=100)
    market: Optional[str] = Field(None, max_length=20)
    transaction_type: Optional[str] = Field(None, pattern="^(BUY|SELL)$")
    quantity: Optional[Decimal] = Field(None, gt=0)
    price: Optional[Decimal] = Field(None, gt=0)
    fee: Optional[Decimal] = Field(None, ge=0)
    transaction_date: Optional[date] = None
    currency: Optional[str] = Field(None, max_length=10)
    notes: Optional[str] = None


class TransferCreate(BaseModel):
    """转仓：把持仓从一个账户桶移到另一个（成本基础跟随，不产生盈亏）。

    创建 TRANSFER_OUT / TRANSFER_IN 互指交易对；账户为 null 表示"未指定账户"桶。
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., max_length=20)
    market: str = Field(..., max_length=20)
    quantity: Decimal = Field(..., gt=0)
    from_broker_account_id: Optional[int] = Field(None, description="转出账户；null=未指定账户桶")
    to_broker_account_id: Optional[int] = Field(None, description="转入账户；null=未指定账户桶")
    transfer_date: date = Field(...)
    notes: Optional[str] = None


class TransactionResponse(TransactionBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    # 放开 BUY|SELL 校验：转仓对（TRANSFER_OUT/TRANSFER_IN）也要能出现在列表里。
    transaction_type: str = Field(..., description="BUY / SELL / TRANSFER_OUT / TRANSFER_IN")
    linked_transaction_id: Optional[int] = Field(
        None, description="转仓对的另一腿 id；普通交易为 null"
    )
    import_batch_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime
