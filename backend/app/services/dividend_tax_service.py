"""日期独立的股息税事实；归属只用于证券明细，不改变现金流。"""

from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models.cash_event import CashEvent
from ..models.corporate_action import CorporateAction
from ..models.dividend_tax_allocation import DividendTaxAllocation
from .broker_import_common import import_note
from .portfolio.semantics import dividend_cash_date, is_received_dividend


def load_dividend_tax_events(db: Session, user_id: int, **filters):
    query = db.query(CashEvent).filter(
        CashEvent.user_id == user_id, CashEvent.tax_kind == "DIVIDEND"
    )
    for name, operator in (("start_date", "__ge__"), ("end_date", "__le__")):
        if filters.get(name) is not None:
            query = query.filter(getattr(CashEvent.event_date, operator)(filters[name]))
    if filters.get("broker_account_id") is not None:
        query = query.filter(CashEvent.broker_account_id == filters["broker_account_id"])
    if filters.get("unassigned_account"):
        query = query.filter(CashEvent.broker_account_id.is_(None))
    events = query.order_by(CashEvent.event_date, CashEvent.id).all()
    if not filters.get("symbol") and not filters.get("market"):
        return events
    # 证券过滤只能展示明确归属部分，不能把整笔税额重复分到每只证券。
    actions = db.query(CorporateAction).filter(CorporateAction.user_id == user_id)
    for field in ("symbol", "market"):
        if filters.get(field):
            actions = actions.filter(getattr(CorporateAction, field) == filters[field])
    ids = {action.id for action in actions.all()}
    return [
        SimpleNamespace(amount=amount, currency=event.currency, event_date=event.event_date)
        for event in events
        for amount in [
            sum(
                (a.amount for a in event.tax_allocations if a.corporate_action_id in ids),
                Decimal(0),
            )
        ]
        if amount > 0
    ]


def create_dividend_tax_event(db, *, user_id, broker_account_id, flow, broker_name):
    event = CashEvent(
        user_id=user_id,
        broker_account_id=broker_account_id,
        event_type="TAX",
        tax_kind="DIVIDEND",
        amount=abs(flow.amount),
        currency=flow.currency,
        event_date=flow.trade_date,
        notes=import_note(broker_name, flow.business_name, flow.security_code or "待归属股息"),
    )
    db.add(event)
    db.flush()
    return event


def attribute_tax_cash_source(source, cash_event_id):
    source.cash_event_id = cash_event_id
    source.corporate_action_id = None
    source.skip_reason = None
    return source


def validate_tax_event(event):
    if event.tax_kind is not None and event.event_type != "TAX":
        raise HTTPException(status_code=422, detail="股息税只能用于税费现金事件")


def validate_tax_allocation(event, action):
    if (
        not is_received_dividend(action, event.event_date)
        or event.tax_kind != "DIVIDEND"
        or action.action_type != "CASH_DIVIDEND"
        or action.user_id != event.user_id
        or action.broker_account_id != event.broker_account_id
        or (action.currency or "CNY") != event.currency
        or dividend_cash_date(action) > event.event_date
    ):
        raise HTTPException(status_code=422, detail="税款须归属同一账户、币种且已到账的现金股息")


def validate_existing_tax_allocations(db, *, event=None, action=None):
    if event is not None:
        validate_tax_event(event)
        for allocation in event.tax_allocations:
            linked = db.get(CorporateAction, allocation.corporate_action_id)
            validate_tax_allocation(event, linked)
        if sum((a.amount for a in event.tax_allocations), Decimal(0)) > event.amount:
            raise HTTPException(status_code=422, detail="税款金额不能小于已分摊金额；请先调整归属")
    if action is not None:
        allocations = (
            db.query(DividendTaxAllocation)
            .filter(DividendTaxAllocation.corporate_action_id == action.id)
            .all()
        )
        for allocation in allocations:
            validate_tax_allocation(db.get(CashEvent, allocation.cash_event_id), action)


def replace_tax_allocations(db, event, allocations):
    if event.tax_kind != "DIVIDEND":
        raise HTTPException(status_code=422, detail="该现金事件不是股息税")
    ids = [item.corporate_action_id for item in allocations]
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=422, detail="同一股息不能重复分摊")
    if sum((item.amount for item in allocations), Decimal(0)) > event.amount:
        raise HTTPException(status_code=422, detail="分摊合计不能超过税款金额")
    for item in allocations:
        action = (
            db.query(CorporateAction)
            .filter(
                CorporateAction.id == item.corporate_action_id,
                CorporateAction.user_id == event.user_id,
            )
            .first()
        )
        if action is None:
            raise HTTPException(status_code=404, detail="股息记录不存在")
        validate_tax_allocation(event, action)
    db.query(DividendTaxAllocation).filter(DividendTaxAllocation.cash_event_id == event.id).delete(
        synchronize_session=False
    )
    for item in allocations:
        db.add(
            DividendTaxAllocation(
                user_id=event.user_id,
                cash_event_id=event.id,
                corporate_action_id=item.corporate_action_id,
                amount=item.amount,
            )
        )
    db.flush()
    db.expire(event, ["tax_allocations"])
