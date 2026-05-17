from sqlalchemy.orm import Session
from sqlalchemy import func, extract
from decimal import Decimal, DecimalException
from datetime import date
from typing import Dict, List, Any, Tuple, Optional
from collections import deque
from ..models.transaction import Transaction
from ..models.holding import Holding
from ..models.corporate_action import CorporateAction
from . import exchange_rate_service


def _to_cny_or_original(db: Session, amount: Decimal, currency: Optional[str]) -> Decimal:
    try:
        return exchange_rate_service.convert_to_cny(db, amount, currency or "CNY")
    except ValueError:
        return amount


def _xirr(cash_flows: List[Tuple[date, Decimal]]) -> Optional[Decimal]:
    """Calculate annualized money-weighted return from dated cash flows."""
    flows = [(flow_date, amount) for flow_date, amount in cash_flows if amount != 0]
    if not flows or not any(amount < 0 for _, amount in flows) or not any(amount > 0 for _, amount in flows):
        return None

    start_date = min(flow_date for flow_date, _ in flows)

    def npv(rate: Decimal) -> Decimal:
        total = Decimal("0")
        base = Decimal("1") + rate
        for flow_date, amount in flows:
            years = Decimal((flow_date - start_date).days) / Decimal("365")
            total += amount / (base ** years)
        return total

    low = Decimal("-0.9999")
    high = Decimal("1")
    low_value = npv(low)
    high_value = npv(high)

    for _ in range(80):
        if low_value == 0:
            return low
        if low_value * high_value < 0:
            break
        high *= Decimal("2")
        high_value = npv(high)
        if high > Decimal("1000000"):
            return None

    for _ in range(120):
        mid = (low + high) / Decimal("2")
        mid_value = npv(mid)
        if abs(mid_value) < Decimal("0.000001"):
            return mid
        if low_value * mid_value <= 0:
            high = mid
            high_value = mid_value
        else:
            low = mid
            low_value = mid_value

    return (low + high) / Decimal("2")


def get_summary_statistics(db: Session, user_id: int) -> Dict[str, Any]:
    """Get overall summary statistics with multi-currency support."""

    # Total holdings value (total cost) - 需要按币种转换
    holdings = db.query(Holding).filter(Holding.user_id == user_id).all()

    total_invested_cny = Decimal("0")
    total_invested_by_currency = {}

    for h in holdings:
        currency = h.currency or "CNY"
        amount = Decimal(str(h.total_cost))

        # 记录各币种明细
        if currency not in total_invested_by_currency:
            total_invested_by_currency[currency] = Decimal("0")
        total_invested_by_currency[currency] += amount

        # 转换为CNY汇总
        try:
            amount_cny = exchange_rate_service.convert_to_cny(db, amount, currency)
            total_invested_cny += amount_cny
        except ValueError:
            # 如果找不到汇率，直接加（假设是CNY）
            total_invested_cny += amount

    total_holdings = len(holdings)

    # Total transactions
    total_transactions = db.query(Transaction).filter(Transaction.user_id == user_id).count()

    # 转换为USD
    try:
        total_invested_usd = exchange_rate_service.convert_to_usd(db, total_invested_cny)
    except ValueError:
        total_invested_usd = Decimal("0")

    # 获取使用的汇率
    exchange_rates_used = {}
    for currency in total_invested_by_currency.keys():
        if currency != "CNY":
            try:
                rate_info = exchange_rate_service.get_rate_info(db, currency, "CNY")
                if rate_info:
                    exchange_rates_used[currency] = float(rate_info['rate'])
            except ValueError:
                pass

    return {
        "total_invested_cny": round(float(total_invested_cny), 2),
        "total_invested_usd": round(float(total_invested_usd), 2),
        "total_invested": round(float(total_invested_cny), 2),  # 保持向后兼容
        "total_invested_by_currency": {k: round(float(v), 2) for k, v in total_invested_by_currency.items()},
        "total_holdings": total_holdings,
        "total_transactions": total_transactions,
        "markets_count": db.query(Transaction.market).filter(Transaction.user_id == user_id).distinct().count(),
        "base_currency": "CNY",
        "exchange_rates_used": exchange_rates_used
    }


