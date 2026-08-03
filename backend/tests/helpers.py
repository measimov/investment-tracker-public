"""跨测试文件共享的构造/清理助手。

reset_tables 的模型列表由各测试文件自持（RESET_MODELS 常量）：删除顺序对
外键敏感，且每个文件的清空范围各不相同——不要在这里统一列表。
"""

from datetime import date
from decimal import Decimal

from app.models.broker_account import BrokerAccount
from app.models.holding import Holding
from app.models.transaction import Transaction
from app.models.user import User


def reset_tables(db, models):
    """按调用方给定顺序逐表删除后提交。"""
    for model in models:
        db.query(model).delete()
    db.commit()


def make_account(db, broker="CMB", *, commit=False, **overrides):
    values = {"user_id": 1, "broker": broker, "account_name": broker, "base_currency": "CNY"}
    values.update(overrides)
    account = BrokerAccount(**values)
    db.add(account)
    if commit:
        db.commit()
        db.refresh(account)
    else:
        db.flush()
    return account


def add_transaction(db, **overrides):
    values = {
        "user_id": 1,
        "symbol": "AAPL",
        "name": "Apple",
        "market": "美股",
        "transaction_type": "BUY",
        "quantity": Decimal("100"),
        "price": Decimal("10"),
        "fee": Decimal("0"),
        "transaction_date": date(2026, 1, 1),
        "currency": "USD",
    }
    values.update(overrides)
    txn = Transaction(**values)
    db.add(txn)
    db.flush()
    return txn


def get_user(db):
    return db.query(User).filter(User.id == 1).one()


def get_rows(db, symbol="AAPL", market="美股"):
    return (
        db.query(Holding)
        .filter(Holding.user_id == 1, Holding.symbol == symbol, Holding.market == market)
        .order_by(Holding.id)
        .all()
    )


def ibkr_csv(*data_rows: str) -> bytes:
    header = "\n".join(
        [
            "Statement,Header,域名称,域值",
            "总结,Header,域名称,域值",
            "总结,Data,基础货币,USD",
            "Transaction History,Header,日期,账户,说明,交易类型,代码,数量,价格,"
            "Price Currency,总额,佣金,净额",
        ]
    )
    return (header + "\n" + "\n".join(data_rows) + "\n").encode("utf-8")


def seed_security_rule(db, user_id, rule_type, symbol, market=None, payload=None, note=None):
    """测试用：直接种一条账本特例规则（表驱动后测试需自备规则数据）。"""
    from app.models.security_rule import SecurityRule

    rule = SecurityRule(
        user_id=user_id,
        rule_type=rule_type,
        symbol=symbol,
        market=market,
        payload=payload,
        note=note,
    )
    db.add(rule)
    db.commit()
    return rule


PCT_RELISTING_PAYLOAD = {
    "new_symbol": "PCT",
    "new_market": "新加坡股",
    "new_currency": "SGD",
    "old_currency": "HKD",
    "name": "柏能集团",
}
