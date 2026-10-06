from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from decimal import Decimal
from datetime import date, datetime
from typing import Optional, Literal

from ..core.markets import require_manual_market
from .read_models import read_model


# 公司行动类型枚举
ActionType = Literal[
    "CASH_DIVIDEND",  # 现金股息
    "STOCK_DIVIDEND",  # 股票股息/红股
    "RIGHTS_ISSUE",  # 配股
    "STOCK_SPLIT",  # 拆股
    "REVERSE_SPLIT",  # 合股
    "BONUS_ISSUE",  # 送股
    "SPIN_OFF",  # 拆分
    "MERGER",  # 合并
    "OPENING_POSITION",  # 期初建仓 / 转托管转入（账户级绝对数量，成本可选，#174）
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
    tax_withheld: Optional[Decimal] = Field(
        default=None, ge=0, description="预扣税金额；未知留空，明确免税填 0"
    )
    tax_rate: Optional[Decimal] = Field(
        None, ge=0, le=1, description="税率（0-1之间，如0.10表示10%）"
    )
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

    # 期初建仓（OPENING_POSITION）专用：建仓数量 / 单位成本(可选) / 总成本(可选)。
    # 两个成本都留空 = 成本未知（派生状态，不存布尔），持仓与已实现盈亏标记为估计
    cost_basis_adjustment: Optional[Decimal] = Field(
        None, ge=0, description="期初建仓总成本（可选）"
    )
    adjusted_quantity: Optional[Decimal] = Field(None, ge=0, description="期初建仓数量")
    adjusted_cost_per_share: Optional[Decimal] = Field(
        None, ge=0, description="期初建仓单位成本（可选）"
    )

    # 其他
    currency: str = Field(default="CNY", max_length=10, description="币种")
    notes: Optional[str] = Field(None, description="备注")

    @field_validator("tax_rate")
    @classmethod
    def validate_tax_rate(cls, v):
        if v is not None and (v < 0 or v > 1):
            raise ValueError("税率必须在0-1之间")
        return v


class CorporateActionCreate(CorporateActionBase):
    receipt_confirmed: bool = Field(False, exclude=True, description="已按凭证确认到账")
    amount_basis: Literal["GROSS_NET", "NET_ONLY"] = "GROSS_NET"

    @field_validator("distribution_ratio", "split_ratio", mode="before")
    @classmethod
    def _normalize_ratio(cls, value):
        return normalize_ratio(value)

    @model_validator(mode="after")
    def _normalize_symbol(self):
        # 手工入口共享归一化（大写 + 港股补零）；只挂在 Create 上，
        # Response 序列化不应"显示时修复"库里的历史形态
        from ..services.symbol_normalization import normalize_manual_symbol

        self.symbol = normalize_manual_symbol(self.symbol, self.market)
        return self

    """创建公司行动"""
    model_config = ConfigDict(extra="forbid")

    @field_validator("market")
    @classmethod
    def _manual_market(cls, value):
        # 只接受手工市场：任意字符串（如「HK」）会入库成新的身份键（#278）
        return require_manual_market(value)

    @model_validator(mode="after")
    def validate_quantity_fields(self):
        """Quantity-affecting actions must carry at least one usable field.

        Replay implementations interpret these with a single priority rule
        (issue #47: distribution_ratio > shares_received; split_ratio >
        new_shares); a record with neither field would silently change nothing.
        """
        validate_quantity_action_fields(self)
        if self.action_type == "CASH_DIVIDEND":
            if self.amount_basis == "NET_ONLY":
                if self.net_dividend is None or self.net_dividend <= 0:
                    raise ValueError("请填写实际净到账额（大于 0）")
                self.total_dividend = self.tax_withheld = self.tax_rate = None
                return self
            validate_cash_dividend_total(self.total_dividend)
            # 只给税率不给税额：按 总额×税率 推导预扣税（与 /cash-dividend 快捷接口同一算式）。
            # 否则税率只是一个不参与任何金额的标签——semantics.cash_dividend_amounts
            # 只读 tax_withheld，预扣税汇总会漏税、税后按全额进统计与对账（#220）。
            # net_dividend 未显式给时不存，由 cash_dividend_amounts 按 gross−tax 派生。
            if "tax_withheld" not in self.model_fields_set or self.tax_withheld is None:
                derived = derive_tax_withheld(self.total_dividend, self.tax_rate)
                self.tax_withheld = derived
        return self


def normalize_ratio(value: Optional[str]) -> Optional[str]:
    """比例输入归一：全角冒号（中文输入法常见）→ 半角，去空白。空串视为未填。"""
    if not isinstance(value, str):
        # 非字符串（JSON 里的 2、Excel 把「1:2」读成的 time）原样交给后面的 str 类型拒绝（422），
        # 这里调用 .replace 会抛 AttributeError 变成 500（PR #296 评审）
        return value
    normalized = value.replace("：", ":").replace(" ", "").strip()
    return normalized or None


def validate_quantity_action_fields(action) -> None:
    """数量类公司行动必须带**可解析**的数量字段（Create 与 PATCH 合并后的记录共用，#270）。

    重放按 semantics 的单一优先级解释这些字段（distribution_ratio > shares_received；
    split_ratio > new_shares；配股要认购数量与认购价同在）。此前只校验非空：「10/3」或
    缺认购价的配股能入库，四处重放一致地当作无事发生，持仓静默少算且没有任何信号。
    action 为任意带同名属性的对象（schema 实例或合并后的 ORM 行）。
    """
    from ..services.portfolio.semantics import parse_ratio

    def positive_ratio(ratio) -> bool:
        # parse_ratio 只要求第一项 > 0（存量重放口径不动）；写入时第二项也必须 > 0：
        # 「1:0」会把所有桶清零、「10:-3」按 0.7 减仓
        parsed = parse_ratio(ratio)
        return parsed is not None and parsed[1] > 0

    action_type = getattr(action, "action_type", None)
    ratio_hint = "比例格式应为「基数:数量」，如 10:3"
    if action_type in ("STOCK_DIVIDEND", "BONUS_ISSUE"):
        ratio = getattr(action, "distribution_ratio", None)
        if ratio and not positive_ratio(ratio):
            raise ValueError(f"送转比例「{ratio}」无法解析或不为正：{ratio_hint}")
        if not ratio and not getattr(action, "shares_received", None):
            raise ValueError("股票股息/送股必须提供 distribution_ratio 或 shares_received")
    elif action_type in ("STOCK_SPLIT", "REVERSE_SPLIT"):
        ratio = getattr(action, "split_ratio", None)
        if ratio and not positive_ratio(ratio):
            raise ValueError(
                f"拆合股比例「{ratio}」无法解析或不为正：比例格式应为「原股数:新股数」，如 1:2"
            )
        if not ratio and getattr(action, "new_shares", None) is None:
            raise ValueError("拆股/合股必须提供 split_ratio 或 new_shares")
    elif action_type == "RIGHTS_ISSUE":
        if not getattr(action, "subscription_quantity", None) or not getattr(
            action, "subscription_price", None
        ):
            raise ValueError(
                "配股必须同时提供认购数量 subscription_quantity 与认购价 subscription_price"
            )
    elif action_type == "OPENING_POSITION":
        validate_opening_position_fields(
            getattr(action, "adjusted_quantity", None),
            getattr(action, "adjusted_cost_per_share", None),
            getattr(action, "cost_basis_adjustment", None),
        )


def validate_cash_dividend_total(total_dividend) -> None:
    """现金股息必须有股息总额：统计/对账只读总额，只填每股的记录按 0 计、等于没有。"""
    if total_dividend is None or total_dividend <= 0:
        raise ValueError("现金股息必须提供股息总额 total_dividend（>0）")


def derive_tax_withheld(total_dividend, tax_rate) -> Optional[Decimal]:
    """预扣税 = 总额 × 税率；任一缺失返回 None（不推导）。

    `/cash-dividend` 快捷接口与通用创建/更新入口共用这一份算式。
    """
    if total_dividend is None or tax_rate is None:
        return None
    return Decimal(str(total_dividend)) * Decimal(str(tax_rate))


def validate_opening_position_fields(quantity, cost_per_share, total_cost) -> None:
    """期初建仓：数量必填且 >0；两个成本字段都给时必须一致（按数量容差 1 分）。"""
    if quantity is None or quantity <= 0:
        raise ValueError("期初建仓必须提供 adjusted_quantity（>0）")
    if cost_per_share is not None and total_cost is not None:
        tolerance = max(Decimal("0.01") * quantity, Decimal("0.01"))
        if abs(cost_per_share * quantity - total_cost) > tolerance:
            raise ValueError("期初建仓的 单位成本×数量 与 总成本 不一致")


class OpeningPositionCostUpdate(BaseModel):
    """只补录成本（导入建的期初仓也允许）：数量/日期/账户来自对账单，不在此改。"""

    model_config = ConfigDict(extra="forbid")

    adjusted_cost_per_share: Optional[Decimal] = Field(None, ge=0)
    cost_basis_adjustment: Optional[Decimal] = Field(None, ge=0)
    notes: Optional[str] = None


class CorporateActionUpdate(BaseModel):
    """更新公司行动"""

    model_config = ConfigDict(extra="forbid")
    receipt_confirmed: bool = Field(False, exclude=True)
    amount_basis: Optional[Literal["GROSS_NET", "NET_ONLY"]] = None

    @field_validator("market")
    @classmethod
    def _manual_market(cls, value):
        # 只接受手工市场：任意字符串（如「HK」）会入库成新的身份键（#278）
        return require_manual_market(value)

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

    cost_basis_adjustment: Optional[Decimal] = Field(None, ge=0)
    adjusted_quantity: Optional[Decimal] = Field(None, ge=0)
    adjusted_cost_per_share: Optional[Decimal] = Field(None, ge=0)

    currency: Optional[str] = Field(None, max_length=10)
    notes: Optional[str] = None

    @field_validator("distribution_ratio", "split_ratio", mode="before")
    @classmethod
    def _normalize_ratio(cls, value):
        return normalize_ratio(value)


class CorporateActionResponse(read_model(CorporateActionBase)):
    """公司行动响应"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    receipt_status: str = "RECEIVED"
    amount_basis: str = "LEGACY"
    dividend_suggestion_id: Optional[int] = None
    import_batch_id: Optional[int] = None
    # 展示字段：导入产物（带批次或被来源流水引用）不可编辑/删除。
    # 读端点与补录成本端点计算；创建/更新入口本就只对可变记录成功，缺省 False
    read_only: bool = False
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
    payment_date: Optional[date] = None
    receipt_confirmed: bool = False
    dividend_per_share: Decimal = Field(..., gt=0, description="每股股息")
    # 总额必填：统计与对账只读总额（与通用创建入口的 CASH_DIVIDEND 校验一致）
    total_dividend: Decimal = Field(..., gt=0, description="总股息")
    tax_rate: Optional[Decimal] = Field(
        default=None, ge=0, le=1, description="凭证确认税率；免税填 0，不预设税率"
    )
    currency: str = Field(default="CNY", max_length=10)
    notes: Optional[str] = None

    def to_corporate_action(self) -> CorporateActionCreate:
        """转换为标准公司行动创建模型"""
        tax_withheld = None
        net_dividend = None

        if self.tax_rate is not None:
            tax_withheld = derive_tax_withheld(self.total_dividend, self.tax_rate)
            net_dividend = self.total_dividend - tax_withheld

        return CorporateActionCreate(
            payment_date=self.payment_date,
            receipt_confirmed=self.receipt_confirmed,
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
            notes=self.notes,
        )
