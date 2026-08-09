from pydantic import BaseModel, Field, ConfigDict, field_validator
from decimal import Decimal
from datetime import date, datetime
from typing import Optional


# 交易的**业务必填**字段：更新时显式传 null 一律拒绝。
#
# 这份清单是这一族缺陷的总闸门。此前是发现一个补一个（先 symbol/market/
# currency 的空串，再它们的显式 null，再 transaction_type/quantity/price/
# fee/transaction_date 的显式 null），每轮都漏。根因相同：TransactionUpdate
# 的字段全是 Optional（表达"可以不传"），而 pydantic 无法区分"没传"与
# "显式传 null"——后者会进 update_data → setattr → commit，直到用
# TransactionResponse 序列化响应时才失败：用户看到 500，脏数据已经落库。
#
# 注意判据是**业务**必填而非列 nullable：fee 与 currency 的列是 nullable
# （历史遗留），但它们在业务上恒有值（fee 默认 0、currency 默认 CNY），
# 置 null 同样会让响应序列化失败。
# tests/test_transaction_update_blank_fields.py 有一条守护用例，按 ORM 模型的
# 非空列反查这份清单，新增字段漏登记时会直接报红。
TRANSACTION_REQUIRED_FIELDS = (
    "symbol",
    "market",
    "transaction_type",
    "quantity",
    "price",
    "fee",
    "transaction_date",
    "currency",
)

# 真正可空的字段：null 是合法的"清空"语义，不得被上面的规则误伤。
TRANSACTION_NULLABLE_FIELDS = ("broker_account_id", "name", "notes")


def _reject_explicit_null(value, info):
    """业务必填字段不接受显式 null（字段未传时 validator 不会触发）。"""
    if value is None:
        raise ValueError(f"{info.field_name} 不能为空")
    return value


def _require_non_blank(value, info):
    """必填字符串不得为 null / 空 / 纯空白；返回 strip 后的值。

    三层都需要，缺一不可：
    - 只有 max_length 时 symbol=""/market=""/currency="" 全部能通过；
    - min_length=1 拦不住 " "（标准导入的 normalize_symbol_value 还会把纯空白
      转成 ""），所以要 strip 后判空；
    - **显式传 null 也必须拒**。这里曾写 `if value is None: return value`，
      本意是放行"未传该字段"的部分更新——但那是多余的：pydantic 的 validator
      对未出现在请求里的字段根本不会触发（exclude_unset 也不会带上它）。
      结果这行反而放行了显式 `{"currency": null}`，它会进 update_data →
      setattr → commit，直到响应用 TransactionResponse 序列化时才失败：
      用户看到 500，而脏数据已经落库。

    创建与更新两条路径必须共用同一份判据（TransactionUpdate 不继承
    TransactionBase，只能显式复用）。
    """
    if value is None or not str(value).strip():
        raise ValueError(f"{info.field_name} 不能为空")
    return str(value).strip()


class TransactionBase(BaseModel):
    broker_account_id: Optional[int] = None
    # min_length=1 不够：CSV 里的纯空白 symbol 经 normalize_symbol_value 会变成
    # ""，而 " " 这样的值也不该算有效标的。下面的 validator 按 strip 后判空。
    symbol: str = Field(..., min_length=1, max_length=20, description="Stock/Asset symbol")
    name: Optional[str] = Field(None, max_length=100, description="Asset name")
    market: str = Field(..., min_length=1, max_length=20, description="Market (A股, 港股, 美股, 加密货币, etc.)")
    transaction_type: str = Field(..., pattern="^(BUY|SELL)$", description="Transaction type: BUY or SELL")
    quantity: Decimal = Field(..., gt=0, description="Quantity")
    price: Decimal = Field(..., gt=0, description="Price per unit")
    fee: Decimal = Field(default=Decimal("0"), ge=0, description="Transaction fee")
    transaction_date: date = Field(..., description="Transaction date")
    currency: str = Field(default="CNY", min_length=1, max_length=10, description="Currency")
    notes: Optional[str] = Field(None, description="Notes")

    _reject_blank = field_validator("symbol", "market", "currency")(_require_non_blank)


class TransactionCreate(TransactionBase):
    model_config = ConfigDict(extra="forbid")


class TransactionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    broker_account_id: Optional[int] = None
    symbol: Optional[str] = Field(None, min_length=1, max_length=20)
    name: Optional[str] = Field(None, max_length=100)
    market: Optional[str] = Field(None, min_length=1, max_length=20)
    transaction_type: Optional[str] = Field(None, pattern="^(BUY|SELL)$")
    quantity: Optional[Decimal] = Field(None, gt=0)
    price: Optional[Decimal] = Field(None, gt=0)
    fee: Optional[Decimal] = Field(None, ge=0)
    transaction_date: Optional[date] = None
    currency: Optional[str] = Field(None, min_length=1, max_length=10)
    notes: Optional[str] = None

    # 与创建路径共用同一判据：不复用的话，PUT 的空白字段会先落库，
    # 直到响应序列化才报错（500 + 脏数据）
    _reject_blank = field_validator("symbol", "market", "currency")(_require_non_blank)
    # 其余业务必填字段：显式 null 同样拒绝（字符串三项已由上面的 validator 覆盖）
    _reject_null = field_validator(
        *(f for f in TRANSACTION_REQUIRED_FIELDS if f not in ("symbol", "market", "currency"))
    )(_reject_explicit_null)


class TransferCreate(BaseModel):
    """转仓：把持仓从一个账户桶移到另一个（成本基础跟随，不产生盈亏）。

    创建 TRANSFER_OUT / TRANSFER_IN 互指交易对；账户为 null 表示"未指定账户"桶。
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., max_length=20)
    market: str = Field(..., max_length=20)
    quantity: Decimal = Field(..., gt=0)
    from_broker_account_id: Optional[int] = Field(None, description="转出账户；null=未指定账户桶")
    to_broker_account_id: Optional[int] = Field(None, description="转入账户；null=未指定账户桶")
    transfer_date: date = Field(...)
    notes: Optional[str] = None


class TransactionResponse(TransactionBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    # 放开 BUY|SELL 校验：转仓对（TRANSFER_OUT/TRANSFER_IN）也要能出现在列表里。
    transaction_type: str = Field(..., description="BUY / SELL / TRANSFER_OUT / TRANSFER_IN")
    linked_transaction_id: Optional[int] = Field(
        None, description="转仓对的另一腿 id；普通交易为 null"
    )
    import_batch_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime
