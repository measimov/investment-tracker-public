from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Tuple
from ..core.logging import get_app_logger
from ..models.transaction import Transaction
from ..models.holding import Holding
from ..models.corporate_action import CorporateAction
from .portfolio.semantics import (
    QUANTITY_ACTION_TYPES,
    action_has_ratio,
    apply_action_quantity,
    bonus_share_factor,
    split_share_factor,
)

logger = get_app_logger(__name__)

# price_updated_at 可能为 NULL（视为最旧）；与 tz-aware 值比较需同为 aware。
_DATETIME_MIN = datetime.min.replace(tzinfo=timezone.utc)


class _AccountReplayFallback(Exception):
    """按账户重放遇到归属矛盾（跨账户卖出/无法归属的绝对数量行动），退回合并桶。"""


# 公开别名：API 层转仓校验需要显式感知"账户重放不成立"。
AccountReplayError = _AccountReplayFallback


def _acquire_advisory_lock(db: Session, key: str) -> None:
    db.execute(
        sa_text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": key},
    )


def lock_security_timeline(db: Session, user_id: int, symbol: str, market: str) -> None:
    """事务级 advisory lock：串行化同一 (user, symbol, market) 时间线的全部写入。

    所有会改变该证券交易/公司行动时间线的写入口（交易 CRUD、转仓、公司行动
    CUD、导入重算）必须在读取-校验-写入之前取得此锁；锁随事务结束自动释放，
    同一事务内可重入。用 advisory lock 而非行锁，是因为需要串行化的是"时间线"
    这一逻辑对象——持仓行可能不存在（首笔买入）或被删除重建。
    """
    _acquire_advisory_lock(db, f"security-timeline:{user_id}:{symbol}:{market}")


def lock_record(db: Session, namespace: str, record_id: int) -> None:
    """事务级记录锁：update/delete 必须先按记录 id 串行化，再在锁内重读该行，
    用刷新后的键取时间线锁——否则等锁的 session 会拿着并发修改前的旧
    symbol/market 重算错误的时间线。

    锁层级纪律（防死锁）：先记录锁（按 id 升序）、后时间线锁（按键排序）；
    任何路径不得持时间线锁再取记录锁。
    """
    _acquire_advisory_lock(db, f"{namespace}:{record_id}")


def replay_transactions_per_account(transactions, corporate_actions, symbol: str, market: str):
    """对已加载的交易/公司行动做严格按账户重放（抛 AccountReplayError）。

    纯内存操作：调用方负责查询与（可选的）日期截断——对账闭环用它做
    as-of 快照日的持仓重放，转仓校验用它做全时间线重放。
    """
    events = [
        {'type': 'transaction', 'date': txn.transaction_date, 'data': txn}
        for txn in transactions
    ] + [
        {'type': 'corporate_action', 'date': action.ex_date, 'data': action}
        for action in corporate_actions
    ]
    events.sort(key=_event_sort_key)
    return _replay_events(events, symbol, market, per_account=True)


def replay_transactions_merged(transactions, corporate_actions, symbol: str, market: str):
    """合并单桶重放（per_account=False）：归属矛盾时的降级口径。

    与 replay_transactions_per_account 同一事件构建与排序；返回 NULL 单桶状态。
    数量总和仍然可信（含送转/拆股因子），只是无法按账户拆分。
    """
    events = [
        {'type': 'transaction', 'date': txn.transaction_date, 'data': txn}
        for txn in transactions
    ] + [
        {'type': 'corporate_action', 'date': action.ex_date, 'data': action}
        for action in corporate_actions
    ]
    events.sort(key=_event_sort_key)
    return _replay_events(events, symbol, market, per_account=False)


