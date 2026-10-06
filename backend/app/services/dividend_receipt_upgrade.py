"""Reviewed historical receipt upgrade. No cash anchors, source hashes or costs are edited."""

import copy
import hashlib
import json
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import inspect

from ..core.timeutil import local_today
from ..models.broker_account import BrokerAccount
from ..models.broker_fund_flow import BrokerFundFlow
from ..models.cash_event import CashEvent
from ..models.corporate_action import CorporateAction
from ..models.corporate_action_suggestion import CorporateActionSuggestion
from ..models.dividend_tax_allocation import DividendTaxAllocation
from ..models.holding import Holding
from ..models.ibkr_activity_flow import IbkrActivityFlow
from ..models.import_batch import ImportBatch
from ..models.reconciliation_snapshot import ReconciliationSnapshot
from ..models.transaction import Transaction
from .broker_import_common import append_note, lock_broker_import
from .portfolio.semantics import cash_dividend_amounts, dividend_cash_date, is_received_dividend
from .reconciliation_service import derive_account_cash_asof

VERSION = 1
MODELS = (
    BrokerAccount,
    BrokerFundFlow,
    CashEvent,
    CorporateAction,
    CorporateActionSuggestion,
    DividendTaxAllocation,
    Holding,
    IbkrActivityFlow,
    ImportBatch,
    ReconciliationSnapshot,
    Transaction,
)
DIVIDEND_BUSINESSES = {"股息入账", "产品红利发放", "红利入账"}


def _json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return value


def _record(row):
    return {
        column.key: _json_value(getattr(row, column.key))
        for column in inspect(type(row)).columns
        if column.key not in {"created_at", "updated_at"}
    }


def _digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=_json_value
        ).encode()
    ).hexdigest()


def _ledger(db, user_id):
    return {
        model.__tablename__: [
            _record(row)
            for row in db.query(model).filter(model.user_id == user_id).order_by(model.id)
        ]
        for model in MODELS
    }


def _positive_dividend(source):
    if isinstance(source, BrokerFundFlow):
        return source.business_name in DIVIDEND_BUSINESSES and source.amount > 0
    return source.activity_type == "股息" and (source.gross_amount or 0) > 0


def _source_detail(source):
    return {
        "kind": source.__tablename__,
        "id": source.id,
        "row_hash": source.row_hash,
        "account_id": source.broker_account_id,
        "date": source.trade_date.isoformat(),
        "currency": source.currency if isinstance(source, BrokerFundFlow) else source.base_currency,
        "amount": str(source.amount if isinstance(source, BrokerFundFlow) else source.gross_amount),
        "skip_reason": source.skip_reason,
    }


