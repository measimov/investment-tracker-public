"""章节抽取质量审计：跑固件或跑库内存量，输出 locator/置信度/质量标记分布。

**这是 bump 版本号之前的门禁**。顺序反了就是花一次 LLM 的钱买回另一批错误
摘要，而且第二次更难发现——因为"重跑过了"会被当成"修好了"。

用法：
    python scripts/report_extraction_audit.py --fixtures       # 零成本，跑真实固件
    python scripts/report_extraction_audit.py --live           # 跑库内 report_section 存量
    python scripts/report_extraction_audit.py --live --symbol 000921

`--live` 只读 `security_profile_data` 里已缓存的节选**结果**（不重新下载 PDF，
也不调用 LLM），因此可以随时跑。要看新抽取器在真实报告上的表现用 `--fixtures`。
"""

import argparse
import collections
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "reports"


def _audit_fixtures() -> int:
    from app.services.report_sections import extract_cn_sections, extract_us_items

    rows = []
    for path in sorted(FIXTURE_DIR.glob("*.gz")):
        name = path.name.split(".")[0]
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            text = handle.read()
        if name.startswith("us_"):
            # 表单类型必须从固件元数据取：默认按 10-K 抽 20-F 会得到一份
            # "看起来正常"的错误结果，而这个脚本正是用来防这种事的
            meta_path = FIXTURE_DIR / f"{name}.meta.json"
            form_type = "10-K"
            if meta_path.exists():
                form_type = json.loads(meta_path.read_text(encoding="utf-8")).get(
                    "report_type"
                ) or "10-K"
            extracted = extract_us_items(text, form_type=form_type)
        else:
            extracted = extract_cn_sections(text)
        for section, result in extracted.items():
            rows.append({
                "source": name,
                "section": section,
                "locator": result.locator if result else "missing",
                "confidence": round(result.confidence, 2) if result else 0.0,
                "chars": result.chars if result else 0,
                "flags": list(result.quality_flags) if result else ["missing"],
            })
    return _report(rows)


def _audit_live(symbol_filter: str | None) -> int:
    from app.database import SessionLocal
    from app.models.security_profile import SecurityProfileData
    from app.services.report_sections import score_section

    db = SessionLocal()
    try:
        query = db.query(SecurityProfileData).filter(
            SecurityProfileData.dataset == "report_section"
        )
        if symbol_filter:
            query = query.filter(SecurityProfileData.symbol == symbol_filter)
        rows = []
        for row in query.all():
            payload = row.payload or {}
            if payload.get("extract_status") != "ok":
                rows.append({
                    "source": f"{row.symbol}/{row.period_key}",
                    "section": "-",
                    "locator": "failed",
                    "confidence": 0.0,
                    "chars": 0,
                    "flags": [str(payload.get("error"))[:60] or "failed"],
                })
                continue
            meta = payload.get("section_meta") or {}
            sections = payload.get("sections") or {}
            for section, body in sections.items():
                info = meta.get(section) or {}
                # 对**存着的正文**重新评分，而不是读旧行自报的元数据：v1 行根本
                # 没有 confidence/flags 字段，只看元数据的话这批抽错的节选会以
                # "无标记"的姿态通过审计——正是它们当初骗过金样测试的方式
                confidence, flags = score_section(section, body)
                rows.append({
                    "source": f"{row.symbol}/{row.period_key}",
                    "section": section,
                    "locator": info.get("locator") or "unknown",
                    "confidence": round(confidence, 2),
                    "chars": info.get("chars") or len(body),
                    "flags": flags,
                    "extractor_version": payload.get("extractor_version") or 1,
                    "head": body[:80].replace("\n", " "),
                })
        return _report(rows, live=True)
    finally:
        db.close()


def _report(rows: list[dict], *, live: bool = False) -> int:
    if not rows:
        print("没有可审计的数据")
        return 1
    print(f"共 {len(rows)} 个章节实例\n")

    for key in ("locator", "section"):
        counter = collections.Counter(row[key] for row in rows)
        print(f"[{key}] " + "  ".join(f"{k}={v}" for k, v in counter.most_common()))
    flags = collections.Counter(flag for row in rows for flag in (row["flags"] or []))
    print("[flags] " + ("  ".join(f"{k}={v}" for k, v in flags.most_common()) or "无"))
    if live:
        versions = collections.Counter(row.get("extractor_version") for row in rows)
        print("[extractor_version] " + "  ".join(f"v{k}={v}" for k, v in versions.items()))

    # 门禁：业务概要抽成登记信息页是本轮修复的目标缺陷
    boilerplate = [r for r in rows if "boilerplate_profile" in (r["flags"] or [])]
    # "没抽到"与"抽到了但可疑"必须分开计：中文年报本就未必设独立风险章节，
    # 把 missing 混进低置信里会让门禁读数虚高、失去意义
    missing = [r for r in rows if r["locator"] == "missing"]
    low = [
        r for r in rows
        if r["locator"] != "missing"
        and r.get("confidence") is not None
        and r["confidence"] < 0.35
    ]
    print(
        f"\nboilerplate_profile: {len(boilerplate)}    "
        f"低置信(<0.35): {len(low)}    未定位(可能确实未披露): {len(missing)}"
    )
    for row in sorted(rows, key=lambda r: (r["source"], r["section"])):
        confidence = row.get("confidence")
        confidence_text = "  ? " if confidence is None else f"{confidence:>4}"
        print(
            f"  {row['source']:<28} {row['section']:<16} {row['locator']:<15}"
            f" conf={confidence_text} chars={row['chars']:>7}"
            f" {','.join(row['flags'] or [])}"
        )
        if row.get("head"):
            print(f"      → {row['head']}")
    return 1 if boilerplate else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", action="store_true", help="审计真实固件（零成本）")
    parser.add_argument("--live", action="store_true", help="审计库内已缓存节选")
    parser.add_argument("--symbol", help="--live 时只看某个标的")
    parser.add_argument("--json", action="store_true", help="附带输出 JSON")
    args = parser.parse_args()
    if not args.fixtures and not args.live:
        parser.error("至少指定 --fixtures 或 --live")
    code = 0
    if args.fixtures:
        print("=== 固件审计 ===")
        code |= _audit_fixtures()
    if args.live:
        print("\n=== 库内存量审计 ===")
        code |= _audit_live(args.symbol)
    if args.json:
        print(json.dumps({"exit_code": code}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