def get_statistics_by_market(db: Session, user_id: int) -> List[Dict[str, Any]]:
    """Get statistics grouped by market with multi-currency support."""

    holdings = db.query(Holding).filter(Holding.user_id == user_id).all()

    market_stats = {}
    for holding in holdings:
        market = holding.market
        currency = holding.currency or "CNY"
        amount = Decimal(str(holding.total_cost))

        if market not in market_stats:
            market_stats[market] = {
                "market": market,
                "total_cost_cny": Decimal("0"),
                "total_cost_usd": Decimal("0"),
                "total_cost_by_currency": {},
                "holdings_count": 0
            }

        # 记录各币种明细
        if currency not in market_stats[market]["total_cost_by_currency"]:
            market_stats[market]["total_cost_by_currency"][currency] = Decimal("0")
        market_stats[market]["total_cost_by_currency"][currency] += amount

        # 转换为CNY
        try:
            amount_cny = exchange_rate_service.convert_to_cny(db, amount, currency)
            market_stats[market]["total_cost_cny"] += amount_cny
        except ValueError:
            market_stats[market]["total_cost_cny"] += amount

        market_stats[market]["holdings_count"] += 1

    # 转换为USD
    result = []
    for item in market_stats.values():
        try:
            item["total_cost_usd"] = exchange_rate_service.convert_to_usd(db, item["total_cost_cny"])
        except ValueError:
            item["total_cost_usd"] = Decimal("0")

        result.append({
            "market": item["market"],
            "total_cost_cny": round(float(item["total_cost_cny"]), 2),
            "total_cost_usd": round(float(item["total_cost_usd"]), 2),
            "total_cost": round(float(item["total_cost_cny"]), 2),  # 向后兼容
            "total_cost_by_currency": {k: round(float(v), 2) for k, v in item["total_cost_by_currency"].items()},
            "holdings_count": item["holdings_count"]
        })

    return result


def get_statistics_by_time(db: Session, user_id: int, group_by: str = "month") -> List[Dict[str, Any]]:
    """Get transaction statistics grouped by time period."""

    if group_by == "month":
        # Group by year and month
        results = db.query(
            extract('year', Transaction.transaction_date).label('year'),
            extract('month', Transaction.transaction_date).label('month'),
            Transaction.transaction_type,
            func.count(Transaction.id).label('count'),
            func.sum(Transaction.quantity * Transaction.price).label('amount')
        ).filter(
            Transaction.user_id == user_id
        ).group_by(
            'year', 'month', Transaction.transaction_type
        ).order_by('year', 'month').all()

        time_stats = {}
        for row in results:
            key = f"{int(row.year)}-{int(row.month):02d}"
            if key not in time_stats:
                time_stats[key] = {
                    "period": key,
                    "buy_count": 0,
                    "sell_count": 0,
                    "buy_amount": 0,
                    "sell_amount": 0
                }

            if row.transaction_type == "BUY":
                time_stats[key]["buy_count"] = row.count
                time_stats[key]["buy_amount"] = round(float(row.amount or 0), 2)
            else:
                time_stats[key]["sell_count"] = row.count
                time_stats[key]["sell_amount"] = round(float(row.amount or 0), 2)

        return list(time_stats.values())

    return []


def get_profit_loss_analysis(db: Session, user_id: int) -> List[Dict[str, Any]]:
    """Get profit/loss analysis for current holdings."""

    holdings = db.query(Holding).filter(Holding.user_id == user_id).all()

    analysis = []
    for holding in holdings:
        analysis.append({
            "symbol": holding.symbol,
            "name": holding.name,
            "market": holding.market,
            "quantity": float(holding.quantity),
            "avg_cost": float(holding.avg_cost),
            "total_cost": float(holding.total_cost),
            "currency": holding.currency
        })

    # Sort by total cost descending
    analysis.sort(key=lambda x: x["total_cost"], reverse=True)

    return analysis


