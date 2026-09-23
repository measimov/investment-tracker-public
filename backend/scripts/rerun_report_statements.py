"""抽取器 / prompt 版本 bump 之后一次跑完港股三张报表的重抽（不靠 UI 每次 4 份）。

版本号已承担失效判定：`ensure_report_statements` 把 extractor/prompt 版本不符的抽取行
视同未抽取并重跑，所以本脚本**不删任何行**——只是按 max_new 驱动重跑并报告进度。
失败行保留 attempts 语义（确定性失败两次即封顶）。校验规则升版（validation_version）
不必走本脚本，用 revalidate_report_statements.py 零下载零 LLM 重算即可。

用法：
    python scripts/rerun_report_statements.py --dry-run             # 只看会重跑哪些
    python scripts/rerun_report_statements.py --symbol 00700
    python scripts/rerun_report_statements.py --all --max-new 24     # 每个标的单次最多 24 份
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _stale_rows(db, symbol: str | None):
    from app.models.security_profile import SecurityProfileData
    from app.services.report_statement_prompts import STATEMENT_PROMPT_VERSION
    from app.services.report_statement_service import EXTRACT_DATASET
    from app.services.report_statements import STATEMENT_EXTRACTOR_VERSION

    query = db.query(SecurityProfileData).filter(
        SecurityProfileData.dataset == EXTRACT_DATASET, SecurityProfileData.market == "港股"
    )
    if symbol:
        query = query.filter(SecurityProfileData.symbol == symbol)
    stale = []
    for row in query.all():
        payload = row.payload or {}
        if (
            int(payload.get("extractor_version") or 1) != STATEMENT_EXTRACTOR_VERSION
            or int(payload.get("prompt_version") or 1) != STATEMENT_PROMPT_VERSION
        ):
            stale.append(row)
    return stale


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol")
    parser.add_argument("--all", action="store_true", help="重跑全部过期的港股报表抽取")
    parser.add_argument("--max-new", type=int, default=24, help="每个标的单次最多处理多少份报告")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.all and not args.symbol:
        parser.error("指定 --symbol 或 --all")

    from app.database import SessionLocal
    from app.services.report_statement_service import ensure_report_statements

    db = SessionLocal()
    try:
        stale = _stale_rows(db, args.symbol)
        symbols = sorted({row.symbol for row in stale})
        print(f"过期抽取 {len(stale)} 份，涉及 {len(symbols)} 只港股")
        for symbol in symbols:
            print(f"  {symbol}: {sum(1 for row in stale if row.symbol == symbol)} 份")
        if args.dry_run:
            print("\n--dry-run：未做任何修改")
            return 0
        if args.symbol and not symbols:
            symbols = [args.symbol]  # 尚无抽取行的标的也允许首跑
        if not symbols:
            print("没有需要重跑的报表")
            return 0
        for symbol in symbols:
            print(f"\n重跑 港股 {symbol} ...", flush=True)
            result = ensure_report_statements(db, symbol, "港股", max_new=args.max_new)
            print(
                f"  total={result['total']} completed={result['completed']}"
                f" generated={result['generated']} failed={result['failed']}"
                f" suspect={result['suspect']} remaining={result['remaining']}"
            )
            for gap in result["gaps"]:
                print(f"  ! {gap}")
            if result.get("fatal"):
                print(f"  !! 致命错误，停止：{result['fatal']}")
                return 1
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
