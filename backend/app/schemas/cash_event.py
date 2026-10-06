from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from .read_models import read_model


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
    tax_kind: Optional[Literal["DIVIDEND"]] = None
    amount: Decimal = Field(..., gt=0)
    currency: str = Field(default="CNY", min_length=1, max_length=10)
    event_date: date
    notes: Optional[str] = None


class CashEventCreate(CashEventBase):
    broker_account_id: int


class CashEventUpdate(BaseModel):
    broker_account_id: Optional[int] = None
    event_type: Optional[CashEventType] = None
    tax_kind: Optional[Literal["DIVIDEND"]] = None
    amount: Optional[Decimal] = Field(None, gt=0)
    currency: Optional[str] = Field(None, min_length=1, max_length=10)
    event_date: Optional[date] = None
    notes: Optional[str] = None


class DividendTaxAllocationInput(BaseModel):
    corporate_action_id: int
    amount: Decimal = Field(gt=0, max_digits=24, decimal_places=8)


class DividendTaxAllocationsUpdate(BaseModel):
    allocations: list[DividendTaxAllocationInput] = Field(max_length=100)


class DividendTaxAllocationResponse(read_model(DividendTaxAllocationInput)):
    model_config = ConfigDict(from_attributes=True)


class CashEventResponse(read_model(CashEventBase)):
    model_config = ConfigDict(from_attributes=True)

    id: int
    broker_account_id: Optional[int]
    created_at: datetime
    updated_at: datetime
    # 导入产物（被券商来源流水链接）：API 层不可改删，前端据此隐藏编辑/删除。
    # 与交易、公司行动同名同判据（api/_ownership.annotate_read_only，#283）
    read_only: bool = False
    tax_allocations: list[DividendTaxAllocationResponse] = Field(default_factory=list)
    unallocated_tax_amount: Optional[Decimal] = None