def calculate_fifo_pnl_per_symbol(
    db: Session,
    user_id: int,
    symbol: str,
    market: str
) -> Dict[str, Any]:
    """
    单只股票的FIFO盈亏计算（整合公司行动）

    处理规则：
    1. 送股：调整FIFO队列中现有批次的数量和单价，总成本不变
    2. 配股：作为新的买入交易插入队列
    3. 拆股/合股：按比例调整队列中所有批次
    4. 事件优先级：公司行动(0) > 买入(1) > 卖出(2)

    Returns:
        {
            'symbol': str,
            'market': str,
            'realized_pnl': float,           # 已实现盈亏
            'sold_cost': float,              # 已卖出成本
            'current_holdings_cost': float,  # 当前持仓成本
            'buy_queue': list                # FIFO剩余批次
        }
    """
    # 1. 获取所有交易记录
    transactions = db.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.symbol == symbol,
        Transaction.market == market
    ).order_by(Transaction.transaction_date, Transaction.id).all()

    # 2. 获取所有影响持仓的公司行动
    corporate_actions = db.query(CorporateAction).filter(
        CorporateAction.user_id == user_id,
        CorporateAction.symbol == symbol,
        CorporateAction.market == market,
        CorporateAction.action_type.in_([
            'STOCK_DIVIDEND', 'BONUS_ISSUE',
            'STOCK_SPLIT', 'REVERSE_SPLIT',
            'RIGHTS_ISSUE'
        ])
    ).order_by(CorporateAction.ex_date, CorporateAction.id).all()

    # 3. 合并事件时间线
    events = []

    for txn in transactions:
        priority = 1 if txn.transaction_type == 'BUY' else 2
        events.append({
            'type': txn.transaction_type,
            'date': txn.transaction_date,
            'price': float(txn.price),
            'quantity': float(txn.quantity),
            'fee': float(txn.fee or 0),
            'priority': priority
        })

    for action in corporate_actions:
        events.append({
            'type': action.action_type,
            'date': action.ex_date,
            'data': action,
            'priority': 0  # 公司行动优先处理
        })

    # 按日期和优先级排序（优化2）
    events.sort(key=lambda x: (x['date'], x['priority']))

    # 4. 初始化FIFO队列
    buy_queue = deque()
    realized_pnl = Decimal(0)
    sold_cost = Decimal(0)

    # 5. 处理事件流
    for event in events:
        event_type = event['type']

        if event_type == 'BUY':
            # 买入：加入FIFO队列
            total_cost = Decimal(str(event['price'])) * Decimal(str(event['quantity'])) + Decimal(str(event['fee']))
            cost_per_share = total_cost / Decimal(str(event['quantity']))

            buy_queue.append({
                'price': cost_per_share,
                'quantity': Decimal(str(event['quantity'])),
                'total_cost': total_cost,
                'date': event['date']
            })

        elif event_type == 'SELL':
            # 卖出：FIFO配对
            sell_qty = Decimal(str(event['quantity']))
            available_qty = sum(record['quantity'] for record in buy_queue)
            if sell_qty > available_qty:
                continue
            sell_proceeds = Decimal(str(event['price'])) * sell_qty - Decimal(str(event['fee']))
            matched_cost = Decimal(0)

            while sell_qty > 0 and buy_queue:
                buy_record = buy_queue[0]

                if buy_record['quantity'] <= sell_qty:
                    # 完全卖出这批
                    matched_qty = buy_record['quantity']
                    matched_cost += buy_record['total_cost']
                    buy_queue.popleft()
                else:
                    # 部分卖出
                    matched_qty = sell_qty
                    ratio = matched_qty / buy_record['quantity']
                    batch_cost = buy_record['total_cost'] * ratio
                    matched_cost += batch_cost

                    # 更新剩余
                    buy_record['quantity'] -= matched_qty
                    buy_record['total_cost'] -= batch_cost

                sell_qty -= matched_qty

            # 计算这笔卖出的盈亏
            batch_pnl = sell_proceeds - matched_cost
            realized_pnl += batch_pnl
            sold_cost += matched_cost

        elif event_type in ['STOCK_DIVIDEND', 'BONUS_ISSUE']:
            # 送股：调整队列中所有批次
            action = event['data']
            if action.distribution_ratio:
                try:
                    parts = action.distribution_ratio.split(':')
                    base, bonus = int(parts[0]), int(parts[1])
                    ratio = Decimal(1) + Decimal(bonus) / Decimal(base)

                    # 调整所有批次
                    for buy_record in buy_queue:
                        buy_record['quantity'] *= ratio
                        # price 自动摊薄（total_cost 不变）
                        buy_record['price'] = buy_record['total_cost'] / buy_record['quantity']
                except (ValueError, DecimalException, ZeroDivisionError):
                    pass

        elif event_type == 'RIGHTS_ISSUE':
            # 配股：作为新的买入交易
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
            # 拆股/合股：按比例调整所有批次
            action = event['data']
            if action.split_ratio:
                try:
                    parts = action.split_ratio.split(':')
                    old_shares, new_shares = int(parts[0]), int(parts[1])
                    split_ratio = Decimal(new_shares) / Decimal(old_shares)

                    # 调整所有批次
                    for buy_record in buy_queue:
                        buy_record['quantity'] *= split_ratio
                        buy_record['price'] /= split_ratio
                        # total_cost 不变
                except (ValueError, DecimalException, ZeroDivisionError):
                    pass

    # 6. 计算当前持仓成本
    current_holdings_cost = sum(b['total_cost'] for b in buy_queue)

    # 7. 返回结果
    return {
        'symbol': symbol,
        'market': market,
        'realized_pnl': float(realized_pnl),
        'sold_cost': float(sold_cost),
        'current_holdings_cost': float(current_holdings_cost),
        'buy_queue': [
            {
                'price': float(b['price']),
                'quantity': float(b['quantity']),
                'total_cost': float(b['total_cost']),
                'date': str(b['date'])
            }
            for b in buy_queue
        ]
    }


