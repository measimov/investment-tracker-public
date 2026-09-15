import argparse

from app.core.logging import configure_logging
from app.services.user_seed import seed_initial_users


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Investment Tracker management commands")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("seed", help="Create initial admin and user accounts")
    subcommands.add_parser(
        "rebuild-holdings",
        help="Replay every user's holdings per broker account (run after the "
        "account-scoped-holdings migration)",
    )
    dayquot = subcommands.add_parser(
        "sync-hkex-dayquot",
        help="Pull HKEX Daily Quotations (official HK closes) for tracked HK "
        "securities over the last N days (site keeps ~1 month)",
    )
    dayquot.add_argument("--days", type=int, default=10, help="calendar days to look back")
    catalog = subcommands.add_parser(
        "sync-security-catalog",
        help="Refresh the security catalog (Tushare basics + HKEX list of securities)",
    )
    catalog.add_argument("--market", action="append", help="limit to a market, repeatable")
    catalog.add_argument("--source", action="append", help="limit to a loader source, repeatable")
    catalog.add_argument(
        "--no-force", action="store_true", help="skip sources refreshed within the interval"
    )
    return parser


def rebuild_holdings() -> int:
    from sqlalchemy import union

    from app.database import SessionLocal
    from app.models.corporate_action import CorporateAction
    from app.models.holding import Holding
    from app.models.transaction import Transaction
    from app.services.holding_service import recalculate_holdings

    db = SessionLocal()
    rebuilt = 0
    failures = []
    try:
        txn_keys = db.query(
            Transaction.user_id, Transaction.symbol, Transaction.market
        )
        action_keys = db.query(
            CorporateAction.user_id, CorporateAction.symbol, CorporateAction.market
        )
        # 现存持仓行也纳入键集合：某标的的交易被全部删除后，其持仓行
        # 不再出现在交易/公司行动键里，重放会跳过它留下孤儿行——
        # recalculate_holdings 对零事件键的语义就是删除持仓。
        holding_keys = db.query(Holding.user_id, Holding.symbol, Holding.market)
        keys = sorted(set(db.execute(union(txn_keys, action_keys, holding_keys))))
        for user_id, symbol, market in keys:
            try:
                recalculate_holdings(db, user_id, symbol, market)
                rebuilt += 1
            except ValueError as exc:
                db.rollback()
                failures.append((user_id, symbol, market, str(exc)))
        print(f"Rebuilt holdings for {rebuilt}/{len(keys)} (user, symbol, market) keys.")
        if failures:
            print("Failures (fix the data, then rerun):")
            for user_id, symbol, market, message in failures:
                print(f"  user={user_id} {symbol}({market}): {message}")
            return 1
        return 0
    finally:
        db.close()

def sync_hkex_dayquot(days: int) -> int:
    from app.database import SessionLocal
    from app.services.hkex_dayquot_source import sync_recent_dayquots

    db = SessionLocal()
    try:
        result = sync_recent_dayquots(db, lookback_days=days, max_reports=days)
    finally:
        db.close()
    print(
        f"Universe {result['universe']} HK securities; processed {len(result['processed'])} "
        f"report(s), already synced {len(result['skipped_done'])}, "
        f"no report {len(result['no_report'])}."
    )
    for item in result["processed"]:
        print(
            f"  {item['report_date']}: stored {item['stored']}, missing {item['missing']}, "
            f"suspended {item['suspended']}, unpriced {item['unpriced']}"
        )
        for conflict in item["conflicts"]:
            print(
                f"    CONFLICT {conflict['symbol']}: {conflict['existing_source']} "
                f"{conflict['existing_close']} -> official {conflict['official_close']}"
            )
    for error in result["errors"]:
        print(f"  ERROR {error['report_date']}: {error['error']}")
    return 1 if result["errors"] else 0


def sync_security_catalog(markets, sources, *, force: bool) -> int:
    from app.database import SessionLocal
    from app.services.security_catalog_service import sync_security_catalog as run_sync

    db = SessionLocal()
    try:
        result = run_sync(db, markets=markets, sources=sources, force=force)
    finally:
        db.close()
    failed = 0
    for item in result["sources"]:
        line = f"  {item['source']:<22s} {item['status']:<8s}"
        if item["status"] == "ok":
            line += f" seen={item['rows_seen']} upserted={item['rows_upserted']}"
        elif item["status"] == "skipped":
            line += f" reason={item.get('reason')}"
        else:
            failed += 1
            line += f" error={item.get('error')}"
        print(line)
    print(f"Done in {result['duration_seconds']:.1f}s; {failed} source(s) failed.")
    return 1 if failed else 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    configure_logging()

    if args.command == "seed":
        created_count = seed_initial_users()
        print(f"Seed complete. Created {created_count} user(s).")
        return 0

    if args.command == "rebuild-holdings":
        return rebuild_holdings()

    if args.command == "sync-hkex-dayquot":
        return sync_hkex_dayquot(args.days)

    if args.command == "sync-security-catalog":
        return sync_security_catalog(args.market, args.source, force=not args.no_force)

    parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
