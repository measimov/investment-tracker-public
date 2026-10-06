"""跨测试文件共享的构造/清理助手。

reset_tables 的模型列表由各测试文件自持（RESET_MODELS 常量）：删除顺序对
外键敏感，且每个文件的清空范围各不相同——不要在这里统一列表。
"""

import importlib.util
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from app.models.broker_account import BrokerAccount
from app.models.holding import Holding
from app.models.transaction import Transaction
from app.models.user import User


_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def load_migration(revision: str):
    """按 revision 号加载迁移脚本模块（文件名以数字开头，不能普通 import）。"""
    matches = sorted(_MIGRATIONS_DIR.glob(f"{revision}_*.py"))
    assert len(matches) == 1, f"revision {revision} 应恰好对应一个迁移文件，实得 {matches}"
    spec = importlib.util.spec_from_file_location(f"_migration_{revision}", matches[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_migration(db, revision: str, direction: str = "upgrade"):
    """在当前会话的连接/事务里跑**真实**迁移脚本的 upgrade/downgrade。

    刻意不复制迁移里的 SQL：复制版永远为真，迁移改了也照样绿，正是这种
    "迁移漂移"让重复追加审计备注的缺陷躲过了整套测试。走 alembic 的
    Operations 代理即可让脚本里的 `op.*` 落到本连接上，调用方 rollback
    就能还原（PostgreSQL 的 DDL 是事务性的），不污染共享测试库。
    """
    module = load_migration(revision)
    connection = db.connection()
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        getattr(module, direction)()
    return module


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


@contextmanager
def drifted_last_seen_column(db):
    """临时把 utterances.last_seen_at 改名，复现列漂移导致的真实 SQL 错误。"""
    db.execute(
        text(
            "ALTER TABLE xueqiu_archiver_utterances RENAME COLUMN last_seen_at TO last_seen_at_drift"
        )
    )
    db.commit()
    try:
        yield
    finally:
        db.rollback()
        db.execute(
            text(
                "ALTER TABLE xueqiu_archiver_utterances RENAME COLUMN last_seen_at_drift TO last_seen_at"
            )
        )
        db.commit()


def seed_legacy_accepted_dividend(db, user, suggestion_id, overrides=None):
    """Seed a pre-0042 announcement booking for history/revision regression tests.

    Never use the removed cash-accept API to construct historical fixtures.
    Stock acceptance remains exercised through the live service.
    """
    from app.models.corporate_action import CorporateAction
    from app.models.corporate_action_suggestion import CorporateActionSuggestion
    from app.services.dividend_sync_service import accept_suggestion

    suggestion = db.get(CorporateActionSuggestion, suggestion_id)
    if suggestion.action_type != "CASH_DIVIDEND":
        return accept_suggestion(db, user, suggestion_id, overrides or {})
    values = overrides or {}
    gross = Decimal(str(values.get("total_dividend", suggestion.estimated_total_dividend or 0)))
    tax = Decimal(str(values.get("tax_withheld", 0)))
    action = CorporateAction(
        user_id=user.id,
        symbol=suggestion.symbol,
        market=suggestion.market,
        broker_account_id=values.get("broker_account_id", suggestion.broker_account_id),
        action_type="CASH_DIVIDEND",
        ex_date=suggestion.ex_date,
        payment_date=suggestion.pay_date,
        currency=suggestion.currency,
        total_dividend=gross,
        tax_withheld=tax,
        net_dividend=gross - tax,
    )
    db.add(action)
    db.flush()
    suggestion.status = "ACCEPTED"
    suggestion.created_corporate_action_id = action.id
    db.commit()
    return action
