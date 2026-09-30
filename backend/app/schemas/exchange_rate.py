from pydantic import BaseModel, ConfigDict, Field, field_validator
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from .read_models import read_model


class ExchangeRateCreate(BaseModel):
    """手工录入汇率。来源由服务端定为 manual（#277：客户端不能自称官方中间价）。"""

    from_currency: str = Field(..., max_length=10, description="源币种代码")
    to_currency: str = Field(..., max_length=10, description="目标币种代码")
    rate: Decimal = Field(..., gt=0, description="汇率")
    effective_date: date = Field(..., description="生效日期")


class ExchangeRateUpdate(BaseModel):
    """手工修正汇率数值；改过数值的行一律记为 manual（不再被自动刷新覆盖）。"""

    rate: Decimal = Field(..., gt=0)


class ExchangeRate(read_model(ExchangeRateCreate)):
    source: Optional[str] = Field(None, description="汇率来源")
    is_active: bool = Field(True, description="是否启用（手工删除 = 停用，保留审计）")
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_validator("is_active", mode="before")
    @classmethod
    def _null_is_inactive(cls, value):
        # 列可为 NULL（历史手工行）：按停用处理，否则 include_inactive 的列表序列化 500
        return False if value is None else value


class ExchangeRateLatestDetail(BaseModel):
    """单个币种对基准货币的最新汇率及其自身的生效日期/来源"""

    rate: Decimal = Field(..., description="汇率")
    effective_date: date = Field(..., description="该币种汇率的生效日期")
    source: Optional[str] = Field(None, description="该币种汇率的来源")


class ExchangeRateLatest(BaseModel):
    """最新汇率响应"""

    base_currency: str = Field(default="CNY", description="基准货币")
    rates: dict[str, Decimal] = Field(..., description="各币种对基准货币的汇率")
    effective_date: date = Field(..., description="各币种中最新的汇率日期（兼容字段）")
    source: str = Field(..., description="最新那条汇率的来源（兼容字段）")
    details: dict[str, ExchangeRateLatestDetail] = Field(
        default_factory=dict, description="按币种的最新汇率、生效日期与来源（不含基准货币）"
    )


class ExchangeRateCheck(BaseModel):
    """官方中间价与第三方报价的一次逐日比对（#200）"""

    from_currency: str
    to_currency: str
    check_date: date
    official_date: date
    official_source: str
    official_rate: Decimal
    reference_source: str
    reference_rate: Decimal
    diff_pct: Decimal

    model_config = ConfigDict(from_attributes=True)
