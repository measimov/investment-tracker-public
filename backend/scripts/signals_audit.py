"""预计算信号（analysis_signals，#265）全量体检：只读，不写任何表、不外呼。

对全部跟踪标的（活跃用户持仓 ∪ 自选）计算 signals，输出：
- 各项覆盖率（非空比例），看哪类数据普遍缺失；
- 极端值（|同比| 超过 1000%、股息占自由现金流超过 500%、股息支付率超过 300%），逐条过目；
- 标签兜底回放：用各标的**最新一份**分析的标签跑 validate_tags，列出会被丢掉的标签与原因。

用法：
    python scripts/signals_audit.py            # 全部跟踪标的
    python scripts/signals_audit.py --symbol 600941 --market A股 --dump   # 单只并打印完整 signals
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

EXTREME_YOY_PCT = 1000
EXTREME_DIV_TO_FCF_PCT = 500
EXTREME_PAYOUT_PCT = 300


def _extremes(symbol, market, signals):
    found = []
    periods = signals.get("period_signals") or {}
    for block_name in ("latest_fy", "latest_interim"):
        block = periods.get(block_name) or {}
        for field in ("revenue", "net_income", "net_income_dedt"):
            yoy = (block.get(field) or {}).get("yoy_pct")
            if yoy is not None and abs(yoy) > EXTREME_YOY_PCT:
                found.append(f"{block_name}.{field} 同比 {yoy}%")
    for entry in (signals.get("shareholder_returns") or {}).get("by_year") or []:
        ratio = entry.get("dividends_to_fcf_pct")
        if ratio is not None and ratio > EXTREME_DIV_TO_FCF_PCT:
            found.append(f"{entry['fiscal_year_end']} 股息/自由现金流 {ratio}%")
        payout = entry.get("payout_ratio_pct")
        if payout is not None and payout > EXTREME_PAYOUT_PCT:
            found.append(f"{entry['fiscal_year_end']} 股息支付率 {payout}%")
    return [f"{symbol}/{market}: {item}" for item in found]


def _coverage(signals, counter):
    periods = signals.get("period_signals") or {}
    returns = signals.get("shareholder_returns") or {}
    latest_year = (returns.get("by_year") or [{}])[0]
    checks = {
        "latest_fy": bool(periods.get("latest_fy")),
        "latest_interim": bool(periods.get("latest_interim")),
        "second_half": bool(periods.get("second_half")),
        "fcf(最新财年)": latest_year.get("fcf_yi") is not None,
        "dividends_total(最新财年)": latest_year.get("dividends_total_yi") is not None,
        "dividend_yield": (returns.get("dividend_yield") or {}).get("value_pct") is not None,
        "ever_paid 已知": returns.get("ever_paid") is not None,
        "balance_changes": bool((signals.get("balance_changes") or {}).get("items")),
        "net_cash": bool((signals.get("balance_changes") or {}).get("net_cash")),
        "roe": bool(signals.get("roe")),
    }
    for name, ok in checks.items():
        counter[name] += int(ok)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol")
    parser.add_argument("--market")
    parser.add_argument("--dump", action="store_true", help="打印完整 signals（单只时用）")
    args = parser.parse_args()

    from app.database import SessionLocal
    from app.models.security_profile import SecurityAnalysis
    from app.services.analysis_signals import signals_from_inputs, validate_tags
    from app.services.security_industry_service import scope_keys
    from app.services.security_profile_service import compute_graham_for, load_signal_inputs

    db = SessionLocal()
    try:
        keys = [(args.symbol, args.market)] if args.symbol else scope_keys(db)
        coverage, by_market = Counter(), Counter()
        extremes, dropped, errors = [], [], []
        for symbol, market in keys:
            try:
                graham = compute_graham_for(db, symbol, market) or {}
                signals = signals_from_inputs(
                    market, load_signal_inputs(db, symbol, market), graham
                )
            except Exception as exc:  # noqa: BLE001 - 体检报出来，不中断
                errors.append(f"{symbol}/{market}: {exc}")
                continue
            if signals.get("status") != "ok":
                continue
            by_market[market] += 1
            _coverage(signals, coverage)
            extremes.extend(_extremes(symbol, market, signals))
            if args.dump:
                print(json.dumps(signals, ensure_ascii=False, indent=1))
            latest = (
                db.query(SecurityAnalysis)
                .filter(SecurityAnalysis.symbol == symbol, SecurityAnalysis.market == market)
                .order_by(SecurityAnalysis.created_at.desc())
                .first()
            )
            if latest and latest.tags:
                kept, adjustments = validate_tags(latest.tags, signals, graham)
                for item in adjustments:
                    dropped.append(
                        f"{symbol}/{market} {latest.created_at:%Y-%m-%d}: "
                        f"{item.get('tag') or item.get('tags')} —— "
                        f"{item.get('reason') or item.get('reasons')}"
                    )
        total = sum(by_market.values())
        print(f"有信号的标的 {total} 只：{dict(by_market)}；失败 {len(errors)}")
        print("覆盖率：")
        for name in sorted(coverage, key=lambda n: -coverage[n]):
            print(f"  {name}: {coverage[name]}/{total}")
        print(f"极端值 {len(extremes)} 条：")
        for line in extremes:
            print("  " + line)
        print(f"标签兜底回放（最新一份分析）会丢掉 {len(dropped)} 个：")
        for line in dropped:
            print("  " + line)
        for line in errors:
            print("  [错误] " + line)
    finally:
        db.rollback()
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
