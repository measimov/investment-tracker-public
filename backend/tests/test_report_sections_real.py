"""真实年报固件的章节抽取金样。

构造固件测的是构造者对格式的想象：旧的 `_make_report()` 把「一、主要业务」
写进了「公司简介」节里，于是 `assert "主要业务" in business.text` 在真实数据
抽到注册地址时**照样绿灯**——20/20 份线上报告的业务概要抽错了两个月无人发现。

这里断言**不变量**（边界、真尾部、质量标记）而非字面文本，固件是
pdfplumber 抽取后的文本快照（正是 extract_cn_sections 的真实输入）。
生成见 scripts/dump_report_fixture.py。
"""

import gzip
import json
from pathlib import Path

import pytest

from app.services.report_sections import (
    budget_section,
    cjk_ratio,
    extract_cn_sections,
    extract_us_items,
    html_to_text,
    score_section,
    strip_english_lines,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "reports"


def _load(name: str) -> str:
    with gzip.open(FIXTURE_DIR / f"{name}.pages.txt.gz", "rt", encoding="utf-8") as handle:
        return handle.read()


def _meta(name: str) -> dict:
    return json.loads((FIXTURE_DIR / f"{name}.meta.json").read_text(encoding="utf-8"))


CN_FIXTURES = ["cn_600036_20251231", "cn_000921_20181231"]
HK_FIXTURES = ["hk_00700_20251231", "hk_02156_20251231", "hk_09618_20251231"]
ALL_FIXTURES = CN_FIXTURES + HK_FIXTURES


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_mdna_is_located_with_real_boundaries(name):
    """管理层讨论与分析：四份真实年报都必须抽到，且不是无边界的盲窗。"""
    sections = extract_cn_sections(_load(name))
    mdna = sections["mdna"]
    assert mdna is not None, f"{name} 未定位到 mdna"
    assert mdna.chars > 2_000
    # 盲窗定长 50k 是"不知道边界"的标志；有真实边界时长度不会正好卡在上限
    assert mdna.chars != 50_000
    assert not mdna.truncated  # 抽取期不再截断（预算只在 digest 期做）


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_business_is_never_the_registration_boilerplate(name):
    """[回归锁] 业务概要不得抽成「公司简介和主要财务指标」那页登记信息。

    根因是 `max(candidates, key=len)` 让长度覆盖了优先级；判据 `len<200`
    是长度检查而非内容检查，于是几万字符的 boilerplate 稳稳通过。
    """
    sections = extract_cn_sections(_load(name))
    business = sections.get("business")
    if business is None:
        return  # 该报告确实没有业务概要节，由 company_profile 兜底
    assert "boilerplate_profile" not in business.quality_flags
    # 登记信息页的典型内容不得成段出现在开头
    head = business.text[:1_500]
    assert not ("股票简称" in head and "注册地址" in head)


def test_old_format_report_extracts_real_business_overview():
    """2018 旧格式（公司业务概要与公司简介并存）是本次缺陷的原始现场。"""
    sections = extract_cn_sections(_load("cn_000921_20181231"))
    business = sections["business"]
    assert business is not None
    assert business.locator == "section_title"
    assert business.confidence >= 0.35
    assert "主要业务" in business.text[:200]
    assert "注册地址" not in business.text[:500]


@pytest.mark.parametrize("name", HK_FIXTURES)
def test_traditional_chinese_titles_are_supported(name):
    """港股年报用繁体且有变体（管理層討論及/與分析、業務回顧）。"""
    sections = extract_cn_sections(_load(name))
    assert sections["mdna"] is not None
    assert cjk_ratio(sections["mdna"].text) > 0.3


def test_bilingual_report_strips_english_half():
    """中英同册的港股年报：剔英文行后字符数应显著下降（token 直接减半），
    且正文仍是中文。"""
    raw = _load("hk_02156_20251231")
    stripped, bilingual = strip_english_lines(raw)
    assert bilingual is True
    assert len(stripped) < len(raw) * 0.5
    assert cjk_ratio(stripped) > cjk_ratio(raw)

    sections = extract_cn_sections(raw)
    assert "bilingual_source" in sections["mdna"].quality_flags


def test_single_language_report_is_not_stripped():
    """A股（无英文半册）不得被误删行。"""
    raw = _load("cn_000921_20181231")
    stripped, bilingual = strip_english_lines(raw)
    assert bilingual is False
    assert stripped == raw


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_budget_keeps_the_real_tail(name):
    """[回归锁] 预算裁剪后必须保留章节**真尾部**。

    此前抽取期先 50k 硬切、digest 再取"尾 6000 字"，拿到的其实是切点前的
    内容——而 风险要点/展望 两个 digest 字段恰恰依赖真尾。
    """
    mdna = extract_cn_sections(_load(name))["mdna"]
    body, meta = budget_section(mdna.text, budget=30_000)
    assert len(body) <= 30_000
    if meta.strategy == "full":
        assert body == mdna.text.strip()
        return
    assert meta.omitted_chars > 0
    # 真尾：裁剪结果的末尾与章节原文末尾一致
    assert body[-200:] in mdna.text


def test_budget_names_the_omitted_subsections():
    """省略处必须写明省了哪些小节：`……[中段省略]……` 不说省了什么，而 prompt
    要求"原文未提及一律写'原文未提及'"——截断会伪装成"公司没披露"。"""
    text = (
        "前言部分。\n"
        + "一、主营业务分析\n" + "核心内容。" * 2_000
        + "\n二、募集资金使用情况\n" + "低价值内容。" * 2_000
        + "\n三、主要控股参股公司\n" + "子公司名录。" * 2_000
    )
    body, meta = budget_section(text, budget=12_000)
    assert meta.strategy == "structured"
    assert "主营业务分析" not in meta.dropped_subsections  # 高权重保留
    assert meta.dropped_subsections  # 低权重被丢
    assert "已省略小节" in body
    for name in meta.dropped_subsections[:3]:
        assert name in body  # 名字如实写进占位符


def test_keyword_window_spans_the_chapters_repeated_running_headers():
    """[回归锁] 港股把章节名当页眉逐页重复，见标题即截断只会切出一页残段。

    00700 的 `業務回顧及展望` 是 `主席報告` 章内的小节，起点后 297 字就撞上
    该章页眉；旧实现（跳过起点后 500 字内的标题）停在第二个页眉处，只留 1,026
    字、止于年报第 5 页。正确边界是下一个**不同**的顶层标题 `管理層討論及分析`。
    """
    business = extract_cn_sections(_load("hk_00700_20251231"))["business"]
    assert business is not None
    assert business.locator == "keyword_window"
    # 第二个同名页眉之后的正文仍在
    assert business.text.count("主席報告") >= 2
    assert business.chars > 1_200
    # 且在下一个不同标题前结束
    assert "管理層討論及分析" not in business.text
    assert "馬化騰" in business.text[-300:]  # 主席報告 的真实落款即章节真尾


def test_risk_section_needs_a_real_subsection_heading():
    """[回归锁] 风险章节只认**独占一行**的小节标题，且没有关键词盲窗兜底。

    「主要风险」作为子串会命中财务报表附注里的收入确认政策——实测招行与海信
    家电都抽出「主要风险和报酬转移给客户」开头的 50k 盲窗，一段会计政策被贴上
    【风险因素】喂给 LLM。中文年报本就未必设独立风险章节，抽不到就该是 None。
    """
    found = {
        name: extract_cn_sections(_load(name)).get("risk_factors")
        for name in ALL_FIXTURES
    }
    # 四份真实年报里只有 02156 设了「主要風險和不確定因素」小节
    assert {name for name, r in found.items() if r} == {"hk_02156_20251231"}
    risk = found["hk_02156_20251231"]
    assert risk.locator == "subsection_heading"
    assert risk.text.startswith("主要風險和不確定因素")
    assert risk.chars > 1_000  # 不是被本章页眉截断的一页残段
    assert risk.confidence >= 0.35
    for name, result in found.items():
        if result is None:
            continue
        assert "主要风险和报酬" not in result.text  # 会计政策不得混入


def test_secondary_listing_20f_translation_is_supported():
    """[回归锁] 港股二次上市的中概股年报是 20-F 的中文翻译版（京东 09618）：
    没有「管理層討論及分析」，MD&A 叫「營運與財務回顧及前景」、业务叫
    「有關本公司的資料」。词表不认它们时四期年报全部定位失败——整标的无声
    消失在批量回填里。"""
    sections = extract_cn_sections(_load("hk_09618_20251231"))
    business, mdna = sections["business"], sections["mdna"]
    assert business is not None and mdna is not None
    assert "本公司的歷史" in business.text[:200]  # Item 4.A 的真实开头
    assert "財務狀況與經營業績的討論" in mdna.text[:100]  # Item 5 的真实开头
    assert business.chars > 20_000 and mdna.chars > 10_000
    assert not business.truncated and not mdna.truncated
    # [评审回归] "抽到且够长"不够——第一版就是靠猜的边界标题跨过了下一章，
    # 44.5k 的"MD&A"里混着治理/薪酬/持股。必须断言**真实的下一章标题不在内**：
    # business 止于 MD&A 之前，MD&A 止于「董事、高級管理人員和員工」之前
    assert "營運與財務回顧及前景" not in business.text[100:]
    for next_chapter in ("董事、高級管理人員和員工", "主要股東及關聯交易", "薪酬委員會"):
        assert next_chapter not in mdna.text, f"MD&A 混入下一章内容: {next_chapter}"
        assert next_chapter not in business.text[100:]
    # MD&A 的真实尾部是税务不确定性讨论（205 页「董事」章之前的最后正文）
    assert "未確認的不確定稅務狀況" in mdna.text[-600:]


def test_score_section_flags_registration_page():
    boilerplate = "股票简称 招商银行 股票代码 600036 注册地址 深圳市 办公地址 深圳市 " * 20
    score, flags = score_section("business", boilerplate)
    assert score == 0.0
    assert "boilerplate_profile" in flags

    real = "公司主营业务涵盖冰箱、空调等，经营模式为自主研发生产销售，行业情况如下，核心竞争力在于" * 5
    score, flags = score_section("business", real)
    assert score >= 0.35
    assert "boilerplate_profile" not in flags


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_fixture_meta_matches_content(name):
    meta = _meta(name)
    assert meta["chars"] == len(_load(name))
    assert meta["market"] in ("A股", "港股")


# ---------------------------------------------------------------------------
# 美股 10-K / 20-F 真实固件
# ---------------------------------------------------------------------------


def _load_html(name: str) -> str:
    with gzip.open(FIXTURE_DIR / f"{name}.html.gz", "rt", encoding="utf-8") as handle:
        return handle.read()


def test_20f_items_are_located_by_foreign_issuer_numbering():
    """[回归锁] 20-F 的 Item 编号与 10-K 完全不同：业务在 Item 4、MD&A 在
    Item 5、风险因素是 Item 3 之下的 D 小节。套 10-K 编号只会抽到别的章节。"""
    items = extract_us_items(_load_html("us_PDD_20251231"), form_type="20-F")
    business, mdna, risk = items["business"], items["mdna"], items["risk_factors"]
    assert business is not None and mdna is not None and risk is not None
    # 三节都必须**从各自的真实标题**开始，而不是从正文里的交叉引用开始
    assert business.text.lower().startswith("item 4. information on the company")
    assert mdna.text.lower().startswith("item 5. operating and financial review")
    assert risk.text.lower().startswith("d. risk factors")
    # 边界正确：business 不得吃进 MD&A
    assert "Item 5. Operating and Financial Review and Prospects\n" not in business.text
    for section in (business, mdna, risk):
        assert section.confidence >= 0.35
        assert "unbounded_item" not in section.quality_flags


def test_10k_rules_on_a_20f_do_not_silently_return_garbage():
    """表单类型选错时宁可返回 None/低置信，也不能给出一段看似正常的错误章节。"""
    items = extract_us_items(_load_html("us_PDD_20251231"), form_type="10-K")
    assert items["business"] is None
    assert items["risk_factors"] is None
    mdna = items["mdna"]  # Item 7 在 20-F 里是"主要股东"，命中的是交叉引用
    assert mdna is None or mdna.confidence < 0.5


def test_ixbrl_header_does_not_leak_into_text():
    """[回归锁] iXBRL filing 的 <ix:header> 只剥标签会残留 18k 字符 XBRL 上下文
    （`0001737806 2025 FY false ... http://fasb.org/us-gaap/...`），而且就堆在
    文档最前面，正好落进 Item 候选段。"""
    text = html_to_text(_load_html("us_PDD_20251231"))
    assert "http://fasb.org/us-gaap" not in text
    assert "us-gaap:RetainedEarningsMember" not in text


def test_ixbrl_split_words_do_not_break_item_boundaries():
    """[回归锁] iXBRL 把标题拆进多个内联标签，剥标签后成了 `ITE M 6.`、
    `IT EM`、`OPERATING AND FINAN CIAL`（BABA 20-F 三种变体并存）。

    写死 `item` 的正则匹配不到下一章标题 → MD&A 从 Item 5 一路吞到文末，
    再被存储上限截断成 200,007 字符并打上 unbounded_item。
    """
    items = extract_us_items(_load_html("us_BABA_20260331"), form_type="20-F")
    mdna = items["mdna"]
    assert mdna is not None
    assert "unbounded_item" not in mdna.quality_flags
    assert not mdna.truncated  # 有真实边界就不该撞上存储上限
    assert mdna.chars < 200_000
    # 起点是被拆开的正文标题，终点在同样被拆开的 ITE M 6. 之前
    assert mdna.text.lower().startswith("item 5. operating and finan cial")
    assert "DIRECTORS, SENIOR MANAGEMENT AND EMPLOYEES" not in mdna.text
    assert items["business"] is not None and items["risk_factors"] is not None


def test_10k_extraction_is_unchanged_by_the_20f_work():
    items = extract_us_items(_load_html("us_AAPL_20250927"), form_type="10-K")
    assert items["business"].text.lower().startswith("item 1. business")
    assert "Item 1A" not in items["business"].text
    assert items["mdna"] is not None and items["risk_factors"] is not None
