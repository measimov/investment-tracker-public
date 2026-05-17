from sqlalchemy.orm import Session
from decimal import Decimal, DecimalException
from ..models.transaction import Transaction
from ..models.holding import Holding
from ..models.corporate_action import CorporateAction
from ..models.broker_fund_flow import BrokerFundFlow
from ..models.ibkr_activity_flow import IbkrActivityFlow


def _numeric_sort_value(value):
    if value is None:
        return (1, 0, "")
    text = str(value).strip()
    if not text:
        return (1, 0, "")
    try:
        return (0, 0, int(text))
    except ValueError:
        return (0, 1, text)


def _transaction_type_sort_value(transaction_type):
    return 0 if transaction_type == "BUY" else 1


def _transaction_sort_key(txn, broker_flow_by_transaction_id):
    flow = broker_flow_by_transaction_id.get(txn.id)
    if flow:
        source_row = getattr(flow, 'source_row_number', None)
        return (
            txn.transaction_date,
            0,
            _numeric_sort_value(getattr(flow, 'serial_number', None)),
            _numeric_sort_value(getattr(flow, 'contract_number', None)),
            _numeric_sort_value(source_row),
            _transaction_type_sort_value(txn.transaction_type),
            txn.id,
        )
    return (txn.transaction_date, 1, None, None, None, _transaction_type_sort_value(txn.transaction_type), txn.id)


def _event_sort_key(event):
    if event['type'] == 'transaction':
        return (
            event['date'],
            1,
            _transaction_type_sort_value(event['data'].transaction_type),
            event['data'].id,
        )
    return (event['date'], 0, 0, event['data'].id)


def validate_no_oversell(transactions):
    """Raise ValueError if ordered transactions sell more than available shares."""
    quantity = Decimal("0")
    for txn in sorted(transactions, key=lambda item: (
        item.transaction_date,
        _transaction_type_sort_value(item.transaction_type),
        item.id if item.id is not None else 10**18,
    )):
        txn_quantity = Decimal(str(txn.quantity))
        if txn.transaction_type == "BUY":
            quantity += txn_quantity
        elif txn.transaction_type == "SELL":
            if txn_quantity > quantity:
                raise ValueError(
                    f"Sell quantity {txn_quantity} exceeds available quantity {quantity} "
                    f"for {txn.symbol} ({txn.market}) on {txn.transaction_date}"
                )
            quantity -= txn_quantity


