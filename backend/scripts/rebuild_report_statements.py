"""构建逻辑升版（STATEMENT_BUILD_VERSION）后从已存抽取行重建港股报表科目行。

零下载、零 LLM：抽取行里存着定位到的结构化行与模型给出的科目映射，重建只重跑
「映射 → 取数 → EPS 单位折算 → 夹层权益 → 资产小计修复 → 校验」这一段代码口径。
抽取器/prompt 版本过期的抽取行不在此列（它们要经 rerun_report_statements.py 重抽）。

用法：
    python scripts/rebuild_report_statements.py --all --dry-run --report   # 演练并对比，不落库
    python scripts/rebuild_report_statements.py --all --report             # 重建并按标的输出前后对比
    python scripts/rebuild_report_statements.py --symbol 02669 --force     # 不看版本强制重建
"""

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 前后对比时逐个比较的科目（与 report_statement_checks.NUMERIC_FIELDS 一致，含夹层权益）
REPORT_FIELDS = (
    "total_revenue",
    "cost_of_revenue",
    "gross_profit",
    "operating_income",
    "n_income_attr_p",
    "total_profit",
    "income_tax",
    "ebitda",
    "sga_exp",
    "int_exp",
    "basic_eps",
    "diluted_eps",
    "total_assets",
    "total_nca",
    "total_cur_assets",
    "total_cur_liab",
    "total_ncl",
    "accounts_receiv",
    "inventories",
    "fix_assets",
    "money_cap",
    "total_liab",
    "total_hldr_eqy_exc_min_int",
    "total_equity",
    "minority_int",
    "total_debt",
    "n_cashflow_act",
    "capex",
    "depr_fa_coga_dpba",
    "free_cashflow",
    "mezzanine_equity",
)


def _status(payload: Dict[str, Any]) -> str:
    return str((payload.get("validation") or {}).get("status") or "—")


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float) and abs(value) >= 1e4:
        return f"{value:,.0f}"
    return f"{value:g}" if isinstance(value, float) else str(value)


def diff_statement_rows(
    before: Dict[str, Dict[str, Any]], after: Dict[str, Dict[str, Any]]
) -> List[str]:
    """同一标的重建前后的会计期行 → 人读的差异行（状态、存疑科目、改动科目、构建标注）。"""
    from app.services.report_statement_checks import restated_fields

    lines: List[str] = []
    for period in sorted(set(before) | set(after), reverse=True):
        old, new = before.get(period) or {}, after.get(period) or {}
        parts: List[str] = []
        if _status(old) != _status(new):
            parts.append(f"状态 {_status(old)} → {_status(new)}")
        old_suspect = (old.get("validation") or {}).get("suspect_fields") or []
        new_suspect = (new.get("validation") or {}).get("suspect_fields") or []
        if old_suspect != new_suspect:
            parts.append(f"存疑科目 {old_suspect or '无'} → {new_suspect or '无'}")
        changed = [
            f"{field} {_fmt(old.get(field))} → {_fmt(new.get(field))}"
            for field in REPORT_FIELDS
            if old.get(field) != new.get(field)
        ]
        if changed:
            parts.append("改动：" + "；".join(changed))
        if new.get("eps_unit"):
            parts.append(f"EPS 按仙折元（依据 {new['eps_unit'].get('basis')}）")
        if new.get("repaired_fields"):
            repaired = "；".join(
                f"{field} {item.get('from_row')}→{item.get('to_row') or '推导'}"
                for field, item in new["repaired_fields"].items()
            )
            parts.append(f"小计修复：{repaired}")
        restated = restated_fields(new)
        if restated:
            parts.append(f"已重列（保留原值）：{restated}")
        if parts:
            lines.append(f"    {period}: " + "；".join(parts))
    return lines


def _snapshot(db, symbol: str) -> Dict[str, Dict[str, Any]]:
    from app.models.security_profile import SecurityProfileData
    from app.services.report_statement_service import STATEMENT_DATASET

    db.expire_all()
    rows = db.query(SecurityProfileData).filter(
        SecurityProfileData.dataset == STATEMENT_DATASET,
        SecurityProfileData.market == "港股",
        SecurityProfileData.symbol == symbol,
    )
    return {row.period_key: dict(row.payload or {}) for row in rows}


def _suspect_count(snapshot: Dict[str, Dict[str, Any]], *, current_only: bool) -> int:
    from app.services.report_statement_prompts import statement_row_current

    return sum(
        1
        for payload in snapshot.values()
        if _status(payload) == "suspect" and (not current_only or statement_row_current(payload))
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol")
    parser.add_argument("--all", action="store_true", help="重建全部港股")
    parser.add_argument("--force", action="store_true", help="不看构建版本，全部重建")
    parser.add_argument("--dry-run", action="store_true", help="演练：写入留在事务里，最后回滚")
    parser.add_argument("--report", action="store_true", help="按标的输出前后对比")
    args = parser.parse_args()
    if not args.all and not args.symbol:
        parser.error("指定 --symbol 或 --all")

    from app.database import SessionLocal
    from app.models.security_profile import SecurityProfileData
    from app.services.report_statement_service import EXTRACT_DATASET, rebuild_report_statements

    db = SessionLocal()
    try:
        query = db.query(SecurityProfileData.symbol).filter(
            SecurityProfileData.dataset == EXTRACT_DATASET, SecurityProfileData.market == "港股"
        )
        if args.symbol:
            query = query.filter(SecurityProfileData.symbol == args.symbol)
        symbols = sorted({row[0] for row in query.distinct().all()})
        totals = {"rebuilt": 0, "failed": 0, "suspect_before": 0, "suspect_after": 0}
        for symbol in symbols:
            before = _snapshot(db, symbol) if args.report else {}
            outcome = rebuild_report_statements(
                db, symbol, "港股", force=args.force, commit=not args.dry_run
            )
            totals["rebuilt"] += outcome["rebuilt"]
            totals["failed"] += outcome["failed"]
            print(
                f"  {symbol}: 重建 {outcome['rebuilt']} 份，失败 {outcome['failed']} 份，"
                f"EPS 以仙列示 {outcome['eps_cents_reports']} 份，"
                f"小计修复 {len(outcome['repaired_periods'])} 期，"
                f"存疑 {len(outcome['suspect_periods'])} 期",
                flush=True,
            )
            if args.report:
                after = _snapshot(db, symbol)
                # 前：部署前全部行都当前（构建版本升版后旧行不再「当前」，按全部行计）；后：只计当前行
                suspect_before = _suspect_count(before, current_only=False)
                suspect_after = _suspect_count(after, current_only=True)
                totals["suspect_before"] += suspect_before
                totals["suspect_after"] += suspect_after
                print(f"    存疑期 {suspect_before} → {suspect_after}")
                for line in diff_statement_rows(before, after):
                    print(line)
        print(
            f"共重建 {totals['rebuilt']} 份，失败 {totals['failed']} 份"
            + (
                f"；存疑期 {totals['suspect_before']} → {totals['suspect_after']}"
                if args.report
                else ""
            )
        )
        if args.dry_run:
            db.rollback()
            print("\n--dry-run：已回滚，未做任何修改")
        return 1 if totals["failed"] else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
