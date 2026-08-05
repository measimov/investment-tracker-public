"""财报章节抽取金样（纯函数）：节标题/目录页码/关键词三级回退与截断；
美股 10-K Item 定位（目录/正文双出现取正文段）。"""

import pytest

from app.services.report_sections import (
    SECTION_STORE_MAX_CHARS,
    budget_section,
    extract_cn_sections,
    extract_us_items,
    html_to_text,
    pages_to_text,
)


def _make_report(mdna_title: str = "管理层讨论与分析", body_chars: int = 3000) -> str:
    """构造带目录与正文的年报文本（\\x0c 分页）。"""
    toc = (
        "目录\n"
        "第一节 重要提示、目录和释义……………………3\n"
        "第二节 公司简介和主要财务指标………………5\n"
        "第三节 公司业务概要…………………………8\n"
        f"第四节 {mdna_title}……………………12\n"
        "第五节 公司治理………………………………45\n"
    )
    # 登记信息页：真实年报里它长且靠前，正是把优先级压过业务概要的那一节
    profile = (
        "第二节 公司简介和主要财务指标\n"
        "股票简称 测试股份 股票代码 000000\n注册地址 某市某区\n办公地址 某市某区\n"
        "信息披露媒体 证券时报\n" + "历年财务指标数据" * 400
    )
    business = (
        "第三节 公司业务概要\n"
        "一、主要业务\n本公司主营零售银行与批发金融业务，经营模式为吸收存款发放贷款。"
        + "业务描述" * 100
    )
    mdna = (
        f"第四节 {mdna_title}\n"
        "一、经营情况讨论与分析\n报告期内公司实现营业收入 3,391.23 亿元。"
        + "经营分析内容" * (body_chars // 6)
        + "\n二、主营构成\n零售金融业务收入占比 57.32%。前五名客户占比 3.1%。"
    )
    governance = "第五节 公司治理\n公司治理结构完善。" + "治理内容" * 50
    return pages_to_text([toc, "释义页", profile, business, mdna, governance])


def test_extract_by_section_title_new_format():
    sections = extract_cn_sections(_make_report())
    mdna = sections["mdna"]
    assert mdna is not None
    assert mdna.locator == "section_title"
    assert "经营情况讨论与分析" in mdna.text
    assert "主营构成" in mdna.text
    assert "公司治理结构完善" not in mdna.text  # 终止于下一节
    business = sections["business"]
    assert business is not None
    # [回归锁] 登记信息页更长，旧实现的 max(key=len) 会让它压过业务概要
    assert "主要业务" in business.text
    assert "注册地址" not in business.text
    assert "boilerplate_profile" not in business.quality_flags


def test_extract_legacy_board_report_title():
    """旧年份年报：'董事会报告'标题兜底。"""
    sections = extract_cn_sections(_make_report(mdna_title="董事会报告"))
    mdna = sections["mdna"]
    assert mdna is not None
    assert mdna.locator == "section_title"
    assert "营业收入" in mdna.text


def test_extract_falls_back_to_keyword_window():
    """无标准节标题的排版：关键词窗口保底并标记 locator。"""
    text = pages_to_text([
        "封面",
        "目录\n管理层讨论与分析………12",
        "正文前言" * 50,
        "以下为管理层讨论与分析的内容：报告期内营业收入增长。" + "分析" * 300,
    ])
    sections = extract_cn_sections(text)
    mdna = sections["mdna"]
    assert mdna is not None
    assert mdna.locator == "keyword_window"
    assert "营业收入增长" in mdna.text


def test_oversized_section_is_kept_whole_not_truncated():
    """抽取期不再截断：预算控制唯一发生在 digest 期（budget_section）。

    此前 50k 硬切造成"双重截断"——digest 取的"尾部"是切点前的内容而非
    章节真尾，而 风险要点/展望 恰恰依赖真尾。
    """
    sections = extract_cn_sections(_make_report(body_chars=120_000))
    mdna = sections["mdna"]
    assert mdna is not None
    assert mdna.truncated is False
    assert mdna.chars > 100_000


def test_extract_returns_none_when_section_missing():
    text = pages_to_text(["封面", "无关内容" * 100])
    sections = extract_cn_sections(text)
    assert sections["mdna"] is None
    assert sections["business"] is None


def test_toc_entries_are_not_mistaken_for_body():
    """目录行（带点线页码）不得被当成正文节起点。"""
    sections = extract_cn_sections(_make_report())
    mdna = sections["mdna"]
    assert mdna is not None
    # 正文段应远长于目录行残段
    assert mdna.chars > 1000


# ---------------------------------------------------------------------------
# 美股 10-K（Item 1 / 1A / 7）
# ---------------------------------------------------------------------------


def make_10k_html(mdna_repeats: int = 300) -> str:
    """构造带完整目录 + 正文的 10-K HTML（Item 标题目录/正文各出现一次）。"""
    toc = (
        "<table>"
        "<tr><td>Item 1.</td><td>Business</td><td>3</td></tr>"
        "<tr><td>Item 1A.</td><td>Risk Factors</td><td>10</td></tr>"
        "<tr><td>Item 1B.</td><td>Unresolved Staff Comments</td><td>22</td></tr>"
        "<tr><td>Item 2.</td><td>Properties</td><td>23</td></tr>"
        "<tr><td>Item 7.</td><td>Management&#8217;s Discussion and Analysis</td><td>25</td></tr>"
        "<tr><td>Item 7A.</td><td>Quantitative and Qualitative Disclosures</td><td>40</td></tr>"
        "<tr><td>Item 8.</td><td>Financial Statements</td><td>42</td></tr>"
        "</table>"
    )
    business = (
        "<h2>Item 1. Business</h2><p>The Company designs consumer electronics "
        "and services. " + "segment and supply chain detail. " * 40 + "</p>"
    )
    risk = (
        "<h2>Item 1A. Risk Factors</h2><p>Macroeconomic and industry risks. "
        + "risk factor detail. " * 40 + "</p>"
    )
    unresolved = "<h2>Item 1B. Unresolved Staff Comments</h2><p>None.</p>"
    mdna = (
        "<h2>Item 7. Management&#8217;s Discussion and Analysis</h2>"
        "<p>Net sales increased 8% driven by Services. "
        + "management discussion detail. " * mdna_repeats + "</p>"
    )
    item7a = (
        "<h2>Item 7A. Quantitative and Qualitative Disclosures About Market Risk"
        "</h2><p>Interest rate risk disclosure.</p>"
    )
    return f"<html><body>{toc}{business}{risk}{unresolved}{mdna}{item7a}</body></html>"


def test_extract_us_items_takes_body_over_toc():
    """Item 标题目录/正文双出现：目录残段（极短）被过滤，取正文段。"""
    items = extract_us_items(make_10k_html())
    mdna = items["mdna"]
    assert mdna is not None
    assert mdna.locator == "item_heading"
    assert "Net sales increased" in mdna.text
    assert "Interest rate risk" not in mdna.text  # 终止于 Item 7A
    assert "consumer electronics" not in mdna.text  # 不含 Item 1 内容
    business = items["business"]
    assert business is not None
    assert "consumer electronics" in business.text
    assert "Macroeconomic" not in business.text  # 终止于 Item 1A
    risk = items["risk_factors"]
    assert risk is not None
    assert "Macroeconomic" in risk.text


def test_extract_us_items_missing_item_returns_none():
    """无 Item 7 正文（只有目录行）→ mdna 为 None，不误取目录残段。"""
    html = (
        "<table><tr><td>Item 7.</td><td>MD&amp;A</td><td>25</td></tr>"
        "<tr><td>Item 7A.</td><td>Market Risk</td><td>40</td></tr></table>"
        "<h2>Item 1. Business</h2><p>" + "body detail. " * 60 + "</p>"
    )
    items = extract_us_items(html)
    assert items["mdna"] is None


def test_us_items_only_truncate_at_defensive_store_limit():
    """美股同理：只有超过防御性存储上限才截断。"""
    items = extract_us_items(make_10k_html(mdna_repeats=3_000))
    mdna = items["mdna"]
    assert mdna is not None
    assert mdna.truncated is False
    assert mdna.chars <= SECTION_STORE_MAX_CHARS


def test_accounting_note_heading_is_not_taken_as_a_risk_section():
    """[回归锁] 「主要风险和报酬转移给客户」是收入确认政策，不是风险章节。

    只要求"独占一行 + 任意 0-4 个尾随字符"是不够的——「和报酬」正好三个字，
    附注标题会重新被放进来，然后带着 low_confidence 被落库并送去摘要。
    尾随只允许页码/编号/标点，且风险章节低置信一律返回 None。
    """
    text = pages_to_text([
        "封面",
        "目录\n第一节 重要提示……3",
        "第七节 财务报告\n26、收入\n本公司在履行合同中的履约义务时确认收入。\n"
        "主要风险和报酬\n转移给客户；客户已接受该商品等迹象表明取得控制权。"
        + "会计政策内容。" * 400,
    ])
    assert extract_cn_sections(text)["risk_factors"] is None

    # 更硬的一版：附注正文本身就是风险词密集的（金融工具附注满篇信用风险/
    # 汇率风险），内容置信度拦不住它——只有标题尾随规则能拦
    dense = pages_to_text([
        "封面",
        "目录\n第一节 重要提示……3",
        "第七节 财务报告\n26、收入\n主要风险和报酬\n"
        "转移给客户。本集团的金融工具面临信用风险、流动资金风险、利率风险与"
        "汇率风险，监管环境变化与市场竞争可能导致不确定性。" * 60,
    ])
    assert extract_cn_sections(dense)["risk_factors"] is None

    # 真正的小节标题（尾随只有页码/标点）仍应命中
    real = pages_to_text([
        "封面",
        "目录\n第一节 重要提示……3",
        "第四节 董事会报告\n主要风险和不确定因素 12\n"
        "本集团面临政府政策风险、汇率风险与市场竞争加剧的风险，"
        "可能导致毛利率下降。" * 40,
    ])
    risk = extract_cn_sections(real)["risk_factors"]
    assert risk is not None and risk.locator == "subsection_heading"
    assert risk.confidence >= 0.35


def test_low_confidence_risk_section_is_dropped_not_persisted():
    """[回归锁] 风险章节只有一条定位路径，达不到置信阈值就是没定位到。

    返回带 low_confidence 的结果会被 _ensure_section 照样落库并送去摘要——
    其他章节可以"低置信也先用着"，风险不行：它直接决定 risk_level。
    """
    text = pages_to_text([
        "封面",
        "目录\n第一节 重要提示……3",
        # 标题合法，正文却是子公司名录（无任何风险语义）
        "第四节 董事会报告\n主要风险\n"
        + "本公司主要控股子公司包括甲公司、乙公司、丙公司，注册资本合计一百万元。" * 60,
    ])
    assert extract_cn_sections(text)["risk_factors"] is None


# ---------------------------------------------------------------------------
# digest 输入预算装箱
# ---------------------------------------------------------------------------


def test_oversized_high_weight_block_is_sliced_not_dropped():
    """[回归锁] 单节超预算的高权重块必须受控切片，不得整块丢弃。

    风险因素/展望常常一节就超过整章预算。旧实现"装不下即丢弃"，只要后面还有
    一个小块装得下就照样走 structured 返回，于是风险全段和章节真尾一起消失——
    而这两样正是本改动要保护的东西。
    """
    text = (
        "本节概要。\n"
        "一、主营业务分析\n" + "主营内容。" * 300
        + "\n二、风险因素\n风险开头标记。" + "风险细节。" * 9_000 + "风险结尾标记。"
        + "\n三、公司未来发展的展望\n" + "展望细节。" * 600 + "章节真尾标记。"
    )
    body, meta = budget_section(text, budget=10_000)
    assert meta.strategy == "structured"
    assert len(body) <= 10_000
    assert "风险因素" not in meta.dropped_subsections
    assert "风险因素" in meta.sliced_subsections
    assert "风险开头标记。" in body
    assert "风险结尾标记。" in body  # 切片保留块内真尾
    assert body.endswith("章节真尾标记。")  # 章节真尾同样不得丢


@pytest.mark.parametrize("budget", [3_000, 10_000, 20_000, 30_000])
def test_every_high_weight_block_gets_a_share_not_just_the_first(budget):
    """[回归锁] 多个超预算的同权重小节必须各自拿到份额。

    只做"装不下就切片"仍是贪心：排序后的第一个高权重块会把余量吃光，后面同为
    权重 3 的风险因素/经营情况讨论与分析照样整节消失——静默删除路径只是从
    "全部丢弃"缩成了"除第一个以外全部丢弃"。
    """
    text = (
        "本节概要。\n"
        "一、主营业务分析\n主营开头。" + "主营细节。" * 9_000 + "主营结尾。"
        + "\n二、风险因素\n风险开头。" + "风险细节。" * 9_000 + "风险结尾。"
        + "\n三、经营情况讨论与分析\n经营开头。" + "经营细节。" * 9_000 + "经营结尾。"
        + "\n四、公司未来发展的展望\n" + "展望细节。" * 600 + "章节真尾标记。"
    )
    body, meta = budget_section(text, budget=budget)
    assert meta.strategy == "structured"
    assert len(body) <= budget
    for title in ("主营业务分析", "风险因素", "经营情况讨论与分析"):
        assert title not in meta.dropped_subsections
    for tag in ("主营", "风险", "经营"):
        assert f"{tag}开头。" in body  # 各自的头
        assert f"{tag}结尾。" in body  # 与各自的尾都在
    assert body.endswith("章节真尾标记。")


@pytest.mark.parametrize("budget", [3_000, 8_000, 20_000, 60_000])
def test_budget_result_never_exceeds_the_requested_budget(budget):
    """省略标记必须在装箱**前**预留额度。

    标记若是等块占满预算后再追加，`budget_section(..., budget=N)` 就会静默
    超出 N——而下游正是拿这个值控制 LLM 输入总量的。小节名越长超得越多。
    """
    long_title = "关于本报告期内公司主要经营情况的补充说明"
    text = "前言。\n" + "".join(
        f"{cn}、{long_title}{index}\n" + "小节内容。" * 1_000 + "\n"
        for index, cn in enumerate("一二三四五六七八")
    )
    body, meta = budget_section(text, budget=budget)
    assert meta.strategy in ("structured", "full")
    assert len(body) <= budget
    assert meta.kept_chars == len(body)


def test_head_tail_fallback_stays_within_budget_and_keeps_real_tail():
    """无小节结构时的退化路径同样受预算约束，且必须落在真尾。"""
    text = "无结构正文内容。" * 5_000 + "全文真尾标记。"
    body, meta = budget_section(text, budget=4_000)
    assert meta.strategy == "head_tail"
    assert len(body) <= 4_000
    assert body.endswith("全文真尾标记。")

    tiny, _ = budget_section(text, budget=80)  # 预算小到放不下标记本身
    assert len(tiny) <= 80
    assert tiny.endswith("全文真尾标记。")


def test_html_to_text_strips_tags_and_entities():
    text = html_to_text(
        "<p>Revenue &amp; margin&nbsp;grew</p><div>Q4&#8217;s results</div>"
    )
    assert "Revenue & margin grew" in text
    assert "Q4's results" in text
    assert "<" not in text  # 无残留标签