def calculate_current_holdings_performance(
    db: Session,
    user_id: int,
    current_prices: Dict[str, float]
) -> Dict[str, Any]:
    """
    计算当前持仓表现（基于FIFO剩余批次，优化1）- 支持多币种

    Args:
        user_id: User ID
        current_prices: {symbol: current_price}

    Returns:
        {
            'unrealized_pnl_cny': float,           # 未实现盈亏（CNY）
            'unrealized_pnl_usd': float,           # 未实现盈亏（USD）
            'current_holdings_cost_cny': float,    # 当前持仓成本（CNY）
            'current_holdings_cost_usd': float,    # 当前持仓成本（USD）
            'unrealized_pnl_rate': float,          # 浮盈率
            'current_market_value_cny': float,     # 当前市值（CNY）
            'current_market_value_usd': float,     # 当前市值（USD）
            'holdings_detail': list                # 各股票明细
        }
    """
    # 获取所有持仓的symbol列表（包含currency信息）
    holdings = db.query(Holding.symbol, Holding.market, Holding.name, Holding.currency).filter(
        Holding.user_id == user_id
    ).all()

    total_unrealized_pnl_cny = Decimal(0)
    total_holdings_cost_cny = Decimal(0)
    total_market_value_cny = Decimal(0)
    holdings_detail = []

    for symbol, market, name, currency in holdings:
        currency = currency or "CNY"  # 默认CNY

        # 计算该股票的FIFO信息
        fifo_result = calculate_fifo_pnl_per_symbol(db, user_id, symbol, market)

        current_price = current_prices.get(symbol)
        if not current_price:
            continue

        buy_queue = fifo_result['buy_queue']
        holdings_cost = Decimal(str(fifo_result['current_holdings_cost']))

        # 基于FIFO剩余批次计算未实现盈亏（优化1）
        unrealized_pnl = Decimal(0)
        total_qty = Decimal(0)

        for batch in buy_queue:
            batch_pnl = (Decimal(str(current_price)) - Decimal(str(batch['price']))) * Decimal(str(batch['quantity']))
            unrealized_pnl += batch_pnl
            total_qty += Decimal(str(batch['quantity']))

        market_value = Decimal(str(current_price)) * total_qty

        # 转换为CNY
        try:
            holdings_cost_cny = exchange_rate_service.convert_to_cny(db, holdings_cost, currency)
            unrealized_pnl_cny = exchange_rate_service.convert_to_cny(db, unrealized_pnl, currency)
            market_value_cny = exchange_rate_service.convert_to_cny(db, market_value, currency)
        except ValueError:
            # 如果找不到汇率，假设是CNY
            holdings_cost_cny = holdings_cost
            unrealized_pnl_cny = unrealized_pnl
            market_value_cny = market_value

        total_unrealized_pnl_cny += unrealized_pnl_cny
        total_holdings_cost_cny += holdings_cost_cny
        total_market_value_cny += market_value_cny

        holdings_detail.append({
            'symbol': symbol,
            'name': name,
            'market': market,
            'currency': currency,
            'quantity': float(total_qty),
            'current_price': current_price,
            'holdings_cost': float(holdings_cost),
            'holdings_cost_cny': float(holdings_cost_cny),
            'market_value': float(market_value),
            'market_value_cny': float(market_value_cny),
            'unrealized_pnl': float(unrealized_pnl),
            'unrealized_pnl_cny': float(unrealized_pnl_cny),
            'unrealized_pnl_rate': float(unrealized_pnl / holdings_cost * 100) if holdings_cost > 0 else 0
        })

    # 计算总收益率
    unrealized_pnl_rate = Decimal(0)
    if total_holdings_cost_cny > 0:
        unrealized_pnl_rate = total_unrealized_pnl_cny / total_holdings_cost_cny * Decimal(100)

    # 转换为USD
    try:
        total_unrealized_pnl_usd = exchange_rate_service.convert_to_usd(db, total_unrealized_pnl_cny)
        total_holdings_cost_usd = exchange_rate_service.convert_to_usd(db, total_holdings_cost_cny)
        total_market_value_usd = exchange_rate_service.convert_to_usd(db, total_market_value_cny)
    except ValueError:
        total_unrealized_pnl_usd = Decimal(0)
        total_holdings_cost_usd = Decimal(0)
        total_market_value_usd = Decimal(0)

    return {
        'unrealized_pnl_cny': float(total_unrealized_pnl_cny),
        'unrealized_pnl_usd': float(total_unrealized_pnl_usd),
        'unrealized_pnl': float(total_unrealized_pnl_cny),  # 向后兼容
        'current_holdings_cost_cny': float(total_holdings_cost_cny),
        'current_holdings_cost_usd': float(total_holdings_cost_usd),
        'current_holdings_cost': float(total_holdings_cost_cny),  # 向后兼容
        'unrealized_pnl_rate': float(unrealized_pnl_rate),
        'current_market_value_cny': float(total_market_value_cny),
        'current_market_value_usd': float(total_market_value_usd),
        'current_market_value': float(total_market_value_cny),  # 向后兼容
        'holdings_detail': holdings_detail,
        'base_currency': 'CNY'
    }


