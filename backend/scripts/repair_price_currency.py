#!/usr/bin/env python3
"""修正 security_prices 存量行的 currency（#276）。

#276 之前各写入方各自推断行情币种：B 股一律写 CNY（沪 B 应为美元、深 B 应为港元），港股
一律写 HKD（人民币柜台应为 CNY），同一行的币种还会随写入顺序来回翻转。修复后所有写入都经
market_data_service.resolve_price_currency，这个脚本把存量行按同一规则改正。

只处理有确定报价规则的市场（A股/B股/港股/美股）；逐标的一次 UPDATE，幂等，可重复运行。

    cd backend && source venv/bin/activate
    python scripts/repair_price_currency.py --dry-run    # 先看会改哪些
    python scripts/repair_price_currency.py

部署机上：

    docker compose exec -T backend python scripts/repair_price_currency.py --dry-run
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MARKETS = ("A股", "B股", "港股", "美股")


def repair(*, dry_run: bool):
    from sqlalchemy import func

    from app.database import SessionLocal
    from app.models.security_price import SecurityPrice
    from app.services.market_data_service import resolve_price_currency

    db = SessionLocal()
    changes = []
    try:
        groups = (
            db.query(
                SecurityPrice.symbol, SecurityPrice.market, SecurityPrice.currency, func.count()
            )
            .filter(SecurityPrice.market.in_(MARKETS))
            .group_by(SecurityPrice.symbol, SecurityPrice.market, SecurityPrice.currency)
            .order_by(SecurityPrice.market, SecurityPrice.symbol)
            .all()
        )
        resolved_cache = {}
        for symbol, market, currency, count in groups:
            key = (symbol, market)
            if key not in resolved_cache:
                resolved_cache[key] = resolve_price_currency(db, symbol, market)
            target = resolved_cache[key]
            if currency == target:
                continue
            changes.append((symbol, market, currency, target, int(count)))
            if not dry_run:
                (
                    db.query(SecurityPrice)
                    .filter(
                        SecurityPrice.symbol == symbol,
                        SecurityPrice.market == market,
                        SecurityPrice.currency == currency,
                    )
                    .update({SecurityPrice.currency: target}, synchronize_session=False)
                )
        if not dry_run:
            db.commit()
        return changes
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="只列出将要修改的行，不写库")
    args = parser.parse_args()

    changes = repair(dry_run=args.dry_run)
    verb = "将修改" if args.dry_run else "已修改"
    total = sum(change[4] for change in changes)
    print(f"{verb} {len(changes)} 组、{total} 行")
    for symbol, market, old, new, count in changes:
        print(f"  {symbol}（{market}）{old} → {new}：{count} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
