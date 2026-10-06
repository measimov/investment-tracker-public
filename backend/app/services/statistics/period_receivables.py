"""期间股息应收调整：实收损益 + 期末待收 − 期初待收，不重复确认到账。"""

from datetime import date, timedelta
from decimal import Decimal

from .fx import DbExchangeRateLookup
from .receivables import load_receivable_context, receivable_entries


def summarize_receivable_change(opening, closing, opening_date, closing_date, rates, cash_pnl):
    """两端采用同一组可核算公告；任一端未知，整笔不做单边调整。"""
    opening_total = closing_total = Decimal(0)
    unresolved = set()
    missing_rates = set()
    for identity in opening.keys() | closing.keys():
        before, after = opening[identity], closing[identity]
        before_amount, after_amount = before["amount"], after["amount"]
        if before_amount is None or after_amount is None:
            unresolved.add(identity)
            continue
        if before_amount == after_amount == 0:
            continue
        currency = after["row"].currency
        amounts = []
        for amount, day in ((before_amount, opening_date), (after_amount, closing_date)):
            if not amount:
                amounts.append(Decimal(0))
                continue
            rate = rates.get_rate_strictly_on_or_before(currency, "CNY", day)
            if rate is None:
                missing_rates.add(currency)
                amounts.append(None)
            else:
                amounts.append(amount * rate)
        if any(amount is None for amount in amounts):
            unresolved.add(identity)
            continue
        opening_total += amounts[0]
        closing_total += amounts[1]
    change = closing_total - opening_total
    return {
        "cash_basis_pnl_cny": cash_pnl,
        "opening_receivable_cny": float(round(opening_total, 2)),
        "closing_receivable_cny": float(round(closing_total, 2)),
        "receivable_change_cny": float(round(change, 2)),
        "estimated_pnl_cny": float(round(Decimal(str(cash_pnl)) + change, 2))
        if cash_pnl is not None
        else None,
        "is_partial": bool(unresolved or missing_rates),
        "unresolved_count": len(unresolved),
        "missing_rate_currencies": sorted(missing_rates),
    }


def add_period_receivables(db, user_id, periods, actions, today, *, rate_lookup=None):
    """只给月/年追加双口径；旧损益、收益率、日口径和账本不改写。"""
    context = load_receivable_context(db, user_id, actions)
    rates = rate_lookup if rate_lookup is not None else DbExchangeRateLookup.from_db(db)
    closing = receivable_entries(db, user_id, context, today)
    openings = {}
    for key in ("mtd", "ytd"):
        period = periods[key]
        day = date.fromisoformat(period["start_date"]) - timedelta(days=1)
        if day not in openings:
            openings[day] = receivable_entries(db, user_id, context, day)
        period["receivable_pnl"] = summarize_receivable_change(
            openings[day], closing, day, today, rates, period["pnl_cny"]
        )