def calculate_realized_pnl_fifo(db: Session, user_id: int) -> Dict[str, Any]:
    """
    计算已实现盈亏（FIFO方法）- 支持多币种

    Returns:
        {
            'realized_pnl_cny': float,         # 已实现盈亏总额（CNY）
            'realized_pnl_usd': float,         # 已实现盈亏总额（USD）
            'sold_cost_cny': float,            # 已卖出部分成本（CNY）
            'sold_cost_usd': float,            # 已卖出部分成本（USD）
            'realized_pnl_rate': float,        # 已实现收益率
            'trades_detail': list              # 各股票明细
        }
    """
    # 获取所有有交易的symbol（包含currency）
    symbols_query = db.query(
        Transaction.symbol,
        Transaction.market,
        Transaction.currency
    ).filter(
        Transaction.user_id == user_id
    ).distinct().all()

    total_realized_pnl_cny = Decimal(0)
    total_sold_cost_cny = Decimal(0)
    trades_detail = []

    for symbol, market, currency in symbols_query:
        currency = currency or "CNY"  # 默认CNY

        result = calculate_fifo_pnl_per_symbol(db, user_id, symbol, market)

        realized_pnl = Decimal(str(result['realized_pnl']))
        sold_cost = Decimal(str(result['sold_cost']))

        if realized_pnl != 0 or sold_cost != 0:
            # 转换为CNY
            try:
                realized_pnl_cny = exchange_rate_service.convert_to_cny(db, realized_pnl, currency)
                sold_cost_cny = exchange_rate_service.convert_to_cny(db, sold_cost, currency)
            except ValueError:
                realized_pnl_cny = realized_pnl
                sold_cost_cny = sold_cost

            total_realized_pnl_cny += realized_pnl_cny
            total_sold_cost_cny += sold_cost_cny

            trades_detail.append({
                'symbol': symbol,
                'market': market,
                'currency': currency,
                'realized_pnl': float(realized_pnl),
                'realized_pnl_cny': float(realized_pnl_cny),
                'sold_cost': float(sold_cost),
                'sold_cost_cny': float(sold_cost_cny),
                'realized_pnl_rate': float(realized_pnl / sold_cost * 100) if sold_cost > 0 else 0
            })

    # 计算总收益率
    realized_pnl_rate = Decimal(0)
    if total_sold_cost_cny > 0:
        realized_pnl_rate = total_realized_pnl_cny / total_sold_cost_cny * Decimal(100)

    # 转换为USD
    try:
        total_realized_pnl_usd = exchange_rate_service.convert_to_usd(db, total_realized_pnl_cny)
        total_sold_cost_usd = exchange_rate_service.convert_to_usd(db, total_sold_cost_cny)
    except ValueError:
        total_realized_pnl_usd = Decimal(0)
        total_sold_cost_usd = Decimal(0)

    return {
        'realized_pnl_cny': float(total_realized_pnl_cny),
        'realized_pnl_usd': float(total_realized_pnl_usd),
        'realized_pnl': float(total_realized_pnl_cny),  # 向后兼容
        'sold_cost_cny': float(total_sold_cost_cny),
        'sold_cost_usd': float(total_sold_cost_usd),
        'sold_cost': float(total_sold_cost_cny),  # 向后兼容
        'realized_pnl_rate': float(realized_pnl_rate),
        'trades_detail': trades_detail,
        'base_currency': 'CNY'
    }


