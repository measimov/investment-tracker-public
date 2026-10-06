"""章节抽取 v6（#340 #344 #345）与报告清单（#346）的回归。

复现样本一律走生产入口（`extract_cn_sections` / `extract_us_items` / 清单函数），真实固件断言
**不变量**（数字是否保留、下一章是否被吞进来、起点是否对），构造样例只补真实固件覆盖不到的
版式，并配上不应改变的真实反例。
"""

import gzip
import json
import re
from pathlib import Path

import pytest
import requests

from app.services import hk_report_catalog, report_fetchers
from app.services import report_digest_service as svc
from app.services import report_sections as rs
from app.services.llm_client import LLMClientError

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "reports"
AMOUNT_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+%?|\d+%")


def _load(name: str) -> str:
    with gzip.open(FIXTURE_DIR / f"{name}.pages.txt.gz", "rt", encoding="utf-8") as handle:
        return handle.read()


def _load_html(name: str) -> str:
    with gzip.open(FIXTURE_DIR / f"{name}.html.gz", "rt", encoding="utf-8") as handle:
        return handle.read()


def _form(name: str) -> str:
    meta = json.loads((FIXTURE_DIR / f"{name}.meta.json").read_text(encoding="utf-8"))
    return meta.get("report_type") or "10-K"


# ---------------------------------------------------------------------------
# #340 双语册剔英文行不得删掉数字
# ---------------------------------------------------------------------------


def test_bilingual_mdna_keeps_its_amounts_02156():
    """02156 2025 年报 MD&A 原文（p17–40）有 360 个金额/百分比，修复前抽出的 mdna 里 0 个，
    正文变成「收入約人民幣 / 百萬元」。"""
    sections = rs.extract_cn_sections(_load("hk_02156_20251231"))
    mdna = sections["mdna"]
    assert mdna is not None and "bilingual_source" in mdna.quality_flags
    amounts = AMOUNT_RE.findall(mdna.text)
    assert len(amounts) >= 200  # 与原文同一数量级
    for figure in ("2,225.4", "24.9%", "1,781.1"):
        assert figure in mdna.text


def test_numeric_lines_do_not_make_a_monolingual_report_bilingual():
    """09618 2020 是单语裁页：三列数字行多，修复前被判成双语、65 行带中文标签的数字行全被删。"""
    text = _load("hk_09618_20201231")
    stripped, bilingual = rs.strip_english_lines(text)
    assert bilingual is False and stripped == text
    # 真双语册仍判双语、仍剔掉纯英文段落
    stripped, bilingual = rs.strip_english_lines(_load("hk_02156_20251231"))
    assert bilingual is True
    assert "The Board is pleased to present" not in stripped


def test_amount_lines_survive_but_plain_english_lines_do_not():
    text = "\n".join(
        [
            "本集團收入約人民幣",
            "Revenue amounted to approximately RMB2,225.4 million, up 24.9%",
            "The Group continued to focus on service quality in the year",
            "收入 REVENUE 5 15,099,076 12,639,332",
        ]
        * 10
    )
    stripped, bilingual = rs.strip_english_lines(text)
    assert bilingual is True
    assert "2,225.4" in stripped and "15,099,076" in stripped
    assert "service quality" not in stripped


def test_page_breaks_on_dropped_english_lines_are_kept():
    text = "中文正文一行\n" * 5 + "English only line\x0cMore English\n" * 5 + "中文\n"
    stripped, _ = rs.strip_english_lines(text)
    assert stripped.count("\x0c") == text.count("\x0c")


# ---------------------------------------------------------------------------
# #344 正文起点、章节边界、A股 风险小节
# ---------------------------------------------------------------------------


def test_hk_mdna_first_page_is_not_skipped_00700():
    """修复前 mdna 从页中「收入。截至…」开始，丢掉 MD&A 第一页整张全年损益对比表。"""
    mdna = rs.extract_cn_sections(_load("hk_00700_20251231"))["mdna"]
    assert mdna.text.lstrip().startswith("截至二零二五年十二月三十一日止年度與")
    assert "751,766" in mdna.text and "229,801" in mdna.text


