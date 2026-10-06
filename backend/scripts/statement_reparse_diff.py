"""抽取器升版前的全量重解析对比（只读，不写库、不调 LLM）。

对库里全部港股报表抽取记录（`report_statement_extract`，状态 ok 且存有结构化行）：重新下载
报告 PDF → 用**当前代码**重新定位与解析 → 与库里存的结构化行逐行比较，列出解析结果变了的
报告与行。合并抽取器改动前跑一次，确认每处变化都能归因到那次修复（数据修复的通用验收）。

PDF 的逐页文本按报告缓存在 `--cache-dir`（gzip），重复运行不重下载。

用法：
    python scripts/statement_reparse_diff.py --cache-dir /tmp/hk_pages --out diff.json
    python scripts/statement_reparse_diff.py --symbol 01133 --cache-dir /tmp/hk_pages
"""

import argparse
import gzip
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _pages(url: str, cache_dir: Path):
    from app.services.report_fetchers import download_report_pdf
    from app.services.report_pdf_text import extract_pages

    cache = cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".pages.txt.gz")
    if cache.exists():
        with gzip.open(cache, "rt", encoding="utf-8") as handle:
            return handle.read().split("\x0c")
    pages = extract_pages(download_report_pdf(url, source="hkexnews"))
    with gzip.open(cache, "wt", encoding="utf-8") as handle:
        handle.write("\x0c".join(pages))
    return pages


def _row_key(row):
    return (row.get("label") or "", row.get("note") or "")


def _diff_statement(old, new):
    """同一张表新旧结构化行的差异：按 (标签, 附注) 对齐，同键多行按出现顺序配对。"""
    changes = []
    for field in ("column_count", "years", "unit_multiplier", "currency", "page_start"):
        if (old or {}).get(field) != (new or {}).get(field):
            changes.append({"meta": field, "old": (old or {}).get(field), "new": new.get(field)})
    old_rows = [r for r in (old or {}).get("rows") or []]
    new_rows = [r for r in (new or {}).get("rows") or []]
    buckets = {}
    for row in old_rows:
        buckets.setdefault(_row_key(row), []).append(row)
    for row in new_rows:
        matches = buckets.get(_row_key(row)) or []
        if matches:
            before = matches.pop(0)
            if before.get("values") != row.get("values"):
                changes.append(
                    {
                        "label": row.get("label"),
                        "note": row.get("note"),
                        "old": before.get("values"),
                        "new": row.get("values"),
                    }
                )
        else:
            changes.append(
                {
                    "label": row.get("label"),
                    "note": row.get("note"),
                    "old": None,
                    "new": row.get("values"),
                }
            )
    for key, rest in buckets.items():
        for row in rest:
            changes.append({"label": key[0], "note": key[1], "old": row.get("values"), "new": None})
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol")
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--out", help="差异明细写入的 JSON 文件")
    args = parser.parse_args()
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    from app.database import SessionLocal
    from app.models.security_profile import SecurityProfileData
    from app.services.report_statement_service import EXTRACT_DATASET, actual_period_end
    from app.services.report_statements import locate_statements

    db = SessionLocal()
    report = {"compared": 0, "changed": 0, "failed": [], "reports": {}}
    try:
        query = db.query(SecurityProfileData).filter(
            SecurityProfileData.dataset == EXTRACT_DATASET, SecurityProfileData.market == "港股"
        )
        if args.symbol:
            query = query.filter(SecurityProfileData.symbol == args.symbol)
        rows = sorted(query.all(), key=lambda r: (r.symbol, r.period_key))
        for row in rows:
            payload = row.payload or {}
            url = payload.get("source_url")
            if payload.get("status") != "ok" or not payload.get("statements") or not url:
                continue
            name = f"{row.symbol} {row.period_key}"
            try:
                pages = _pages(url, cache_dir)
                found = locate_statements(pages, report_type=payload.get("report_type"))
                located = {k: v for k, v in found.items() if v is not None}
                target = {
                    "end_date": payload.get("end_date"),
                    "report_type": payload.get("report_type"),
                }
                actual_period_end(located, target)
                new = {kind: parsed.to_payload() for kind, parsed in located.items()}
            except Exception as exc:  # noqa: BLE001 - 对比报出来，不中断
                report["failed"].append(f"{name}: {str(exc)[:160]}")
                print(f"[失败] {name}: {str(exc)[:160]}", flush=True)
                continue
            report["compared"] += 1
            old = payload.get("statements") or {}
            diffs = {}
            for kind in sorted(set(old) | set(new)):
                changes = _diff_statement(old.get(kind), new.get(kind) or {})
                if changes:
                    diffs[kind] = changes
            if diffs:
                report["changed"] += 1
                report["reports"][name] = {
                    "stored_extractor_version": payload.get("extractor_version"),
                    "diffs": diffs,
                }
                print(
                    f"[变化] {name}: " + "，".join(f"{k} {len(v)} 行" for k, v in diffs.items()),
                    flush=True,
                )
    finally:
        db.rollback()
        db.close()
    print(
        f"对比 {report['compared']} 份，解析变化 {report['changed']} 份，失败 {len(report['failed'])} 份"
    )
    if args.out:
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
