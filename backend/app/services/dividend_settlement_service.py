"""Read-only settlement evidence, shared by current and historical forecasts.

Cash facts and source rows are never changed here. A date-window match only finds
candidates: completion additionally requires an unambiguous account/security,
broker cash evidence and the full announced amount to close within one cent.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from ..config import settings
from ..models.broker_fund_flow import BrokerFundFlow
from ..models.corporate_action_suggestion import CorporateActionSuggestion
from ..models.ibkr_activity_flow import IbkrActivityFlow
from .dividend_sync_service import MATCH_WINDOW_BEFORE_DAYS, superseded_suggestion_date
from .portfolio.semantics import cash_dividend_amounts, dividend_cash_date, is_received_dividend

_CENT = Decimal("0.01")
_SOURCE_PRECISION = Decimal("0.00000001")


@dataclass(frozen=True)
class CashProof:
    # Values are (amount, gross_is_known). Net is not promoted to gross merely
    # because a broker source exists; unknown withholding remains unknown.
    amounts: dict[str, tuple[Decimal, bool]]


@dataclass
class SettlementEvidence:
    receipts: list = field(default_factory=list)
    completion_date: date | None = None
    completion_source: str | None = None
    review_reason: str | None = None
    verified_gross_by_receipt: dict[int, Decimal] = field(default_factory=dict)


@dataclass
class SettlementContext:
    user_id: int
    by_suggestion: dict[int, SettlementEvidence]


def _same_amount(left, right):
    return left is not None and right is not None and abs(left - right) <= _SOURCE_PRECISION


def _active(row):
    return (
        row.status != "IGNORED"
        and not superseded_suggestion_date(row)
        and not (row.match_detail or {}).get("forecast_withdrawn")
    )


def _identity(row, action):
    return (
        row.user_id == action.user_id
        and row.broker_account_id is not None
        and row.broker_account_id == action.broker_account_id
        and (row.symbol, row.market) == (action.symbol, action.market)
    )


def _in_window(row, action):
    paid = dividend_cash_date(action)
    return bool(
        paid
        and row.ex_date - timedelta(days=MATCH_WINDOW_BEFORE_DAYS)
        <= paid
        <= (row.pay_date or row.ex_date) + timedelta(days=settings.dividend_sync_match_window_days)
    )


def _broker_proof(action, rows):
    if not rows or any(
        r.broker not in ("招商证券", "东方财富证券")
        or r.business_name not in ("股息入账", "产品红利发放", "红利入账")
        or r.user_id != action.user_id
        or r.broker_account_id != action.broker_account_id
        or r.security_code != action.symbol
        or r.trade_date != dividend_cash_date(action)
        or r.currency != action.currency
        or r.skip_reason
        or r.amount is None
        or r.amount <= 0
        for r in rows
    ):
        return None
    net = sum((r.amount for r in rows), Decimal(0))
    if not _same_amount(net, cash_dividend_amounts(action)[2]):
        return None
    amount, gross_known = net, False
    if action.amount_basis == "GROSS_NET":
        # This basis is an explicitly verified receipt, unlike LEGACY or an
        # announcement estimate. Preserve its confirmed withholding and require
        # its cash arithmetic to agree with the independent broker net amount.
        if (
            action.total_dividend is None
            or action.tax_withheld is None
            or action.tax_withheld < 0
            or not _same_amount(action.total_dividend - action.tax_withheld, net)
        ):
            return None
        amount, gross_known = action.total_dividend, True
    amounts = {action.currency: (amount, gross_known)}
    # A source's actual settlement rate can support comparison in HKD. This
    # deliberately does not use today's FX or presume a withholding percentage.
    if (
        action.market == "港股"
        and action.currency == "CNY"
        and all(
            r.statement_type == "hk_connect" and r.settlement_rate and r.settlement_rate > 0
            for r in rows
        )
    ):
        rates = {r.settlement_rate for r in rows}
        if len(rates) == 1:
            amounts["HKD"] = (amount / next(iter(rates)), gross_known)
        elif not gross_known or amount == net:
            amounts["HKD"] = (
                sum((r.amount / r.settlement_rate for r in rows), Decimal(0)),
                gross_known,
            )
    return CashProof(amounts)


def _ibkr_proof(action, rows):
    if not rows or any(
        r.user_id != action.user_id
        or r.broker_account_id != action.broker_account_id
        or (r.symbol, r.market) != (action.symbol, action.market)
        or r.trade_date != dividend_cash_date(action)
        or r.base_currency != action.currency
        or r.skip_reason
        or r.gross_amount is None
        or r.activity_type not in ("股息", "外国预扣税")
        or (r.activity_type == "股息" and r.gross_amount <= 0)
        or (r.activity_type == "外国预扣税" and r.gross_amount >= 0)
        or (r.net_amount is not None and not _same_amount(r.net_amount, r.gross_amount))
        or r.commission
        for r in rows
    ):
        return None
    gross = sum((r.gross_amount for r in rows if r.activity_type == "股息"), Decimal(0))
    net = sum((r.gross_amount for r in rows), Decimal(0))
    if gross <= 0 or not _same_amount(net, cash_dividend_amounts(action)[2]):
        return None
    if action.amount_basis == "GROSS_NET" and not (
        _same_amount(action.total_dividend, gross)
        and _same_amount(action.tax_withheld, gross - net)
    ):
        return None
    # Transaction History may present HK stocks in base USD. A description of
    # the HKD per-share dividend alone does not prove original currency cash or
    # the quantity actually paid, so it cannot prove a full HKD settlement.
    return CashProof({action.currency: (gross, True)})


def build_settlement_context(db, user_id, suggestions, actions):
    """Load sources once; reuse the result for any number of as-of boundaries.

    All user announcements participate in ambiguity checks even if the caller
    requested only one page. Future receipts remain in context, but consumers
    must filter them by the queried date before summing cash or remaining gross.
    """
    all_rows = {
        row.id: row
        for row in db.query(CorporateActionSuggestion)
        .filter(
            CorporateActionSuggestion.user_id == user_id,
            CorporateActionSuggestion.action_type == "CASH_DIVIDEND",
        )
        .all()
    }
    all_rows.update(
        {s.id: s for s in suggestions if s.user_id == user_id and s.action_type == "CASH_DIVIDEND"}
    )
    actions = [
        a
        for a in actions
        if a.user_id == user_id
        and a.broker_account_id is not None
        and is_received_dividend(a, date.max)
    ]
    by_id = {a.id: a for a in actions}
    source_rows = {}
    for model in (BrokerFundFlow, IbkrActivityFlow):
        grouped = defaultdict(list)
        if by_id:
            for source in (
                db.query(model)
                .filter(model.user_id == user_id, model.corporate_action_id.in_(by_id))
                .all()
            ):
                grouped[source.corporate_action_id].append(source)
        source_rows[model] = grouped
    proofs = {}
    for action in actions:
        broker = source_rows[BrokerFundFlow][action.id]
        ibkr = source_rows[IbkrActivityFlow][action.id]
        # Two independent source families claiming the same receipt need review.
        if broker and ibkr:
            continue
        proof = _broker_proof(action, broker) if broker else _ibkr_proof(action, ibkr)
        if proof:
            proofs[action.id] = proof

    explicit = defaultdict(dict)
    for action in actions:
        if action.dividend_suggestion_id in all_rows:
            row = all_rows[action.dividend_suggestion_id]
            if _identity(row, action):
                explicit[row.id][action.id] = action
    for row in all_rows.values():
        if (row.match_detail or {}).get("receipt_links_reviewed"):
            continue
        for aid in (row.created_corporate_action_id, row.matched_corporate_action_id):
            action = by_id.get(aid)
            if (
                action
                and _identity(row, action)
                and action.dividend_suggestion_id in (None, row.id)
            ):
                explicit[row.id][aid] = action

    candidates = defaultdict(dict)
    ambiguous = set()
    for action in actions:
        if action.dividend_suggestion_id is not None:
            owners = (
                [all_rows[action.dividend_suggestion_id]]
                if action.dividend_suggestion_id in all_rows
                else []
            )
        else:
            owners = [
                row
                for row in all_rows.values()
                if _active(row) and _identity(row, action) and _in_window(row, action)
            ]
        if len(owners) > 1:
            ambiguous.update(row.id for row in owners)
        elif owners:
            row = owners[0]
            if _identity(row, action) and _in_window(row, action) and action.id in proofs:
                candidates[row.id][action.id] = action

    result = {}
    for row in all_rows.values():
        reviewed = (row.match_detail or {}).get("receipt_links_reviewed")
        linked = dict(explicit[row.id])
        if not reviewed and not row.receipt_complete and row.id not in ambiguous:
            linked.update(candidates[row.id])
        evidence = SettlementEvidence(
            receipts=sorted(linked.values(), key=lambda a: (dividend_cash_date(a), a.id))
        )
        result[row.id] = evidence
        if row.receipt_complete and evidence.receipts:
            evidence.completion_source = "manual"
            evidence.completion_date = max(dividend_cash_date(a) for a in evidence.receipts)
            continue
        if reviewed:
            evidence.review_reason = "manual_review_pending"
            continue
        if row.market == "B股" and row.source == "tushare-dividend":
            evidence.review_reason = "currency_unverified"
            continue
        if row.id in ambiguous:
            evidence.review_reason = "ambiguous_receipt"
            continue
        if not evidence.receipts:
            continue
        if (
            row.broker_account_id is None
            or row.record_date_quantity is None
            or row.record_date_quantity <= 0
        ):
            evidence.review_reason = "entitlement_unverified"
            continue
        if row.estimated_total_dividend is None or row.estimated_total_dividend <= 0:
            evidence.review_reason = "announced_amount_unknown"
            continue
        comparison = []
        for action in evidence.receipts:
            if action.dividend_suggestion_id != row.id and action.id not in candidates[row.id]:
                # Old matched_action_id is only a date-window hint, not a
                # permanent identity override after announcement revisions.
                evidence.review_reason = "receipt_period_unverified"
                break
            proof = proofs.get(action.id)
            if proof is None:
                evidence.review_reason = "statement_evidence_missing"
                break
            if row.currency not in proof.amounts:
                evidence.review_reason = "receipt_currency_mismatch"
                break
            amount, gross_known = proof.amounts[row.currency]
            comparison.append(amount)
            if gross_known:
                evidence.verified_gross_by_receipt[action.id] = amount
        else:
            total = sum(comparison, Decimal(0))
            if abs(total - row.estimated_total_dividend) < _CENT:
                evidence.completion_source = "statement"
                evidence.completion_date = max(dividend_cash_date(a) for a in evidence.receipts)
            else:
                evidence.review_reason = "receipt_amount_unresolved"
    return SettlementContext(user_id=user_id, by_suggestion=result)
