"""security_rules 请求/响应 schema：payload 按 rule_type 判别式强类型校验。"""

from datetime import date, datetime
from typing import Any, Dict, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

VALID_MARKETS = {"A股", "B股", "港股", "美股", "新加坡股", "加密货币"}
VALID_RULE_TYPES = {
    "EXCLUDE",
    "CASH_MANAGEMENT",
    "RELISTING",
    "NAME_OVERRIDE",
    "PRICE_GAP_EXEMPTION",
    "CMB_CASH_BUSINESS",
}
# CMB 业务映射的 symbol 是业务名、无市场；其余类型必须给市场
MARKET_REQUIRED_TYPES = VALID_RULE_TYPES - {"CMB_CASH_BUSINESS"}
# 仅允许 CMB 导入器方向语义（CASH_INFLOW_EVENT_TYPES/对账推导）覆盖的类型；
# FX_IN/FX_OUT 是 IBKR 外汇兑换专用，CMB 方向校验不认识，禁止映射
CMB_ALLOWED_EVENT_TYPES = (
    "DEPOSIT",
    "WITHDRAWAL",
    "INTEREST",
    "FEE",
    "TAX",
    "TRANSFER_IN",
    "TRANSFER_OUT",
    "OTHER",
)


class _RelistingPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_symbol: str = Field(min_length=1, max_length=20)
    new_market: str
    new_currency: str = Field(min_length=1, max_length=10)
    old_currency: str = Field(min_length=1, max_length=10)
    name: Optional[str] = Field(None, max_length=100)

    @field_validator("new_symbol", "new_currency", "old_currency")
    @classmethod
    def _strip_upper(cls, value: str) -> str:
        # 与外层 symbol 同规范：strip + 大写，否则小写代码永远匹配不到
        # 解析后的大写标的
        value = value.strip().upper()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("new_market")
    @classmethod
    def _known_market(cls, value: str) -> str:
        value = value.strip()
        if value not in VALID_MARKETS:
            raise ValueError(f"未知市场: {value}")
        return value

    @field_validator("name")
    @classmethod
    def _strip_optional_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return value.strip() or None


class _NameOverridePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def _strip_reject_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class _PriceGapPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_date: date
    end_date: Optional[date] = None

    @model_validator(mode="after")
    def _range_ok(self) -> "_PriceGapPayload":
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date 不能早于 start_date")
        return self


class _CmbCashBusinessPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: Literal[CMB_ALLOWED_EVENT_TYPES]


_PAYLOAD_MODELS = {
    "RELISTING": _RelistingPayload,
    "NAME_OVERRIDE": _NameOverridePayload,
    "PRICE_GAP_EXEMPTION": _PriceGapPayload,
    "CMB_CASH_BUSINESS": _CmbCashBusinessPayload,
}


class SecurityRuleCreate(BaseModel):
    rule_type: str
    symbol: str = Field(min_length=1, max_length=50)
    market: Optional[str] = Field(None, max_length=20)
    payload: Optional[Dict[str, Any]] = None
    note: Optional[str] = Field(None, max_length=200)

    @field_validator("rule_type", "symbol", "market")
    @classmethod
    def _strip_and_reject_blank(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def _validate_by_type(self) -> "SecurityRuleCreate":
        if self.rule_type not in VALID_RULE_TYPES:
            raise ValueError(f"未知规则类型: {self.rule_type}")
        if self.rule_type in MARKET_REQUIRED_TYPES:
            if not self.market:
                raise ValueError(f"{self.rule_type} 规则必须指定市场")
            if self.market not in VALID_MARKETS:
                raise ValueError(f"未知市场: {self.market}")
        elif self.market is not None:
            # 唯一键含 market：放行非空市场会让同一业务名靠不同 market
            # 绕过唯一性，读取端折字典时事件类型不确定
            raise ValueError("CMB_CASH_BUSINESS 规则不接受市场字段")

        model = _PAYLOAD_MODELS.get(self.rule_type)
        if model is None:
            if self.payload:
                raise ValueError(f"{self.rule_type} 规则不接受 payload")
            return self
        try:
            validated = model.model_validate(self.payload or {})
        except ValidationError as exc:  # 畸形 payload 统一转 422，绝不 500
            first = exc.errors()[0]
            location = ".".join(str(part) for part in first["loc"]) or "payload"
            raise ValueError(f"payload.{location}: {first['msg']}") from exc
        self.payload = validated.model_dump(mode="json")
        return self


class SecurityRuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    rule_type: str
    symbol: str
    market: Optional[str]
    payload: Optional[Dict[str, Any]]
    note: Optional[str]
    created_at: datetime
