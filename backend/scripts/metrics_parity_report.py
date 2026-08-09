#!/usr/bin/env python3
"""收益口径双跑对比：改动前后在同一份真实账本上比较统计输出。

改数值口径（折算、年化基准、FIFO 精度、去重键…）时，"测试全绿"证明不了
"用户看到的数字没变"。这个脚本把一次改动的数值影响量化出来：

    # 1) 在改动前的代码上生成基准
    git stash            # 或 git worktree add /tmp/old <改动前的 commit>
    python scripts/metrics_parity_report.py --out /tmp/metrics_old.json

    # 2) 在改动后的代码上生成对照
    git stash pop
    python scripts/metrics_parity_report.py --out /tmp/metrics_new.json

    # 3) 比对
    python scripts/metrics_parity_report.py --compare /tmp/metrics_old.json /tmp/metrics_new.json

比对会把差异分成三类：新增字段 / 删除字段 / **数值变化**。前两类通常是
响应结构扩展，第三类必须逐项解释清楚——比如年化基准从 365 改到 365.25 时，
波动率类应恰好 ×sqrt(365.25/365)=1.000342，对不上就说明混进了别的改动。

列表顺序不构成契约的字段（closed_trades / trades_detail / holdings_detail /
by_symbol / statistics_by_market …）会先按内容规范化排序再比对：去重键一变
遍历顺序就变，不规范化的话会淹没在上万条"差异"里。

只读，不写任何表；analytics 的 refresh_history 保持默认 False，不外呼。
建议指向一份**可丢弃的**账本副本，而不是生产库。与其他脚本一样需要 app 的
必填配置（SECRET_KEY 等），本地跑可以随便给值——脚本不签发任何令牌：

    cd backend && source venv/bin/activate
    DATABASE_URL=postgresql://localhost:5432/investment_rebuild \
    SECRET_KEY=x ADMIN_INITIAL_PASSWORD=x DEMO_INITIAL_PASSWORD=x \
        python scripts/metrics_parity_report.py --out /tmp/metrics_new.json

部署机上：

    docker compose run --rm backend python scripts/metrics_parity_report.py --out /tmp/m.json
"""

import argparse
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 顺序无契约的列表：比对前按内容排序，避免遍历顺序变化淹没真正的数值差异
ORDER_INSENSITIVE_KEYS = {
    "closed_trades",
    "trades_detail",
    "holdings_detail",
    "by_symbol",
    "statistics_by_market",
    "unpriced_positions",
    "missing_price_history",
}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, date):
        return value.isoformat()
    return value


def _canonicalize(value: Any, key: str = None) -> Any:
    if isinstance(value, dict):
        return {k: _canonicalize(v, k) for k, v in value.items()}
    if isinstance(value, list):
        items = [_canonicalize(item) for item in value]
        if key in ORDER_INSENSITIVE_KEYS:
            items = sorted(
                items, key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False, default=str)
            )
        return items
    return value


def build_snapshot(user_id: int) -> Dict[str, Any]:
    from app.database import SessionLocal
    from app.models.holding import Holding
    from app.services import statistics as ss

    db = SessionLocal()
    try:
        # 估值输入取自持仓表的现价，保证两次运行输入完全一致（不依赖外部行情）
        prices = {
            f"{h.symbol}:{h.market}": float(h.current_price)
            for h in db.query(Holding).filter(Holding.user_id == user_id).all()
            if h.current_price is not None
        }
        snapshot = {
            "summary_statistics": ss.get_summary_statistics(db, user_id),
            "statistics_by_market": ss.get_statistics_by_market(db, user_id),
            "realized_pnl": ss.calculate_realized_pnl_fifo(db, user_id),
            "dividend_summary": ss.get_dividend_summary(db, user_id),
            "current_performance": ss.calculate_current_holdings_performance(db, user_id, prices),
            "performance_summary": ss.calculate_performance_summary(db, user_id, prices),
            "performance_analytics": ss.calculate_performance_analytics(
                db, user_id, prices,
                start_date=date(2000, 1, 1), end_date=date.today(),
            ),
        }
    finally:
        db.close()

    snapshot = _jsonable(snapshot)
    # 依赖运行时刻的字段不参与比对
    snapshot.get("performance_analytics", {}).pop("generated_at", None)
    return snapshot


def diff(old: Any, new: Any, path: str = "") -> Tuple[List, List, List]:
    added, removed, changed = [], [], []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in new.keys() - old.keys():
            added.append((f"{path}.{key}", new[key]))
        for key in old.keys() - new.keys():
            removed.append((f"{path}.{key}", old[key]))
        for key in old.keys() & new.keys():
            a, r, c = diff(old[key], new[key], f"{path}.{key}")
            added += a
            removed += r
            changed += c
    elif isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            changed.append((path, (f"len {len(old)}", f"len {len(new)}")))
        else:
            for index, (o, n) in enumerate(zip(old, new)):
                a, r, c = diff(o, n, f"{path}[{index}]")
                added += a
                removed += r
                changed += c
    elif old != new:
        changed.append((path, (old, new)))
    return added, removed, changed


def compare(old_path: str, new_path: str) -> int:
    with open(old_path) as fh:
        old = _canonicalize(json.load(fh))
    with open(new_path) as fh:
        new = _canonicalize(json.load(fh))

    added, removed, changed = diff(old, new)
    print(f"新增字段 {len(added)}、删除字段 {len(removed)}、数值变化 {len(changed)}\n")

    if added:
        print("--- 新增字段 ---")
        for path, value in sorted(added):
            print(f"  + {path} = {value!r}")
    if removed:
        print("\n--- 删除字段 ---")
        for path, value in sorted(removed):
            print(f"  - {path} = {value!r}")
    if changed:
        print("\n--- 数值变化（每一条都要能解释）---")
        for path, (o, n) in sorted(changed):
            try:
                ratio = f"   比值={float(n) / float(o):.9f}" if float(o) else ""
            except (TypeError, ValueError, ZeroDivisionError):
                ratio = ""
            print(f"  ! {path}: {o} -> {n}{ratio}")

    print("\n--- 判定 ---")
    if changed:
        print("存在数值变化：逐项确认是预期的口径调整后才可继续（例如重生成冻结基线）。")
        return 1
    print("无数值变化：改动对既有数据不可见。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", help="生成快照到该路径")
    parser.add_argument("--user-id", type=int, default=2, help="账本所属用户（默认 2 = demo）")
    parser.add_argument("--compare", nargs=2, metavar=("OLD", "NEW"), help="比对两份快照")
    args = parser.parse_args()

    if args.compare:
        return compare(*args.compare)
    if not args.out:
        parser.error("需要 --out 或 --compare")

    snapshot = build_snapshot(args.user_id)
    with open(args.out, "w") as fh:
        json.dump(snapshot, fh, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    print(f"snapshot written: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
