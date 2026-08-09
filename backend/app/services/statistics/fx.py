"""汇率装载与折算口径（统计编排层专用；纯换算逻辑在 portfolio/fx 内核）。"""

from datetime import date
from decimal import Decimal
from typing import Optional, Set

from sqlalchemy.orm import Session

from ...models.exchange_rate import ExchangeRate
from ...models.transaction import Transaction
from .. import exchange_rate_service
from ..portfolio.fx import ExchangeRateLookup, convert_on_date


class DbExchangeRateLookup(ExchangeRateLookup):
    """纯内核 ExchangeRateLookup + DB 装载入口。

    每次 from_db 全量加载 active 汇率并排序——**每个请求只构造一次**，
    由入口函数向下传递（issue #136：此前一次 analytics 请求构造两次、
    performance_summary 再来一次）。
    """

    @classmethod
    def from_db(cls, db: Session) -> "DbExchangeRateLookup":
        records = (
            db.query(ExchangeRate)
            .filter(
                ExchangeRate.is_active.is_(True),
            )
            .order_by(
                ExchangeRate.from_currency,
                ExchangeRate.to_currency,
                ExchangeRate.effective_date,
            )
            .all()
        )
        return cls(records)


def to_cny_on_date(
    amount: Decimal,
    currency: Optional[str],
    effective_date: date,
    rate_lookup: ExchangeRateLookup,
) -> Decimal:
    return convert_on_date(amount, currency, effective_date, rate_lookup)


def to_usd_or_zero(db: Session, amount_cny: Decimal) -> Decimal:
    """CNY→USD 展示换算；缺 USD/CNY 汇率时按既有口径归零。"""
    try:
        return exchange_rate_service.convert_to_usd(db, amount_cny)
    except ValueError:
        return Decimal("0")


def to_cny_or_track_missing(
    db: Session,
    amount: Decimal,
    currency: str,
    missing: Set[str],
) -> Decimal:
    """折算 CNY；缺汇率时**剔除并记录币种**，返回 0。

    此前四处汇总（summary / by_market / 持仓表现 / 已实现盈亏）在缺汇率时
    走的是 `except ValueError: total += amount`——把 USD/HKD 原值直接混进
    CNY 总额，无 warning 无标记。系统对 stale price、oversell、缺行情都有
    可见的数据质量信号，唯独缺汇率会静默污染最核心的 CNY 汇总；而
    refresh_rates_if_stale 保证 USD/HKD/SGD 常在，兜底几乎只在新币种/新库时
    触发——正是最不该静默出错的时刻。

    统一到股息汇总早已采用的正确口径：剔除 + 记录，由调用方汇报给前端。
    """
    try:
        return exchange_rate_service.convert_to_cny(db, amount, currency)
    except ValueError:
        missing.add(currency)
        return Decimal("0")


def missing_rate_warning(missing: Set[str]) -> Optional[str]:
    """缺汇率币种 → 统一的中文数据质量提示（进 data_quality.warnings）。"""
    if not missing:
        return None
    return (
        f"缺少 {'/'.join(sorted(missing))} 对 CNY 的汇率，"
        "这些币种的金额未计入 CNY 汇总（不会按原值混入）。"
    )


def txn_signed_cash_flow(txn: Transaction) -> Optional[Decimal]:
    """BUY → -(毛额+费)、SELL → 毛额-费；其余类型不产生外部现金流。"""
    gross = Decimal(str(txn.quantity)) * Decimal(str(txn.price))
    fee = Decimal(str(txn.fee or 0))
    if txn.transaction_type == "BUY":
        return -(gross + fee)
    if txn.transaction_type == "SELL":
        return gross - fee
    return None
