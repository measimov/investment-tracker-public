"""Broker receipt matching shared by preview and commit; hashes stay broker-owned."""

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from ..config import settings
from ..models.broker_fund_flow import BrokerFundFlow
from ..models.corporate_action import CorporateAction
from ..models.corporate_action_suggestion import CorporateActionSuggestion
from ..models.ibkr_activity_flow import IbkrActivityFlow
from .broker_import_common import append_note
from .dividend_sync_service import match_existing_action, superseded_suggestion_date
from .dividend_tax_service import validate_existing_tax_allocations
from .holding_service import lock_record, lock_security_timeline
from .portfolio.semantics import cash_dividend_amounts, dividend_cash_date


@dataclass(frozen=True)
class DividendReceipt:
    row_hash: str
    symbol: str
    market: str
    payment_date: date
    currency: str
    amount: Decimal
    gross: Optional[Decimal] = None
    tax: Optional[Decimal] = None


@dataclass
class DividendReceiptPlan:
    db: Session
    user_id: int
    account_id: int
    matches: dict = field(default_factory=dict)
    receipts: dict = field(default_factory=dict)
    applied: dict = field(default_factory=dict)
    unmatched_warnings: list = field(default_factory=list)

    def warnings(self):
        return self.unmatched_warnings + [
            f"{self.receipts[key].symbol} {self.receipts[key].payment_date} 到账"
            + (
                f"将复用实收股息 #{action.id}，保留公告 #{action.dividend_suggestion_id} 的关联"
                if action and action.dividend_suggestion_id is not None
                else f"将合并至已接受的股息 #{action.id}"
                if action
                else f"将关联预计股息 #{suggestion.id}"
            )
            + "；以券商现金凭证记实收，剩余及全部到账状态须核对"
            for key, (action, suggestion) in self.matches.items()
        ]

    def group_key(self, key):
        action, suggestion = self.matches[key]
        receipt = self.receipts[key]
        return (
            action.id if action else None,
            suggestion.id if suggestion else None,
            receipt.payment_date,
            receipt.currency,
            receipt.row_hash if receipt.gross is not None else None,
        )

    def apply(self, row_hash: str, *, batch_id: Optional[int], source_note: str):
        if row_hash not in self.matches:
            return None
        action, suggestion = self.matches[row_hash]
        if (
            action is not None
            and action.dividend_suggestion_id is not None
            and (suggestion is None or action.dividend_suggestion_id != suggestion.id)
        ):
            raise ValueError("到账记录已有原公告关联，不能自动改挂；请先显式核对关联")
        key = self.group_key(row_hash)
        if key in self.applied:
            return self.applied[key]
        receipts = [
            r for h, r in self.receipts.items() if h in self.matches and self.group_key(h) == key
        ]
        receipt = receipts[0]
        net = sum((r.amount for r in receipts), Decimal(0))
        if action is None:
            action = CorporateAction(
                user_id=self.user_id,
                broker_account_id=self.account_id,
                symbol=receipt.symbol,
                market=receipt.market,
                name=suggestion.name,
                action_type="CASH_DIVIDEND",
                ex_date=suggestion.ex_date,
            )
            self.db.add(action)
        elif suggestion:
            suggestion.match_detail = {
                **(suggestion.match_detail or {}),
                "broker_receipt": {
                    "previous_currency": action.currency,
                    "previous_gross": str(action.total_dividend),
                    "previous_tax": str(action.tax_withheld),
                    "previous_net": str(action.net_dividend),
                    "amount_basis": "net_receipt",
                    "payment_date": receipt.payment_date.isoformat(),
                    "currency": receipt.currency,
                    "net_amount": str(net),
                    "source_hashes": [r.row_hash for r in receipts],
                },
            }
        # A prior verified manual receipt may contribute tax evidence, an accepted
        # announcement never does. Unknown withholding must stay NULL.
        manual_verified = action.amount_basis == "GROSS_NET" and action.currency == receipt.currency
        if all(r.gross is not None and r.tax is not None for r in receipts):
            action.total_dividend = sum((r.gross for r in receipts), Decimal(0))
            action.tax_withheld = sum((r.tax for r in receipts), Decimal(0))
            action.amount_basis = "GROSS_NET"
        elif not manual_verified:
            action.total_dividend = action.tax_withheld = None
            action.amount_basis = "NET_ONLY"
        action.receipt_status = "RECEIVED"
        action.payment_date = receipt.payment_date
        action.currency = receipt.currency
        action.net_dividend = net
        action.tax_rate = None
        action.dividend_per_share = None
        action.import_batch_id = batch_id
        action.notes = append_note(action.notes, source_note)
        if suggestion:
            action.dividend_suggestion_id = suggestion.id
            suggestion.updated_at = datetime.now(timezone.utc)
        self.db.flush()
        self.applied[key] = action
        return action


