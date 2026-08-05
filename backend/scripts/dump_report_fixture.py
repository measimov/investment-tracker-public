"""把真实年报抽成文本快照，作为章节抽取的金样固件。

PDF 3-30MB 不进 git，但 pdfplumber 抽取后的纯文本 gzip 只有 100-300KB，
且**它正是 extract_cn_sections 的真实输入**（\x0c 分页约定完全对齐）。

构造固件测的是构造者对格式的想象：现有 _make_report() 把「一、主要业务」
写进了「公司简介」节里，于是断言在真实数据抽到注册地址时照样绿灯。

只读源站，永不在 CI 跑。年报是公开披露文件，无版权/脱敏顾虑。

用法:
    python scripts/dump_report_fixture.py cn 600036          # A股最新年报
    python scripts/dump_report_fixture.py hk 00700           # 港股最新年报
    python scripts/dump_report_fixture.py us AAPL            # 美股最新年报 HTML
"""

import argparse
import gzip
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pdfplumber  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "reports"


def _write(name: str, text: str, meta: dict) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / f"{name}.pages.txt.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(text)
    (FIXTURE_DIR / f"{name}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"写入 {path}  ({len(text)} 字符, {path.stat().st_size // 1024}KB)")


def _pdf_to_pages_text(content: bytes) -> str:
    from app.services.report_sections import pages_to_text

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        return pages_to_text([page.extract_text() or "" for page in pdf.pages])


def dump_cn(symbol: str, index: int = 0) -> None:
    from app.services.report_digest_service import plan_report_targets_detailed
    from app.services.report_fetchers import download_report_pdf

    targets = plan_report_targets_detailed(symbol, "A股")["targets"]
    annual = [t for t in targets if t["report_type"] == "annual"]
    if len(annual) <= index:
        raise SystemExit(f"{symbol} 只检索到 {len(annual)} 份年报")
    target = annual[index]
    content = download_report_pdf(target["url"])
    text = _pdf_to_pages_text(content)
    _write(f"cn_{symbol}_{target['end_date']}", text, {
        "market": "A股", "symbol": symbol, "end_date": target["end_date"],
        "title": target["title"], "source_url": target["url"],
        "pdf_bytes": len(content), "chars": len(text),
    })


def dump_hk(symbol: str, index: int = 0) -> None:
    from app.services.report_digest_service import plan_report_targets_detailed
    from app.services.report_fetchers import download_report_pdf

    targets = plan_report_targets_detailed(symbol, "港股")["targets"]
    if len(targets) <= index:
        raise SystemExit(f"{symbol} 只检索到 {len(targets)} 份年报")
    top = targets[index]
    end_date = top["end_date"]
    content = download_report_pdf(top["url"], source="hkexnews")
    text = _pdf_to_pages_text(content)
    _write(f"hk_{symbol}_{end_date}", text, {
        "market": "港股", "symbol": symbol, "end_date": end_date,
        "title": top["title"], "source_url": top["url"],
        "pdf_bytes": len(content), "chars": len(text),
    })


def dump_us(symbol: str, index: int = 0) -> None:
    from app.services.report_digest_service import plan_report_targets_detailed
    from app.services.report_fetchers import edgar_download_filing

    targets = plan_report_targets_detailed(symbol, "美股")["targets"]
    if len(targets) <= index:
        raise SystemExit(f"{symbol} 只检索到 {len(targets)} 份年报（10-K/20-F）")
    target = targets[index]
    ref = target["url"]
    html = edgar_download_filing(ref["cik"], ref["accession"], ref["document"])
    path = FIXTURE_DIR / f"us_{symbol}_{target['end_date']}.html.gz"
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(html)
    (FIXTURE_DIR / f"us_{symbol}_{target['end_date']}.meta.json").write_text(
        json.dumps({
            "market": "美股", "symbol": symbol, "end_date": target["end_date"],
            "report_type": target.get("report_type"), "title": target["title"],
            "chars": len(html),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"写入 {path}  ({len(html)} 字符, {path.stat().st_size // 1024}KB)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("market", choices=["cn", "hk", "us"])
    parser.add_argument("symbol")
    parser.add_argument("--index", type=int, default=0, help="第几份（0=最新）")
    args = parser.parse_args()
    {"cn": dump_cn, "hk": dump_hk, "us": dump_us}[args.market](args.symbol, args.index)


if __name__ == "__main__":
    main()