def recalculate_holdings(
    db: Session,
    user_id: int,
    symbol: str,
    market: str,
    commit: bool = True,
):
    """
    重新计算特定用户、symbol和market的持仓，考虑交易记录和公司行动

    Args:
        db: Database session
        user_id: User ID to filter holdings
        symbol: Stock symbol
        market: Market type

    逻辑:
    1. 按时间顺序处理所有交易和公司行动
    2. 买入: 新平均成本 = (原持仓数量 × 原平均成本 + 买入数量 × 买入价格 + 手续费) / (原持仓数量 + 买入数量)
    3. 卖出: 平均成本不变，减少持仓数量
    4. 股票股息/红股: 增加持仓数量，调整平均成本
    5. 拆股: 调整持仓数量和平均成本
    6. 配股: 类似买入，但价格可能不同
    """
    # 获取所有交易记录。券商导入的同日多笔交易需要按券商流水号排序，
    # 因为导出文件行序不一定等于实际成交顺序。
    transactions = db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.symbol == symbol,
        Transaction.market == market
    ).all()
    transaction_ids = [txn.id for txn in transactions]
    broker_flow_by_transaction_id = {}
    if transaction_ids:
        broker_flows = db.query(BrokerFundFlow).filter(
            BrokerFundFlow.user_id == user_id,
            BrokerFundFlow.transaction_id.in_(transaction_ids),
        ).all()
        broker_flow_by_transaction_id = {
            flow.transaction_id: flow for flow in broker_flows if flow.transaction_id
        }
        ibkr_flows = db.query(IbkrActivityFlow).filter(
            IbkrActivityFlow.user_id == user_id,
            IbkrActivityFlow.transaction_id.in_(transaction_ids),
        ).all()
        broker_flow_by_transaction_id.update({
            flow.transaction_id: flow for flow in ibkr_flows if flow.transaction_id
        })
    transactions.sort(key=lambda txn: _transaction_sort_key(txn, broker_flow_by_transaction_id))

    # 获取所有公司行动，按除权除息日排序
    corporate_actions = db.query(CorporateAction).filter(
        CorporateAction.user_id == user_id,
        CorporateAction.symbol == symbol,
        CorporateAction.market == market
    ).order_by(CorporateAction.ex_date, CorporateAction.id).all()

    # 如果既没有交易也没有公司行动，删除持仓
    if not transactions and not corporate_actions:
        db.query(Holding).filter(
            Holding.user_id == user_id,
            Holding.symbol == symbol,
            Holding.market == market
        ).delete()
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

    # 计算持仓
    quantity = Decimal("0")
    avg_cost = Decimal("0")
    total_cost = Decimal("0")
    name = None
    currency = "CNY"

    for event in events:
        if event['type'] == 'transaction':
            txn = event['data']

            if txn.transaction_type == "BUY":
                # 买入交易
                new_quantity = quantity + Decimal(str(txn.quantity))
                if new_quantity > 0:
                    # 计算新的平均成本
                    total_cost = (quantity * avg_cost) + (Decimal(str(txn.quantity)) * Decimal(str(txn.price))) + Decimal(str(txn.fee))
                    avg_cost = total_cost / new_quantity
                    quantity = new_quantity
                else:
                    quantity = new_quantity
                    avg_cost = Decimal("0")
                    total_cost = Decimal("0")

            elif txn.transaction_type == "SELL":
                # 卖出交易
                sell_quantity = Decimal(str(txn.quantity))
                if sell_quantity > quantity:
                    raise ValueError(
                        f"Sell quantity {sell_quantity} exceeds available quantity {quantity} "
                        f"for {txn.symbol} ({txn.market}) on {txn.transaction_date}"
                    )
                quantity -= sell_quantity
                if quantity > 0:
                    # 平均成本保持不变，更新总成本
                    total_cost = quantity * avg_cost
                else:
                    # 全部卖出，重置
                    quantity = Decimal("0")
                    avg_cost = Decimal("0")
                    total_cost = Decimal("0")

            # 更新名称和货币
            if txn.name:
                name = txn.name
            currency = txn.currency

        elif event['type'] == 'corporate_action':
            action = event['data']

            if action.action_type == "STOCK_DIVIDEND" or action.action_type == "BONUS_ISSUE":
                # 股票股息/送股
                if action.shares_received and quantity > 0:
                    shares_received = Decimal(str(action.shares_received))
                    # 增加持股数量，总成本不变，平均成本降低
                    new_quantity = quantity + shares_received
                    avg_cost = total_cost / new_quantity
                    quantity = new_quantity

            elif action.action_type == "STOCK_SPLIT":
                # 拆股
                if action.split_ratio and quantity > 0:
                    # 解析拆股比例，如 "1:2" 表示1股拆成2股
                    try:
                        old_shares, new_shares = action.split_ratio.split(':')
                        ratio = Decimal(new_shares) / Decimal(old_shares)
                        quantity = quantity * ratio
                        avg_cost = total_cost / quantity if quantity > 0 else Decimal("0")
                    except (ValueError, DecimalException, ZeroDivisionError):
                        pass

            elif action.action_type == "REVERSE_SPLIT":
                # 合股
                if action.split_ratio and quantity > 0:
                    # 解析合股比例，如 "10:1" 表示10股合成1股
                    try:
                        old_shares, new_shares = action.split_ratio.split(':')
                        ratio = Decimal(new_shares) / Decimal(old_shares)
                        quantity = quantity * ratio
                        avg_cost = total_cost / quantity if quantity > 0 else Decimal("0")
                    except (ValueError, DecimalException, ZeroDivisionError):
                        pass

            elif action.action_type == "RIGHTS_ISSUE":
                # 配股 - 类似买入
                if action.subscription_quantity and action.subscription_price:
                    sub_qty = Decimal(str(action.subscription_quantity))
                    sub_price = Decimal(str(action.subscription_price))
                    new_quantity = quantity + sub_qty
                    if new_quantity > 0:
                        total_cost = (quantity * avg_cost) + (sub_qty * sub_price)
                        avg_cost = total_cost / new_quantity
                        quantity = new_quantity

            if action.action_type != "CASH_DIVIDEND":
                # Cash dividends are income events; they must not replace the
                # holding's security name or trading currency.
                if action.name:
                    name = action.name
                if action.currency:
                    currency = action.currency

    # 更新或创建持仓记录
    holding = db.query(Holding).filter(
        Holding.user_id == user_id,
        Holding.symbol == symbol,
        Holding.market == market
    ).first()

    if quantity <= 0:
        # 没有持仓，删除记录
        if holding:
            db.delete(holding)
    else:
        if holding:
            # 更新现有持仓
            holding.quantity = quantity
            holding.avg_cost = avg_cost
            holding.total_cost = total_cost
            if name:
                holding.name = name
            holding.currency = currency
        else:
            # 创建新持仓
            holding = Holding(
                user_id=user_id,
                symbol=symbol,
                name=name or symbol,
                market=market,
                quantity=quantity,
                avg_cost=avg_cost,
                total_cost=total_cost,
                currency=currency
            )
            db.add(holding)

    if commit:
        db.commit()
    else:
        db.flush()

    if holding and quantity > 0:
        db.refresh(holding)
        return holding
    return None


