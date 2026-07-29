from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from decimal import Decimal
from datetime import date, datetime
from typing import Optional, Literal


# 公司行动类型枚举
ActionType = Literal[
    "CASH_DIVIDEND",      # 现金股息
    "STOCK_DIVIDEND",     # 股票股息/红股
    "RIGHTS_ISSUE",       # 配股
    "STOCK_SPLIT",        # 拆股
    "REVERSE_SPLIT",      # 合股
    "BONUS_ISSUE",        # 送股
    "SPIN_OFF",           # 拆分
    "MERGER"              # 合并
]


class CorporateActionBase(BaseModel):
    """公司行动基础模型"""
    broker_account_id: Optional[int] = None
    symbol: str = Field(..., max_length=20, description="股票代码")
    name: Optional[str] = Field(None, max_length=100, description="资产名称")
    market: str = Field(..., max_length=20, description="市场（A股、港股、美股等）")
    action_type: ActionType = Field(..., description="公司行动类型")

    # 日期
    ex_date: date = Field(..., description="除权除息日")
    record_date: Optional[date] = Field(None, description="登记日")
    payment_date: Optional[date] = Field(None, description="支付日/到账日")

    # 现金股息相关
    dividend_per_share: Optional[Decimal] = Field(None, ge=0, description="每股股息金额")
    total_dividend: Optional[Decimal] = Field(None, ge=0, description="股息总额")

    # 税务相关
    tax_withheld: Optional[Decimal] = Field(default=Decimal("0"), ge=0, description="预扣税金额")
    tax_rate: Optional[Decimal] = Field(None, ge=0, le=1, description="税率（0-1之间，如0.10表示10%）")
    net_dividend: Optional[Decimal] = Field(None, ge=0, description="税后净股息")

    # 股票股息/红股相关
    shares_received: Optional[Decimal] = Field(None, ge=0, description="获得的股票数量")
    distribution_ratio: Optional[str] = Field(None, max_length=20, description="分配比例（如10:3）")

    # 配股相关
    subscription_price: Optional[Decimal] = Field(None, ge=0, description="认购价格")
    subscription_quantity: Optional[Decimal] = Field(None, ge=0, description="认购数量")
    subscription_amount: Optional[Decimal] = Field(None, ge=0, description="认购金额")

    # 拆股/合股相关
    split_ratio: Optional[str] = Field(None, max_length=20, description="拆股比例（如1:2）")
    new_shares: Optional[Decimal] = Field(None, ge=0, description="拆股后的股数")

    # 成本基础调整
    cost_basis_adjustment: Optional[Decimal] = Field(None, description="成本基础调整金额")
    adjusted_quantity: Optional[Decimal] = Field(None, ge=0, description="调整后的持股数量")
    adjusted_cost_per_share: Optional[Decimal] = Field(None, ge=0, description="调整后的每股成本")

    # 其他
    currency: str = Field(default="CNY", max_length=10, description="币种")
    notes: Optional[str] = Field(None, description="备注")

    @field_validator('tax_rate')
    @classmethod
    def validate_tax_rate(cls, v):
        if v is not None and (v < 0 or v > 1):
            raise ValueError('税率必须在0-1之间')
        return v


class CorporateActionCreate(CorporateActionBase):
    """创建公司行动"""
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_quantity_fields(self):
        """Quantity-affecting actions must carry at least one usable field.

        Replay implementations interpret these with a single priority rule
        (issue #47: distribution_ratio > shares_received; split_ratio >
        new_shares); a record with neither field would silently change nothing.
        """
        if self.action_type in ("STOCK_DIVIDEND", "BONUS_ISSUE"):
            if not self.distribution_ratio and not self.shares_received:
                raise ValueError(
                    "股票股息/送股必须提供 distribution_ratio 或 shares_received"
                )
        elif self.action_type in ("STOCK_SPLIT", "REVERSE_SPLIT"):
            if not self.split_ratio and self.new_shares is None:
                raise ValueError("拆股/合股必须提供 split_ratio 或 new_shares")
        return self


