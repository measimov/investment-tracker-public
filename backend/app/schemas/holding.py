from pydantic import BaseModel, Field, ConfigDict
from decimal import Decimal
from datetime import datetime
from typing import Optional


class HoldingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    broker_account_id: Optional[int] = Field(
        None, description="Owning broker account; null = 未指定账户桶"
    )
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


class AdminHoldingResponse(HoldingResponse):
    """admin 视图专用：附归属用户名（issue #137）。

    此前是给 ORM 实例挂动态属性 `holding.username` 喂 HoldingResponse 的
    可选字段——该字段在所有非 admin 端点恒为 null，纯属 schema 污染。
    """

    username: Optional[str] = None


class HoldingPriceUpdate(BaseModel):
    """Schema for updating a single holding price"""
    current_price: Decimal = Field(..., gt=0, description="Current price must be greater than 0")


class PriceBatchUpdate(BaseModel):
    """Schema for batch price updates"""
    symbol: str = Field(..., description="Stock symbol")
    market: str = Field(..., description="Market")
    price: Decimal = Field(..., gt=0, description="Current price")
