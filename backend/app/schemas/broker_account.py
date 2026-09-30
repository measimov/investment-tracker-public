from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field
from .read_models import read_model


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


class BrokerAccountResponse(read_model(BrokerAccountBase)):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
    # 是否已有交易/公司行动/现金事件/导入批次等引用：有则只能停用不能删除（列表端点计算）
    has_records: bool = False