def get_dividend_summary(db: Session, user_id: int) -> Dict[str, Any]:
    """
    股息统计摘要（独立模块，不混入盈亏）- 支持多币种

    Returns:
        {
            'total_dividend_gross_cny': float,  # 税前总额（CNY）
            'total_dividend_gross_usd': float,  # 税前总额（USD）
            'total_tax_cny': float,             # 总税费（CNY）
            'total_tax_usd': float,             # 总税费（USD）
            'total_dividend_net_cny': float,    # 税后总额（CNY）
            'total_dividend_net_usd': float,    # 税后总额（USD）
            'by_symbol': list                   # 按股票分组
        }
    """
    dividends = db.query(CorporateAction).filter(
        CorporateAction.user_id == user_id,
        CorporateAction.action_type == 'CASH_DIVIDEND'
    ).all()

    total_gross_cny = Decimal(0)
    total_tax_cny = Decimal(0)
    total_net_cny = Decimal(0)

    by_symbol = {}

    for div in dividends:
        symbol = div.symbol
        currency = div.currency or "CNY"  # 获取股息的货币

        gross = Decimal(str(div.total_dividend or 0))
        tax = Decimal(str(div.tax_withheld or 0))
        net = Decimal(str(div.net_dividend or (gross - tax)))

        # 转换为CNY
        try:
            gross_cny = exchange_rate_service.convert_to_cny(db, gross, currency)
            tax_cny = exchange_rate_service.convert_to_cny(db, tax, currency)
            net_cny = exchange_rate_service.convert_to_cny(db, net, currency)
        except ValueError:
            gross_cny = gross
            tax_cny = tax
            net_cny = net

        total_gross_cny += gross_cny
        total_tax_cny += tax_cny
        total_net_cny += net_cny

        if symbol not in by_symbol:
            by_symbol[symbol] = {
                'symbol': symbol,
                'name': div.name,
                'market': div.market,
                'total_gross': Decimal(0),
                'total_gross_cny': Decimal(0),
                'total_tax': Decimal(0),
                'total_tax_cny': Decimal(0),
                'total_net': Decimal(0),
                'total_net_cny': Decimal(0),
                'count': 0,
                'currency': currency
            }

        by_symbol[symbol]['total_gross'] += gross
        by_symbol[symbol]['total_gross_cny'] += gross_cny
        by_symbol[symbol]['total_tax'] += tax
        by_symbol[symbol]['total_tax_cny'] += tax_cny
        by_symbol[symbol]['total_net'] += net
        by_symbol[symbol]['total_net_cny'] += net_cny
        by_symbol[symbol]['count'] += 1

    # 转换为USD
    try:
        total_gross_usd = exchange_rate_service.convert_to_usd(db, total_gross_cny)
        total_tax_usd = exchange_rate_service.convert_to_usd(db, total_tax_cny)
        total_net_usd = exchange_rate_service.convert_to_usd(db, total_net_cny)
    except ValueError:
        total_gross_usd = Decimal(0)
        total_tax_usd = Decimal(0)
        total_net_usd = Decimal(0)

    # 转换为列表并格式化
    by_symbol_list = [
        {
            'symbol': v['symbol'],
            'name': v['name'],
            'market': v['market'],
            'currency': v['currency'],
            'total_gross': float(v['total_gross']),
            'total_gross_cny': float(v['total_gross_cny']),
            'total_tax': float(v['total_tax']),
            'total_tax_cny': float(v['total_tax_cny']),
            'total_net': float(v['total_net']),
            'total_net_cny': float(v['total_net_cny']),
            'count': v['count']
        }
        for v in by_symbol.values()
    ]

    return {
        'total_dividend_gross_cny': float(total_gross_cny),
        'total_dividend_gross_usd': float(total_gross_usd),
        'total_dividend_gross': float(total_gross_cny),  # 向后兼容
        'total_tax_cny': float(total_tax_cny),
        'total_tax_usd': float(total_tax_usd),
        'total_tax': float(total_tax_cny),  # 向后兼容
        'total_dividend_net_cny': float(total_net_cny),
        'total_dividend_net_usd': float(total_net_usd),
        'total_dividend_net': float(total_net_cny),  # 向后兼容
        'by_symbol': by_symbol_list,
        'base_currency': 'CNY'
    }


