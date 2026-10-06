"""独立、可审阅的历史股息税升级；schema 迁移本身不改账本。"""

from decimal import Decimal
from types import SimpleNamespace

from ..models.broker_fund_flow import BrokerFundFlow
from ..models.cash_event import CashEvent
from ..models.corporate_action import CorporateAction
from ..models.dividend_tax_allocation import DividendTaxAllocation
from ..models.ibkr_activity_flow import IbkrActivityFlow
from ..models.reconciliation_snapshot import ReconciliationSnapshot
from .broker_import_common import lock_broker_import
from .dividend_tax_service import attribute_tax_cash_source, create_dividend_tax_event
from .portfolio.semantics import dividend_cash_date
from .reconciliation_service import derive_account_cash_asof


def build_dividend_tax_upgrade_plan(
    db, user_id, *, anchors=None, preserve_existing_cash_delta=False
):
    """anchors 为显式 {现金锚点 ID: 原券商快照 ID}，不自动猜测哪笔余额应改。"""
    anchors = anchors or {}
    facts, actions, blockers = [], {}, []
    for model, kind in (
        (BrokerFundFlow, "broker_fund_flow"),
        (IbkrActivityFlow, "ibkr_activity_flow"),
    ):
        query = db.query(model).filter(model.user_id == user_id, model.cash_event_id.is_(None))
        if model is BrokerFundFlow:
            query = query.filter(
                model.business_name.in_(["股息红利税补缴", "股息红利差异扣税"]), model.amount < 0
            )
        else:
            query = query.filter(model.activity_type == "外国预扣税", model.gross_amount < 0)
        for source in query.order_by(model.id).all():
            if source.transaction_id is not None or source.skip_reason not in (
                None,
                "unattributed_tax",
            ):
                continue
            amount = abs(source.amount if model is BrokerFundFlow else source.gross_amount)
            currency = source.currency if model is BrokerFundFlow else source.base_currency
            action = (
                db.get(CorporateAction, source.corporate_action_id)
                if source.corporate_action_id
                else None
            )
            if action is not None and dividend_cash_date(action) == source.trade_date:
                continue  # 同日既有预扣税无需拆分，也不会重复扣款。
            if action is not None and (
                action.user_id != user_id
                or action.action_type != "CASH_DIVIDEND"
                or action.broker_account_id != source.broker_account_id
                or (action.currency or "CNY") != currency
                or dividend_cash_date(action) > source.trade_date
            ):
                blockers.append(f"{kind}:{source.id} 的既有股息关联无法确认")
                continue
            facts.append(
                {
                    "source_kind": kind,
                    "source_id": source.id,
                    "row_hash": source.row_hash,
                    "skip_reason": source.skip_reason,
                    "broker_account_id": source.broker_account_id,
                    "event_date": source.trade_date.isoformat(),
                    "currency": currency,
                    "amount": str(amount),
                    "corporate_action_id": source.corporate_action_id,
                    "dividend_cash_date": dividend_cash_date(action).isoformat()
                    if action
                    else None,
                }
            )
            if action is not None:
                entry = actions.setdefault(
                    action.id,
                    {
                        "id": action.id,
                        "tax_before": str(action.tax_withheld or Decimal(0)),
                        "net_before": str(action.net_dividend)
                        if action.net_dividend is not None
                        else None,
                        "gross": str(action.total_dividend)
                        if action.total_dividend is not None
                        else None,
                        "extracted_tax": Decimal(0),
                    },
                )
                entry["extracted_tax"] += amount

    for entry in actions.values():
        tax = Decimal(entry["tax_before"])
        extracted = entry["extracted_tax"]
        gross = Decimal(entry["gross"]) if entry["gross"] is not None else None
        net = Decimal(entry["net_before"]) if entry["net_before"] is not None else None
        # 旧模型若曾截断为 0，不能靠加回税款还原未知的真实净额。
        if (
            tax < extracted
            or gross is None
            or tax > gross
            or (net is not None and net != gross - tax)
        ):
            blockers.append(f"股息 {entry['id']} 的原税额/净额不支持无损拆分")
        entry["tax_after"] = str(tax - extracted)
        entry["net_after"] = str(net + extracted) if net is not None else None
        entry["extracted_tax"] = str(extracted)

    anchor_changes = []
    affected_accounts = {(f["broker_account_id"], f["currency"]) for f in facts}
    residual_anchors = (
        db.query(CashEvent)
        .filter(
            CashEvent.user_id == user_id,
            CashEvent.notes.contains("锚点"),
            CashEvent.notes.contains("残差"),
        )
        .all()
    )
    for event in residual_anchors:
        if (
            event.broker_account_id,
            event.currency,
        ) in affected_accounts and event.id not in anchors:
            blockers.append(f"残差锚点 {event.id} 须显式指定原券商快照（--anchor EVENT:SNAPSHOT）")
    seen = set()
    for event_id, snapshot_id in sorted(anchors.items()):
        event = db.get(CashEvent, event_id)
        snapshot = db.get(ReconciliationSnapshot, snapshot_id)
        if (
            event is None
            or snapshot is None
            or event.user_id != user_id
            or snapshot.user_id != user_id
        ):
            blockers.append(f"锚点 {event_id} 或快照 {snapshot_id} 不属于该用户")
            continue
        key = (event.broker_account_id, event.currency)
        if (
            key in seen
            or snapshot.broker_account_id != event.broker_account_id
            or event.event_date > snapshot.snapshot_date
            or not snapshot.source_filename
            or event.currency not in (snapshot.cash_balances or {})
            or event.event_type not in ("DEPOSIT", "WITHDRAWAL", "TRANSFER_IN", "TRANSFER_OUT")
        ):
            blockers.append(f"锚点 {event_id} 缺少唯一、同账户币种的原券商快照依据")
            continue
        seen.add(key)
        before = derive_account_cash_asof(
            db, user_id, event.broker_account_id, snapshot.snapshot_date
        ).get(event.currency, Decimal(0))
        broker_cash = Decimal(str(snapshot.cash_balances[event.currency]))
        if abs(before - broker_cash) > Decimal("0.01") and not preserve_existing_cash_delta:
            blockers.append(f"锚点 {event_id} 的原快照现金当前已不匹配，不能用补税掩盖其他差异")
        cutoff = snapshot.snapshot_date.isoformat()
        delta = sum(
            (
                (
                    Decimal(f["amount"])
                    if f["dividend_cash_date"] and f["dividend_cash_date"] <= cutoff
                    else Decimal(0)
                )
                - (Decimal(f["amount"]) if f["event_date"] <= cutoff else Decimal(0))
                for f in facts
                if (f["broker_account_id"], f["currency"]) == key
            ),
            Decimal(0),
        )
        sign = 1 if event.event_type in ("DEPOSIT", "TRANSFER_IN") else -1
        after = event.amount - delta / sign
        if after <= 0:
            blockers.append(f"锚点 {event_id} 重算后非正数，需要单独审阅资金方向")
        if delta:
            anchor_changes.append(
                {
                    "id": event.id,
                    "snapshot_id": snapshot.id,
                    "event_date": event.event_date.isoformat(),
                    "event_type": event.event_type,
                    "currency": event.currency,
                    "broker_account_id": event.broker_account_id,
                    "amount_before": str(event.amount),
                    "amount_after": str(after),
                    "snapshot_date": cutoff,
                    "broker_cash": str(broker_cash),
                    "derived_cash_before": str(before),
                    "existing_cash_delta": str(before - broker_cash),
                    "expected_cash_after": str(
                        before if preserve_existing_cash_delta else broker_cash
                    ),
                    "tax_cash_delta": str(delta),
                    "notes_before": event.notes,
                }
            )
    return {
        "version": 1,
        "user_id": user_id,
        "anchors": {str(k): v for k, v in anchors.items()},
        "preserve_existing_cash_delta": preserve_existing_cash_delta,
        "facts": facts,
        "dividend_changes": list(actions.values()),
        "anchor_changes": anchor_changes,
        "blockers": blockers,
    }


