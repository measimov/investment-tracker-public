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
    reference = subcommands.add_parser(
        "sync-reference-rates",
        help="Backfill/refresh reference rate series (SHIBOR 3M, US T-bill 3M) used as "
        "the risk-free rate",
    )
    reference.add_argument(
        "--start", help="backfill from this date (YYYY-MM-DD; default: earliest transaction - 15d)"
    )
    adj = subcommands.add_parser(
        "recompute-hk-adj-factors",
        help="Recompute HK adj_factor/adj_close_price from cached HKEXnews cash-dividend "
        "forms (no network; forms are fetched by the dividend sync job)",
    )
    adj.add_argument("--symbol", action="append", help="limit to HK symbols, repeatable")
    catalog = subcommands.add_parser(
        "sync-security-catalog",
        help="Refresh the security catalog (Tushare basics + HKEX list of securities)",
    )
    catalog.add_argument("--market", action="append", help="limit to a market, repeatable")
    catalog.add_argument("--source", action="append", help="limit to a loader source, repeatable")
    catalog.add_argument(
        "--no-force", action="store_true", help="skip sources refreshed within the interval"
    )
    industries = subcommands.add_parser(
        "sync-security-industries",
        help="Refresh industry classification for held/watched securities "
        "(Tushare / EDGAR SIC first, East Money F10 fills gaps)",
    )
    industries.add_argument(
        "--force", action="store_true", help="refetch everything, ignoring the freshness window"
    )
    collector = subcommands.add_parser(
        "xueqiu-collector",
        help="Run the Xueqiu author-utterance collector (long-running loop by default)",
    )
    collector.add_argument("--once", action="store_true", help="run one authors cycle and exit")
    collector.add_argument(
        "--author", action="append", help="limit to these Xueqiu user ids (repeatable; implies --once)"
    )
    collector.add_argument(
        "--dry-run", action="store_true",
        help="fetch and parse but write nothing (implies --once)",
    )
    collector.add_argument(
        "--max-posts", type=int,
        help="low-cost check: profile page 1, at most N candidate posts, 1 comment page each "
        "(use with --dry-run for a shadow run; implies --once)",
    )
    collector.add_argument(
        "--symbols-once", action="store_true",
        help="run one per-symbol cycle (announcements/discussion + cubes) and exit",
    )
    collector.add_argument(
        "--symbol", action="append",
        help="with --symbols-once: only these codes (our code, e.g. 600519 / 00700; "
        "repeatable; needs --market; skips cubes and does not mark the day as done)",
    )
    collector.add_argument("--market", help="market of --symbol (A股/B股/港股/美股)")
    collector.add_argument(
        "--database-url",
        help="shadow run against another (test) database; name must contain test/e2e/shadow",
    )
    archive = subcommands.add_parser(
        "xueqiu-import-archive-exports",
        help="one-off idempotent import of xueqiu-timeline-archiver per-symbol Markdown exports",
    )
    archive.add_argument("--dir", required=True, help="the old repo's exports/ directory")
    archive.add_argument("--dry-run", action="store_true", help="parse and count, write nothing")
    subcommands.add_parser(
        "xueqiu-collector-health",
        help="exit 0 if the collector heartbeat is fresh (docker healthcheck)",
    )
    subcommands.add_parser(
        "notify-test",
        help="Send a test notification to every NOTIFY_URLS channel (URLs are masked in output)",
    )
    notify = subcommands.add_parser(
        "notify",
        help="Raise or resolve an external alert (e.g. from backup.sh); goes through the same "
        "state machine as the periodic checks (no repeat pushes while active)",
    )
    notify.add_argument("--key", required=True, help="alert key, e.g. backup")
    notify.add_argument(
        "--severity", choices=("info", "warning", "critical"), default="warning"
    )
    notify.add_argument("--title", help="alert title (required unless --resolve)")
    notify.add_argument("--message", default="", help="alert details")
    notify.add_argument("--resolve", action="store_true", help="mark the alert as recovered")
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

def sync_reference_rates(start) -> int:
    from datetime import date

    from app.database import SessionLocal
    from app.services.reference_rate_service import SERIES, sync_series

    db = SessionLocal()
    failed = 0
    try:
        for series in SERIES:
            outcome = sync_series(db, series, start=date.fromisoformat(start) if start else None)
            print(f"{series}: wrote {outcome['written']} row(s), ranges {outcome['ranges']}")
            if outcome["error"]:
                failed += 1
                print(f"  ERROR {outcome['error']}")
    finally:
        db.close()
    return 1 if failed else 0


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


