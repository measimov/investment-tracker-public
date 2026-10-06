"""默认只读预演股息实收升级；仅应用无阻断、与当前账本一致的已审阅计划。"""

import argparse
from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def write_private_json(path, value):
    """Financial plans must never be written to a tracked repository path."""
    root = Path(__file__).resolve().parents[2]
    path = path.resolve()
    if (root / ".git").exists() and path.is_relative_to(root):
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", str(path)], cwd=root, check=False
        )
        if ignored.returncode != 0:
            raise ValueError("财务计划须输出到 Git 忽略目录或仓库外目录")
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
            name = output.name
            os.fchmod(output.fileno(), 0o600)
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def main():
    from app.database import SessionLocal
    from app.services.dividend_receipt_upgrade import (
        apply_dividend_receipt_upgrade_plan,
        build_dividend_receipt_upgrade_plan,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--apply-reviewed-plan", type=Path)
    args = parser.parse_args()
    if args.apply_reviewed_plan and args.apply_reviewed_plan.resolve() == args.out.resolve():
        raise ValueError("执行结果与审阅计划必须保存为不同文件")
    with SessionLocal() as db:
        if args.apply_reviewed_plan:
            document = json.loads(args.apply_reviewed_plan.read_text())
            plan = document.get("plan", document)
            if plan["user_id"] != args.user_id or (
                args.as_of and plan["as_of"] != args.as_of.isoformat()
            ):
                raise ValueError("计划用户或截止日期与参数不一致")
            count = apply_dividend_receipt_upgrade_plan(db, plan)
            result = {
                "status": "applied" if count else "already_applied",
                "changed_actions": count,
                "plan": plan,
            }
            # The private result must be writable before the financial transaction commits.
            write_private_json(args.out, {**result, "status": "prepared_uncommitted"})
            db.commit()
            write_private_json(args.out, result)
        else:
            db.connection().connection.set_session(readonly=True)
            plan = build_dividend_receipt_upgrade_plan(db, args.user_id, as_of=args.as_of)
            result = {"status": "dry_run", "plan": plan}
            write_private_json(args.out, result)
        print(
            f"{result['status']}: {len(plan['changes'])} proposed changes; {len(plan['blockers'])} blockers; {len(plan['review_required'])} manual reviews"
        )
        return 2 if plan["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
