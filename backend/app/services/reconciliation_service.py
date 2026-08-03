"""对账闭环（任务序 4）：系统重放 vs 券商月末快照的自动比对。

把系统按账户重放出的 as-of 持仓、以及从现金事件+交易流推导出的账户现金，
与 ReconciliationSnapshot 里录入/导入的券商快照逐项 diff。比对只读、只报告：
差异是指向根因的数据质量信号（漏录交易/公司行动/现金事件、账户归属错误），
不做任何自动修复。

现金推导口径（写入 diff 的 methodology）：
- 现金事件按类型定方向：DEPOSIT/TRANSFER_IN/FX_IN/INTEREST/OTHER 为流入，
  WITHDRAWAL/TRANSFER_OUT/FX_OUT/FEE/TAX 为流出（金额恒正）。
- 交易：BUY 流出 数量×价格+费用，SELL 流入 数量×价格−费用，币种取交易币种。
- 现金股息：归属该账户的 CASH_DIVIDEND 按税后净额流入（支付日，缺省除权日）。
- 未归属账户（NULL）的股息与现金事件不计入任何账户的推导现金。

整体状态 = 持仓差 + 重放一致性 + 现金差三者共同决定（防"现金未闭合却整体
绿灯"的假绿）。分范围对账单（statement_scope）只比对范围内市场的持仓、不比
现金，其 MATCHED 语义是"范围内持仓一致"。
"""

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from ..core.logging import get_app_logger
from ..models.broker_fund_flow import BrokerFundFlow
from ..models.cash_event import CashEvent
from ..models.corporate_action import CorporateAction
from ..models.transaction import Transaction
from .cmb_fund_flow_importer import BROKER_NAME as CMB_BROKER_NAME
from .eastmoney_statement_importer import BROKER_NAME as EASTMONEY_BROKER_NAME
from .security_rule_service import get_excluded_keys
from .holding_service import AccountReplayError, replay_transactions_per_account

logger = get_app_logger(__name__)

# 分范围对账单（东方财富）只覆盖单一市场：比对必须按 scope 过滤系统侧持仓，
# 且其现金余额只属于该报表范围，不与账户级推导现金比对。
SCOPE_MARKETS = {
    "stock": {"A股"},
    "hk_connect": {"港股"},
}

QUANTITY_TOLERANCE = Decimal("0.000001")
CASH_TOLERANCE = Decimal("0.01")

CASH_OUTFLOW_TYPES = {"WITHDRAWAL", "TRANSFER_OUT", "FX_OUT", "FEE", "TAX"}

METHODOLOGY_NOTES = [
    "持仓按快照日（含当日）重放账户桶得出；快照视为该交易日收盘后的状态。",
    "现金 = 现金事件（按类型定向）+ 交易现金流 + 归属该账户的税后股息；"
    "推导正确性依赖现金事件录入完整。",
    "未归属账户的股息与现金事件不计入推导现金。",
    "差异只报告不修复：数量差通常指向漏录交易/公司行动或账户归属错误，"
    "现金差通常指向漏录现金事件。",
    "排除清单内的现金管理标的（summary.excluded_symbols）双侧忽略，不参与比对。",
    "沪/深港通等结算币种与记账币种不同的交易，按券商流水推导结算币种现金流"
    "（含费用）：招商流水金额为含费净额直接使用，东财港股通为成交额加/减明细"
    "费用；未知来源口径回退记账币种。",
]


