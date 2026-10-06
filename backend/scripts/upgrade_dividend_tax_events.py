"""默认只读生成计划；--apply-reviewed-plan 仅应用未变化、无阻断的计划。

执行前备份数据库，在恢复库验证指标及二次执行零新增；生产维护期停写后执行。
JSON 含私有账本信息，输出必须放在 Git 忽略的备份目录。
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    from app.database import SessionLocal
    from app.services.dividend_tax_upgrade import (
        apply_dividend_tax_upgrade_plan,
        build_dividend_tax_upgrade_plan,
    )

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--anchor", action="append", default=[], metavar="EVENT:SNAPSHOT")
    parser.add_argument(
        "--preserve-existing-cash-delta",
        action="store_true",
        help="显式保持原有现金差异，不吸收进锚点；差异仍须独立核查，默认阻断",
    )
    parser.add_argument("--apply-reviewed-plan", type=Path)
    args = parser.parse_args()
    anchors = dict(tuple(map(int, value.split(":"))) for value in args.anchor)
    with SessionLocal() as db:
        if args.apply_reviewed_plan:
            plan = json.loads(args.apply_reviewed_plan.read_text())
            plan = plan.get("plan", plan)
            if plan["user_id"] != args.user_id or (
                anchors and plan["anchors"] != {str(k): v for k, v in anchors.items()}
            ):
                raise ValueError("计划用户或锚点参数不一致")
            count = apply_dividend_tax_upgrade_plan(db, plan)
            db.commit()
            result = {"status": "applied", "created_tax_events": count, "plan": plan}
        else:
            db.connection().connection.set_session(readonly=True)
            result = {
                "status": "dry_run",
                "plan": build_dividend_tax_upgrade_plan(
                    db,
                    args.user_id,
                    anchors=anchors,
                    preserve_existing_cash_delta=args.preserve_existing_cash_delta,
                ),
            }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(result, output, indent=2, ensure_ascii=False)
            output.write("\n")
        print(
            f"{result['status']}: {len(result['plan']['facts'])} tax facts; {len(result['plan']['blockers'])} blockers"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