def recompute_hk_adj_factors(symbols) -> int:
    from app.database import SessionLocal
    from app.models.security_price import SecurityPrice
    from app.services.hk_adjustment_factors import MARKET, recompute_hk_adj_factors as run
    from app.services.statistics.fx import DbExchangeRateLookup

    db = SessionLocal()
    try:
        if not symbols:
            symbols = [
                row[0]
                for row in db.query(SecurityPrice.symbol)
                .filter(SecurityPrice.market == MARKET)
                .distinct()
                .order_by(SecurityPrice.symbol)
            ]
        lookup = DbExchangeRateLookup.from_db(db)
        unresolved = 0
        for symbol in symbols:
            result = run(db, symbol, rate_lookup=lookup, commit=True)
            applied = sum(1 for e in result["events"] if e["status"] == "applied")
            bad = [e for e in result["events"] if e["status"] == "unresolved"]
            unresolved += len(bad)
            print(
                f"  {symbol}: rows={result['rows']} updated={result['updated']} "
                f"ex_dates applied={applied} unresolved={len(bad)}"
            )
            for event in bad:
                print(f"    UNRESOLVED {event['ex_date']}: {event.get('reason')}")
    finally:
        db.close()
    return 1 if unresolved else 0


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


def sync_security_industries(*, force: bool) -> int:
    from app.database import SessionLocal
    from app.services.security_industry_service import sync_security_industries as run_sync

    db = SessionLocal()
    try:
        result = run_sync(db, force=force)
    finally:
        db.close()
    failed = 0
    for source, item in result["sources"].items():
        line = (
            f"  {source:<10s} {item['status']:<8s} requested={item['requested']} "
            f"written={item['written']} missing={len(item['missing'])}"
        )
        if item["status"] in ("failed", "partial"):
            failed += 1
            line += f" errors={item['errors'][:3]}"
        print(line)
    print(
        f"Scope {result['scope']} securities, {result['resolved']} with an industry; "
        f"unresolved: {', '.join(result['unresolved']) or 'none'}"
    )
    print(f"Done in {result['duration_seconds']:.1f}s; {failed} source(s) failed.")
    return 1 if failed else 0


SHADOW_DB_MARKERS = ("test", "e2e", "shadow")


def xueqiu_collector(args) -> int:
    import signal
    import threading

    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import sessionmaker

    from app.database import SessionLocal, engine
    from app.services.xueqiu_collector import runner
    from app.services.xueqiu_collector.state import Heartbeat

    stop_event = threading.Event()

    def _stop(signum, _frame):
        print(f"received signal {signum}, stopping at the next boundary...", flush=True)
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if (args.symbol or args.market) and not args.symbols_once:
        print("--symbol / --market only apply to --symbols-once")
        return 2
    if args.symbols_once and (args.author or args.dry_run or args.once or args.max_posts):
        print("--symbols-once cannot be combined with --once / --author / --dry-run / --max-posts")
        return 2
    if args.symbol and not args.market:
        print("--symbol needs --market (A股/B股/港股/美股)")
        return 2

    once = (
        args.once or args.dry_run or bool(args.author) or bool(args.database_url)
        or args.symbols_once or bool(args.max_posts)
    )
    if not once:
        runner.run_collector_loop(stop_event)
        return 0

    session_factory, bind = SessionLocal, engine
    if args.database_url:
        name = make_url(args.database_url).database or ""
        if not any(marker in name.lower() for marker in SHADOW_DB_MARKERS):
            print(f"refusing --database-url {name!r}: name must contain one of {SHADOW_DB_MARKERS}")
            return 2
        bind = create_engine(args.database_url, pool_pre_ping=True)
        session_factory = sessionmaker(bind=bind)
        print(f"shadow run against database {name!r}")

    if args.symbols_once:
        return _xueqiu_symbols_once(args, session_factory(), Heartbeat(bind), stop_event)

    db = session_factory()
    try:
        heartbeat = None if args.dry_run else Heartbeat(bind)
        knobs = runner.CollectorKnobs.from_settings()
        if args.max_posts:
            knobs.max_posts = max(1, args.max_posts)
            knobs.max_comment_pages = 1
        result = runner.run_authors_cycle(
            db,
            author_ids=args.author,
            dry_run=args.dry_run,
            knobs=knobs,
            stop_event=stop_event,
            heartbeat=heartbeat,
        )
    finally:
        db.close()
    print(f"cycle status={result.status} requests={result.request_count}")
    if result.cookie:
        print(f"cookie: {result.cookie['message']}")
    for item in result.authors:
        print(
            f"  {item.author_id}: {item.status} candidates={item.candidate_count} "
            f"replies={item.reply_count} profile_utterances={item.utterance_count}"
            + (f" error={item.error}" if item.error else "")
        )
        if args.dry_run or args.database_url:
            for key in sorted(set(item.utterance_keys)):
                print(f"    {key}")
    if result.message:
        print(result.message)
    return 0 if result.status in ("ok", "no_authors") else 1