def test_body_start_counts_real_pages():
    text = "封面\x0c目录\x0c目录二\x0c公司资料\x0c财务概要\x0c正文开始"
    assert rs._body_start_offset(text) == len("封面\x0c目录\x0c目录二\x0c公司资料\x0c财务概要")


def test_listing_candidates_are_not_section_bodies():
    """紫金 601899 第 6 页的分目录（短行、无句读）之后紧跟董事长致辞，整段照样命中 MD&A 特征词。"""
    listing = "\n".join(
        [
            "报告期内公司所处行业情况",
            "报告期内公司从事的业务情况",
            "报告期内核心竞争力分析",
            "报告期内主要经营情况",
            "资产、负债情况分析",
            "行业经营性信息分析",
            "投资状况分析",
            "7 / 2024年计划及展望",
        ]
    )
    assert rs.looks_like_listing(listing + "\n董事长致辞：报告期内营业收入同比增长。")
    table = "\n".join(["收入 751,766 660,257"] * 8)
    assert not rs.looks_like_listing(table)
    prose = "报告期内公司实现营业收入 2,934.03 亿元，同比增长 8.54%。\n" * 8
    assert not rs.looks_like_listing(prose)


def test_keyword_window_skips_a_low_confidence_first_hit():
    """A股 新版年报业务关键词的第一次出现是第二节登记信息里的「主营业务的变化情况 无变更」：
    取它就是一段会计师事务所地址与财务指标表，应往后找到内容置信度达标的那一处。"""
    registration = (
        "主营业务的变化情况（如有） 无变更\n境内会计师事务所名称 某某会计师事务所\n"
        "境内会计师事务所办公地址 上海市\n股票简称 某某\n注册地址 深圳市\n"
        + "办公地址 深圳市 电子信箱 ir@example.com\n"
        * 20
    )
    analysis = (
        "主营业务分析\n1、概述\n报告期内营业收入同比增长 12%，毛利率提升，经营情况稳健，"
        "前五名客户合计销售占比 8%，产品结构优化。\n" * 20
    )
    text = (
        "封面\x0c目录\x0c目录\x0c资料\x0c资料\x0c第二节 公司简介和主要财务指标\n"
        + registration
        + "\x0c第三节 管理层讨论与分析\n"
        + analysis
    )
    body = rs._find_section_by_keyword(text, ["主营业务"], name="business")
    assert body.startswith("主营业务分析")
    # 不给 name（不评分）时行为同旧实现：取第一处
    assert rs._find_section_by_keyword(text, ["主营业务"]).startswith("主营业务的变化情况")


def test_bank_mdna_stops_before_the_esg_chapter_600036():
    """600036 的页眉是「18 招商银行股份有限公司 第三章 管理层讨论与分析」：修复前「第四章 环境、
    社会与治理(ESG)」认不出，整章约 2 万字并进 MD&A，直到「第五章 公司治理」才停。"""
    sections = rs.extract_cn_sections(_load("cn_600036_20251231"))
    mdna = sections["mdna"]
    assert mdna is not None
    assert not re.search(r"第四章\s*环境、社会与治理", mdna.text)
    assert "3.1 总体经营情况分析" in mdna.text
    assert mdna.chars < 80_000
    # 业务概要不再越过「第二章 会计数据和财务指标摘要」
    business = sections["business"]
    assert business is not None and "第二章 会计数据和财务指标摘要" not in business.text


def test_director_biographies_are_not_part_of_mdna_or_risks():
    """02156 的风险小节修复前约 73% 是董事履历；01995 2018 的 MD&A 修复前吞进整段履历。"""
    sections = rs.extract_cn_sections(_load("hk_02156_20251231"))
    for name in ("mdna", "risk_factors"):
        assert "董事及高級管理層的履歷詳情" not in sections[name].text
    mdna = rs.extract_cn_sections(_load("hk_01995_20181231"))["mdna"]
    assert "董事及高級管理層履歷" not in mdna.text and "林中先生" not in mdna.text


