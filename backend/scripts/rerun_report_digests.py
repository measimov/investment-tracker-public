"""版本号 bump 之后重跑存量财报摘要。

**先跑 `report_extraction_audit.py --fixtures` 确认新抽取器 boilerplate 归零，
再 bump 版本号，最后才跑本脚本。** 顺序反了就是花钱买回另一批错误摘要。

版本号已经承担了失效判定：`ensure_report_digests` 会把 extractor/prompt 版本
不符的行视同未摘要并重新生成，所以本脚本**不删任何行**——只是驱动重跑并报告
进度。失败行保留 attempts 语义（确定性失败两次即封顶）。

`business_profile` 不需要显式清除：它按输入内容指纹（digests + business 节选）
缓存，摘要一变指纹就变，自动重算。

用法：
    python scripts/rerun_report_digests.py --dry-run           # 只看会重跑哪些
    python scripts/rerun_report_digests.py --symbol 000921 --market A股
    python scripts/rerun_report_digests.py --all --max-new 4   # 每个标的最多补 4 份
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _stale_rows(db, symbol: str | None, market: str | None):
    from app.models.security_profile import SecurityProfileData
    from app.services.report_digest_prompts import DIGEST_PROMPT_VERSION
    from app.services.report_sections import SECTION_EXTRACTOR_VERSION

    query = db.query(SecurityProfileData).filter(
        SecurityProfileData.dataset == "report_digest"
    )
    if symbol:
        query = query.filter(SecurityProfileData.symbol == symbol)
    if market:
        query = query.filter(SecurityProfileData.market == market)
    stale = []
    for row in query.all():
        payload = row.payload or {}
        if (
            int(payload.get("extractor_version") or 1) != SECTION_EXTRACTOR_VERSION
            or int(payload.get("prompt_version") or 1) != DIGEST_PROMPT_VERSION
        ):
            stale.append(row)
    return stale


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol")
    parser.add_argument("--market")
    parser.add_argument("--all", action="store_true", help="重跑全部过期摘要")
    parser.add_argument("--max-new", type=int, default=12,
                        help="每个标的单次最多生成多少份（成本护栏）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.all and not args.symbol:
        parser.error("指定 --symbol 或 --all")

    from app.database import SessionLocal
    from app.services.report_digest_service import ensure_report_digests

    db = SessionLocal()
    try:
        stale = _stale_rows(db, args.symbol, args.market)
        pairs = sorted({(row.symbol, row.market) for row in stale})
        print(f"过期摘要 {len(stale)} 行，涉及 {len(pairs)} 个标的")
        for symbol, market in pairs:
            count = sum(1 for row in stale if row.symbol == symbol and row.market == market)
            print(f"  {market} {symbol}: {count} 份")
        if args.dry_run:
            print("\n--dry-run：未做任何修改")
            return 0
        if not pairs:
            print("没有需要重跑的摘要")
            return 0

        for symbol, market in pairs:
            print(f"\n重跑 {market} {symbol} ...", flush=True)
            result = ensure_report_digests(db, symbol, market, max_new=args.max_new)
            print(
                f"  total={result['total']} completed={result['completed']}"
                f" generated={result['generated']} remaining={result['remaining']}"
            )
            for gap in result["gaps"]:
                print(f"  ! {gap}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