def build_dividend_receipt_upgrade_plan(db, user_id, *, as_of=None):
    as_of = as_of or local_today()
    state = _ledger(db, user_id)
    predicted = copy.deepcopy(state)
    predicted_actions = {a["id"]: a for a in predicted["corporate_actions"]}
    actions = (
        db.query(CorporateAction)
        .filter(CorporateAction.user_id == user_id, CorporateAction.action_type == "CASH_DIVIDEND")
        .order_by(CorporateAction.id)
        .all()
    )
    suggestions = (
        db.query(CorporateActionSuggestion)
        .filter(CorporateActionSuggestion.user_id == user_id)
        .order_by(CorporateActionSuggestion.id)
        .all()
    )
    announcement_actions = defaultdict(list)
    for suggestion in suggestions:
        if suggestion.created_corporate_action_id:
            announcement_actions[suggestion.created_corporate_action_id].append(suggestion)
    sources = [
        s
        for model in (BrokerFundFlow, IbkrActivityFlow)
        for s in db.query(model).filter(model.user_id == user_id).order_by(model.id)
    ]
    by_action = defaultdict(list)
    for source in sources:
        if source.corporate_action_id:
            by_action[source.corporate_action_id].append(source)
    allocated = {
        a.corporate_action_id
        for a in db.query(DividendTaxAllocation).filter(DividendTaxAllocation.user_id == user_id)
    }
    changes, review, blockers = [], [], []
    for model in (BrokerFundFlow, IbkrActivityFlow, DividendTaxAllocation):
        if (
            db.query(model.id)
            .filter(
                model.corporate_action_id.in_([a.id for a in actions]), model.user_id != user_id
            )
            .first()
        ):
            blockers.append("股息存在跨用户来源或税款关联，须先修正归属")
    for action in actions:
        if action.amount_basis != "LEGACY" or action.receipt_status == "UNVERIFIED":
            continue
        linked = by_action[action.id]
        evidence = [s for s in linked if _positive_dividend(s)]
        before = _record(action)
        after = {}
        reason = ""
        if evidence:
            details = [_source_detail(s) for s in evidence]
            if (
                any(
                    s.user_id != user_id
                    or s.broker_account_id != action.broker_account_id
                    or s.skip_reason is not None
                    or (s.security_code if isinstance(s, BrokerFundFlow) else s.symbol)
                    != action.symbol
                    for s in linked
                )
                or len({(d["date"], d["currency"]) for d in details}) != 1
                or details[0]["currency"] != action.currency
                or any(s.transaction_id is not None or s.cash_event_id is not None for s in linked)
            ):
                blockers.append(f"股息 #{action.id} 来源账户、日期、币种或链接不一致")
                continue
            payment_date = evidence[0].trade_date
            if dividend_cash_date(action) != payment_date:
                blockers.append(f"股息 #{action.id} 现金日期与凭证不一致，须单独审阅日期修正")
                continue
            if all(isinstance(s, BrokerFundFlow) for s in evidence):
                if len(linked) != len(evidence):
                    blockers.append(
                        f"股息 #{action.id} 仍关联其他来源（可能为旧扣税），先核对股息税升级"
                    )
                    continue
                net = sum((s.amount for s in evidence), Decimal(0))
                if net != cash_dividend_amounts(action)[2]:
                    blockers.append(f"股息 #{action.id} 净额与原始红利流水不符")
                    continue
                after = {
                    "receipt_status": "RECEIVED",
                    "amount_basis": "NET_ONLY",
                    "total_dividend": None,
                    "tax_withheld": None,
                    "tax_rate": None,
                    "net_dividend": _json_value(net),
                    "payment_date": payment_date.isoformat(),
                }
                reason = "券商正向红利流水仅证明净到账额，旧税前及税额不作为凭证"
            elif all(isinstance(s, IbkrActivityFlow) for s in linked):
                taxes = [
                    s
                    for s in linked
                    if s.activity_type == "外国预扣税" and (s.gross_amount or 0) < 0
                ]
                gross = sum((s.gross_amount for s in evidence), Decimal(0))
                tax = sum((abs(s.gross_amount) for s in taxes), Decimal(0))
                if (
                    len(evidence) != 1
                    or len(evidence) + len(taxes) != len(linked)
                    or any(
                        s.trade_date != payment_date or s.base_currency != action.currency
                        for s in taxes
                    )
                    or (gross, tax, gross - tax) != cash_dividend_amounts(action)
                ):
                    blockers.append(f"股息 #{action.id} IBKR 税前、税额、净额或来源不闭合")
                    continue
                after = {
                    "receipt_status": "RECEIVED",
                    "amount_basis": "GROSS_NET",
                    "payment_date": payment_date.isoformat(),
                }
                reason = "IBKR 已链接股息及同日预扣税，保持原币列与金额；不推断实际支付原币"
            else:
                blockers.append(f"股息 #{action.id} 混合券商来源，须单独核对")
                continue
        elif action.id in announcement_actions:
            if linked or action.import_batch_id is not None or action.id in allocated:
                blockers.append(
                    f"公告生成股息 #{action.id} 带来源/批次/税款归属，不能直接降为待核实"
                )
                continue
            if len(announcement_actions[action.id]) != 1:
                blockers.append(f"股息 #{action.id} 有多份创建公告，须核对")
                continue
            suggestion = announcement_actions[action.id][0]
            if (action.symbol, action.market) != (suggestion.symbol, suggestion.market):
                blockers.append(f"股息 #{action.id} 与创建公告证券不一致")
                continue
            after = {"receipt_status": "UNVERIFIED", "dividend_suggestion_id": suggestion.id}
            reason = "仅有公告接受记录，无真实到账来源；保留原金额，移出现金及实收统计"
        else:
            review.append(
                {"action_id": action.id, "reason": "历史手工或未知来源，保留原计算，需人工提供凭证"}
            )
            continue
        after["notes"] = append_note(
            action.notes,
            f"[dividend-receipt-upgrade-v{VERSION}] {reason}；原税前={action.total_dividend}，税额={action.tax_withheld}，净額={action.net_dividend}，状态={action.receipt_status}",
        )
        after_cash = (
            Decimal(0)
            if after.get("receipt_status") == "UNVERIFIED"
            else cash_dividend_amounts(action)[2]
        )
        before_cash = (
            cash_dividend_amounts(action)[2] if is_received_dividend(action, as_of) else Decimal(0)
        )
        if dividend_cash_date(action) > as_of:
            after_cash = Decimal(0)
        changes.append(
            {
                "action_id": action.id,
                "before": before,
                "after": after,
                "reason": reason,
                "sources": [_source_detail(s) for s in linked],
                "expected_cash_delta_as_of": str(after_cash - before_cash),
            }
        )
        predicted_actions[action.id].update(after)
    # Coverage dates describe what was imported, never a claim of continuous coverage.
    coverage = []
    for account in (
        db.query(BrokerAccount).filter(BrokerAccount.user_id == user_id).order_by(BrokerAccount.id)
    ):
        rows = [s for s in sources if s.broker_account_id == account.id]
        incomes = [s for s in rows if _positive_dividend(s) and s.cash_event_id is None]
        dates = [s.trade_date for s in rows]
        income_dates = [s.trade_date for s in incomes]
        taxes = [
            s
            for s in rows
            if (
                isinstance(s, BrokerFundFlow)
                and s.business_name in {"股息红利税补缴", "股息红利差异扣税"}
                and s.amount < 0
            )
            or (
                isinstance(s, IbkrActivityFlow)
                and s.activity_type == "外国预扣税"
                and (s.gross_amount or 0) < 0
            )
        ]
        coverage.append(
            {
                "account_id": account.id,
                "broker": account.broker,
                "source_first_date": min(dates).isoformat() if dates else None,
                "source_last_date": max(dates).isoformat() if dates else None,
                "dividend_first_date": min(income_dates).isoformat() if income_dates else None,
                "dividend_last_date": max(income_dates).isoformat() if income_dates else None,
                "dividend_source_count": len(incomes),
                "cash_event_classified_income_count": sum(
                    _positive_dividend(s) and s.cash_event_id is not None for s in rows
                ),
                "unlinked_dividend_sources": [
                    _source_detail(s) for s in incomes if s.corporate_action_id is None
                ],
                "withholding_source_count": len(taxes),
                "unlinked_tax_sources": [
                    _source_detail(s)
                    for s in taxes
                    if s.corporate_action_id is None and s.cash_event_id is None
                ],
                "continuous_coverage_verified": False,
            }
        )
    cash_checks = []
    cutoffs = {
        (a.id, as_of) for a in db.query(BrokerAccount).filter(BrokerAccount.user_id == user_id)
    }
    cutoffs.update(
        (s.broker_account_id, s.snapshot_date)
        for s in db.query(ReconciliationSnapshot).filter(ReconciliationSnapshot.user_id == user_id)
        if s.broker_account_id
    )
    for account_id, cutoff in sorted(cutoffs):
        cash = derive_account_cash_asof(db, user_id, account_id, cutoff)
        deltas = defaultdict(lambda: Decimal(0))
        for change in changes:
            before = change["before"]
            if (
                change["after"].get("receipt_status") == "UNVERIFIED"
                and before["broker_account_id"] == account_id
            ):
                cash_date = date.fromisoformat(before["payment_date"] or before["ex_date"])
                if cash_date <= cutoff:
                    action = next(a for a in actions if a.id == change["action_id"])
                    deltas[before["currency"]] -= cash_dividend_amounts(action)[2]
        # Unassigned legacy income may have been attributed by sole-holder logic;
        # refuse application rather than guessing its account-level effect.
        cash_checks.append(
            {
                "account_id": account_id,
                "as_of": cutoff.isoformat(),
                "before": {c: str(v) for c, v in sorted(cash.items())},
                "expected_after": {
                    c: str(cash.get(c, Decimal(0)) + deltas[c])
                    for c in sorted(set(cash) | set(deltas))
                },
                "broker_snapshots": [
                    {
                        "snapshot_id": snapshot.id,
                        "reported_cash": snapshot.cash_balances,
                        "difference_before": {
                            c: str(cash.get(c, Decimal(0)) - Decimal(str(v)))
                            for c, v in (snapshot.cash_balances or {}).items()
                        },
                        "difference_after": {
                            c: str(cash.get(c, Decimal(0)) + deltas[c] - Decimal(str(v)))
                            for c, v in (snapshot.cash_balances or {}).items()
                        },
                    }
                    for snapshot in db.query(ReconciliationSnapshot)
                    .filter(
                        ReconciliationSnapshot.user_id == user_id,
                        ReconciliationSnapshot.broker_account_id == account_id,
                        ReconciliationSnapshot.snapshot_date == cutoff,
                    )
                    .order_by(ReconciliationSnapshot.id)
                ],
            }
        )
    if any(
        c["after"].get("receipt_status") == "UNVERIFIED"
        and c["before"]["broker_account_id"] is None
        for c in changes
    ):
        blockers.append("存在未归属账户的公告估算，须先明确其权益账户再升级")
    return {
        "version": VERSION,
        "user_id": user_id,
        "as_of": as_of.isoformat(),
        "before_fingerprint": _digest(state),
        "after_fingerprint": _digest(predicted),
        "changes": changes,
        "review_required": review,
        "coverage": coverage,
        "cash_checks": cash_checks,
        "blockers": blockers,
    }