def calculate_realized_pnl(db: Session, user_id: int, symbol: str, market: str):
    """
    计算已实现盈亏

    Args:
        db: Database session
        user_id: User ID to filter data
        symbol: Stock symbol
        market: Market type

    包括:
    1. 卖出股票的资本利得/损失
    2. 收到的现金股息（税后）
    """
    from decimal import Decimal

    # 获取所有交易
    transactions = db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.symbol == symbol,
        Transaction.market == market
    ).all()
    transactions.sort(key=lambda txn: (
        txn.transaction_date,
        _transaction_type_sort_value(txn.transaction_type),
        txn.id,
    ))

    # 获取所有现金股息
    dividends = db.query(CorporateAction).filter(
        CorporateAction.user_id == user_id,
        CorporateAction.symbol == symbol,
        CorporateAction.market == market,
        CorporateAction.action_type == "CASH_DIVIDEND"
    ).all()

    # 计算卖出盈亏 (简化版，使用平均成本)
    capital_gain = Decimal("0")
    quantity = Decimal("0")
    total_cost = Decimal("0")
    avg_cost = Decimal("0")

    for txn in transactions:
        if txn.transaction_type == "BUY":
            new_quantity = quantity + Decimal(str(txn.quantity))
            if new_quantity > 0:
                total_cost = (quantity * avg_cost) + (Decimal(str(txn.quantity)) * Decimal(str(txn.price))) + Decimal(str(txn.fee))
                avg_cost = total_cost / new_quantity
                quantity = new_quantity

        elif txn.transaction_type == "SELL":
            sell_qty = Decimal(str(txn.quantity))
            sell_price = Decimal(str(txn.price))
            sell_fee = Decimal(str(txn.fee))

            if sell_qty > quantity:
                continue

            # 计算这次卖出的盈亏
            proceeds = sell_qty * sell_price - sell_fee
            cost = sell_qty * avg_cost
            capital_gain += (proceeds - cost)

            # 更新持仓
            quantity -= sell_qty
            if quantity > 0:
                total_cost = quantity * avg_cost
            else:
                quantity = Decimal("0")
                total_cost = Decimal("0")
                avg_cost = Decimal("0")

    # 计算股息收入 (税后)
    dividend_income = Decimal("0")
    for div in dividends:
        if div.net_dividend:
            dividend_income += Decimal(str(div.net_dividend))
        elif div.total_dividend:
            # 如果没有税后金额，使用税前金额
            dividend_income += Decimal(str(div.total_dividend))

    return {
        "capital_gain": float(capital_gain),
        "dividend_income": float(dividend_income),
        "total_realized_pnl": float(capital_gain + dividend_income)
    }
