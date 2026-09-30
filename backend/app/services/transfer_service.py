"""账户间转仓（TRANSFER_OUT / TRANSFER_IN 互指交易对），由 api/transactions 下沉（#283）。

不产生任何盈亏或现金流；账户为 None 表示「未指定账户」桶。成本基础随转出桶的平均成本迁移。

校验策略：对完整时间线做两次严格的按账户重放（插入前基线 + 插入后）——同时覆盖历史日期
转仓（transfer_date 当天转出账户必须真有足够数量）与转仓和后续交易的冲突（转出后未来
卖出会超卖）。与所有时间线写入口共用事务级 advisory lock 串行化并发。

本模块**不提交**：交易对写入与派生持仓重算由调用方在同一事务内提交或回滚。
"""

from datetime import date
from decimal import Decimal
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from ..models.transaction import Transaction
from .holding_service import (
    AccountReplayError,
    lock_security_timeline,
    recalculate_holdings,
    replay_account_buckets,
)


class TransferConflict(ValueError):
    """转仓与现有账本冲突（无持仓、成本未知、归属不一致、插入后重放失败）——API 映射 409。"""


def create_transfer_pair(
    db: Session,
    user_id: int,
    *,
    symbol: str,
    market: str,
    quantity: Decimal,
    from_broker_account_id: Optional[int],
    to_broker_account_id: Optional[int],
    transfer_date: date,
    notes: Optional[str] = None,
) -> Tuple[Transaction, Transaction]:
    """写入转仓交易对并重算持仓（不提交），返回 (转出腿, 转入腿)。"""
    lock_security_timeline(db, user_id, symbol, market)

    # 基线：现有交易的账户归属必须自洽，否则转仓建立在降级合并桶上没有意义
    try:
        baseline = replay_account_buckets(db, user_id, symbol, market)
    except AccountReplayError as exc:
        raise TransferConflict(f"现有交易的账户归属不一致，请先修正数据再转仓：{exc}") from exc

    source_state = baseline.get(from_broker_account_id)
    if source_state is None or source_state["quantity"] <= 0:
        raise TransferConflict("转出账户当前无该证券持仓")
    # 成本未知的桶（期初建仓/转托管转入）转仓会生成零价腿，把「成本未知」伪装成
    # 零成本搬进目标账户（估计标记随之丢失）。先补录成本再转（#174）
    if source_state.get("unknown_cost_quantity", Decimal("0")) > 0 or source_state["avg_cost"] <= 0:
        raise TransferConflict(
            "转出账户桶含成本未知的持仓（期初建仓/转托管转入），转仓会生成零价腿；"
            "请先在公司行动页补录成本再转仓"
        )

    common = {
        "user_id": user_id,
        "symbol": symbol,
        "name": source_state["name"] or symbol,
        "market": market,
        "quantity": quantity,
        # 转仓不产生盈亏；price 仅作展示口径，记录转出时的平均成本
        "price": source_state["avg_cost"],
        "fee": Decimal("0"),
        "transaction_date": transfer_date,
        "currency": source_state["currency"],
        "notes": notes,
    }
    out_leg = Transaction(
        **common, broker_account_id=from_broker_account_id, transaction_type="TRANSFER_OUT"
    )
    db.add(out_leg)
    db.flush()
    in_leg = Transaction(
        **common,
        broker_account_id=to_broker_account_id,
        transaction_type="TRANSFER_IN",
        linked_transaction_id=out_leg.id,
    )
    db.add(in_leg)
    db.flush()
    out_leg.linked_transaction_id = in_leg.id
    db.flush()

    # 插入后重放：按 transfer_date 落在真实时间线里校验，转出账户当日数量不足或与后续
    # 交易冲突都会在这里被拒绝
    try:
        replay_account_buckets(db, user_id, symbol, market)
    except AccountReplayError as exc:
        raise TransferConflict(f"转仓无法成立：{exc}") from exc

    recalculate_holdings(db, user_id, symbol, market, commit=False)
    return out_leg, in_leg