def apply_dividend_receipt_upgrade_plan(db, plan):
    if plan.get("version") != VERSION:
        raise ValueError("不支持的升级计划版本")
    user_id = plan["user_id"]
    lock_broker_import(db, user_id)
    state = _ledger(db, user_id)
    if _digest(state) == plan.get("after_fingerprint") and not plan.get("blockers"):
        return 0
    current = build_dividend_receipt_upgrade_plan(
        db, user_id, as_of=date.fromisoformat(plan["as_of"])
    )
    if plan != current or plan["blockers"]:
        raise ValueError("账本或来源已变化，或计划存在阻断项；请重新预演并审阅")
    # Savepoint ensures a failed invariant cannot leave partial updates even if
    # a caller catches the exception and subsequently commits its outer session.
    with db.begin_nested():
        for change in plan["changes"]:
            action = db.get(CorporateAction, change["action_id"])
            for key, value in change["after"].items():
                if key == "payment_date" and value is not None:
                    value = date.fromisoformat(value)
                elif (
                    key in {"net_dividend", "total_dividend", "tax_withheld", "tax_rate"}
                    and value is not None
                ):
                    value = Decimal(value)
                setattr(action, key, value)
        db.flush()
        if _digest(_ledger(db, user_id)) != plan["after_fingerprint"]:
            raise ValueError("升级后账本指纹与预期不一致；已回滚本次升级")
        for check in plan["cash_checks"]:
            cash = derive_account_cash_asof(
                db, user_id, check["account_id"], date.fromisoformat(check["as_of"])
            )
            if any(
                cash.get(currency, Decimal(0)) != Decimal(amount)
                for currency, amount in check["expected_after"].items()
            ):
                raise ValueError("升级后现金变化超出审阅计划；已回滚本次升级")
    return len(plan["changes"])
