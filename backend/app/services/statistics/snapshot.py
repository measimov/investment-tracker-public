"""组合快照：持仓表现 + 价格新鲜度 + 市场分布 + 近期交易 + 对账状态（路线图序 5）。

一次调用返回看板所需的全部数据，同时是 LLM 报告（目的③）的结构化输入底座：
所有口径标记（权益仓 exact/experimental）与数据质量信号原样携带。
"""

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from sqlalchemy import func, tuple_
from sqlalchemy.orm import Session

from ...models.broker_account import BrokerAccount
from ...models.reconciliation_snapshot import ReconciliationSnapshot
from ...models.transaction import Transaction
from .aggregates import (
    UNPRICED_POSITIONS_WARNING,
    calculate_performance_summary,
    get_statistics_by_market,
)
from .fx import missing_rate_warning
from .pricing import PRICE_STALE_DAYS, resolve_server_prices

# 分范围对账（东财 stock/hk_connect）同一快照日会有多条：聚合最新快照日
# 的全部 scope，整体状态取最差（任一 MISMATCHED 即红，任一 PENDING 则非绿），
# 避免后创建的绿色 scope 把另一范围的红灯挤出首页。
_STATUS_SEVERITY = {"MISMATCHED": 2, "PENDING": 1, "MATCHED": 0}


def _latest_reconciliations_by_account(
    db: Session, user_id: int, account_ids: List[int]
) -> Dict[int, List[ReconciliationSnapshot]]:
    """每账户最新快照日的全部 scope 行——两条查询取齐全部账户（issue #136：
    此前每账户 2 次查询的 N+1）。"""
    if not account_ids:
        return {}
    latest_dates = dict(
        db.query(
            ReconciliationSnapshot.broker_account_id,
            func.max(ReconciliationSnapshot.snapshot_date),
        )
        .filter(
            ReconciliationSnapshot.user_id == user_id,
            ReconciliationSnapshot.broker_account_id.in_(account_ids),
        )
        .group_by(ReconciliationSnapshot.broker_account_id)
        .all()
    )
    if not latest_dates:
        return {}
    rows_by_account: Dict[int, List[ReconciliationSnapshot]] = defaultdict(list)
    for row in (
        db.query(ReconciliationSnapshot)
        .filter(
            ReconciliationSnapshot.user_id == user_id,
            tuple_(
                ReconciliationSnapshot.broker_account_id,
                ReconciliationSnapshot.snapshot_date,
            ).in_(list(latest_dates.items())),
        )
        .order_by(ReconciliationSnapshot.broker_account_id, ReconciliationSnapshot.id)
        .all()
    ):
        rows_by_account[row.broker_account_id].append(row)
    return rows_by_account


def _performance_warnings(
    performance: Dict[str, Any], has_missing_price_list: bool
) -> Tuple[List[str], List[str]]:
    """看板的业绩类警告：持仓表现的数据质量 + 三块的缺汇率币种合成一条。

    - 缺汇率（#218）：持仓表现、已实现盈亏、股息各自剔除了无法折算的金额，
      此前看板只转述持仓表现那一块，已实现/股息缺汇率时首页毫无提示。三块的
      币种取并集只出一条（原先持仓表现那条被并集那条取代，不重复）。
    - 缺价：持仓表现的「部分当前持仓缺少可用估值价格」是泛泛一句，快照另有
      列出具体标的的那条；两条说的是同一批标的，有具体清单时去掉泛泛那条。
    """
    current = performance.get("current_performance", {})
    current_quality = current.get("data_quality", {}) or {}
    current_missing = set(current_quality.get("missing_rate_currencies") or [])
    current_rate_warning = missing_rate_warning(current_missing)

    missing_rates = set(current_missing)
    for block in ("realized_pnl", "dividend_summary"):
        missing_rates.update(performance.get(block, {}).get("missing_rate_currencies") or [])

    warnings: List[str] = []
    for warning in current_quality.get("warnings", []):
        if current_rate_warning and warning == current_rate_warning:
            continue
        if has_missing_price_list and warning == UNPRICED_POSITIONS_WARNING:
            continue
        warnings.append(warning)
    union_warning = missing_rate_warning(missing_rates)
    if union_warning:
        warnings.append(union_warning)
    return warnings, sorted(missing_rates)


