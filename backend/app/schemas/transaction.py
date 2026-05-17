from pydantic import BaseModel, Field, ConfigDict
from decimal import Decimal
from datetime import date, datetime
from typing import Optional


class TransactionBase(BaseModel):
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
    pass


class TransactionUpdate(BaseModel):
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


class TransactionResponse(TransactionBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
