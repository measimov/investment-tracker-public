"""港股报告清单与目标选取只有一份（#281）：摘要与报表同一财年选中同一份报告。"""

from app.services import hk_report_catalog, report_fetchers
from app.services import report_digest_service as digest
from app.services import report_statement_service as statements

ANNUAL = [
    {"title": "2025 年報", "ann_date": "09/04/2026 17:21", "url": "https://x/2025a.pdf"},
    # 同财年重刊（公告日更新，DD/MM 比字符串会判反）
    {"title": "2025 年報", "ann_date": "30/04/2026 09:00", "url": "https://x/2025b.pdf"},
    {"title": "2025 年報摘要", "ann_date": "09/04/2026 17:21", "url": "https://x/sum.pdf"},
    {"title": "二零二四年年報", "ann_date": "08/04/2025 17:02", "url": "https://x/2024.pdf"},
]
INTERIM = [
    {"title": "2025 中期報告", "ann_date": "20/08/2025 17:00", "url": "https://x/2025h1.pdf"},
    {"title": "2025 第三季度報告", "ann_date": "20/11/2025 17:00", "url": "https://x/q3.pdf"},
]


def _patch(monkeypatch, calls):
    def fake(symbol, *, report_type="annual", limit=12):
        calls.append((report_type, limit))
        return ANNUAL if report_type == "annual" else INTERIM

    monkeypatch.setattr(report_fetchers, "hkex_reports", fake)


def test_digest_and_statement_pick_the_same_annual_reports(monkeypatch):
    calls = []
    _patch(monkeypatch, calls)
    digest_targets = [
        t
        for t in digest.plan_report_targets_detailed("00700", "港股")["targets"]
        if t["report_type"] == "annual"
    ]
    statement_targets = [
        t
        for t in statements.plan_statement_targets("00700", "港股")["targets"]
        if t["report_type"] == "annual"
    ]
    assert digest_targets == statement_targets
    assert [t["url"] for t in digest_targets] == ["https://x/2025b.pdf", "https://x/2024.pdf"]
    # 清单份数同一口径（keep + LISTING_EXTRA）
    assert {limit for kind, limit in calls if kind == "annual"} == {
        digest.ANNUAL_YEARS + hk_report_catalog.LISTING_EXTRA
    }


def test_interim_noise_excludes_quarterly_reports(monkeypatch):
    _patch(monkeypatch, [])
    targets = hk_report_catalog.hk_report_targets("00700", "interim", 10)
    assert [t["period_key"] for t in targets] == ["20250630|interim"]