def replay_account_buckets(db: Session, user_id: int, symbol: str, market: str):
    """全量按账户重放（只读，不落库），返回 {account_id: 桶状态}。

    供转仓创建/删除前的严格校验：与 recalculate_holdings 使用同一事件构建
    与排序，但归属矛盾时不降级而是抛 AccountReplayError，让调用方拒绝写入。
    同一会话中未提交的 flush 行也会被计入（校验"插入转仓对之后"的时间线）。
    """
    transactions = db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.symbol == symbol,
        Transaction.market == market
    ).all()
    corporate_actions = db.query(CorporateAction).filter(
        CorporateAction.user_id == user_id,
        CorporateAction.symbol == symbol,
        CorporateAction.market == market
    ).all()
    return replay_transactions_per_account(transactions, corporate_actions, symbol, market)


# 尚未落库的事件在同日排序里的 id 替身：必须**大于**任何持久化 id，
# 这样"本批最新"排在既有交易之后——与 flush 拿到真 id 后的次序一致。
# 券商导入预览的替身交易共用它（broker_import_common），用 0 会让预览与
# 正式导入指向不同的首笔超卖。
UNPERSISTED_SORT_ID = 10**18


def _txn_replay_order(txn):
    """同日事件的重放顺序键：(类型组, 分组id, 腿序)。

    转仓按"对"分组排序（分组 id = 转出腿 id）：对内 OUT 紧邻其配对 IN，
    对间按创建顺序排列——同日链式转仓（A→B 后 B→C）才能正确重放，
    而不是所有 OUT 挤在所有 IN 之前。BUY 在全部转仓之前、SELL 在之后。
    """
    txn_type = txn.transaction_type
    txn_id = txn.id if txn.id is not None else UNPERSISTED_SORT_ID
    if txn_type == "BUY":
        return (0, txn_id, 0)
    if txn_type == "TRANSFER_OUT":
        return (1, txn_id, 0)
    if txn_type == "TRANSFER_IN":
        pair_id = getattr(txn, "linked_transaction_id", None)
        return (1, pair_id if pair_id is not None else txn_id, 1)
    return (2, txn_id, 0)  # SELL 与未知类型


def _event_sort_key(event):
    if event['type'] == 'transaction':
        return (event['date'], 1) + _txn_replay_order(event['data'])
    return (event['date'], 0, 0, event['data'].id, 0)


def validate_no_oversell(transactions):
    """Raise ValueError if ordered transactions sell/transfer more than available.

    调用方传入的列表通常已按账户桶过滤（_validate_transaction_sequence 按
    candidate 的 broker_account_id 过滤）；转仓在桶视角就是数量的增减：
    TRANSFER_OUT 等同卖出数量、TRANSFER_IN 等同买入数量。
    """
    quantity = Decimal("0")
    for txn in sorted(
        transactions,
        key=lambda item: (item.transaction_date,) + _txn_replay_order(item),
    ):
        txn_quantity = Decimal(str(txn.quantity))
        if txn.transaction_type in ("BUY", "TRANSFER_IN"):
            quantity += txn_quantity
        elif txn.transaction_type in ("SELL", "TRANSFER_OUT"):
            if txn_quantity > quantity:
                raise ValueError(
                    f"Sell quantity {txn_quantity} exceeds available quantity {quantity} "
                    f"for {txn.symbol} ({txn.market}) on {txn.transaction_date}"
                )
            quantity -= txn_quantity


