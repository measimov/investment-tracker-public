from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


CashEventType = Literal[
    "DEPOSIT",
    "WITHDRAWAL",
    "INTEREST",
    "FEE",
    "TAX",
    "TRANSFER_IN",
    "TRANSFER_OUT",
    "FX_IN",
    "FX_OUT",
    "OTHER",
]


class CashEventBase(BaseModel):
    event_type: CashEventType
    amount: Decimal = Field(..., gt=0)
    currency: str = Field(default="CNY", min_length=1, max_length=10)
    event_date: date
    notes: Optional[str] = None


class CashEventCreate(CashEventBase):
    broker_account_id: int


class CashEventUpdate(BaseModel):
    broker_account_id: Optional[int] = None
    event_type: Optional[CashEventType] = None
    amount: Optional[Decimal] = Field(None, gt=0)
    currency: Optional[str] = Field(None, min_length=1, max_length=10)
    event_date: Optional[date] = None
    notes: Optional[str] = None


class CashEventResponse(CashEventBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    broker_account_id: Optional[int]
    created_at: datetime
    updated_at: datetime
