import argparse

from app.core.logging import configure_logging
from app.services.user_seed import seed_initial_users


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Investment Tracker management commands")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("seed", help="Create initial admin and user accounts")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    configure_logging()

    if args.command == "seed":
        created_count = seed_initial_users()
        print(f"Seed complete. Created {created_count} user(s).")
        return 0

    parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
