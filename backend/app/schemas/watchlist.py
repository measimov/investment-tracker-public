from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .security_rule import VALID_MARKETS


class WatchlistItemBase(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20)
    market: str = Field(..., max_length=20)
    name: Optional[str] = Field(None, max_length=100)
    note: Optional[str] = Field(None, max_length=500)

    @field_validator("market")
    @classmethod
    def market_must_be_valid(cls, value: str) -> str:
        if value not in VALID_MARKETS:
            raise ValueError(f"market 必须是 {sorted(VALID_MARKETS)} 之一")
        return value

    @model_validator(mode="after")
    def symbol_normalize(self):
        # 走手工入口共享归一化（大写 + 港股补零到 5 位）：档案/分析/价格按
        # (symbol, market) 精确匹配且唯一约束区分大小写，aapl/AAPL 会并存；
        # 港股 700/00700 则会与持仓/导入器的 5 位口径分裂成同券双键
        from ..services.symbol_normalization import normalize_manual_symbol

        normalized = normalize_manual_symbol(self.symbol, self.market)
        if not normalized:
            raise ValueError("symbol 不能为空")
        self.symbol = normalized
        return self


class WatchlistItemCreate(WatchlistItemBase):
    model_config = ConfigDict(extra="forbid")


class WatchlistItemUpdate(BaseModel):
    """只允许改展示属性；symbol/market 是身份，改身份请删除后重加。"""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(None, max_length=100)
    note: Optional[str] = Field(None, max_length=500)


class WatchlistItemResponse(WatchlistItemBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    current_price: Optional[Decimal] = None
    price_updated_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    # 格雷厄姆准则摘要（graham_screen 轻量计算；无档案数据时为 None）
    graham_summary: Optional[Dict[str, Any]] = None


class WatchlistMembershipResponse(BaseModel):
    """详情页轻量 membership 查询的契约（评审 P2：无 response_model 时生成类型退化为
    unknown，前端手写泛型会重新打开类型漂移孔）。"""

    watching: bool
    item_id: Optional[int] = None

