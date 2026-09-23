"""校验规则升版（STATEMENT_VALIDATION_VERSION）后重算存量港股报表行的 validation。

零下载、零 LLM：只重跑恒等式与 Yahoo 交叉核对，并回写 `validation` 块。抽取器/prompt
版本不符的行不在此列（它们要经 rerun_report_statements.py 重抽）。

用法：
    python scripts/revalidate_report_statements.py --all
    python scripts/revalidate_report_statements.py --symbol 00700
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    if not args.all and not args.symbol:
        parser.error("指定 --symbol 或 --all")

    from app.database import SessionLocal
    from app.models.security_profile import SecurityProfileData
    from app.services.report_statement_service import STATEMENT_DATASET, revalidate_report_statements

    db = SessionLocal()
    try:
        query = db.query(SecurityProfileData.symbol).filter(
            SecurityProfileData.dataset == STATEMENT_DATASET, SecurityProfileData.market == "港股"
        )
        if args.symbol:
            query = query.filter(SecurityProfileData.symbol == args.symbol)
        symbols = sorted({row[0] for row in query.distinct().all()})
        total = {"revalidated": 0, "suspect": 0}
        for symbol in symbols:
            outcome = revalidate_report_statements(db, symbol, "港股")
            total["revalidated"] += outcome["revalidated"]
            total["suspect"] += outcome["suspect"]
            print(f"  {symbol}: 重算 {outcome['revalidated']} 行，存疑 {outcome['suspect']} 行")
        print(f"共重算 {total['revalidated']} 行，存疑 {total['suspect']} 行")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