def prepare_dividend_receipts(
    db,
    user_id,
    account_id,
    receipts,
    *,
    existing_hashes,
    lock=False,
    securities=None,
    match_manual=True,
):
    """Preview is read-only; commit recomputes under the caller's user import lock.

    Ambiguous unsourced legacy bookings block (otherwise cash would double).
    Ambiguous forecasts simply remain unlinked, while the genuine cash can book.
    Split dates/currencies become distinct receipts, never net-vs-gross completion.
    """
    plan = DividendReceiptPlan(
        db,
        user_id,
        account_id,
        receipts={r.row_hash: r for r in receipts if r.row_hash not in existing_hashes},
    )
    if not plan.receipts or account_id is None:
        return plan
    actions = (
        db.query(CorporateAction)
        .filter(
            CorporateAction.user_id == user_id,
            CorporateAction.action_type == "CASH_DIVIDEND",
            CorporateAction.import_batch_id.is_(None),
        )
        .order_by(CorporateAction.id)
        .all()
    )
    sourced = (
        {
            r[0]
            for model in (BrokerFundFlow, IbkrActivityFlow)
            for r in db.query(model.corporate_action_id)
            .filter(
                model.user_id == user_id, model.corporate_action_id.in_([a.id for a in actions])
            )
            .all()
        }
        if actions
        else set()
    )
    actions = [a for a in actions if a.id not in sourced]
    by_id = {a.id: a for a in actions}
    suggestions = (
        db.query(CorporateActionSuggestion)
        .filter(
            CorporateActionSuggestion.user_id == user_id,
            CorporateActionSuggestion.action_type == "CASH_DIVIDEND",
            CorporateActionSuggestion.status != "IGNORED",
            CorporateActionSuggestion.receipt_complete.is_(False),
        )
        .order_by(CorporateActionSuggestion.id)
        .all()
    )
    if lock:
        for action in actions:
            lock_record(db, "corporate-action-record", action.id)
            db.refresh(action)
        for suggestion in suggestions:
            lock_record(db, "ca-suggestion-record", suggestion.id)
            db.refresh(suggestion)
    for receipt in plan.receipts.values():
        manual = [
            a
            for a in actions
            if match_manual
            and a.broker_account_id == account_id
            and (a.symbol, a.market) == (receipt.symbol, receipt.market)
            and a.receipt_status == "RECEIVED"
            and a.amount_basis != "LEGACY"
            and dividend_cash_date(a) == receipt.payment_date
            and a.currency == receipt.currency
            and (
                cash_dividend_amounts(a)[2] == receipt.amount
                or (receipt.gross is not None and a.total_dividend == receipt.gross)
            )
        ]
        candidates = []
        for suggestion in suggestions:
            if superseded_suggestion_date(suggestion):
                continue
            legacy = by_id.get(suggestion.created_corporate_action_id)
            legacy = (
                legacy
                if legacy
                and legacy.receipt_status == "RECEIVED"
                and legacy.amount_basis == "LEGACY"
                else None
            )
            account = legacy.broker_account_id if legacy else suggestion.broker_account_id
            if account not in (account_id, None) or (suggestion.symbol, suggestion.market) != (
                receipt.symbol,
                receipt.market,
            ):
                continue
            actual = SimpleNamespace(
                id=-1,
                action_type="CASH_DIVIDEND",
                ex_date=receipt.payment_date,
                broker_account_id=account_id,
            )
            match = match_existing_action(
                {"ex_date": suggestion.ex_date, "pay_date": suggestion.pay_date},
                "CASH_DIVIDEND",
                None,
                [actual],
                match_window_days=settings.dividend_sync_match_window_days,
                broker_account_id=account_id,
            )
            if match:
                candidates.append((legacy, suggestion, account))
        if len(manual) > 1:
            raise ValueError(f"{receipt.symbol} 到账匹配多笔手工记录，请先核对，整批未导入")
        if manual and manual[0].dividend_suggestion_id is not None:
            # A broker proof identifies the existing cash fact. Its established
            # forecast link outranks date-window guesses, including completed or
            # withdrawn announcements excluded from the candidate query above.
            suggestion = db.get(CorporateActionSuggestion, manual[0].dividend_suggestion_id)
            if suggestion is None:
                raise ValueError("到账记录的原公告关联已变化，请刷新后重新预览")
            if lock:
                lock_record(db, "ca-suggestion-record", suggestion.id)
                db.refresh(suggestion)
            try:
                validate_existing_receipt_links(db, manual[0])
            except HTTPException as exc:
                raise ValueError("到账记录的原公告关联不兼容，请先显式核对；整批未导入") from exc
            legacy = None
        else:
            if any(a is not None for a, _, _ in candidates) and (
                len(candidates) > 1 or candidates[0][2] is None
            ):
                raise ValueError(
                    f"{receipt.symbol} {receipt.payment_date} 到账存在多个分红候选或账户未明确，请先核对已接受的公告与账户；整批未导入"
                )
            suggestion = (
                candidates[0][1]
                if len(candidates) == 1 and candidates[0][2] == account_id
                else None
            )
            legacy = candidates[0][0] if suggestion else None
        if manual and legacy and manual[0].id != legacy.id:
            raise ValueError("实际股息与历史公告同时在账，请先核对历史重复记录，整批未导入")
        action = manual[0] if manual else legacy
        if action or suggestion:
            if action:
                projected = SimpleNamespace(
                    id=action.id,
                    user_id=user_id,
                    action_type="CASH_DIVIDEND",
                    broker_account_id=account_id,
                    currency=receipt.currency,
                    receipt_status="RECEIVED",
                    ex_date=action.ex_date,
                    payment_date=receipt.payment_date,
                )
                try:
                    validate_existing_tax_allocations(db, action=projected)
                except HTTPException as exc:
                    raise ValueError(
                        "该股息已分摊递延税，新的到账币种或日期不相容；请先核对税款归属"
                    ) from exc
            plan.matches[receipt.row_hash] = (action, suggestion)
        elif candidates:
            plan.unmatched_warnings.append(
                f"{receipt.symbol} 到账有多个公告候选或权益账户待核对，现金正常入账，公告暂不关联"
            )
    # A legacy booking has only one cash date; migrate it to UNVERIFIED before
    # linking receipts across dates, to preserve tax allocations and its audit.
    for action_id in {a.id for a, _ in plan.matches.values() if a}:
        grouped = [
            plan.receipts[k] for k, (a, _) in plan.matches.items() if a and a.id == action_id
        ]
        action = by_id[action_id]
        if len(grouped) > 1 and (
            action.amount_basis != "LEGACY" or any(r.gross is not None for r in grouped)
        ):
            raise ValueError("多条到账来源匹配同一已有股息，请先核对分配关系；整批未导入")
        if len({(r.payment_date, r.currency) for r in grouped}) > 1:
            raise ValueError(
                "历史已入账公告存在分次到账，请先执行待核实升级后关联各次到账；整批未导入"
            )
    if lock and plan.matches:
        for symbol, market in sorted(securities or {(r.symbol, r.market) for r in receipts}):
            lock_security_timeline(db, user_id, symbol, market)
    return plan


