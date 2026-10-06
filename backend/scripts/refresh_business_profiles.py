"""重生成版本/输入已过期的存量商业画像；默认只预览，--apply 才调用模型并写缓存。

不修改财报原文/摘要、账本或既有分析。版本与输入指纹是续跑标记；成功项重跑跳过。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def stale_profiles(db, *, symbol=None, market=None):
    from app.models.security_profile import SecurityProfileData
    from app.services.business_profile_prompts import PROFILE_PROMPT_VERSION
    from app.services.business_profile_service import (
        build_business_profile_input,
        input_fingerprint,
    )
    from app.services.payload_versions import versions_current

    query = db.query(SecurityProfileData).filter(
        SecurityProfileData.dataset == "business_profile",
        SecurityProfileData.period_key == "current",
    )
    if symbol:
        query = query.filter(SecurityProfileData.symbol == symbol)
    if market:
        query = query.filter(SecurityProfileData.market == market)
    stale = []
    for row in query.order_by(SecurityProfileData.market, SecurityProfileData.symbol).all():
        payload = row.payload or {}
        if (
            payload.get("status") != "ok"
            or not versions_current(payload, prompt_version=int(PROFILE_PROMPT_VERSION))
            or payload.get("input_fingerprint")
            != input_fingerprint(build_business_profile_input(db, row.symbol, row.market))
        ):
            stale.append((row.symbol, row.market))
    return stale


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol")
    parser.add_argument("--market")
    parser.add_argument("--apply", action="store_true", help="调用模型重生成过期画像（默认只预览）")
    args = parser.parse_args()

    from app.database import SessionLocal
    from app.services.business_profile_service import ensure_business_profile, load_business_profile

    db = SessionLocal()
    try:
        pairs = stale_profiles(db, symbol=args.symbol, market=args.market)
        print(f"待刷新商业画像 {len(pairs)} 个", flush=True)
        failures = []
        for symbol, market in pairs:
            print(f"  {market} {symbol}", flush=True)
            if args.apply:
                ensure_business_profile(db, symbol, market)
                if not load_business_profile(db, symbol, market, for_analysis=True)["profile"]:
                    failures.append((symbol, market))
                    print("    刷新未完成，旧画像仍不可用于分析", flush=True)
                else:
                    print("    完成（重跑将跳过）", flush=True)
        if not args.apply:
            print("只读预览：未调用模型、未修改数据库")
        elif failures:
            print(f"{len(failures)} 个未完成；排查后可原命令续跑")
        return 1 if failures else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
