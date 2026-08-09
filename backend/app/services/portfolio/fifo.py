"""FIFO 队列重放与已实现盈亏（纯内核，无 DB 依赖）。

交易与公司行动以鸭子类型传入（ORM 行或任何带同名属性的对象）；
每个 (symbol, market) 的事件排序在内部重建，输入顺序无关。
"""

import logging
from collections import deque
from decimal import Decimal
from typing import Any, Dict, List, Sequence, Tuple

from .semantics import action_has_ratio, bonus_share_factor, split_share_factor

FIFO_ACTION_TYPES = [
    'STOCK_DIVIDEND',
    'BONUS_ISSUE',
    'STOCK_SPLIT',
    'REVERSE_SPLIT',
    'RIGHTS_ISSUE',
]

# 与 core.logging.get_app_logger 的命名约定一致（去掉 app. 前缀），
# 但直接用标准库以保持内核零应用依赖。
logger = logging.getLogger("investment_tracker.services.portfolio.fifo")


def _as_decimal(value: Any) -> Decimal:
    """兼容 Decimal 与 float 入参；float 经 str 中转以免引入二进制表示误差。"""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def empty_fifo_result(symbol: str, market: str) -> Dict[str, Any]:
    return {
        'symbol': symbol,
        'market': market,
        'realized_pnl': 0.0,
        'sold_cost': 0.0,
        'current_holdings_cost': 0.0,
        'closed_trades': [],
        'buy_queue': [],
        'invalid_sell_events': [],
    }


