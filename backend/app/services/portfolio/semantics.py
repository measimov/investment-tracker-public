"""Single source of truth for interpreting corporate-action quantity fields.

Issue #47: position replay is implemented in three places (Holding
recalculation, FIFO realized P&L, TTWR curve) and they used to read different
fields off the same CorporateAction row, producing three different positions.
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
