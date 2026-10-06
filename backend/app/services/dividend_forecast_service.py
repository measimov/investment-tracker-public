"""Read-only forecast views; announcements never generate cash facts."""

from collections import defaultdict
from decimal import Decimal

from ..core.timeutil import local_today
from ..models.corporate_action import CorporateAction
from ..models.corporate_action_suggestion import CorporateActionSuggestion
from .portfolio.semantics import cash_dividend_amounts, is_received_dividend
from .dividend_sync_service import superseded_suggestion_date
from .dividend_settlement_service import build_settlement_context


def forecast_details(
    db, user_id, suggestions, *, today=None, actions=None, settlement_context=None
):
    today = today or local_today()
    if actions is None:
        actions = (
            db.query(CorporateAction)
            .filter(
                CorporateAction.user_id == user_id, CorporateAction.action_type == "CASH_DIVIDEND"
            )
            .all()
        )
    actions = [a for a in actions if a.user_id == user_id and a.action_type == "CASH_DIVIDEND"]
    settlement_context = settlement_context or build_settlement_context(
        db, user_id, suggestions, actions
    )
    if settlement_context.user_id != user_id:
        raise ValueError("股息核对上下文不属于当前用户")
    result = {}
    for suggestion in suggestions:
        if suggestion.user_id != user_id or suggestion.action_type != "CASH_DIVIDEND":
            continue
        evidence = settlement_context.by_suggestion[suggestion.id]
        receipts = [a for a in evidence.receipts if is_received_dividend(a, today)]
        paid = defaultdict(lambda: Decimal(0))
        for action in receipts:
            paid[action.currency] += cash_dividend_amounts(action)[2]
        currency_unverified = suggestion.market == "B股" and suggestion.source == "tushare-dividend"
        inactive = (
            suggestion.status == "IGNORED"
            or bool(superseded_suggestion_date(suggestion))
            or bool((suggestion.match_detail or {}).get("forecast_withdrawn"))
        )
        # The last payment, not today's completion flag or first receipt, ends
        # the historical receivable. This also handles payments split by month.
        complete = bool(receipts and evidence.completion_date and evidence.completion_date <= today)
        if inactive:
            state = "INACTIVE"
        elif complete:
            state = "RECEIVED"
        elif receipts:
            state = "PARTIAL"
        elif suggestion.ex_date > today:
            state = "ANNOUNCED"
        elif (
            currency_unverified
            or suggestion.broker_account_id is None
            or suggestion.record_date_quantity is None
        ):
            state = "NEEDS_REVIEW"
        else:
            state = "PENDING"
        remaining = None
        if state == "RECEIVED":
            remaining = Decimal(0)
        elif state in ("ANNOUNCED", "PENDING"):
            remaining = suggestion.estimated_total_dividend
        elif (
            state == "PARTIAL"
            and all(
                a.amount_basis == "GROSS_NET" and a.currency == suggestion.currency
                for a in receipts
            )
            and suggestion.estimated_total_dividend is not None
        ):
            remaining = max(
                Decimal(0),
                suggestion.estimated_total_dividend
                - sum((cash_dividend_amounts(a)[0] for a in receipts), Decimal(0)),
            )
        elif (
            state == "PARTIAL"
            and receipts
            and all(a.id in evidence.verified_gross_by_receipt for a in receipts)
            and suggestion.estimated_total_dividend is not None
        ):
            remaining = max(
                Decimal(0),
                suggestion.estimated_total_dividend
                - sum((evidence.verified_gross_by_receipt[a.id] for a in receipts), Decimal(0)),
            )
        if currency_unverified:
            remaining = None
        result[suggestion.id] = {
            "receipt_state": state,
            "receipt_complete": complete,
            "completion_source": evidence.completion_source if complete else None,
            "completion_date": evidence.completion_date,
            "review_reason": evidence.review_reason if not complete else None,
            "received_by_currency": dict(paid),
            "receipt_ids": [a.id for a in receipts],
            "remaining_estimated_gross": remaining,
            "overdue": bool(
                not inactive
                and not complete
                and suggestion.pay_date
                and suggestion.pay_date < today
            ),
        }
    return result


def forecast_summary(db, user_id, *, today=None):
    rows = (
        db.query(CorporateActionSuggestion)
        .filter(
            CorporateActionSuggestion.user_id == user_id,
            CorporateActionSuggestion.action_type == "CASH_DIVIDEND",
        )
        .all()
    )
    details = forecast_details(db, user_id, rows, today=today)
    pending = defaultdict(lambda: Decimal(0))
    announced = defaultdict(lambda: Decimal(0))
    unknown = overdue = count = 0
    for row in rows:
        detail = details[row.id]
        state = detail["receipt_state"]
        if state in ("RECEIVED", "INACTIVE"):
            continue
        count += 1
        overdue += int(detail["overdue"])
        amount = detail["remaining_estimated_gross"]
        if amount is None:
            unknown += 1
        elif state == "ANNOUNCED":
            announced[row.currency] += amount
        else:
            pending[row.currency] += amount
    return {
        "pending_count": count,
        "overdue_count": overdue,
        "unknown_amount_count": unknown,
        "pending_gross_by_currency": dict(pending),
        "announced_gross_by_currency": dict(announced),
    }