def calculate_fifo_pnl(
    symbol: str,
    market: str,
    transactions: Sequence[Any],
    corporate_actions: Sequence[Any],
) -> Dict[str, Any]:
    events = []

    for txn in transactions:
        priority = 1 if txn.transaction_type == 'BUY' else 2
        events.append({
            'type': txn.transaction_type,
            'date': txn.transaction_date,
            # 直接存 Decimal：此前是 float(...) 再 Decimal(str(...)) 转回，
            # 多一次 float 往返；而多账户路径（replay_fifo_multi_account）
            # 一直是直接从 ORM 取 Decimal——同一证券在"单账户无转仓"与
            # "多账户"两个分支间切换时，数字会在小数位上漂移。
            'price': Decimal(str(txn.price)),
            'quantity': Decimal(str(txn.quantity)),
            'fee': Decimal(str(txn.fee or 0)),
            'priority': priority,
            'id': txn.id
        })

    for action in corporate_actions:
        events.append({
            'type': action.action_type,
            'date': action.ex_date,
            'data': action,
            'priority': 0,
            'id': action.id
        })

    events.sort(key=lambda x: (x['date'], x['priority'], x['id']))

    buy_queue = deque()
    realized_pnl = Decimal(0)
    sold_cost = Decimal(0)
    invalid_sell_events = []
    closed_trades = []

    for event in events:
        event_type = event['type']

        if event_type == 'BUY':
            total_cost = event['price'] * event['quantity'] + event['fee']
            cost_per_share = total_cost / event['quantity']

            buy_queue.append({
                'price': cost_per_share,
                'quantity': event['quantity'],
                'total_cost': total_cost,
                'date': event['date']
            })

        elif event_type == 'SELL':
            sell_qty = event['quantity']
            available_qty = sum(record['quantity'] for record in buy_queue)
            if sell_qty > available_qty:
                invalid_event = {
                    'transaction_id': event['id'],
                    'date': event['date'].isoformat(),
                    'symbol': symbol,
                    'market': market,
                    'sell_quantity': float(sell_qty),
                    'available_quantity': float(available_qty),
                }
                invalid_sell_events.append(invalid_event)
                logger.warning(
                    "Skipping oversold FIFO transaction id=%s symbol=%s market=%s "
                    "sell_quantity=%s available_quantity=%s",
                    event['id'],
                    symbol,
                    market,
                    sell_qty,
                    available_qty,
                )
                continue
            original_sell_qty = sell_qty
            sell_proceeds = event['price'] * sell_qty - event['fee']
            matched_cost = Decimal(0)
            earliest_buy_date = None

            while sell_qty > 0 and buy_queue:
                buy_record = buy_queue[0]
                if earliest_buy_date is None:
                    earliest_buy_date = buy_record['date']

                if buy_record['quantity'] <= sell_qty:
                    matched_qty = buy_record['quantity']
                    matched_cost += buy_record['total_cost']
                    buy_queue.popleft()
                else:
                    matched_qty = sell_qty
                    ratio = matched_qty / buy_record['quantity']
                    batch_cost = buy_record['total_cost'] * ratio
                    matched_cost += batch_cost

                    buy_record['quantity'] -= matched_qty
                    buy_record['total_cost'] -= batch_cost

                sell_qty -= matched_qty

            batch_pnl = sell_proceeds - matched_cost
            realized_pnl += batch_pnl
            sold_cost += matched_cost

            # One record per closing trade (per SELL fully matched against buy
            # lots), so trade-quality metrics can be measured per trade rather
            # than per symbol (issue #43).
            holding_days = None
            if earliest_buy_date is not None:
                holding_days = (event['date'] - earliest_buy_date).days
            closed_trades.append({
                'symbol': symbol,
                'market': market,
                'date': event['date'].isoformat(),
                'quantity': float(original_sell_qty),
                'proceeds': float(sell_proceeds),
                'matched_cost': float(matched_cost),
                'realized_pnl': float(batch_pnl),
                'holding_days': holding_days,
            })

        elif event_type in ['STOCK_DIVIDEND', 'BONUS_ISSUE']:
            action = event['data']
            queue_quantity = sum(record['quantity'] for record in buy_queue)
            factor = bonus_share_factor(action, queue_quantity)
            if factor is not None:
                for buy_record in buy_queue:
                    buy_record['quantity'] *= factor
                    buy_record['price'] = buy_record['total_cost'] / buy_record['quantity']

        elif event_type == 'RIGHTS_ISSUE':
            action = event['data']
            if action.subscription_quantity and action.subscription_price:
                qty = Decimal(str(action.subscription_quantity))
                price = Decimal(str(action.subscription_price))
                total_cost = price * qty

                if action.subscription_amount:
                    total_cost = Decimal(str(action.subscription_amount))

                buy_queue.append({
                    'price': total_cost / qty,
                    'quantity': qty,
                    'total_cost': total_cost,
                    'date': action.ex_date
                })

        elif event_type in ['STOCK_SPLIT', 'REVERSE_SPLIT']:
            action = event['data']
            queue_quantity = sum(record['quantity'] for record in buy_queue)
            factor = split_share_factor(action, queue_quantity)
            if factor is not None:
                for buy_record in buy_queue:
                    buy_record['quantity'] *= factor
                    buy_record['price'] /= factor

    current_holdings_cost = sum(b['total_cost'] for b in buy_queue)

    return {
        'symbol': symbol,
        'market': market,
        'realized_pnl': float(realized_pnl),
        'sold_cost': float(sold_cost),
        'current_holdings_cost': float(current_holdings_cost),
        'closed_trades': closed_trades,
        'buy_queue': [
            {
                'price': float(b['price']),
                'quantity': float(b['quantity']),
                'total_cost': float(b['total_cost']),
                'date': str(b['date'])
            }
            for b in buy_queue
        ],
        'invalid_sell_events': invalid_sell_events,
    }


def fifo_data_quality(
    fifo_results: Dict[Tuple[str, str], Dict[str, Any]]
) -> Dict[str, Any]:
    invalid_sell_events = [
        event
        for result in fifo_results.values()
        for event in result.get('invalid_sell_events', [])
    ]
    warnings: List[str] = []
    if invalid_sell_events:
        warnings.append(
            "检测到历史卖出数量超过当时可用持仓；相关卖出未计入 FIFO 收益，"
            "请修正交易记录后再使用统计结果。"
        )
    return {
        'warnings': warnings,
        'invalid_sell_event_count': len(invalid_sell_events),
        'invalid_sell_events': invalid_sell_events[:50],
    }