def calculate_total_realized_return(db: Session, user_id: int) -> Dict[str, Any]:
    """Combine realized trading PnL with net dividend income."""
    realized = calculate_realized_pnl_fifo(db, user_id)
    dividends = get_dividend_summary(db, user_id)

    realized_trading_pnl_cny = Decimal(str(realized.get('realized_pnl_cny', 0)))
    realized_trading_pnl_usd = Decimal(str(realized.get('realized_pnl_usd', 0)))
    sold_cost_cny = Decimal(str(realized.get('sold_cost_cny', 0)))
    sold_cost_usd = Decimal(str(realized.get('sold_cost_usd', 0)))
    net_dividend_cny = Decimal(str(dividends.get('total_dividend_net_cny', 0)))
    net_dividend_usd = Decimal(str(dividends.get('total_dividend_net_usd', 0)))

    total_realized_return_cny = realized_trading_pnl_cny + net_dividend_cny
    total_realized_return_usd = realized_trading_pnl_usd + net_dividend_usd
    total_realized_return_rate = Decimal(0)
    if sold_cost_cny > 0:
        total_realized_return_rate = total_realized_return_cny / sold_cost_cny * Decimal(100)

    return {
        'realized_trading_pnl_cny': float(realized_trading_pnl_cny),
        'realized_trading_pnl_usd': float(realized_trading_pnl_usd),
        'net_dividend_income_cny': float(net_dividend_cny),
        'net_dividend_income_usd': float(net_dividend_usd),
        'total_realized_return_cny': float(total_realized_return_cny),
        'total_realized_return_usd': float(total_realized_return_usd),
        'total_realized_return': float(total_realized_return_cny),
        'sold_cost_cny': float(sold_cost_cny),
        'sold_cost_usd': float(sold_cost_usd),
        'total_realized_return_rate': float(total_realized_return_rate),
        'rate_denominator': 'sold_cost_cny',
        'base_currency': 'CNY',
    }