@pytest.mark.parametrize(
    "line",
    [
        "70 招商银行股份有限公司 第四章 环境、社会与治理",
        "第五章 公司治理",
        "招商银行股份有限公司 第三章 管理层讨论与分析",
    ],
)
def test_numbered_chapter_lines_are_boundaries(line):
    heading = rs._normalize_heading_line(line)
    assert rs._numbered_chapter_title(heading)


@pytest.mark.parametrize(
    "line",
    [
        "本公司已在本报告中详细描述存在的主要风险及采取的应对措施，详情请参阅第三章有关风险管理的内容。",
        "详见第六节重要事项。",
        "根據香港法例第622章《公司條例》附表5",
    ],
)
def test_sentences_mentioning_chapters_are_not_boundaries(line):
    assert rs._numbered_chapter_title(rs._normalize_heading_line(line)) is None


RISK_BODY = "面临宏观经济波动风险、市场竞争加剧风险、原材料价格波动风险与汇率风险。" * 12


@pytest.mark.parametrize(
    "heading",
    ["可能面对的风险", "（四）可能面对的风险", "四、可能面对的风险", "(四) 可能面对的风险"],
)
def test_a_share_risk_subsection_accepts_numbering(heading):
    text = "封面\x0c目录\x0c目录\x0c正文\n" + "经营情况\n" * 50 + f"{heading}\n{RISK_BODY}\n"
    risk = rs.extract_cn_sections(text)["risk_factors"]
    assert risk is not None and risk.text.startswith(heading)


def test_accounting_policy_line_is_still_not_a_risk_section():
    text = (
        "封面\x0c目录\x0c目录\x0c正文\n"
        + "经营情况\n" * 50
        + "（四）主要风险和报酬转移给客户\n"
        + RISK_BODY
    )
    assert rs.extract_cn_sections(text)["risk_factors"] is None


# ---------------------------------------------------------------------------
# #345 美股：实体/破折号变体、Items 1 and 2、20-F 上限
# ---------------------------------------------------------------------------

BUSINESS = "Our business designs products for customers in every market segment. " * 20
RISKS = "We may face regulatory risks that could adversely affect our business. " * 20
MDNA = "Results of operations compared to prior fiscal year: revenue and cash flow. " * 20


def _ten_k(sep: str, *, item_word: str = "Item", business_head: str = "1") -> str:
    return (
        "<html><body>"
        f"<p>{item_word}&#160;{business_head}{sep}Business</p><p>{BUSINESS}</p>"
        f"<p>Item&#160;1A{sep}Risk Factors</p><p>{RISKS}</p>"
        f"<p>Item&#160;2{sep}Properties</p><p>Offices.</p>"
        f"<p>Item&#160;7{sep}Management&#8217;s Discussion and Analysis</p><p>{MDNA}</p>"
        f"<p>Item&#160;8{sep}Financial Statements</p><p>Tables.</p>"
        "</body></html>"
    )


@pytest.mark.parametrize(
    "sep",
    [".", "&#8211;", " &mdash; ", " &ndash; ", "&#151;", "&#x2014;", "&#xa0;-&#xa0;", " – ", " "],
)
def test_item_heading_separator_variants(sep):
    extracted = rs.extract_us_items(_ten_k(sep), form_type="10-K")
    for name, marker in (
        ("business", "designs products"),
        ("risk_factors", "regulatory"),
        ("mdna", "Results of operations"),
    ):
        assert extracted[name] is not None, f"{sep!r} → {name} 未定位"
        assert marker in extracted[name].text
        assert "unbounded_item" not in extracted[name].quality_flags


def test_hex_entities_are_decoded():
    text = rs.html_to_text("<p>Item&#xa0;7.&#x2003;Management&#x2019;s</p>")
    assert "Item 7." in text and "Management's" in text


