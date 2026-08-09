"""公司行动的统计口径（从路由层下沉，issue #137）。

现金股息聚合与统计页 get_dividend_summary 同口径：金额归一走内核
`cash_dividend_amounts`（显式 net=0 保留 0，NULL 走 gross−tax 兜底），
CNY 汇总按最新汇率折算、缺汇率剔除并记录币种，原币明细单独给出。
"""

from decimal import Decimal
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from ..models.corporate_action import CorporateAction
from . import exchange_rate_service
from .portfolio.semantics import cash_dividend_amounts


def summarize_cash_dividends(db: Session, actions: List[CorporateAction]) -> Dict[str, Any]:
    """按类型计数 + 现金股息的 CNY 折算汇总与原币分桶。"""
    summary: Dict[str, Any] = {
        "total_count": len(actions),
        "by_type": {},
        "cash_dividends": {
            "count": 0,
            "total_dividend": Decimal("0"),
            "total_tax": Decimal("0"),
            "net_dividend": Decimal("0"),
            # 股息金额跨 CNY/HKD/USD 多币种：汇总必须按最新汇率折算成 CNY
            # （与统计分析页 get_dividend_summary 同口径），原币明细单独给出。
            "base_currency": "CNY",
            "by_currency": {},
            "missing_rate_currencies": [],
        },
    }
    by_currency: dict = {}
    missing_rate_currencies: set = set()

    for action in actions:
        # 按类型统计
        if action.action_type not in summary["by_type"]:
            summary["by_type"][action.action_type] = 0
        summary["by_type"][action.action_type] += 1

        # 现金股息统计
        if action.action_type == "CASH_DIVIDEND":
            summary["cash_dividends"]["count"] += 1
            currency = action.currency or "CNY"
            gross, tax, net = cash_dividend_amounts(action)
            # 缺汇率时不得把外币原值混进 CNY 总额；剔除并记录币种，
            # 原币金额仍完整保留在 by_currency 明细里。
            try:
                gross_cny = exchange_rate_service.convert_to_cny(db, gross, currency)
                tax_cny = exchange_rate_service.convert_to_cny(db, tax, currency)
                net_cny = exchange_rate_service.convert_to_cny(db, net, currency)
            except ValueError:
                missing_rate_currencies.add(currency)
                gross_cny = tax_cny = net_cny = Decimal("0")
            summary["cash_dividends"]["total_dividend"] += gross_cny
            summary["cash_dividends"]["total_tax"] += tax_cny
            summary["cash_dividends"]["net_dividend"] += net_cny
            bucket = by_currency.setdefault(
                currency,
                {
                    "count": 0,
                    "total_dividend": Decimal("0"),
                    "total_tax": Decimal("0"),
                    "net_dividend": Decimal("0"),
                },
            )
            bucket["count"] += 1
            bucket["total_dividend"] += gross
            bucket["total_tax"] += tax
            bucket["net_dividend"] += net

    # 转换Decimal为float以便JSON序列化
    summary["cash_dividends"]["total_dividend"] = float(summary["cash_dividends"]["total_dividend"])
    summary["cash_dividends"]["total_tax"] = float(summary["cash_dividends"]["total_tax"])
    summary["cash_dividends"]["net_dividend"] = float(summary["cash_dividends"]["net_dividend"])
    summary["cash_dividends"]["missing_rate_currencies"] = sorted(missing_rate_currencies)
    summary["cash_dividends"]["by_currency"] = {
        currency: {
            "count": bucket["count"],
            "total_dividend": float(bucket["total_dividend"]),
            "total_tax": float(bucket["total_tax"]),
            "net_dividend": float(bucket["net_dividend"]),
        }
        for currency, bucket in sorted(by_currency.items())
    }

    return summary
