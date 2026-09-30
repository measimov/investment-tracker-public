"""回填人民币汇率中间价历史（#200）：官方有覆盖的日期以官方为准。

生产在 2026-05 之前没有任何汇率行——按日折算查不到「当日或之前」的汇率时退回**最新**
汇率，早年的港币/美元交易于是按今天的汇率折成人民币。本脚本按年切片（上游单次只给一年）
拉取区间内的中间价：
- 没有行的日期新写（source=cfets-ccpr）；
- 第三方（api-ecb / api-backup）写下的同日行改写为官方值；
- 区间内官方没有覆盖的日期（周末、节假日）上的第三方行停用（is_active=False，保留审计）；
- 手工录入（source=manual）的行一律不动。

会改变历史人民币口径的数字：生产上先跑 `metrics_parity_report.py --out` 取快照，回填后
再取一份 `--compare`，差异应全部可由汇率变化解释。可重复执行（已是官方同值的行跳过）。

用法：
    python scripts/backfill_official_fx.py --start 2023-01-01 --dry-run
    python scripts/backfill_official_fx.py --start 2023-01-01
"""

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat, help="默认业务时区今天")
    parser.add_argument("--dry-run", action="store_true", help="只统计将发生的变化，不写库")
    args = parser.parse_args()

    from decimal import Decimal

    from app.core.timeutil import local_today
    from app.database import SessionLocal
    from app.models.exchange_rate import ExchangeRate
    from app.services import chinamoney_source, exchange_rate_service as fx

    end = args.end or local_today()
    if args.start > end:
        parser.error("--start 晚于 --end")

    rows = chinamoney_source.fetch_ccpr_history(args.start, end, fx.REQUIRED_RATE_CURRENCIES)
    print(
        f"中间价 {len(rows)} 个发布日（{rows[0].rate_date} ~ {rows[-1].rate_date}）"
        if rows
        else "区间内没有中间价"
    )
    if not rows:
        return 1

    db = SessionLocal()
    try:
        if args.dry_run:
            existing = {
                (r.from_currency, r.effective_date): r
                for r in db.query(ExchangeRate).filter(
                    ExchangeRate.to_currency == fx.BASE_CURRENCY,
                    ExchangeRate.from_currency.in_(fx.REQUIRED_RATE_CURRENCIES),
                    ExchangeRate.effective_date >= args.start,
                    ExchangeRate.effective_date <= end,
                )
            }
            counts = {"new": 0, "replace_third_party": 0, "unchanged": 0, "manual_kept": 0}
            official_dates = {}
            for row in rows:
                for currency, rate in row.rates.items():
                    official_dates.setdefault(currency, set()).add(row.rate_date)
                    current = existing.get((currency, row.rate_date))
                    if current is None:
                        counts["new"] += 1
                    elif (current.source or "manual") == "manual":
                        counts["manual_kept"] += 1
                    elif (
                        current.source == fx.OFFICIAL_SOURCE and Decimal(str(current.rate)) == rate
                    ):
                        counts["unchanged"] += 1
                    else:
                        counts["replace_third_party"] += 1
            from datetime import timedelta

            from app.config import settings

            def cutoff(currency):
                latest = max(official_dates[currency])
                fresh = latest >= end - timedelta(days=settings.fx_official_max_stale_days)
                return end if fresh else latest

            deactivate = sum(
                1
                for (currency, day), r in existing.items()
                if r.is_active
                and r.source in fx.THIRD_PARTY_SOURCES
                and currency in official_dates
                and day <= cutoff(currency)
                and day not in official_dates[currency]
            )
            print(
                f"[dry-run] 新写 {counts['new']}，改写第三方 {counts['replace_third_party']}，"
                f"已是官方同值 {counts['unchanged']}，手工行保留 {counts['manual_kept']}，"
                f"停用非发布日第三方行 {deactivate}"
            )
            return 0

        stats = fx.apply_official_rows(db, rows, args.start, end)
        print(f"写入/改写 {stats['written']} 行，停用非发布日第三方行 {stats['deactivated']} 行")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