def replay_account_quantities(events, *, on_oversell=None):
    """单账户桶的数量重放——券商导入器账户预检的唯一实现（第四处重放）。

    events：调用方**已排好序**的 Transaction / CorporateAction 混合序列。
    同日 tie-break 因券商而异（招商按流水号/合同号/行号，东财买前卖后），
    刻意留在调用方；本函数只负责数量语义。

    on_oversell(event, available, needed)：减仓超出可用数量时回调。抛出券商
    各自的中文错误 = 严格模式；不抛 = 宽松模式（东财对账口径依赖这一分支，
    数量继续减、可为负）。

    转仓在单账户桶视角就是数量的增减（口径同 validate_no_oversell）：
    TRANSFER_IN 等同买入、TRANSFER_OUT 等同卖出。按 broker_account 过滤后，
    转入腿往往是本账户获得该数量的唯一记录，忽略它会凭空少一整笔。
    """
    quantities: Dict[Tuple[str, str], Decimal] = {}
    for event in events:
        key = (event.symbol, event.market)
        quantity = quantities.get(key, Decimal("0"))
        txn_type = getattr(event, "transaction_type", None)
        if txn_type is None:  # CorporateAction
            quantity = apply_action_quantity(event, quantity)
        elif txn_type in ("BUY", "TRANSFER_IN"):
            quantity += abs(Decimal(str(event.quantity)))
        elif txn_type in ("SELL", "TRANSFER_OUT"):
            needed = abs(Decimal(str(event.quantity)))
            if needed > quantity and on_oversell is not None:
                on_oversell(event, quantity, needed)
            quantity -= needed
        # 其他交易类型不改变数量
        quantities[key] = quantity
    return quantities


def load_account_quantity_actions(
    db: Session,
    *,
    user_id: int,
    broker_account_id: int,
    keys,
    through_date=None,
):
    """单账户预检可见的数量类公司行动。

    可见性规则对齐真实重放（_replay_events + _resolve_action_bucket）：

    - 比例类（distribution_ratio / split_ratio）按每股生效、作用于**所有**
      持仓桶，因此不按账户过滤。分红同步接受送转建议时刻意写
      broker_account_id=None（送转是比例行动，每公告仅一条账户无关建议），
      按账户过滤会让这条最常见的路径在预检里凭空消失。
    - 绝对数量类（shares_received / new_shares / 配股）自带账户的只认本账户；
      **NULL 账户的同样计入**——内核 _resolve_action_bucket 会把它归到唯一
      持仓桶，预检跳过就会少算而误拒整批导入。
    - 明确归属于**其他**账户的绝对数量行动跳过。

    单账户视角只可能多算不可能少算：多个账户同时持有该证券时内核会抛
    AccountReplayError 降级，而预检宁可放行（漏报）也不误拒（错杀整批导入）。
    """
    if not keys:
        return []
    query = db.query(CorporateAction).filter(
        CorporateAction.user_id == user_id,
        CorporateAction.action_type.in_(QUANTITY_ACTION_TYPES),
        CorporateAction.symbol.in_({symbol for symbol, _ in keys}),
    )
    if through_date is not None:
        query = query.filter(CorporateAction.ex_date <= through_date)
    return [
        action for action in query.all()
        if (action.symbol, action.market) in keys
        and (
            action_has_ratio(action)
            or action.broker_account_id in (broker_account_id, None)
        )
    ]