def test_items_one_and_two_combined_heading():
    html = _ten_k(".", item_word="Items", business_head="1 and 2")
    business = rs.extract_us_items(html, form_type="10-K")["business"]
    assert business is not None and "designs products" in business.text


@pytest.mark.parametrize(
    ("name", "phrases"),
    [
        ("us_BABA_20260331", ("Risks Related to Our Corporate Structure", "Our ADSs")),
        ("us_PDD_20251231", ("Doing Business in China", "ADSs")),
    ],
)
def test_20f_risk_factors_are_no_longer_cut_at_200k(name, phrases):
    """BABA 风险因素 35 万字、PDD 30 万字：20 万字上限曾砍掉 VIE / 中国监管 / ADS 风险。"""
    risk = rs.extract_us_items(_load_html(name), form_type=_form(name))["risk_factors"]
    assert risk is not None and not risk.truncated
    assert risk.chars > 250_000
    for phrase in phrases:
        assert phrase.lower() in risk.text.lower()


def test_store_cap_keeps_head_and_true_tail():
    body = "头" * 500_000 + "中" * 300_000 + "尾部结论"
    kept, truncated = rs._truncate(body)
    assert truncated and len(kept) <= rs.SECTION_STORE_MAX_CHARS + 60
    assert kept.startswith("头") and kept.endswith("尾部结论")
    assert "中段省略" in kept


def test_edgar_filing_download_goes_through_the_watchdog(monkeypatch):
    seen = {}

    def fake_download(url, headers):
        seen["url"] = url
        return "Item 7 — Management’s".encode("cp1252")

    monkeypatch.setattr(report_fetchers, "_download_once", fake_download)
    monkeypatch.setattr(report_fetchers, "_throttle", lambda *a, **k: None)
    text = report_fetchers.edgar_download_filing(123, "0000123-26-000001", "x.htm")
    assert seen["url"].endswith("/123/000012326000001/x.htm")
    assert "Management’s" in text  # utf-8 解不开时按 cp1252


# ---------------------------------------------------------------------------
# #346 报告清单
# ---------------------------------------------------------------------------


def _columns(entries):
    return {
        "form": [e[0] for e in entries],
        "accessionNumber": [f"acc-{e[1]}" for e in entries],
        "primaryDocument": [f"{e[1]}.htm" for e in entries],
        "filingDate": [f"{e[1]}-03-01" for e in entries],
        "reportDate": [f"{e[1] - 1}-12-31" for e in entries],
    }


def test_edgar_annual_filings_read_paginated_files(monkeypatch):
    """`filings.recent` 只保证一年或 1000 条：Form 4 多的发行人 recent 里只剩三份年报。"""
    recent = _columns([("4", 2026)] * 5 + [("10-K", 2026), ("10-K", 2025), ("10-K", 2024)])
    older = _columns([("10-K", year) for year in range(2023, 2015, -1)])
    monkeypatch.setattr(
        report_fetchers,
        "edgar_submissions",
        lambda cik: {
            "filings": {"recent": recent, "files": [{"name": "CIK1-submissions-001.json"}]}
        },
    )
    monkeypatch.setattr(report_fetchers, "_edgar_get_json", lambda url: older)
    filings = report_fetchers.edgar_recent_annual_filings(1, limit=10)
    assert [f["accession"] for f in filings] == [f"acc-{y}" for y in range(2026, 2016, -1)]

    def broken(url):
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(report_fetchers, "_edgar_get_json", broken)
    with pytest.raises(report_fetchers.EdgarListingIncomplete) as caught:
        report_fetchers.edgar_recent_annual_filings(1, limit=10)
    assert len(caught.value.filings) == 3