def apply_dividend_tax_upgrade_plan(db, plan):
    """只应用与当前账本重新生成结果完全相等的已审阅计划；事务由调用方提交。"""
    user_id = plan["user_id"]
    lock_broker_import(db, user_id)
    current = build_dividend_tax_upgrade_plan(
        db,
        user_id,
        anchors={int(k): v for k, v in plan["anchors"].items()},
        preserve_existing_cash_delta=plan["preserve_existing_cash_delta"],
    )
    if current != plan or plan["blockers"]:
        raise ValueError("计划有阻断项或账本已变化；请重新生成 dry-run 并审阅")
    for change in plan["dividend_changes"]:
        action = db.get(CorporateAction, change["id"])
        action.tax_withheld = Decimal(change["tax_after"])
        action.net_dividend = (
            Decimal(change["net_after"]) if change["net_after"] is not None else None
        )
    for fact in plan["facts"]:
        model = BrokerFundFlow if fact["source_kind"] == "broker_fund_flow" else IbkrActivityFlow
        source = db.get(model, fact["source_id"])
        event = create_dividend_tax_event(
            db,
            user_id=user_id,
            broker_account_id=fact["broker_account_id"],
            broker_name=source.broker,
            flow=SimpleNamespace(
                amount=Decimal(fact["amount"]),
                currency=fact["currency"],
                trade_date=source.trade_date,
                business_name=source.business_name
                if model is BrokerFundFlow
                else source.activity_type,
                security_code=source.security_code if model is BrokerFundFlow else source.symbol,
            ),
        )
        attribute_tax_cash_source(source, event.id)
        if fact["corporate_action_id"] is not None:
            # 保留既有明确关联，不重猜证券或分配身份；9 条旧关联不会变成新漏税。
            db.add(
                DividendTaxAllocation(
                    user_id=user_id,
                    cash_event_id=event.id,
                    corporate_action_id=fact["corporate_action_id"],
                    amount=event.amount,
                )
            )
    for change in plan["anchor_changes"]:
        db.get(CashEvent, change["id"]).amount = Decimal(change["amount_after"])
    db.flush()
    for change in plan["anchor_changes"]:
        snapshot = db.get(ReconciliationSnapshot, change["snapshot_id"])
        actual = derive_account_cash_asof(
            db, user_id, snapshot.broker_account_id, snapshot.snapshot_date
        ).get(change["currency"], Decimal(0))
        if abs(actual - Decimal(change["expected_cash_after"])) > Decimal("0.01"):
            raise ValueError("升级后现金与原券商快照不一致；整批必须回滚")
    return len(plan["facts"])
