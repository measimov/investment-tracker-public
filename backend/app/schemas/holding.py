from pydantic import BaseModel, Field, ConfigDict
from decimal import Decimal
from datetime import datetime
from typing import Optional


class HoldingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    username: Optional[str] = None
    symbol: str = Field(..., description="Stock/Asset symbol")
    name: str | None = Field(None, description="Asset name")
    market: str = Field(..., description="Market")
    quantity: Decimal = Field(..., description="Current holding quantity")
    avg_cost: Decimal = Field(..., description="Average cost per unit")
    total_cost: Decimal = Field(..., description="Total cost")
    currency: str = Field(..., description="Currency")
    current_price: Optional[Decimal] = Field(None, description="Current stock price")
    price_updated_at: Optional[datetime] = Field(None, description="Price update timestamp")
    updated_at: datetime


class HoldingPriceUpdate(BaseModel):
    """Schema for updating a single holding price"""
    current_price: Decimal = Field(..., gt=0, description="Current price must be greater than 0")


class PriceBatchUpdate(BaseModel):
    """Schema for batch price updates"""
    symbol: str = Field(..., description="Stock symbol")
    market: str = Field(..., description="Market")
    price: Decimal = Field(..., gt=0, description="Current price")
