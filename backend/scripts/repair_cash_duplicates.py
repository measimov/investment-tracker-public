"""默认只读；显式来源配对 + 审阅计划才可合并跨文件重复现金事实。"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    from app.database import SessionLocal
    from app.services.cash_duplicate_repair import (
        apply_cash_duplicate_repair_plan,
        build_cash_duplicate_repair_plan,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument(
        "--pair", action="append", default=[], metavar="DUPLICATE_SOURCE:KEEP_SOURCE"
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--apply-reviewed-plan", type=Path)
    args = parser.parse_args()
    pairs = [tuple(map(int, value.split(":"))) for value in args.pair]
    with SessionLocal() as db:
        if args.apply_reviewed_plan:
            plan = json.loads(args.apply_reviewed_plan.read_text())
            plan = plan.get("plan", plan)
            if plan["user_id"] != args.user_id or (
                pairs and sorted(map(list, pairs)) != plan["pairs"]
            ):
                raise ValueError("计划用户或来源配对不一致")
            count = apply_cash_duplicate_repair_plan(db, plan)
            db.commit()
            result = {"status": "applied", "merged_cash_events": count, "plan": plan}
        else:
            db.connection().connection.set_session(readonly=True)
            result = {
                "status": "dry_run",
                "plan": build_cash_duplicate_repair_plan(db, args.user_id, pairs),
            }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(
        f"{result['status']}: {len(result['plan']['changes'])} duplicate cash events; {len(result['plan']['blockers'])} blockers"
    )


if __name__ == "__main__":
    main()
