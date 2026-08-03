from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

SuggestionStatus = Literal["NEW", "MATCHED", "ACCEPTED", "IGNORED"]


class SuggestionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    broker_account_id: Optional[int]
    symbol: str
    name: Optional[str]
    market: str
    action_type: str
    ann_date: Optional[date]
    record_date: Optional[date]
    ex_date: date
    pay_date: Optional[date]
    currency: str
    cash_div_pre_tax: Optional[Decimal]
    cash_div_after_tax: Optional[Decimal]
    stk_div_per_share: Optional[Decimal]
    record_date_quantity: Optional[Decimal]
    quantity_basis: Optional[str]
    estimated_total_dividend: Optional[Decimal]
    status: SuggestionStatus
    matched_corporate_action_id: Optional[int]
    created_corporate_action_id: Optional[int]
    match_detail: Optional[dict]
    source: str
    created_at: datetime
    updated_at: datetime


class SuggestionAccept(BaseModel):
    """接受建议时的可选 override：账户归属与税额（券商到账常为税前全额）。"""

    broker_account_id: Optional[int] = None
    total_dividend: Optional[Decimal] = Field(None, ge=0)
    tax_withheld: Optional[Decimal] = Field(None, ge=0)


class SecurityEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    market: str
    event_type: str
    event_date: date
    source: str
    payload: Optional[dict]
