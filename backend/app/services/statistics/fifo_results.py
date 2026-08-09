"""按 (symbol, market) 重放 FIFO 的编排入口（内核在 portfolio/fifo）。"""

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from ...core.logging import get_app_logger
from ...models.corporate_action import CorporateAction
from ...models.transaction import Transaction
from ..portfolio.fifo import (
    FIFO_ACTION_TYPES,
    AccountFifoFallback,
    calculate_fifo_pnl,
    merge_account_fifo_results,
    replay_fifo_multi_account,
)

logger = get_app_logger(__name__)


def fifo_results_for_user(
    db: Session,
    user_id: int,
    symbols_markets: Optional[set[Tuple[str, str]]] = None,
    *,
    transactions: Optional[List[Transaction]] = None,
    corporate_actions: Optional[List[CorporateAction]] = None,
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Replay FIFO per (symbol, market).

    Callers that already hold the user's transactions/corporate actions can pass
    them in to avoid re-querying (issue #49); per-key event ordering is
    re-established inside the FIFO kernel, so input order does not matter.
    """
    if transactions is None:
        transactions = (
            db.query(Transaction)
            .filter(Transaction.user_id == user_id)
            .order_by(
                Transaction.symbol,
                Transaction.market,
                Transaction.transaction_date,
                Transaction.id,
            )
            .all()
        )

    if corporate_actions is None:
        corporate_actions = (
            db.query(CorporateAction)
            .filter(
                CorporateAction.user_id == user_id,
                CorporateAction.action_type.in_(FIFO_ACTION_TYPES),
            )
            .order_by(
                CorporateAction.symbol,
                CorporateAction.market,
                CorporateAction.ex_date,
                CorporateAction.id,
            )
            .all()
        )
    else:
        corporate_actions = [
            action for action in corporate_actions if action.action_type in FIFO_ACTION_TYPES
        ]

    transactions_by_key = defaultdict(list)
    actions_by_key = defaultdict(list)

    for txn in transactions:
        key = (txn.symbol, txn.market)
        if symbols_markets is None or key in symbols_markets:
            transactions_by_key[key].append(txn)

    for action in corporate_actions:
        key = (action.symbol, action.market)
        if symbols_markets is None or key in symbols_markets:
            actions_by_key[key].append(action)

    keys = set(transactions_by_key.keys()) | set(actions_by_key.keys())
    if symbols_markets is not None:
        keys |= symbols_markets

    return {
        key: security_fifo(
            key[0], key[1], transactions_by_key.get(key, []), actions_by_key.get(key, [])
        )
        for key in keys
    }


def security_fifo(symbol, market, transactions, corporate_actions):
    """单证券 FIFO：多账户/含转仓时按账户重放后聚合，矛盾时降级合并重放。

    降级条件与 holding_service 的合并桶降级一致；合并重放中转仓是恒等操作，
    数字与账户化之前完全相同。
    """
    accounts = {txn.broker_account_id for txn in transactions}
    has_transfer = any(
        txn.transaction_type in ("TRANSFER_OUT", "TRANSFER_IN") for txn in transactions
    )
    if len(accounts) <= 1 and not has_transfer:
        return calculate_fifo_pnl(symbol, market, transactions, corporate_actions)
    try:
        account_results = replay_fifo_multi_account(symbol, market, transactions, corporate_actions)
        return merge_account_fifo_results(symbol, market, account_results)
    except AccountFifoFallback as exc:
        logger.warning(
            "Account-scoped FIFO fell back to merged replay for %s(%s): %s",
            symbol,
            market,
            exc,
        )
        return calculate_fifo_pnl(symbol, market, transactions, corporate_actions)
