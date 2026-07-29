from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class BrokerAccountBase(BaseModel):
    broker: str = Field(..., min_length=1, max_length=100)
    account_name: str = Field(..., min_length=1, max_length=100)
    account_number_masked: Optional[str] = Field(None, max_length=100)
    base_currency: str = Field(default="CNY", min_length=1, max_length=10)
    is_active: bool = True
    notes: Optional[str] = None


class BrokerAccountCreate(BrokerAccountBase):
    pass


class BrokerAccountUpdate(BaseModel):
    broker: Optional[str] = Field(None, min_length=1, max_length=100)
    account_name: Optional[str] = Field(None, min_length=1, max_length=100)
    account_number_masked: Optional[str] = Field(None, max_length=100)
    base_currency: Optional[str] = Field(None, min_length=1, max_length=10)
    is_active: Optional[bool] = None
    notes: Optional[str] = None


class BrokerAccountResponse(BrokerAccountBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
