"""解析美股年报（20-F / 10-K）封面的 ADS 换算比（1 ADS = N 股普通股），落 security_profile_data/ads_ratio。

每个标的先拉一次 EDGAR submissions 取最新年报：最新年报已按当前解析器版本处理过的零下载
（`--force` 强制重下重解析）。10-K 封面没有登记 ADS 的记 no_ads（按 1:1）；v3 起 10-K 也下载
封面（#352，以 ADS 交易的 10-K 申报人此前被按 1:1 估值）。解析失败的标的可在「特例规则」里
手动填写 ADS_RATIO，规则优先于解析值。

用法：
    python scripts/sync_ads_ratios.py --all          # 持仓 ∪ 自选 ∪ 已有 EDGAR 档案的美股
    python scripts/sync_ads_ratios.py --symbol PDD
    python scripts/sync_ads_ratios.py --all --force
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MARKET = "美股"


def _all_symbols(db) -> list:
    from app.models.holding import Holding
    from app.models.security_profile import SecurityProfileData
    from app.models.watchlist_item import WatchlistItem

    symbols = {
        row[0]
        for row in db.query(Holding.symbol)
        .filter(Holding.market == MARKET, Holding.quantity > 0)
        .distinct()
    }
    symbols |= {
        row[0]
        for row in db.query(WatchlistItem.symbol).filter(WatchlistItem.market == MARKET).distinct()
    }
    symbols |= {
        row[0]
        for row in db.query(SecurityProfileData.symbol)
        .filter(
            SecurityProfileData.market == MARKET,
            SecurityProfileData.dataset == "edgar_companyfacts",
        )
        .distinct()
    }
    return sorted(symbols)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", action="append", help="可重复")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--force", action="store_true", help="忽略缓存，重新下载并解析")
    args = parser.parse_args()
    if not args.all and not args.symbol:
        parser.error("指定 --symbol 或 --all")

    from app.database import SessionLocal
    from app.services.ads_ratio_service import ensure_ads_ratio

    db = SessionLocal()
    failures = 0
    try:
        symbols = _all_symbols(db) if args.all else sorted({s.strip().upper() for s in args.symbol})
        for symbol in symbols:
            try:
                outcome = ensure_ads_ratio(db, symbol, force=args.force)
            except Exception as exc:  # 单标的失败不中断
                db.rollback()
                failures += 1
                print(f"  {symbol}: 异常 {type(exc).__name__}: {str(exc)[:150]}")
                continue
            status = outcome["status"]
            if status in ("failed", "capped", "not_found", "cover_unknown"):
                failures += 1
            detail = ""
            if outcome.get("ratio"):
                detail = f" 1 ADS = {outcome['ratio']} 股（{outcome.get('form')} {outcome.get('filing_date')}）"
            elif outcome.get("error"):
                detail = f" {outcome['error']}"
            elif outcome.get("form"):
                detail = f" 最新年报 {outcome['form']}"
            print(f"  {symbol}: {status}{detail}")
        print(f"共 {len(symbols)} 个标的，未得到换算比 {failures} 个（not_20f、no_ads 不计）")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