def test_us_plan_is_incomplete_when_a_listing_page_fails(monkeypatch):
    monkeypatch.setattr(report_fetchers, "edgar_lookup", lambda symbol: {"cik": 1, "title": "X"})

    def partial(cik, limit):
        raise report_fetchers.EdgarListingIncomplete(
            "page failed",
            [
                {
                    "form": "10-K",
                    "accession": "a",
                    "primary_document": "d.htm",
                    "filing_date": "2026-03-01",
                    "report_date": "2025-12-31",
                }
            ],
        )

    monkeypatch.setattr(report_fetchers, "edgar_recent_annual_filings", partial)
    planned = svc.plan_report_targets_detailed("XYZ", "美股")
    assert planned["complete"] is False
    assert [t["period_key"] for t in planned["targets"]] == ["20251231|10-K"]


@pytest.mark.parametrize(
    ("title", "noise"),
    [
        ("关于2024年年度报告的更正公告", True),
        ("关于2024年年度报告全文及摘要的更正说明", True),
        ("2024年年度报告更正公告", True),
        ("2024年年度报告（更正后）", False),
        ("2024年年度报告（修订版）", False),
        ("2024年年度报告（更新后）", False),
        ("2024年年度报告", False),
        ("2024年年度报告摘要", True),
    ],
)
def test_a_share_report_noise(title, noise):
    assert svc.is_a_share_report_noise(title) is noise


@pytest.mark.parametrize(
    ("title", "years"),
    [
        ("二○二四年年報", [2024]),
        ("二零二五年年報", [2025]),
        ("２０２４年年報", [2024]),
        ("2024/25 年報", [2025, 2024]),
        ("2024-25年報", [2025, 2024]),
        ("2025年年報", [2025]),
        ("年報", []),
    ],
)
def test_hk_title_years(title, years):
    assert hk_report_catalog.extract_report_years(title) == years


def test_hk_cross_year_title_resolves_to_the_later_fiscal_year_end():
    assert hk_report_catalog.infer_hk_fiscal_end("2024/25 年報", "20/06/2025 16:30") == "20250331"
    # 单年份标题的既有结果不变
    assert hk_report_catalog.infer_hk_fiscal_end("2025年年報", "28/03/2026 16:30") == "20251231"
    assert hk_report_catalog.infer_hk_fiscal_end("2025年年報", "20/06/2025 16:30") == "20250331"


def test_cninfo_local_date_uses_business_timezone():
    # 固件 cninfo_600298_page.json 的 announcementTime：UTC 2026-09-24 16:00 = 北京 09-25 零点
    assert report_fetchers.cninfo_local_date(1790265600000) == "2026-09-25"
    assert report_fetchers.cninfo_local_date(None) == ""


def test_year_fallback_uses_local_date_but_fingerprint_keeps_utc(monkeypatch):
    rows = [
        {
            "title": "年度报告",
            "ann_date": "2025-12-31",
            "ann_date_local": "2026-01-01",
            "url": "u1",
        }
    ]
    monkeypatch.setattr(
        svc,
        "cninfo_search_reports",
        lambda symbol, report_type, se_date: rows if report_type == "annual" else [],
    )
    (target,) = svc.plan_report_targets_detailed("600000", "A股")["targets"]
    assert target["end_date"] == "20251231"  # UTC 日期兜底会得出 2024
    assert target["ann_date"] == "2025-12-31"  # 指纹字段原样
    assert svc.source_fingerprint(target) == "u1|2025-12-31"


# ---------------------------------------------------------------------------
# #347-2 瞬时 / 确定性失败分类
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (requests.exceptions.ChunkedEncodingError("reset"), True),
        (requests.exceptions.ContentDecodingError("bad gzip"), True),
        (requests.exceptions.InvalidURL("x"), False),
        (LLMClientError("empty", finish_reason="content_filter"), False),
        (LLMClientError("busy", finish_reason="insufficient_system_resource"), True),
        (LLMClientError("cut", finish_reason="length"), False),
        (LLMClientError("5xx", status_code=503), True),
        (ValueError("未能定位管理层讨论与分析章节"), False),
    ],
)
def test_transient_classification(exc, transient):
    assert svc.is_transient_error(exc) is transient