def recalculate_holdings(
    db: Session,
    user_id: int,
    symbol: str,
    market: str,
    commit: bool = True,
):
    """按账户桶重放特定用户、symbol 和 market 的持仓（交易 + 公司行动）。

    账户语义：
    - 每个 (user, broker_account, symbol, market) 一行持仓；NULL 账户是
      "未指定账户"桶（手工交易）。
    - 比例类公司行动（送股/拆合股的 ratio 字段）按每股比例作用于所有桶；
      绝对数量类（shares_received/new_shares/配股认购）作用于行动自身的账户桶，
      行动未指定账户时归属到唯一持仓桶。
    - 归属矛盾（某桶卖出超过桶内数量、绝对数量行动无法唯一归属）时整个
      (symbol, market) 退回单一合并桶（broker_account_id=NULL）并记录告警——
      这是对账闭环（#4）要消费的数据质量信号，不是静默修复。

    单桶数值算法与历史版本一致：买入摊薄平均成本、卖出保持均价、
    行动按共享语义缩放。
    """
    # 兜底重入锁：API 入口应已提前取锁；importer 等批处理路径在此获得串行化。
    lock_security_timeline(db, user_id, symbol, market)
    # 交易不在这里预排序：下面构建的 events 会经 _event_sort_key 重排，而它是
    # 全序（txn.id 唯一），任何预排序都会被完全覆盖。
    #
    # 此前这里按券商流水号排过一次（并为此多打两次 BrokerFundFlow /
    # IbkrActivityFlow 查询），注释写着"导出文件行序不一定等于实际成交顺序"
    # ——但那份结果从未生效，同日次序一直由 _event_sort_key 的
    # BUY→转仓→SELL 决定。两处注释表达的是互相冲突的意图。
    #
    # 保留 BUY-first 而不是恢复流水序，是因为它才是有正确性价值的那个：
    # FIFO 从队首弹出最老批次，所以同日"先卖后买"在 BUY-first 下成本基础
    # 依然正确；而 BUY-first 还能避免同日回转被误判成超卖。
    transactions = db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.symbol == symbol,
        Transaction.market == market
    ).all()

    # 获取所有公司行动，按除权除息日排序
    corporate_actions = db.query(CorporateAction).filter(
        CorporateAction.user_id == user_id,
        CorporateAction.symbol == symbol,
        CorporateAction.market == market
    ).order_by(CorporateAction.ex_date, CorporateAction.id).all()

    existing_rows = db.query(Holding).filter(
        Holding.user_id == user_id,
        Holding.symbol == symbol,
        Holding.market == market
    ).all()

    # 如果既没有交易也没有公司行动，删除持仓
    if not transactions and not corporate_actions:
        for row in existing_rows:
            db.delete(row)
        if commit:
            db.commit()
        else:
            db.flush()
        return None

    # 合并交易和公司行动，按日期排序
    events = []
    for txn in transactions:
        events.append({
            'type': 'transaction',
            'date': txn.transaction_date,
            'data': txn
        })
    for action in corporate_actions:
        events.append({
            'type': 'corporate_action',
            'date': action.ex_date,
            'data': action
        })

    # 同日默认先处理公司行动，再处理买入，最后处理卖出，避免录入顺序导致成本偏差。
    events.sort(key=_event_sort_key)

    try:
        buckets = _replay_events(events, symbol, market, per_account=True)
    except _AccountReplayFallback as exc:
        logger.warning(
            "Account-scoped replay fell back to merged bucket for user=%s %s(%s): %s",
            user_id, symbol, market, exc,
        )
        buckets = _replay_events(events, symbol, market, per_account=False)

    # 持久化：每个仍有数量的桶一行；其余删除。
    existing_by_account = {row.broker_account_id: row for row in existing_rows}
    surviving = {
        account_id: state
        for account_id, state in buckets.items()
        if state['quantity'] > 0
    }

    # 价格是证券级元数据：旧行被拆桶/合桶删除时，新建行必须继承已有估值，
    # 否则升级重放会丢掉所有手工/缓存价格。取最新一条有价行。
    inherited_price = None
    inherited_price_at = None
    for row in existing_rows:
        if row.current_price is None:
            continue
        if inherited_price is None or (
            (row.price_updated_at or _DATETIME_MIN)
            > (inherited_price_at or _DATETIME_MIN)
        ):
            inherited_price = row.current_price
            inherited_price_at = row.price_updated_at

    for account_id, row in existing_by_account.items():
        if account_id not in surviving:
            db.delete(row)

    persisted = []
    for account_id, state in surviving.items():
        row = existing_by_account.get(account_id)
        if row:
            row.quantity = state['quantity']
            row.avg_cost = state['avg_cost']
            row.total_cost = state['total_cost']
            if state['name']:
                row.name = state['name']
            row.currency = state['currency']
        else:
            row = Holding(
                user_id=user_id,
                broker_account_id=account_id,
                symbol=symbol,
                name=state['name'] or symbol,
                market=market,
                quantity=state['quantity'],
                avg_cost=state['avg_cost'],
                total_cost=state['total_cost'],
                currency=state['currency'],
                current_price=inherited_price,
                price_updated_at=inherited_price_at,
            )
            db.add(row)
        persisted.append(row)

    if commit:
        db.commit()
    else:
        db.flush()

    # 兼容旧返回约定：唯一持仓行时返回该行，多账户行时返回 None。
    if len(persisted) == 1:
        db.refresh(persisted[0])
        return persisted[0]
    return None