def build_portfolio_snapshot(db: Session, user_id: int) -> Dict[str, Any]:
    """组合快照：看板与 LLM 报告共用的一次性全量数据。"""
    prices, sources, freshness = resolve_server_prices(db, user_id)
    performance = calculate_performance_summary(db, user_id, prices)
    markets = get_statistics_by_market(db, user_id)

    recent_transactions = [
        {
            "id": txn.id,
            "symbol": txn.symbol,
            "name": txn.name,
            "market": txn.market,
            "transaction_type": txn.transaction_type,
            "quantity": float(txn.quantity),
            "price": float(txn.price),
            "transaction_date": txn.transaction_date.isoformat(),
            "currency": txn.currency,
            "broker_account_id": txn.broker_account_id,
        }
        for txn in db.query(Transaction)
        .filter(Transaction.user_id == user_id)
        .order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
        .limit(10)
        .all()
    ]

    # 每个账户附带最近一次对账的红绿状态（对账闭环的看板出口）。
    active_accounts = (
        db.query(BrokerAccount)
        .filter(BrokerAccount.user_id == user_id, BrokerAccount.is_active.is_(True))
        .order_by(BrokerAccount.id)
        .all()
    )
    reconciliations = _latest_reconciliations_by_account(
        db, user_id, [account.id for account in active_accounts]
    )
    accounts = []
    for account in active_accounts:
        rows = reconciliations.get(account.id, [])
        latest_reconciliation = None
        if rows:
            overall = max(
                (row.status for row in rows),
                key=lambda status: _STATUS_SEVERITY.get(status, 2),
            )
            latest_reconciliation = {
                "snapshot_date": rows[0].snapshot_date.isoformat(),
                "status": overall,
                "all_scoped": all(row.statement_scope for row in rows),
                "scopes": [
                    {
                        "statement_scope": row.statement_scope,
                        "status": row.status,
                        "compared_at": (row.compared_at.isoformat() if row.compared_at else None),
                    }
                    for row in rows
                ],
            }
        accounts.append(
            {
                "id": account.id,
                "account_name": account.account_name,
                "broker": account.broker,
                "base_currency": account.base_currency,
                "latest_reconciliation": latest_reconciliation,
            }
        )

    stale_prices = sorted(
        key for key, info in freshness.items() if info["stale"] and info["source"] != "missing"
    )
    missing_prices = sorted(key for key, info in freshness.items() if info["source"] == "missing")
    warnings, missing_rate_currencies = _performance_warnings(performance, bool(missing_prices))
    if stale_prices:
        warnings.append(
            f"以下标的估值价格超过 {PRICE_STALE_DAYS} 天未更新：{'、'.join(stale_prices)}"
        )
    if missing_prices:
        warnings.append(f"以下标的缺少可用估值价格：{'、'.join(missing_prices)}")

    # 雪球观点数据源停摆预警：只在"已接入但长时间未更新"时报（采集器未启用、
    # 从未跑过是合法形态，不该在看板报警）。探测本身异常静默——观点数据源的故障不配
    # 拖垮整个看板。
    try:
        from ..xueqiu_opinion_source import source_freshness

        opinion_freshness = source_freshness(db)
        if opinion_freshness["available"] and opinion_freshness["stale"]:
            latest = opinion_freshness.get("latest_scan_at") or "未知时间"
            warnings.append(
                f"雪球观点数据源自 {latest} 起未再更新，"
                "雪球采集器可能已停摆（见观点页「采集器」卡片）"
            )
    except Exception:  # noqa: BLE001 —— 预警是锦上添花
        pass

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_currency": "CNY",
        "prices": {
            "map": prices,
            "sources": sources,
            "freshness": freshness,
            "stale_keys": stale_prices,
            "missing_keys": missing_prices,
        },
        "performance": performance,
        "markets": markets,
        "recent_transactions": recent_transactions,
        "accounts": accounts,
        "data_quality": {
            "warnings": warnings,
            "stale_price_count": len(stale_prices),
            "missing_price_count": len(missing_prices),
            "missing_rate_currencies": missing_rate_currencies,
        },
    }