class AccountFifoFallback(Exception):
    """按账户 FIFO 重放遇到归属矛盾；调用方应退回用户级合并重放。

    触发条件与 holding_service 的合并降级保持一致：桶内卖出/转出超过桶内
    数量、无法唯一归属的绝对数量公司行动、悬空转仓腿。
    """


_MULTI_TXN_TYPES = {"BUY", "SELL", "TRANSFER_OUT", "TRANSFER_IN"}

_BIG_ID = 10**18


def _multi_txn_order(txn) -> Tuple[int, int, int]:
    """同日顺序：BUY → 转仓对（按转出腿 id 分组、对内 OUT 紧邻 IN）→ SELL。

    与 holding_service._txn_replay_order 语义一致，同日链式转仓
    （A→B 后 B→C）按对的创建顺序依次执行。
    """
    txn_type = txn.transaction_type
    txn_id = txn.id if txn.id is not None else _BIG_ID
    if txn_type == "BUY":
        return (1, txn_id, 0)
    if txn_type == "TRANSFER_OUT":
        return (2, txn_id, 0)
    if txn_type == "TRANSFER_IN":
        pair_id = getattr(txn, "linked_transaction_id", None)
        return (2, pair_id if pair_id is not None else txn_id, 1)
    return (3, txn_id, 0)  # SELL


def _queue_quantity(queue) -> Decimal:
    return sum((lot['quantity'] for lot in queue), Decimal("0"))


def _pop_lots(queue, quantity: Decimal):
    """按 FIFO 从队首弹出总量 quantity 的批次（可拆分），保留原始日期与成本。"""
    popped = []
    remaining = quantity
    while remaining > 0 and queue:
        lot = queue[0]
        if lot['quantity'] <= remaining:
            popped.append(lot)
            queue.popleft()
            remaining -= lot['quantity']
        else:
            ratio = remaining / lot['quantity']
            split_cost = lot['total_cost'] * ratio
            popped.append({
                'price': lot['price'],
                'quantity': remaining,
                'total_cost': split_cost,
                'date': lot['date'],
            })
            lot['quantity'] -= remaining
            lot['total_cost'] -= split_cost
            remaining = Decimal("0")
    return popped


