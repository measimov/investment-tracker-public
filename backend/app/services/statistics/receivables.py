"""共用的股息应收余额与核对原因；不写现金、不改变实收或收益曲线。"""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

from ...config import settings
from ...core.timeutil import local_today
from ...models.corporate_action import CorporateAction
from ...models.corporate_action_suggestion import CorporateActionSuggestion
from ..dividend_forecast_service import forecast_details
from ..dividend_sync_service import match_existing_action
from ..dividend_settlement_service import build_settlement_context
from ..portfolio.semantics import dividend_cash_date, is_received_dividend
from .fx import to_cny_or_track_missing


@dataclass
class ReceivableContext:
    suggestions: list
    actions: list
    settlement: object


def load_receivable_context(db, user_id, actions=None):
    """一次装载公告、到账事实及凭证，供当前值或多个历史边界共用。"""
    rows = (
        db.query(CorporateActionSuggestion)
        .filter(
            CorporateActionSuggestion.user_id == user_id,
            CorporateActionSuggestion.action_type == "CASH_DIVIDEND",
        )
        .all()
    )
    if actions is None:
        actions = (
            db.query(CorporateAction)
            .filter(
                CorporateAction.user_id == user_id, CorporateAction.action_type == "CASH_DIVIDEND"
            )
            .all()
        )
    actions = [a for a in actions if a.user_id == user_id and a.action_type == "CASH_DIVIDEND"]
    return ReceivableContext(rows, actions, build_settlement_context(db, user_id, rows, actions))


def receivable_entries(db, user_id, context, today):
    """每份公告在指定日的已知余额与排除原因；未来/已结清余额为零。"""
    rows, actions = context.suggestions, context.actions
    details = forecast_details(
        db, user_id, rows, today=today, actions=actions, settlement_context=context.settlement
    )
    by_id = {a.id: a for a in actions}
    linked_ids = {action_id for detail in details.values() for action_id in detail["receipt_ids"]}
    unlinked = defaultdict(list)
    for action in actions:
        if (
            action.user_id == user_id
            and is_received_dividend(action, today)
            and action.id not in linked_ids
            and action.dividend_suggestion_id is None
        ):
            unlinked[(action.symbol, action.market)].append(
                SimpleNamespace(
                    id=action.id,
                    action_type="CASH_DIVIDEND",
                    ex_date=dividend_cash_date(action),
                    broker_account_id=action.broker_account_id,
                )
            )

    entries = {}
    for row in rows:
        detail = details[row.id]
        entry = {"row": row, "detail": detail, "amount": Decimal(0), "review": None, "reason": None}
        entries[row.id] = entry
        if row.ex_date > today or detail["receipt_state"] in ("INACTIVE", "RECEIVED"):
            continue
        if row.record_date_quantity is not None and row.record_date_quantity <= 0:
            continue
        amount = detail["remaining_estimated_gross"]
        ambiguous_cash = match_existing_action(
            {"ex_date": row.ex_date, "pay_date": row.pay_date},
            "CASH_DIVIDEND",
            None,
            unlinked[(row.symbol, row.market)],
            match_window_days=settings.dividend_sync_match_window_days,
            broker_account_id=row.broker_account_id,
        )
        ambiguous_cash = ambiguous_cash or detail.get("review_reason") in (
            "ambiguous_receipt",
            "receipt_period_unverified",
        )
        missing_entitlement = row.broker_account_id is None or row.record_date_quantity is None
        if (
            missing_entitlement
            or not row.currency
            or amount is None
            or amount < 0
            or ambiguous_cash
        ):
            entry["amount"] = None
            receipts = [by_id[aid] for aid in detail["receipt_ids"]]
            if receipts:
                entry["review"] = "received"
                if row.market == "B股" and row.source == "tushare-dividend":
                    entry["reason"] = "payout_currency_unverified"
                elif any(a.currency != row.currency for a in receipts):
                    entry["reason"] = "currency_mismatch"
                elif any(a.amount_basis != "GROSS_NET" for a in receipts):
                    entry["reason"] = "net_amount_only"
                else:
                    entry["reason"] = "other"
            elif ambiguous_cash:
                entry["review"] = "possible_receipt"
            elif missing_entitlement:
                entry["review"] = "entitlement"
            else:
                entry["review"] = "amount"
        else:
            entry["amount"] = Decimal(str(amount))
            if amount == 0:
                entry["review"] = "zero_remaining"
    return entries


def build_receivable_return(db, user_id, actual_return, actions, *, today=None):
    """税前待收补充展示；疑似/已匹配实收不能再整笔叠加为预计收入。"""
    today = today or local_today()
    context = load_receivable_context(db, user_id, actions)
    entries = receivable_entries(db, user_id, context, today)
    by_currency = defaultdict(lambda: Decimal(0))
    included_count = overdue_count = pending_overdue_count = 0
    review_counts = dict.fromkeys(
        ("received", "possible_receipt", "entitlement", "amount", "zero_remaining"), 0
    )
    received_reasons = dict.fromkeys(
        ("net_amount_only", "currency_mismatch", "payout_currency_unverified", "other"), 0
    )
    for entry in entries.values():
        detail, row = entry["detail"], entry["row"]
        if row.ex_date > today or detail["receipt_state"] in ("INACTIVE", "RECEIVED"):
            continue
        if row.record_date_quantity is not None and row.record_date_quantity <= 0:
            continue
        overdue_count += int(detail["overdue"])
        review = entry["review"]
        if review:
            review_counts[review] += 1
            if review == "received":
                received_reasons[entry["reason"]] += 1
            continue
        if entry["amount"] > 0:
            included_count += 1
            pending_overdue_count += int(detail["overdue"])
            by_currency[row.currency] += entry["amount"]
    unresolved_count = sum(n for key, n in review_counts.items() if key != "zero_remaining")

    missing_rates = set()
    known_pending_cny = sum(
        (
            to_cny_or_track_missing(db, amount, currency, missing_rates)
            for currency, amount in sorted(by_currency.items())
        ),
        Decimal(0),
    )
    actual = Decimal(str(actual_return)) if actual_return is not None else None
    return {
        "as_of": today.isoformat(),
        "base_currency": "CNY",
        "fx_basis": "latest_rate",
        "cash_basis_return_cny": float(actual) if actual is not None else None,
        "known_pending_gross_cny": float(known_pending_cny),
        "known_pending_gross_by_currency": {c: float(v) for c, v in sorted(by_currency.items())},
        "estimated_return_cny": float(actual + known_pending_cny) if actual is not None else None,
        "included_count": included_count,
        "unresolved_count": unresolved_count,
        "overdue_count": overdue_count,  # 兼容旧客户端；新界面只展示正余额中的超期项
        "pending_overdue_count": pending_overdue_count,
        "review_counts": review_counts,
        "received_review_reasons": received_reasons,
        "missing_rate_currencies": sorted(missing_rates),
        "is_partial": bool(unresolved_count or missing_rates),
    }
