from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

VALID_MARKETS = {"A股", "B股", "港股", "美股", "新加坡股", "加密货币"}


class ExcludedSecurityCreate(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    market: str = Field(min_length=1, max_length=20)
    note: Optional[str] = Field(default=None, max_length=200)

    @field_validator("symbol", "market")
    @classmethod
    def _strip_and_reject_blank(cls, value: str) -> str:
        # min_length 在 strip 前校验，全空格会漏过并以空串入库——这里兜住
        value = value.strip()
        if not value:
            raise ValueError("不能为空白字符")
        return value


class ExcludedSecurityResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    market: str
    note: Optional[str]
    created_at: datetime