def replay_fifo_multi_account(
    symbol: str,
    market: str,
    transactions: Sequence[Any],
    corporate_actions: Sequence[Any],
) -> Dict[Any, Dict[str, Any]]:
    """按账户桶重放 FIFO，返回 {broker_account_id: 单账户结果}。

    转仓对把批次（保留原始买入日期与成本）从转出账户队列迁到转入账户队列，
    不产生已实现盈亏。比例类公司行动作用于所有账户队列；绝对数量类作用于
    自身账户或唯一持仓账户，无法唯一归属时抛 AccountFifoFallback。
    """
    events = []
    for txn in transactions:
        if txn.transaction_type not in _MULTI_TXN_TYPES:
            continue
        events.append({
            'kind': 'txn',
            'date': txn.transaction_date,
            'order': _multi_txn_order(txn),
            'data': txn,
        })
    for action in corporate_actions:
        events.append({
            'kind': 'action',
            'date': action.ex_date,
            'order': (0, action.id, 0),
            'data': action,
        })
    events.sort(key=lambda e: (e['date'],) + e['order'])

    queues: Dict[Any, deque] = {}
    realized: Dict[Any, Decimal] = {}
    sold_cost: Dict[Any, Decimal] = {}
    closed_trades: Dict[Any, list] = {}
    pending_transfers: Dict[Any, list] = {}

    def queue_of(account_id):
        if account_id not in queues:
            queues[account_id] = deque()
            realized[account_id] = Decimal("0")
            sold_cost[account_id] = Decimal("0")
            closed_trades[account_id] = []
        return queues[account_id]

    def resolve_action_account(action):
        account_id = getattr(action, "broker_account_id", None)
        if account_id is not None:
            return account_id
        holders = [aid for aid, q in queues.items() if _queue_quantity(q) > 0]
        if len(holders) == 1:
            return holders[0]
        if not holders:
            return None
        raise AccountFifoFallback(
            f"corporate action id={action.id} ({action.action_type}) "
            f"cannot be attributed to a single account for {symbol} ({market})"
        )

    for event in events:
        data = event['data']
        if event['kind'] == 'txn':
            txn_type = data.transaction_type
            account_id = data.broker_account_id

            if txn_type == "BUY":
                queue = queue_of(account_id)
                quantity = Decimal(str(data.quantity))
                total_cost = quantity * Decimal(str(data.price)) + Decimal(str(data.fee or 0))
                queue.append({
                    'price': total_cost / quantity,
                    'quantity': quantity,
                    'total_cost': total_cost,
                    'date': data.transaction_date,
                })

            elif txn_type == "SELL":
                queue = queue_of(account_id)
                sell_qty = Decimal(str(data.quantity))
                if sell_qty > _queue_quantity(queue):
                    raise AccountFifoFallback(
                        f"sell {sell_qty} exceeds account bucket quantity "
                        f"{_queue_quantity(queue)} for {symbol} ({market})"
                    )
                proceeds = sell_qty * Decimal(str(data.price)) - Decimal(str(data.fee or 0))
                earliest = queue[0]['date'] if queue else None
                lots = _pop_lots(queue, sell_qty)
                matched_cost = sum((lot['total_cost'] for lot in lots), Decimal("0"))
                pnl = proceeds - matched_cost
                realized[account_id] += pnl
                sold_cost[account_id] += matched_cost
                holding_days = (
                    (data.transaction_date - earliest).days if earliest is not None else None
                )
                # 保持 Decimal：float 化统一在 merge_account_fifo_results 出口
                closed_trades[account_id].append({
                    'symbol': symbol,
                    'market': market,
                    'date': data.transaction_date.isoformat(),
                    'quantity': sell_qty,
                    'proceeds': proceeds,
                    'matched_cost': matched_cost,
                    'realized_pnl': pnl,
                    'holding_days': holding_days,
                })

            elif txn_type == "TRANSFER_OUT":
                queue = queue_of(account_id)
                move_qty = Decimal(str(data.quantity))
                if move_qty > _queue_quantity(queue):
                    raise AccountFifoFallback(
                        f"transfer out {move_qty} exceeds account bucket quantity "
                        f"{_queue_quantity(queue)} for {symbol} ({market})"
                    )
                pending_transfers[data.id] = _pop_lots(queue, move_qty)

            elif txn_type == "TRANSFER_IN":
                lots = pending_transfers.pop(data.linked_transaction_id, None)
                if lots is None:
                    raise AccountFifoFallback(
                        f"transfer in id={data.id} has no processed linked transfer-out"
                    )
                queue = queue_of(account_id)
                queue.extend(lots)
                # 目标队列保持按买入日期的 FIFO 顺序（迁移批次保留原始日期）。
                ordered = sorted(queue, key=lambda lot: lot['date'])
                queue.clear()
                queue.extend(ordered)

        else:
            action = data
            action_type = action.action_type
            if action_type in ("STOCK_DIVIDEND", "BONUS_ISSUE", "STOCK_SPLIT", "REVERSE_SPLIT"):
                if action_has_ratio(action):
                    targets = [aid for aid, q in queues.items() if _queue_quantity(q) > 0]
                else:
                    target = resolve_action_account(action)
                    targets = (
                        [target]
                        if target in queues and _queue_quantity(queues[target]) > 0
                        else []
                    )
                for aid in targets:
                    queue = queues[aid]
                    quantity = _queue_quantity(queue)
                    if action_type in ("STOCK_DIVIDEND", "BONUS_ISSUE"):
                        factor = bonus_share_factor(action, quantity)
                    else:
                        factor = split_share_factor(action, quantity)
                    if factor is not None:
                        for lot in queue:
                            lot['quantity'] *= factor
                            lot['price'] = lot['total_cost'] / lot['quantity']

            elif action_type == "RIGHTS_ISSUE":
                if action.subscription_quantity and action.subscription_price:
                    target = resolve_action_account(action)
                    queue = queue_of(target)
                    qty = Decimal(str(action.subscription_quantity))
                    price = Decimal(str(action.subscription_price))
                    total_cost = (
                        Decimal(str(action.subscription_amount))
                        if action.subscription_amount
                        else price * qty
                    )
                    queue.append({
                        'price': total_cost / qty,
                        'quantity': qty,
                        'total_cost': total_cost,
                        'date': action.ex_date,
                    })

    if pending_transfers:
        raise AccountFifoFallback(
            f"unmatched transfer-out legs for {symbol} ({market}): "
            f"ids={sorted(pending_transfers)}"
        )

    # 账户级结果**保持 Decimal**：它们只是 merge_account_fifo_results 的中间量，
    # 在这里转 float 会让精度在聚合之前就丢掉，之后再 Decimal(str(...)) 也补不回来
    # ——NUMERIC(18,8) 量级下单账户与多账户两条分支会得出不同的 sold_cost
    # （实测 8931992295.31575055 + 1641890924.22944734 差 1.9e-06）。
    # float 化统一发生在用户级出口（merge_account_fifo_results）。
    results: Dict[Any, Dict[str, Any]] = {}
    for account_id, queue in queues.items():
        results[account_id] = {
            'symbol': symbol,
            'market': market,
            'broker_account_id': account_id,
            'realized_pnl': realized[account_id],
            'sold_cost': sold_cost[account_id],
            'current_holdings_cost': sum(
                (lot['total_cost'] for lot in queue), Decimal("0")
            ),
            'closed_trades': closed_trades[account_id],
            'buy_queue': [
                {
                    'price': lot['price'],
                    'quantity': lot['quantity'],
                    'total_cost': lot['total_cost'],
                    'date': str(lot['date']),
                }
                for lot in queue
            ],
            'invalid_sell_events': [],
        }
    return results