def replay_account_positions_asof(
    db: Session,
    user_id: int,
    account_id: Optional[int],
    as_of: date,
) -> Tuple[Dict[Tuple[str, str], Decimal], List[Dict[str, Any]]]:
    """重放到 as_of（含当日）的账户持仓数量；归属矛盾的证券单独报告。"""
    transactions = db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.transaction_date <= as_of,
    ).all()
    corporate_actions = db.query(CorporateAction).filter(
        CorporateAction.user_id == user_id,
        CorporateAction.ex_date <= as_of,
    ).all()

    txns_by_key = defaultdict(list)
    for txn in transactions:
        txns_by_key[(txn.symbol, txn.market)].append(txn)
    actions_by_key = defaultdict(list)
    for action in corporate_actions:
        actions_by_key[(action.symbol, action.market)].append(action)

    # 只重放该账户参与过的证券：无关证券的归属矛盾不应污染这个账户的对账。
    relevant_keys = {
        key
        for key, txns in txns_by_key.items()
        if any(txn.broker_account_id == account_id for txn in txns)
    } | {
        key
        for key, actions in actions_by_key.items()
        if any(action.broker_account_id == account_id for action in actions)
    }

    positions: Dict[Tuple[str, str], Decimal] = {}
    inconsistent: List[Dict[str, Any]] = []
    for key in sorted(relevant_keys):
        symbol, market = key
        try:
            buckets = replay_transactions_per_account(
                txns_by_key.get(key, []),
                actions_by_key.get(key, []),
                symbol,
                market,
            )
        except AccountReplayError as exc:
            inconsistent.append({
                "symbol": symbol,
                "market": market,
                "reason": str(exc),
            })
            continue
        state = buckets.get(account_id)
        if state and state["quantity"] > 0:
            positions[key] = state["quantity"]
    return positions, inconsistent


def _settlement_cash_flow(flow: Any, txn: Any) -> Optional[Decimal]:
    """带结算汇率的流水行 → 该笔交易的带符号结算现金流（结算币种）。

    金额语义因券商而异，不能一概按"含费净额"直加：
    - 招商证券：amount 本身是带符号、已含费用的结算净额，直接使用；
    - 东方财富证券（港股通）：amount 是无符号 CNY 成交额，费用在明细
      费列——按交易方向扣回（BUY 流出成交额+费，SELL 流入成交额−费）；
    - 未知券商：返回 None，调用方回退记账币种口径，绝不猜。
    """
    amount = Decimal(str(flow.amount))
    if flow.broker == CMB_BROKER_NAME:
        return amount
    if flow.broker == EASTMONEY_BROKER_NAME:
        gross = abs(amount)
        fee = sum(
            Decimal(str(value or 0))
            for value in (
                flow.stamp_tax,
                flow.commission,
                flow.handling_fee,
                flow.management_fee,
                flow.settlement_fee,
                flow.transfer_fee,
                flow.other_fee,
            )
        )
        if txn.transaction_type == "BUY":
            return -(gross + fee)
        return gross - fee
    return None