def calculate_account_total_return(
    db: Session,
    user_id: int,
    current_prices: Dict[str, float]
) -> Dict[str, Any]:
    """
    Calculate account-level total return.

    Total return includes realized trading PnL, unrealized PnL, and net dividends.
    Simple return uses estimated net invested principal as the denominator:
    current market value - total return. XIRR uses transaction/dividend cash flows
    plus current market value as the terminal value.
    """
    realized = calculate_realized_pnl_fifo(db, user_id)
    dividends = get_dividend_summary(db, user_id)
    current = calculate_current_holdings_performance(db, user_id, current_prices)

    realized_trading_pnl_cny = Decimal(str(realized.get('realized_pnl_cny', 0)))
    net_dividend_cny = Decimal(str(dividends.get('total_dividend_net_cny', 0)))
    unrealized_pnl_cny = Decimal(str(current.get('unrealized_pnl_cny', 0)))
    current_market_value_cny = Decimal(str(current.get('current_market_value_cny', 0)))

    total_return_cny = realized_trading_pnl_cny + unrealized_pnl_cny + net_dividend_cny
    net_invested_principal_cny = current_market_value_cny - total_return_cny
    total_return_rate = Decimal("0")
    if net_invested_principal_cny > 0:
        total_return_rate = total_return_cny / net_invested_principal_cny * Decimal("100")

    cash_flows: List[Tuple[date, Decimal]] = []
    transactions = db.query(Transaction).filter(Transaction.user_id == user_id).all()
    for txn in transactions:
        currency = txn.currency or "CNY"
        quantity = Decimal(str(txn.quantity))
        gross = quantity * Decimal(str(txn.price))
        fee = Decimal(str(txn.fee or 0))
        if txn.transaction_type == "BUY":
            amount = -(gross + fee)
        elif txn.transaction_type == "SELL":
            amount = gross - fee
        else:
            continue
        cash_flows.append((txn.transaction_date, _to_cny_or_original(db, amount, currency)))

    dividend_actions = db.query(CorporateAction).filter(
        CorporateAction.user_id == user_id,
        CorporateAction.action_type == 'CASH_DIVIDEND'
    ).all()
    for div in dividend_actions:
        currency = div.currency or "CNY"
        gross = Decimal(str(div.total_dividend or 0))
        tax = Decimal(str(div.tax_withheld or 0))
        net = Decimal(str(div.net_dividend if div.net_dividend is not None else gross - tax))
        flow_date = div.payment_date or div.ex_date
        cash_flows.append((flow_date, _to_cny_or_original(db, net, currency)))

    if current_market_value_cny > 0:
        cash_flows.append((date.today(), current_market_value_cny))

    xirr_rate = _xirr(cash_flows)

    try:
        total_return_usd = exchange_rate_service.convert_to_usd(db, total_return_cny)
        net_invested_principal_usd = exchange_rate_service.convert_to_usd(db, net_invested_principal_cny)
        current_market_value_usd = exchange_rate_service.convert_to_usd(db, current_market_value_cny)
    except ValueError:
        total_return_usd = Decimal("0")
        net_invested_principal_usd = Decimal("0")
        current_market_value_usd = Decimal("0")

    return {
        'total_return_cny': float(total_return_cny),
        'total_return_usd': float(total_return_usd),
        'total_return': float(total_return_cny),
        'total_return_rate': float(total_return_rate),
        'annualized_return_rate': float(xirr_rate * Decimal("100")) if xirr_rate is not None else None,
        'net_invested_principal_cny': float(net_invested_principal_cny),
        'net_invested_principal_usd': float(net_invested_principal_usd),
        'current_market_value_cny': float(current_market_value_cny),
        'current_market_value_usd': float(current_market_value_usd),
        'realized_trading_pnl_cny': float(realized_trading_pnl_cny),
        'unrealized_pnl_cny': float(unrealized_pnl_cny),
        'net_dividend_income_cny': float(net_dividend_cny),
        'cash_flow_count': len(cash_flows),
        'rate_denominator': 'net_invested_principal_cny',
        'annualized_method': 'xirr',
        'base_currency': 'CNY',
    }