def merge_account_fifo_results(
    symbol: str,
    market: str,
    account_results: Dict[Any, Dict[str, Any]],
) -> Dict[str, Any]:
    """把账户级 FIFO 结果聚合为用户级证券结果（现有消费方的输入形状）。

    这里是 Decimal→float 的**唯一出口**：replay_fifo_multi_account 交出来的
    账户级结果全程保持 Decimal，聚合完再一次性转 float。此前账户级就先转了
    float，精度在聚合之前已丢，导致单账户与多账户两条分支对同一份数据得出
    不同的 sold_cost。
    """
    merged = empty_fifo_result(symbol, market)
    closed = []
    lots = []
    totals = {
        'realized_pnl': Decimal("0"),
        'sold_cost': Decimal("0"),
        'current_holdings_cost': Decimal("0"),
    }
    for result in account_results.values():
        for field in totals:
            totals[field] += _as_decimal(result[field])
        closed.extend(result['closed_trades'])
        lots.extend(result['buy_queue'])
    for field, value in totals.items():
        merged[field] = float(value)
    merged['closed_trades'] = [
        {**trade, **{
            key: float(_as_decimal(trade[key]))
            for key in ('proceeds', 'matched_cost', 'realized_pnl', 'quantity')
            if key in trade
        }}
        for trade in sorted(closed, key=lambda t: t['date'])
    ]
    merged['buy_queue'] = [
        {**lot, **{
            key: float(_as_decimal(lot[key]))
            for key in ('price', 'quantity', 'total_cost')
            if key in lot
        }}
        for lot in sorted(lots, key=lambda lot: lot['date'])
    ]
    return merged