def validate_existing_receipt_links(db, action):
    """Editing a cash fact must preserve the identity checked when linking it."""
    with db.no_autoflush:
        suggestions = (
            db.query(CorporateActionSuggestion)
            .filter(
                or_(
                    CorporateActionSuggestion.id == action.dividend_suggestion_id,
                    and_(
                        CorporateActionSuggestion.action_type == "CASH_DIVIDEND",
                        or_(
                            CorporateActionSuggestion.created_corporate_action_id == action.id,
                            CorporateActionSuggestion.matched_corporate_action_id == action.id,
                        ),
                    ),
                )
            )
            .all()
        )
    for suggestion in suggestions:
        if suggestion.id != action.dividend_suggestion_id and (
            action.dividend_suggestion_id is not None
            or (suggestion.match_detail or {}).get("receipt_links_reviewed")
        ):
            continue
        if (
            action.user_id,
            action.broker_account_id,
            action.symbol,
            action.market,
            action.action_type,
        ) != (
            suggestion.user_id,
            suggestion.broker_account_id,
            suggestion.symbol,
            suggestion.market,
            "CASH_DIVIDEND",
        ) or suggestion.action_type != "CASH_DIVIDEND":
            raise HTTPException(
                status_code=409,
                detail="此到账记录已关联分红公告，请先在“核对到账”中解除关联，再修改账户、证券或行动类型",
            )


def reconcile_manual_receipt(db, action):
    """Confirmed manual/standard CSV facts share broker matching and duplicate guards."""
    if action.action_type != "CASH_DIVIDEND":
        return
    peers = (
        db.query(CorporateAction)
        .filter(
            CorporateAction.user_id == action.user_id,
            CorporateAction.broker_account_id == action.broker_account_id,
            CorporateAction.symbol == action.symbol,
            CorporateAction.market == action.market,
            CorporateAction.currency == action.currency,
            CorporateAction.action_type == "CASH_DIVIDEND",
            CorporateAction.receipt_status == "RECEIVED",
            CorporateAction.id != action.id,
        )
        .all()
    )
    for peer in peers:
        if (
            dividend_cash_date(peer) == action.payment_date
            and cash_dividend_amounts(peer)[2] == action.net_dividend
        ):
            raise ValueError(
                f"同账户已有相同日期、币种、金额的实收股息 #{peer.id}，请核对或导入带独立流水号的券商凭证，未重复入账"
            )
    plan = prepare_dividend_receipts(
        db,
        action.user_id,
        action.broker_account_id,
        [
            DividendReceipt(
                "manual",
                action.symbol,
                action.market,
                action.payment_date,
                action.currency,
                action.net_dividend,
                gross=action.total_dividend,
                tax=action.tax_withheld,
            )
        ],
        existing_hashes=set(),
        lock=True,
        match_manual=False,
    )
    match = plan.matches.get("manual")
    if match and match[0] is not None:
        raise ValueError("历史公告尚在实际账本，请先核实该记录或执行历史升级，未重复入账")
    if match and match[1] and action.dividend_suggestion_id is None:
        action.dividend_suggestion_id = match[1].id
        match[1].updated_at = datetime.now(timezone.utc)
