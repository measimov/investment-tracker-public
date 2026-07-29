#!/usr/bin/env python3
"""行情覆盖度只读体检报告。

评估 GET 统计接口的服务端估值价格(resolve_server_prices)对当前持仓的实际覆盖:
- 每个用户、每个市场的持仓价格来源分布 (holding / latest_history / missing)
- 价格新鲜度: holding.price_updated_at 距今天数、latest_history 收盘日距今天数
- 明确列出 missing 和严重过期的标的, 供决策是否需要引入第二行情源

只读, 不写任何表。在部署机上运行:
    docker compose run --rm backend python scripts/price_coverage_report.py
或本地激活 venv 后:
    cd backend && python scripts/price_coverage_report.py
"""

from collections import defaultdict
from datetime import date, datetime, timezone

from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.holding import Holding
from app.models.security_price import SecurityPrice
from app.models.user import User
from app.services.statistics_service import resolve_server_prices

STALE_HOLDING_DAYS = 7  # holding.current_price 超过该天数未更新视为过期
STALE_HISTORY_DAYS = 14  # 最新历史收盘价超过该天数视为过期


def _days_ago(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        now = datetime.now(timezone.utc) if value.tzinfo else datetime.now()
        return (now - value).days
    if isinstance(value, date):
        return (date.today() - value).days
    return None


def report_user(db: Session, user: User) -> None:
    holdings = (
        db.query(Holding)
        .filter(Holding.user_id == user.id, Holding.quantity > 0)
        .order_by(Holding.market, Holding.symbol)
        .all()
    )
    if not holdings:
        print(f"\n用户 {user.username}: 无持仓, 跳过")
        return

    _, sources = resolve_server_prices(db, user.id)

    # 每个持仓 (symbol, market) 的最新历史价日期, 单次批量查询
    pairs = [(h.symbol, h.market) for h in holdings]
    latest_history: dict[tuple[str, str], date] = {}
    rows = (
        db.query(SecurityPrice.symbol, SecurityPrice.market, SecurityPrice.price_date)
        .filter(tuple_(SecurityPrice.symbol, SecurityPrice.market).in_(pairs))
        .order_by(SecurityPrice.price_date.desc())
        .all()
    )
    for symbol, market, price_date in rows:
        latest_history.setdefault((symbol, market), price_date)

    by_market: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    problems: list[str] = []

    for h in holdings:
        key = f"{h.symbol}:{h.market}"
        source = sources.get(key, "missing")
        by_market[h.market][source] += 1

        if source == "missing":
            problems.append(f"  [缺失]   {h.market:8s} {h.symbol:12s} {h.name or '':16s} 无任何可用价格")
            continue

        if source == "holding":
            age = _days_ago(h.price_updated_at)
            if age is None or age > STALE_HOLDING_DAYS:
                shown = "从未记录" if age is None else f"{age} 天前"
                problems.append(
                    f"  [过期]   {h.market:8s} {h.symbol:12s} {h.name or '':16s} "
                    f"holding 价更新于 {shown}"
                )
        elif source == "latest_history":
            hist_date = latest_history.get((h.symbol, h.market))
            age = _days_ago(hist_date)
            if age is None or age > STALE_HISTORY_DAYS:
                shown = "无记录" if age is None else f"{age} 天前 ({hist_date})"
                problems.append(
                    f"  [回退旧] {h.market:8s} {h.symbol:12s} {h.name or '':16s} "
                    f"回退到历史收盘价, 最新为 {shown}"
                )

    print(f"\n用户 {user.username}: 持仓 {len(holdings)} 只")
    print(f"  {'市场':10s} {'holding':>8s} {'history':>8s} {'missing':>8s}")
    for market in sorted(by_market):
        counts = by_market[market]
        print(
            f"  {market:10s} {counts.get('holding', 0):8d} "
            f"{counts.get('latest_history', 0):8d} {counts.get('missing', 0):8d}"
        )

    if problems:
        print(f"  问题标的 ({len(problems)}):")
        for line in problems:
            print(line)
    else:
        print("  全部标的价格来源健康")


def main() -> int:
    db = SessionLocal()
    try:
        users = db.query(User).filter(User.is_active.is_(True)).order_by(User.id).all()
        print("=" * 72)
        print(f"行情覆盖度报告  {date.today()}  (只读)")
        print(f"判定阈值: holding 价 >{STALE_HOLDING_DAYS} 天为过期, 历史价 >{STALE_HISTORY_DAYS} 天为过期")
        print("=" * 72)
        for user in users:
            report_user(db, user)
        print()
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
