"""Single source of truth for interpreting corporate-action quantity fields.

Issue #47: position replay is implemented in four places (Holding
recalculation, FIFO realized P&L, TTWR curve, and the brokers' account-level
position pre-check) and they used to read different fields off the same
CorporateAction row, producing different positions.
Every replay must derive its quantity change through these helpers so the
field-priority rules are defined exactly once:

- Stock dividend / bonus issue: ``distribution_ratio`` ("base:bonus") takes
  priority; ``shares_received`` (absolute share count) is the fallback.
- Split / reverse split: ``split_ratio`` ("old:new") takes priority;
  ``new_shares`` (absolute post-split total) is the fallback.

Both helpers return a multiplicative factor so callers can scale a plain
quantity, an average-cost holding, or every lot in a FIFO queue identically.
"""

from decimal import Decimal, DecimalException
from typing import Optional, Tuple


def parse_ratio(value: Optional[str]) -> Optional[Tuple[Decimal, Decimal]]:
    """Parse an "a:b" ratio string into positive decimals, else None."""
    if not value:
        return None
    try:
        left, right = value.split(":")
        first = Decimal(left)
        second = Decimal(right)
        if first <= 0:
            return None
        return first, second
    except (ValueError, DecimalException):
        return None


def bonus_share_factor(action, current_quantity: Decimal) -> Optional[Decimal]:
    """Quantity multiplier for STOCK_DIVIDEND / BONUS_ISSUE, or None if no-op.

    Priority: distribution_ratio ("base:bonus" -> 1 + bonus/base), falling back
    to shares_received relative to the caller's current quantity.
    """
    parts = parse_ratio(getattr(action, "distribution_ratio", None))
    if parts:
        base, bonus = parts
        return Decimal("1") + bonus / base
    shares_received = getattr(action, "shares_received", None)
    if shares_received and current_quantity > 0:
        return (current_quantity + Decimal(str(shares_received))) / current_quantity
    return None


def split_share_factor(action, current_quantity: Decimal) -> Optional[Decimal]:
    """Quantity multiplier for STOCK_SPLIT / REVERSE_SPLIT, or None if no-op.

    Priority: split_ratio ("old:new" -> new/old), falling back to new_shares
    (absolute post-split total) relative to the caller's current quantity.
    """
    parts = parse_ratio(getattr(action, "split_ratio", None))
    if parts:
        old_shares, new_shares = parts
        return new_shares / old_shares
    new_total = getattr(action, "new_shares", None)
    if new_total is not None and current_quantity > 0:
        return Decimal(str(new_total)) / current_quantity
    return None


def cash_dividend_amounts(action) -> Tuple[Decimal, Decimal, Decimal]:
    """现金股息金额归一：返回 (gross, tax, net)。

    显式 net_dividend=0 是有效值（如全额预扣），必须以 `is not None` 区分
    0 与 NULL——用 `or` 会把合法的 0 当成缺失而回退成 gross−tax，凭空多算一笔。

    这条判据此前在四处各写一遍（统计汇总、TTWR 曲线、对账、公司行动页），
    专门为防漂移而设的 helper 只覆盖了其中两处，且它落在编排层，纯内核的
    curve.py 无法 import。放这里与 bonus_share_factor 同构：纯函数、鸭子类型、
    零依赖，四处共用同一份。
    """
    gross = Decimal(str(getattr(action, "total_dividend", None) or 0))
    tax = Decimal(str(getattr(action, "tax_withheld", None) or 0))
    net_raw = getattr(action, "net_dividend", None)
    net = Decimal(str(net_raw)) if net_raw is not None else gross - tax
    return gross, tax, net


# 会改变持仓数量的公司行动（现金股息等不在其列）
QUANTITY_ACTION_TYPES = (
    "STOCK_DIVIDEND",
    "BONUS_ISSUE",
    "RIGHTS_ISSUE",
    "STOCK_SPLIT",
    "REVERSE_SPLIT",
)


def action_has_ratio(action) -> bool:
    """行动是否以「每股比例」表达。

    比例类按每股生效，重放时作用于**所有**持仓桶；绝对数量类（shares_received
    / new_shares / 配股认购数）只能落到某一个桶。调用方据此决定账户归属与
    可见范围。
    """
    action_type = getattr(action, "action_type", None)
    if action_type in ("STOCK_DIVIDEND", "BONUS_ISSUE"):
        return parse_ratio(getattr(action, "distribution_ratio", None)) is not None
    if action_type in ("STOCK_SPLIT", "REVERSE_SPLIT"):
        return parse_ratio(getattr(action, "split_ratio", None)) is not None
    return False


def apply_action_quantity(action, quantity: Decimal) -> Decimal:
    """单个持仓桶在一次公司行动之后的数量；无法解释的行动原样返回。

    与 _replay_events / _replay_account_fifo / TTWR 曲线共用同一组因子，差别
    只在这里针对「一个桶」而非「桶集合」——目标桶的选择由调用方负责。
    """
    action_type = getattr(action, "action_type", None)
    if action_type in ("STOCK_DIVIDEND", "BONUS_ISSUE"):
        factor = bonus_share_factor(action, quantity)
        return quantity * factor if factor is not None else quantity
    if action_type in ("STOCK_SPLIT", "REVERSE_SPLIT"):
        factor = split_share_factor(action, quantity)
        return quantity * factor if factor is not None else quantity
    if action_type == "RIGHTS_ISSUE":
        # 双字段守卫：与 holding_service / fifo / curve 三处一致，缺认购价的
        # 配股在任何重放里都不计入数量。
        sub_qty = getattr(action, "subscription_quantity", None)
        sub_price = getattr(action, "subscription_price", None)
        if sub_qty and sub_price:
            return quantity + Decimal(str(sub_qty))
    return quantity