class CorporateActionUpdate(BaseModel):
    """更新公司行动"""
    model_config = ConfigDict(extra="forbid")

    broker_account_id: Optional[int] = None
    symbol: Optional[str] = Field(None, max_length=20)
    name: Optional[str] = Field(None, max_length=100)
    market: Optional[str] = Field(None, max_length=20)
    action_type: Optional[ActionType] = None

    ex_date: Optional[date] = None
    record_date: Optional[date] = None
    payment_date: Optional[date] = None

    dividend_per_share: Optional[Decimal] = Field(None, ge=0)
    total_dividend: Optional[Decimal] = Field(None, ge=0)

    tax_withheld: Optional[Decimal] = Field(None, ge=0)
    tax_rate: Optional[Decimal] = Field(None, ge=0, le=1)
    net_dividend: Optional[Decimal] = Field(None, ge=0)

    shares_received: Optional[Decimal] = Field(None, ge=0)
    distribution_ratio: Optional[str] = Field(None, max_length=20)

    subscription_price: Optional[Decimal] = Field(None, ge=0)
    subscription_quantity: Optional[Decimal] = Field(None, ge=0)
    subscription_amount: Optional[Decimal] = Field(None, ge=0)

    split_ratio: Optional[str] = Field(None, max_length=20)
    new_shares: Optional[Decimal] = Field(None, ge=0)

    cost_basis_adjustment: Optional[Decimal] = None
    adjusted_quantity: Optional[Decimal] = Field(None, ge=0)
    adjusted_cost_per_share: Optional[Decimal] = Field(None, ge=0)

    currency: Optional[str] = Field(None, max_length=10)
    notes: Optional[str] = None


class CorporateActionResponse(CorporateActionBase):
    """公司行动响应"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    import_batch_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime


# 快捷创建模型
class CashDividendCreate(BaseModel):
    """现金股息快捷创建"""
    model_config = ConfigDict(extra="forbid")

    broker_account_id: Optional[int] = None
    symbol: str = Field(..., max_length=20)
    name: Optional[str] = Field(None, max_length=100)
    market: str = Field(..., max_length=20)
    ex_date: date
    dividend_per_share: Decimal = Field(..., gt=0, description="每股股息")
    total_dividend: Optional[Decimal] = Field(None, gt=0, description="总股息")
    tax_rate: Optional[Decimal] = Field(default=Decimal("0.1"), ge=0, le=1, description="税率，默认10%")
    currency: str = Field(default="CNY", max_length=10)
    notes: Optional[str] = None

    def to_corporate_action(self) -> CorporateActionCreate:
        """转换为标准公司行动创建模型"""
        tax_withheld = None
        net_dividend = None

        if self.total_dividend and self.tax_rate:
            tax_withheld = self.total_dividend * self.tax_rate
            net_dividend = self.total_dividend - tax_withheld

        return CorporateActionCreate(
            broker_account_id=self.broker_account_id,
            symbol=self.symbol,
            name=self.name,
            market=self.market,
            action_type="CASH_DIVIDEND",
            ex_date=self.ex_date,
            dividend_per_share=self.dividend_per_share,
            total_dividend=self.total_dividend,
            tax_rate=self.tax_rate,
            tax_withheld=tax_withheld,
            net_dividend=net_dividend,
            currency=self.currency,
            notes=self.notes
        )


class StockDividendCreate(BaseModel):
    """红股/股票股息快捷创建"""
    model_config = ConfigDict(extra="forbid")

    broker_account_id: Optional[int] = None
    symbol: str = Field(..., max_length=20)
    name: Optional[str] = Field(None, max_length=100)
    market: str = Field(..., max_length=20)
    ex_date: date
    shares_received: Decimal = Field(..., gt=0, description="获得的股数")
    distribution_ratio: str = Field(..., description="分配比例，如'10:3'表示每10股送3股")
    currency: str = Field(default="CNY", max_length=10)
    notes: Optional[str] = None

    def to_corporate_action(self) -> CorporateActionCreate:
        """转换为标准公司行动创建模型"""
        return CorporateActionCreate(
            broker_account_id=self.broker_account_id,
            symbol=self.symbol,
            name=self.name,
            market=self.market,
            action_type="STOCK_DIVIDEND",
            ex_date=self.ex_date,
            shares_received=self.shares_received,
            distribution_ratio=self.distribution_ratio,
            currency=self.currency,
            notes=self.notes
        )
