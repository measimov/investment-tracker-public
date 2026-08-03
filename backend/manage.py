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

    parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