def _xueqiu_symbols_once(args, db, heartbeat, stop_event) -> int:
    from app.services.xueqiu_collector import symbols

    try:
        targets = symbols.explicit_targets(args.symbol, args.market) if args.symbol else None
    except ValueError as exc:
        print(str(exc))
        db.close()
        return 2
    try:
        result = symbols.run_symbols_cycle(
            db,
            targets=targets,
            include_market_wide=targets is None,
            record_daily=targets is None,
            stop_event=stop_event,
            heartbeat=heartbeat,
        )
    finally:
        db.close()
    print(f"symbols cycle status={result.status} requests={result.request_count}")
    if result.message:
        print(result.message)
    for failure in result.failures:
        print(f"  failed: {failure}")
    return 0 if result.status == "ok" else 1


def xueqiu_import_archive_exports(args) -> int:
    from pathlib import Path

    from app.database import SessionLocal
    from app.services.xueqiu_collector.archive_import import import_archive_exports

    directory = Path(args.dir)
    if not directory.is_dir():
        print(f"not a directory: {directory}")
        return 2
    db = SessionLocal()
    try:
        stats = import_archive_exports(db, directory, dry_run=args.dry_run)
    finally:
        db.close()
    mode = "dry-run (nothing written)" if args.dry_run else "imported"
    print(f"{mode}: files={stats.files} symbols={len(stats.symbols)} "
          f"malformed_blocks={stats.malformed_blocks}")
    for key in sorted(stats.parsed):
        print(
            f"  {key}: parsed={stats.parsed[key]} unique={len(stats.unique.get(key, ()))} "
            f"inserted={stats.inserted.get(key, 0)} "
            f"with_author={stats.with_author.get(key, 0)} with_text={stats.with_text.get(key, 0)}"
        )
    if stats.skipped_files:
        print(f"  skipped (unrecognized symbol): {', '.join(stats.skipped_files)}")
    return 0


def xueqiu_collector_health() -> int:
    from app.config import settings
    from app.services.xueqiu_collector.state import heartbeat_age_seconds

    age = heartbeat_age_seconds()
    limit = settings.xueqiu_collector_health_max_age_minutes * 60
    if age is None:
        print("no heartbeat file")
        return 1
    print(f"heartbeat age {age:.0f}s (limit {limit}s)")
    return 0 if age <= limit else 1


def notify_test() -> int:
    from app.services import notification_service

    summary = notification_service.channel_summary()
    if not summary["configured"]:
        print("NOTIFY_URLS is empty: no notification channel configured")
    for item in summary["channels"]:
        flag = "ok" if item["valid"] else "UNRECOGNIZED"
        print(f"channel {item['kind']:<8} {item['channel']}  [{flag}]")
    result = notification_service.send_test()
    print(f"result: {result['status']} - {result['message']}")
    for item in result["channels"]:
        detail = "sent" if item["ok"] else f"failed ({item['error']})"
        print(f"  {item['channel']}: {detail}")
    return 0 if result["ok"] else 1


_ALERT_KEY_RE = r"^[A-Za-z0-9_.:-]{1,200}$"


def notify(args) -> int:
    import re

    from app.database import SessionLocal
    from app.services import alert_service

    if not re.match(_ALERT_KEY_RE, args.key):
        print(f"invalid --key {args.key!r}: use letters, digits and _ . : - (max 200)")
        return 2
    if not args.resolve and not (args.title or "").strip():
        print("--title is required unless --resolve")
        return 2
    db = SessionLocal()
    try:
        if args.resolve:
            outcome = alert_service.resolve_alert(db, args.key)
        else:
            outcome = alert_service.raise_alert(
                db,
                alert_service.Alert(
                    key=args.key,
                    severity=args.severity,
                    title=args.title.strip(),
                    message=args.message or "",
                ),
            )
    finally:
        db.close()
    status = outcome.get("notify_status") or ("pushed" if outcome["notified"] else "not pushed")
    print(f"alert {outcome['key']}: {outcome['action']} ({status})")
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "xueqiu-collector-health":
        # healthcheck 每几分钟跑一次：不配置日志，免得每次探活都往文件里写一行
        return xueqiu_collector_health()

    if args.command == "xueqiu-collector":
        # 独立进程、同一日志目录：用自己的日志文件，不与 Web 进程抢 app.log 的轮转
        configure_logging(app_log_name="xueqiu-collector.log")
        return xueqiu_collector(args)

    configure_logging()

    if args.command == "xueqiu-import-archive-exports":
        return xueqiu_import_archive_exports(args)

    if args.command == "seed":
        created_count = seed_initial_users()
        print(f"Seed complete. Created {created_count} user(s).")
        return 0

    if args.command == "rebuild-holdings":
        return rebuild_holdings()

    if args.command == "sync-hkex-dayquot":
        return sync_hkex_dayquot(args.days)

    if args.command == "sync-reference-rates":
        return sync_reference_rates(args.start)

    if args.command == "recompute-hk-adj-factors":
        return recompute_hk_adj_factors(args.symbol)

    if args.command == "sync-security-catalog":
        return sync_security_catalog(args.market, args.source, force=not args.no_force)

    if args.command == "sync-security-industries":
        return sync_security_industries(force=args.force)

    if args.command == "notify-test":
        return notify_test()

    if args.command == "notify":
        return notify(args)

    parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