def derive_account_cash_asof(
    db: Session,
    user_id: int,
    account_id: Optional[int],
    as_of: date,
) -> Dict[str, Decimal]:
    """按币种推导账户到 as_of（含当日）的现金余额。"""
    balances: Dict[str, Decimal] = defaultdict(lambda: Decimal("0"))

    events = db.query(CashEvent).filter(
        CashEvent.user_id == user_id,
        CashEvent.broker_account_id == account_id,
        CashEvent.event_date <= as_of,
    ).all()
    for event in events:
        amount = Decimal(str(event.amount))
        currency = event.currency or "CNY"
        if event.event_type in CASH_OUTFLOW_TYPES:
            balances[currency] -= amount
        else:
            balances[currency] += amount

    transactions = db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.broker_account_id == account_id,
        Transaction.transaction_date <= as_of,
        Transaction.transaction_type.in_(["BUY", "SELL"]),
    ).all()
    # 结算币种感知：沪/深港通以 HKD 记账、CNY 实际结算——按记账币种推导会
    # 造出不存在的 HKD 流出并漏掉真实 CNY 流出。券商流水行保留了 CNY 结算
    # 金额与推导结算汇率，命中即以流水结算口径入账（金额语义因券商而异，
    # 见 _settlement_cash_flow）；口径未知或未命中的交易回退记账币种。
    settlement_by_txn: Dict[int, Any] = {}
    if transactions:
        settlement_flows = db.query(BrokerFundFlow).filter(
            BrokerFundFlow.user_id == user_id,
            BrokerFundFlow.transaction_id.in_([txn.id for txn in transactions]),
            BrokerFundFlow.settlement_rate.isnot(None),
        ).all()
        settlement_by_txn = {flow.transaction_id: flow for flow in settlement_flows}
    for txn in transactions:
        flow = settlement_by_txn.get(txn.id)
        settlement_cash = (
            _settlement_cash_flow(flow, txn) if flow is not None else None
        )
        if settlement_cash is not None:
            balances[flow.currency or "CNY"] += settlement_cash
            continue
        gross = Decimal(str(txn.quantity)) * Decimal(str(txn.price))
        fee = Decimal(str(txn.fee or 0))
        currency = txn.currency or "CNY"
        if txn.transaction_type == "BUY":
            balances[currency] -= gross + fee
        else:
            balances[currency] += gross - fee

    dividends = db.query(CorporateAction).filter(
        CorporateAction.user_id == user_id,
        CorporateAction.broker_account_id == account_id,
        CorporateAction.action_type == "CASH_DIVIDEND",
    ).all()
    for dividend in dividends:
        pay_date = dividend.payment_date or dividend.ex_date
        if pay_date is None or pay_date > as_of:
            continue
        gross = Decimal(str(dividend.total_dividend or 0))
        tax = Decimal(str(dividend.tax_withheld or 0))
        net = Decimal(str(
            dividend.net_dividend if dividend.net_dividend is not None else gross - tax
        ))
        balances[dividend.currency or "CNY"] += net

    return {currency: amount for currency, amount in balances.items() if amount != 0}


