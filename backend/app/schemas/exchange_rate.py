from pydantic import BaseModel, ConfigDict, Field
from datetime import date, datetime
from decimal import Decimal
from typing import Optional


class ExchangeRateBase(BaseModel):
    from_currency: str = Field(..., max_length=10, description="源币种代码")
    to_currency: str = Field(..., max_length=10, description="目标币种代码")
    rate: Decimal = Field(..., gt=0, description="汇率")
    effective_date: date = Field(..., description="生效日期")
    source: Optional[str] = Field(default='manual', max_length=50, description="汇率来源")
    is_active: Optional[bool] = Field(default=True, description="是否启用")


class ExchangeRateCreate(ExchangeRateBase):
    pass


class ExchangeRateUpdate(BaseModel):
    rate: Optional[Decimal] = Field(None, gt=0)
    is_active: Optional[bool] = None
    source: Optional[str] = Field(None, max_length=50)


class ExchangeRate(ExchangeRateBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ExchangeRateLatest(BaseModel):
    """最新汇率响应"""
    base_currency: str = Field(default="CNY", description="基准货币")
    rates: dict[str, Decimal] = Field(..., description="各币种对基准货币的汇率")
    effective_date: date = Field(..., description="汇率日期")
    source: str = Field(..., description="数据来源")


class CurrencyConvertRequest(BaseModel):
    """货币转换请求"""
    amount: Decimal = Field(..., description="金额")
    from_currency: str = Field(..., max_length=10, description="源币种")
    to_currency: str = Field(..., max_length=10, description="目标币种")


class CurrencyConvertResponse(BaseModel):
    """货币转换响应"""
    amount: Decimal = Field(..., description="原金额")
    from_currency: str
    converted_amount: Decimal = Field(..., description="转换后金额")
    to_currency: str
    rate: Decimal = Field(..., description="使用的汇率")
    effective_date: date = Field(..., description="汇率日期")
