"""期间约定改动的生产全量对比快照（只读，不写任何表、不外呼；#337 PR-2）。

对全部跟踪标的（活跃用户持仓 ∪ 自选，A股/港股/美股）输出 signals、格雷厄姆、利润质量三块的
规范化 JSON，一行一只标的。改动前后各跑一次（``PYTHONPATH`` 决定加载哪一侧的 ``app/``），
再逐项 diff：signals 与格雷厄姆必须零差异，利润质量的每处变化都要能归因。

用法（生产容器里，两侧代码分别放在 /tmp/pp_base/app 与 /tmp/pp_branch/app）：
    docker exec investment-tracker-backend sh -c \\
        'cd /tmp/pp_base && PYTHONPATH=/tmp/pp_base python /tmp/periods_parity.py --out /tmp/base.jsonl'
    python scripts/periods_parity.py --diff base.jsonl branch.jsonl
"""

import argparse
import json
import sys

MARKETS = ("A股", "港股", "美股")


def _snapshot(out_path: str) -> int:
    import app
    from app.database import SessionLocal
    from app.services.analysis_signals import signals_from_inputs
    from app.services.earnings_quality import compute_earnings_quality, market_statements
    from app.services.security_industry_service import scope_keys
    from app.services.security_profile_service import (
        compute_graham_for,
        load_annual_statement_datasets,
        load_signal_inputs,
        load_symbol_profile,
    )

    print(f"app 来自 {app.__file__}", file=sys.stderr)
    db = SessionLocal()
    count = 0
    try:
        keys = sorted((s, m) for s, m in scope_keys(db) if m in MARKETS)
        with open(out_path, "w", encoding="utf-8") as handle:
            for symbol, market in keys:
                record = {"symbol": symbol, "market": market}
                try:
                    graham = compute_graham_for(db, symbol, market) or {}
                    record["graham"] = graham
                    record["signals"] = signals_from_inputs(
                        market, load_signal_inputs(db, symbol, market), graham
                    )
                    annual = load_annual_statement_datasets(db, symbol, market)
                    if annual is None:
                        annual = load_symbol_profile(db, symbol, market)["datasets"]
                    statements = market_statements(market, annual)
                    record["earnings_quality"] = compute_earnings_quality(
                        statements["income"],
                        statements["balancesheet"],
                        statements["cashflow"],
                        statements["fina_indicator"],
                        market=market,
                    )
                except Exception as exc:  # noqa: BLE001 - 对比报出来，不中断
                    record["error"] = f"{type(exc).__name__}: {exc}"
                db.rollback()
                handle.write(
                    json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n"
                )
                count += 1
    finally:
        db.rollback()
        db.close()
    print(f"写出 {count} 只标的 → {out_path}", file=sys.stderr)
    return 0


def _walk(a, b, path, out):
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a:
                out.append(("新增", f"{path}/{key}", None, b[key]))
            elif key not in b:
                out.append(("删除", f"{path}/{key}", a[key], None))
            else:
                _walk(a[key], b[key], f"{path}/{key}", out)
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for index, (left, right) in enumerate(zip(a, b)):
            _walk(left, right, f"{path}[{index}]", out)
    elif a != b:
        out.append(("变化", path, a, b))


def _diff(base_path: str, branch_path: str) -> int:
    def load(path):
        with open(path, encoding="utf-8") as handle:
            return {(r["symbol"], r["market"]): r for r in map(json.loads, handle)}

    base, branch = load(base_path), load(branch_path)
    if set(base) != set(branch):
        print(
            f"标的集合不同：只在基线 {set(base) - set(branch)}，只在分支 {set(branch) - set(base)}"
        )
    total = 0
    for key in sorted(set(base) & set(branch)):
        changes = []
        _walk(base[key], branch[key], "", changes)
        # 利润质量的 metric_semantics 是说明文本，所有标的同样变化，单独汇总
        semantics = [c for c in changes if "/metric_semantics/" in c[1]]
        changes = [c for c in changes if "/metric_semantics/" not in c[1]]
        if semantics and key == min(base):
            print(f"[全体] metric_semantics 文本变化 {len(semantics)} 处：")
            for _, path, _, _ in semantics:
                print("   ", path.split("/metric_semantics/")[1])
        if not changes:
            continue
        total += len(changes)
        print(f"{key[0]}/{key[1]}：{len(changes)} 处")
        for kind, path, old, new in changes:
            print(
                f"  {kind} {path}: "
                f"{json.dumps(old, ensure_ascii=False)[:160]} → "
                f"{json.dumps(new, ensure_ascii=False)[:160]}"
            )
    print(f"共 {len(base)} 只标的，metric_semantics 以外的差异 {total} 处")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="写出快照（JSON Lines）")
    parser.add_argument("--diff", nargs=2, metavar=("BASE", "BRANCH"), help="对比两份快照")
    args = parser.parse_args()
    if args.diff:
        return _diff(*args.diff)
    if args.out:
        return _snapshot(args.out)
    parser.error("需要 --out 或 --diff")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