def compare_snapshot(db: Session, snapshot) -> Tuple[str, Dict[str, Any]]:
    """比对单个快照，返回 (status, diff_detail)；不修改快照对象。"""
    as_of = snapshot.snapshot_date
    account_id = snapshot.broker_account_id
    scope_markets = SCOPE_MARKETS.get(getattr(snapshot, "statement_scope", None))

    system_positions, inconsistent = replay_account_positions_asof(
        db, snapshot.user_id, account_id, as_of
    )
    if scope_markets is not None:
        # 分范围快照：系统侧只保留该范围内市场，另一市场的持仓不算"快照缺记录"。
        system_positions = {
            key: quantity
            for key, quantity in system_positions.items()
            if key[1] in scope_markets
        }
        inconsistent = [
            item for item in inconsistent if item["market"] in scope_markets
        ]
    snapshot_positions: Dict[Tuple[str, str], Decimal] = {}
    for position in snapshot.positions or []:
        key = (position["symbol"], position["market"])
        quantity = Decimal(str(position["quantity"]))
        if quantity > 0:
            snapshot_positions[key] = snapshot_positions.get(key, Decimal("0")) + quantity

    # 排除清单双侧过滤：清单内标的（如货币基金）既不算"系统缺记录"，
    # 也不算"快照缺记录"；其重放异常同样不阻塞 MATCHED——"双侧忽略"必须
    # 覆盖 inconsistent，否则排除标的的归属矛盾仍会把整体判红（并卡住
    # 东财导入门禁）。只记录实际生效的排除项，供 diff 明细审计。
    excluded_keys = get_excluded_keys(db, snapshot.user_id)
    inconsistent_keys = {(item["symbol"], item["market"]) for item in inconsistent}
    applied_exclusions = sorted(
        excluded_keys & (set(system_positions) | set(snapshot_positions) | inconsistent_keys)
    )
    if applied_exclusions:
        system_positions = {
            key: quantity
            for key, quantity in system_positions.items()
            if key not in excluded_keys
        }
        snapshot_positions = {
            key: quantity
            for key, quantity in snapshot_positions.items()
            if key not in excluded_keys
        }
        inconsistent = [
            item
            for item in inconsistent
            if (item["symbol"], item["market"]) not in excluded_keys
        ]

    position_diffs = []
    position_mismatches = 0
    for key in sorted(set(system_positions) | set(snapshot_positions)):
        symbol, market = key
        system_quantity = system_positions.get(key)
        snapshot_quantity = snapshot_positions.get(key)
        if system_quantity is None:
            status = "MISSING_IN_SYSTEM"
        elif snapshot_quantity is None:
            status = "MISSING_IN_SNAPSHOT"
        elif abs(system_quantity - snapshot_quantity) <= QUANTITY_TOLERANCE:
            status = "MATCH"
        else:
            status = "QUANTITY_MISMATCH"
        if status != "MATCH":
            position_mismatches += 1
        position_diffs.append({
            "symbol": symbol,
            "market": market,
            "snapshot_quantity": float(snapshot_quantity) if snapshot_quantity is not None else None,
            "system_quantity": float(system_quantity) if system_quantity is not None else None,
            "delta": float((system_quantity or Decimal("0")) - (snapshot_quantity or Decimal("0"))),
            "status": status,
        })

    # 现金比对：分范围快照的现金只是该报表范围的余额，与账户级推导不可比，跳过。
    cash_compared = scope_markets is None
    cash_diffs = []
    cash_mismatches = 0
    if cash_compared:
        derived_cash = derive_account_cash_asof(db, snapshot.user_id, account_id, as_of)
        snapshot_cash = {
            currency: Decimal(str(amount))
            for currency, amount in (snapshot.cash_balances or {}).items()
        }
        for currency in sorted(set(derived_cash) | set(snapshot_cash)):
            derived = derived_cash.get(currency, Decimal("0"))
            declared = snapshot_cash.get(currency, Decimal("0"))
            status = "MATCH" if abs(derived - declared) <= CASH_TOLERANCE else "MISMATCH"
            if status != "MATCH":
                cash_mismatches += 1
            cash_diffs.append({
                "currency": currency,
                "snapshot_balance": float(declared),
                "derived_balance": float(derived),
                "delta": float(derived - declared),
                "status": status,
            })

    # 整体状态 = 持仓一致 + 重放自洽 + 现金一致（分范围快照不比现金，
    # 此时 MATCHED 语义为"范围内持仓一致"，前端按 scope 显示"持仓一致"）。
    # 现金参与判定是防"假绿"：现金未闭合的账户不能显示为整体对账完成。
    matched = (
        position_mismatches == 0
        and not inconsistent
        and cash_mismatches == 0
    )
    diff_detail = {
        "as_of": as_of.isoformat(),
        "positions": position_diffs,
        "cash": cash_diffs,
        "replay_inconsistent": inconsistent,
        "summary": {
            "scope": getattr(snapshot, "statement_scope", None),
            "cash_compared": cash_compared,
            "excluded_symbols": [
                {"symbol": symbol, "market": market}
                for symbol, market in applied_exclusions
            ],
            "position_count": len(position_diffs),
            "position_mismatches": position_mismatches,
            "cash_count": len(cash_diffs),
            "cash_mismatches": cash_mismatches,
            "replay_inconsistent_count": len(inconsistent),
            "matched": matched,
        },
        "methodology_notes": METHODOLOGY_NOTES,
    }
    return ("MATCHED" if matched else "MISMATCHED"), diff_detail


def run_and_store_compare(db: Session, snapshot, *, commit: bool = True):
    """比对并把结果写回快照行（status / diff_detail / compared_at）。"""
    status, diff_detail = compare_snapshot(db, snapshot)
    snapshot.status = status
    snapshot.diff_detail = diff_detail
    snapshot.compared_at = datetime.now(timezone.utc)
    if commit:
        db.commit()
        db.refresh(snapshot)
    else:
        db.flush()
    return snapshot