def _new_bucket_state():
    return {
        'quantity': Decimal("0"),
        'avg_cost': Decimal("0"),
        'total_cost': Decimal("0"),
        'name': None,
        'currency': "CNY",
    }


# 语义已上提到内核 semantics（原先 holding_service 与 fifo 各有一份拷贝）
_action_has_ratio = action_has_ratio


def _resolve_action_bucket(action, buckets, per_account):
    """绝对数量类行动的目标桶：行动自带账户优先，否则唯一持仓桶。"""
    if not per_account:
        return None
    account_id = getattr(action, "broker_account_id", None)
    if account_id is not None:
        return account_id
    holders = [aid for aid, state in buckets.items() if state['quantity'] > 0]
    if len(holders) == 1:
        return holders[0]
    if not holders:
        # 尚无持仓（如先配股后买入的异常录入）：落到未指定账户桶。
        return None
    raise _AccountReplayFallback(
        f"corporate action id={action.id} ({action.action_type}) 无账户归属且存在多个持仓桶"
    )


def _replay_events(events, symbol, market, *, per_account):
    """时间线单次重放；per_account=False 时所有事件归入 NULL 单桶（历史行为）。

    转仓对（TRANSFER_OUT/TRANSFER_IN）在账户模式下按平均成本迁移数量与成本；
    合并模式下同桶内迁移是恒等操作，直接跳过。
    """
    buckets = {}
    # 已处理转出腿的 (数量, 迁移成本)，等待配对转入腿领取；键为转出交易 id。
    pending_transfers = {}

    def bucket(account_id):
        if account_id not in buckets:
            buckets[account_id] = _new_bucket_state()
        return buckets[account_id]

    for event in events:
        if event['type'] == 'transaction':
            txn = event['data']

            if txn.transaction_type in ("TRANSFER_OUT", "TRANSFER_IN"):
                if not per_account:
                    continue  # 合并单桶内转仓是恒等操作
                if txn.transaction_type == "TRANSFER_OUT":
                    state = bucket(txn.broker_account_id)
                    move_qty = Decimal(str(txn.quantity))
                    if move_qty > state['quantity']:
                        raise _AccountReplayFallback(
                            f"transfer out {move_qty} exceeds bucket quantity "
                            f"{state['quantity']} for {symbol} ({market}) on {txn.transaction_date}"
                        )
                    moved_cost = state['avg_cost'] * move_qty
                    state['quantity'] -= move_qty
                    if state['quantity'] > 0:
                        state['total_cost'] = state['quantity'] * state['avg_cost']
                    else:
                        state['quantity'] = Decimal("0")
                        state['avg_cost'] = Decimal("0")
                        state['total_cost'] = Decimal("0")
                    pending_transfers[txn.id] = (move_qty, moved_cost)
                else:  # TRANSFER_IN
                    entry = pending_transfers.pop(txn.linked_transaction_id, None)
                    if entry is None:
                        raise _AccountReplayFallback(
                            f"transfer in id={txn.id} has no processed linked "
                            f"transfer-out (linked={txn.linked_transaction_id})"
                        )
                    move_qty, moved_cost = entry
                    state = bucket(txn.broker_account_id)
                    new_quantity = state['quantity'] + move_qty
                    state['total_cost'] = state['quantity'] * state['avg_cost'] + moved_cost
                    state['avg_cost'] = (
                        state['total_cost'] / new_quantity if new_quantity > 0 else Decimal("0")
                    )
                    state['quantity'] = new_quantity
                continue

            account_id = txn.broker_account_id if per_account else None
            state = bucket(account_id)

            if txn.transaction_type == "BUY":
                new_quantity = state['quantity'] + Decimal(str(txn.quantity))
                if new_quantity > 0:
                    state['total_cost'] = (
                        state['quantity'] * state['avg_cost']
                        + Decimal(str(txn.quantity)) * Decimal(str(txn.price))
                        + Decimal(str(txn.fee))
                    )
                    state['avg_cost'] = state['total_cost'] / new_quantity
                    state['quantity'] = new_quantity
                else:
                    state['quantity'] = new_quantity
                    state['avg_cost'] = Decimal("0")
                    state['total_cost'] = Decimal("0")

            elif txn.transaction_type == "SELL":
                sell_quantity = Decimal(str(txn.quantity))
                if sell_quantity > state['quantity']:
                    # 走到合并桶仍超卖，说明账本本身缺记录（不是账户归属问题）：
                    # 文案要能直接指向排查方向，与东财导入器的中文提示同风格。
                    message = (
                        f"{symbol}（{market}）在 {txn.transaction_date} "
                        f"卖出 {sell_quantity}，超过可用数量 {state['quantity']}；"
                        "请检查是否缺少期初持仓或买入记录"
                    )
                    if per_account:
                        raise _AccountReplayFallback(message)
                    raise ValueError(message)
                state['quantity'] -= sell_quantity
                if state['quantity'] > 0:
                    state['total_cost'] = state['quantity'] * state['avg_cost']
                else:
                    state['quantity'] = Decimal("0")
                    state['avg_cost'] = Decimal("0")
                    state['total_cost'] = Decimal("0")

            if txn.name:
                state['name'] = txn.name
            state['currency'] = txn.currency

        elif event['type'] == 'corporate_action':
            action = event['data']
            action_type = action.action_type

            if action_type in ("STOCK_DIVIDEND", "BONUS_ISSUE", "STOCK_SPLIT", "REVERSE_SPLIT"):
                if _action_has_ratio(action):
                    # 比例按每股生效，作用于所有持仓桶。
                    targets = [aid for aid, s in buckets.items() if s['quantity'] > 0]
                else:
                    target = _resolve_action_bucket(action, buckets, per_account)
                    targets = [target] if target in buckets and buckets[target]['quantity'] > 0 else []
                for aid in targets:
                    state = buckets[aid]
                    if action_type in ("STOCK_DIVIDEND", "BONUS_ISSUE"):
                        factor = bonus_share_factor(action, state['quantity'])
                    else:
                        factor = split_share_factor(action, state['quantity'])
                    if factor is not None:
                        state['quantity'] = state['quantity'] * factor
                        state['avg_cost'] = (
                            state['total_cost'] / state['quantity']
                            if state['quantity'] > 0 else Decimal("0")
                        )

            elif action_type == "RIGHTS_ISSUE":
                if action.subscription_quantity and action.subscription_price:
                    target = _resolve_action_bucket(action, buckets, per_account)
                    state = bucket(target)
                    sub_qty = Decimal(str(action.subscription_quantity))
                    sub_price = Decimal(str(action.subscription_price))
                    new_quantity = state['quantity'] + sub_qty
                    if new_quantity > 0:
                        state['total_cost'] = state['quantity'] * state['avg_cost'] + sub_qty * sub_price
                        state['avg_cost'] = state['total_cost'] / new_quantity
                        state['quantity'] = new_quantity

            if action_type != "CASH_DIVIDEND":
                # Cash dividends are income events; they must not replace the
                # holding's security name or trading currency.
                affected = [s for s in buckets.values() if s['quantity'] > 0] or list(buckets.values())
                for state in affected:
                    if action.name:
                        state['name'] = action.name
                    if action.currency:
                        state['currency'] = action.currency

    if per_account and pending_transfers:
        raise _AccountReplayFallback(
            f"unmatched transfer-out legs for {symbol} ({market}): "
            f"ids={sorted(pending_transfers)}"
        )

    return buckets
