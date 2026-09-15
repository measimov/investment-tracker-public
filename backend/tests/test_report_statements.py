"""三张报表定位/解析纯函数：真实港股年报/中报文本金样（\\x0c 分页，与 report_sections 同源）。

固件覆盖的版式：00700 2025（标准 IFRS，每页重复标题，五年財務概要页同名标题，附註里
「43 綜合現金流量表附註」）、00700 2014（老版式，附註节含「42 綜合現金流量表」整行小节
标题）、00700 2026 中报（簡明报表，损益四列 = 三个月+六个月）、01995 2018（「綜合損益及全面
收益表」变体，千元）、02156 2025（中英双语）、09926 2025（附注列在左）、09618 2025（美国
准则口径：年份升序 + 美元折算列，「合併經營狀況及綜合收益表」）。
"""

import gzip
from decimal import Decimal
from pathlib import Path

import pytest

from app.services import report_statements as rs

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "reports"


def _pages(name: str):
    with gzip.open(FIXTURE_DIR / f"{name}.pages.txt.gz", "rt", encoding="utf-8") as handle:
        return handle.read().split("\x0c")


ANNUAL = {
    # name: (income_pages, balance_pages, cashflow_pages, unit, currency, years)
    "hk_00700_20251231": ((130, 131), (132, 134), (139, 140), 1_000_000, "CNY", [2025, 2024]),
    "hk_00700_20141231": ((80, 82), (75, 79), (85, 86), 1_000_000, "CNY", [2014, 2013]),
    "hk_01995_20181231": ((73, 73), (74, 75), (78, 79), 1_000, "CNY", [2018, 2017]),
    "hk_02156_20251231": ((270, 271), (272, 273), (276, 277), 1_000, "CNY", [2025, 2024]),
    "hk_09926_20251231": ((120, 121), (122, 123), (126, 127), 1_000, "CNY", [2025, 2024]),
    "hk_09618_20251231": ((265, 266), (262, 264), (267, 270), 1_000_000, "CNY", None),
}


@pytest.mark.parametrize("name", sorted(ANNUAL))
def test_annual_reports_locate_all_three_statements(name):
    income, balance, cashflow, unit, currency, years = ANNUAL[name]
    found = rs.locate_statements(_pages(name), report_type="annual")
    for kind, expected in (("income", income), ("balance", balance), ("cashflow", cashflow)):
        parsed = found[kind]
        assert parsed is not None, f"{name} 未定位 {kind}"
        assert (parsed.page_start, parsed.page_end) == expected, (name, kind, parsed.page_start, parsed.page_end)
        assert parsed.unit_multiplier == unit
        assert parsed.currency == currency
        assert len(parsed.rows) >= rs.MIN_ROWS
        if years is not None:
            assert parsed.years == years
        # 主导列数下的行占绝对多数（附注号已剥离、年份行已剔除）
        on_grid = sum(1 for row in parsed.rows if len(row.values) == parsed.column_count)
        assert on_grid >= len(parsed.rows) * 0.85, (name, kind)


def test_tencent_2025_values_and_note_splitting():
    found = rs.locate_statements(_pages("hk_00700_20251231"), report_type="annual")
    income, balance, cashflow = found["income"], found["balance"], found["cashflow"]
    assert income.column_count == balance.column_count == cashflow.column_count == 2
    # 无标签合计行：收入合计（附注 6）
    total = next(r for r in income.rows if r.label == "" and r.note == "6")
    assert total.values == [Decimal("751766"), Decimal("660257")]
    cost = next(r for r in income.rows if r.label == "收入成本")
    assert cost.note == "7" and cost.values == [Decimal("-329173"), Decimal("-311011")]
    # 附注号粘在数值列前：「物業、設備及器材 17 149,905 80,185」
    ppe = next(r for r in balance.rows if r.label == "物業、設備及器材")
    assert ppe.note == "17" and ppe.values == [Decimal("149905"), Decimal("80185")]
    eps = next(r for r in income.rows if r.label == "－基本")
    assert eps.note == "13(a)" and eps.values == [Decimal("24.749"), Decimal("20.938")]
    cfo = next(r for r in cashflow.rows if r.label == "經營活動所得現金流量淨額")
    assert cfo.values == [Decimal("303052"), Decimal("258521")]
    # 五年財務概要（第 4 页，五列）不得被当成损益表；附註节同名小节不得被当成现金流量表
    assert income.page_start == 130 and cashflow.page_start == 139
    cols = rs.period_columns(income, report_type="annual", end_date="20251231")
    assert [(c.column, c.end_date, c.fp, c.is_primary) for c in cols] == [
        (0, "20251231", "FY", True), (1, "20241231", "FY", False),
    ]


def test_tencent_2014_old_layout_skips_notes_subsection_title():
    """2014 年报附註节里有整行「42 綜合現金流量表」小节标题（p195）：起始页页首是
    「綜合財務報表附註」，必须剔除，真正的现金流量表在 p85。"""
    found = rs.locate_statements(_pages("hk_00700_20141231"), report_type="annual")
    assert found["cashflow"].page_start == 85
    assert found["balance"].page_start == 75 and found["balance"].page_end == 79


def test_interim_report_four_columns_and_balance_prior_fiscal_year():
    found = rs.locate_statements(_pages("hk_00700_20260630_interim"), report_type="interim")
    income, balance, cashflow = found["income"], found["balance"], found["cashflow"]
    assert (income.page_start, income.page_end) == (25, 26)
    assert income.column_count == 4 and income.interim_four_columns
    assert income.years == [2026, 2025, 2026, 2025]
    total = next(r for r in income.rows if r.label == "" and r.note == "6")
    assert total.values == [Decimal("204785"), Decimal("184504"), Decimal("401243"), Decimal("364526")]
    # 四列取六个月那组
    cols = rs.period_columns(income, report_type="interim", end_date="20260630")
    assert [(c.column, c.end_date, c.fp) for c in cols] == [(2, "20260630", "H1"), (3, "20250630", "H1")]
    # 资产负债表两列：期末 H1 + 上财年末 FY
    cols = rs.period_columns(balance, report_type="interim", end_date="20260630")
    assert [(c.column, c.end_date, c.fp, c.is_primary) for c in cols] == [
        (0, "20260630", "H1", True), (1, "20251231", "FY", False),
    ]
    cols = rs.period_columns(cashflow, report_type="interim", end_date="20260630")
    assert [(c.column, c.end_date, c.fp) for c in cols] == [(0, "20260630", "H1"), (1, "20250630", "H1")]
    assert cashflow.column_count == 2 and not cashflow.interim_four_columns


def test_jd_us_gaap_layout_years_ascending_with_usd_column():
    """京东：合併經營狀況及綜合收益表三年 + 美元折算列，年份升序——本期列由表头年份决定。"""
    found = rs.locate_statements(_pages("hk_09618_20251231"), report_type="annual")
    income, balance = found["income"], found["balance"]
    assert income.title.startswith("合併經營狀況及綜合收益表")
    assert income.years == [2023, 2024, 2025] and income.column_count == 4
    cols = rs.period_columns(income, report_type="annual", end_date="20251231")
    assert [(c.column, c.end_date, c.is_primary) for c in cols] == [
        (2, "20251231", True), (1, "20241231", False),
    ]
    assert balance.years == [2024, 2025] and balance.column_count == 3
    cols = rs.period_columns(balance, report_type="annual", end_date="20251231")
    assert [(c.column, c.end_date) for c in cols] == [(1, "20251231"), (0, "20241231")]
    assert rs.years_consistent(income, end_date="20251231")
    assert not rs.years_consistent(income, end_date="20221231")


def test_bilingual_report_rows_keep_english_labels():
    found = rs.locate_statements(_pages("hk_02156_20251231"), report_type="annual")
    income = found["income"]
    revenue = next(r for r in income.rows if r.label == "Revenue")
    assert revenue.note == "5" and revenue.values == [Decimal("3880549"), Decimal("3292901")]
    # 「FOR THE YEAR ENDED 31 DECEMBER 2025」不得被当成只有一个值 2025 的行
    assert not any(len(r.values) == 1 and r.values[0] == 2025 for r in income.rows)


@pytest.mark.parametrize("line, columns, expected", [
    # 已知两列：比列数多一个 token 且首 token 是附注形态 → 剥附注
    ("收入成本 7 (329,173) (311,011)", 2, ("收入成本", "7", [Decimal("-329173"), Decimal("-311011")])),
    ("6 751,766 660,257", 2, ("", "6", [Decimal("751766"), Decimal("660257")])),
    ("物業、設備及器材 17 149,905 80,185", 2, ("物業、設備及器材", "17", [Decimal("149905"), Decimal("80185")])),
    # 列数吻合：小额整数就是数值，哪怕另一列带千分位（评审 P1 复现）
    ("利息收入 80 1,200", 2, ("利息收入", "", [Decimal("80"), Decimal("1200")])),
    ("80 1,200", 2, ("", "", [Decimal("80"), Decimal("1200")])),
    ("利息收入 4 5", 2, ("利息收入", "", [Decimal("4"), Decimal("5")])),
    # 列数未知：不猜附注，原样当数值（宁多一列不错列）
    ("收入成本 7 (329,173) (311,011)", 0, ("收入成本", "", [Decimal("7"), Decimal("-329173"), Decimal("-311011")])),
    ("229,801 196,467", 0, ("", "", [Decimal("229801"), Decimal("196467")])),
    ("投資物業 268 —", 0, ("投資物業", "", [Decimal("268"), None])),
    # 标签尾部的非数字附注号（12(a) / 七、1）不依赖列数
    ("所得稅開支 12(a) (47,448) (45,018)", 0, ("所得稅開支", "12(a)", [Decimal("-47448"), Decimal("-45018")])),
    ("货币资金 七、1 4,076,200,534.07 4,551,004,583.08", 0,
     ("货币资金", "七、1", [Decimal("4076200534.07"), Decimal("4551004583.08")])),
    ("－基本 13(a) 24.749 20.938", 0, ("－基本", "13(a)", [Decimal("24.749"), Decimal("20.938")])),
])
def test_parse_row(line, columns, expected):
    assert rs.parse_row(line, expected_columns=columns) == expected


def test_small_amounts_are_not_mistaken_for_notes_when_columns_match():
    """评审 P1：两列表头下「利息收入 80 1,200」/「80 1,200」是合法数据行，不得把 80 剥成附注
    让本期取到上期的 1,200；同表内真实附注行（「收入 6 1,000 900」）仍要剥。整表到 resolve_value。"""
    pages = [
        "綜合收益表\n截至二零二五年十二月三十一日止年度\n二零二五年 二零二四年\n附註 人民幣百萬元 人民幣百萬元\n"
        "收入 6 1,000 900\n利息收入 80 1,200\n80 1,200\n財務成本 7 (12) (9)\n其他 5 4\n毛利 300 250\n"
        "除稅前盈利 200 150\n年度盈利 180 130\n"
    ]
    parsed = rs.locate_statements(pages, report_type="annual")["income"]
    assert parsed.column_count == 2
    by_label = {row.label: row for row in parsed.rows}
    assert (by_label["收入"].note, by_label["收入"].values) == ("6", [Decimal("1000"), Decimal("900")])
    assert (by_label["利息收入"].note, by_label["利息收入"].values) == ("", [Decimal("80"), Decimal("1200")])
    unlabeled = next(row for row in parsed.rows if row.label == "")
    assert (unlabeled.note, unlabeled.values) == ("", [Decimal("80"), Decimal("1200")])
    assert (by_label["財務成本"].note, by_label["財務成本"].values) == ("7", [Decimal("-12"), Decimal("-9")])
    interest = by_label["利息收入"].row_id
    assert rs.resolve_value(parsed, [interest], 0, scale=True) == Decimal("80000000")
    assert rs.resolve_value(parsed, [interest], 1, scale=True) == Decimal("1200000000")
    assert all(len(row.values) == 2 for row in parsed.rows)


def test_expected_columns_rules():
    # 1) 表头"每列一个币种/单位 token"最可靠：两列人民币 → 2；人民币两列 + 美元折算一列 → 3；
    #    整表美元计价两列 → 2（不能靠"表头提到美元"去猜有折算列）
    assert rs._expected_columns(["綜合財務狀況表", "二零二五年 二零二四年", "附註 人民幣百萬元 人民幣百萬元"],
                                [2025, 2024], {3: 30, 2: 1}, {3: ["17", "18"], 2: ["10,000"]}) == 2
    assert rs._expected_columns(["合併資產負債表", "2024年 2025年", "附註 人民幣 人民幣 美元"], [2024, 2025],
                                {4: 24, 3: 26, 1: 3}, {4: ["4", "7"], 3: ["108,350", "7,619"]}) == 3
    assert rs._expected_columns(["綜合財務狀況表", "二零二五年 二零二四年", "附註 美元千元 美元千元"],
                                [2025, 2024], {3: 5, 2: 1}, {3: ["10", "11", "12", "13", "14"], 2: ["10,000"]}) == 2
    assert rs._expected_columns(["綜合損益表", "Notes RMB’000 RMB’000"], [2025, 2024], {3: 8, 2: 3}, {}) == 2
    #    币种与单位分开排版：一个单位 token = 一列，紧邻币种不另计（评审复现）
    assert rs._expected_columns(["綜合財務狀況表", "二零二五年 二零二四年", "附註 美元 千元 美元 千元"],
                                [2025, 2024], {3: 5, 2: 1}, {3: ["10", "11"], 2: ["10,000"]}) == 2
    assert rs._expected_columns(["Consolidated Balance Sheet", "2025 2024", "Notes RMB million RMB million"],
                                [2025, 2024], {3: 5, 2: 1}, {3: ["10", "11"], 2: ["10,000"]}) == 2
    assert rs._unit_token_columns(["附註 美元 千元 美元 千元"]) == 2
    assert rs._unit_token_columns(["附註 人民幣 人民幣 美元"]) == 3
    assert rs._unit_token_columns(["（以百萬元計，股份及每股數據除外）"]) == 0
    #    表头布局与数据行不吻合（说明分组没确认）→ 退回年份/数据行约束
    assert rs._expected_columns(["綜合收益表", "二零二五年 二零二四年", "附註 人民幣 千元 港元 千元 美元 千元 x"],
                                [2025, 2024], {3: 30, 2: 10}, {3: ["17"], 2: ["950"]}) == 2
    # 2) 无单位行：年份数 vs 行 token 最小常见值
    no_units = ["綜合財務狀況表", "二零二五年 二零二四年"]
    assert rs._expected_columns(no_units, [2025, 2024], {3: 30, 2: 10, 1: 2}, {3: ["17", "18"], 2: ["950"]}) == 2
    assert rs._expected_columns(no_units, [2025, 2024], {3: 30}, {3: ["17", "18", "19"]}) == 2  # 全行带附注
    assert rs._expected_columns(no_units, [2025, 2024], {3: 26, 4: 24}, {3: ["108,350"], 4: ["4"]}) == 3  # 真多一列
    # 3) 无年份无单位：最小常见值
    assert rs._expected_columns(["合并利润表", "项目 附注 本期 上期"], [], {2: 30, 3: 4}, {}) == 2


@pytest.mark.parametrize("unit_line", [
    "附註 美元千元 美元千元",  # 币种+单位粘在一起
    "附註 美元 千元 美元 千元",  # 币种与单位分开排版（评审复现）
    "Notes US$ thousand US$ thousand",
])
def test_usd_denominated_two_column_table_with_single_unlabeled_total(unit_line):
    """评审 P1：整表以美元计价 ≠ 多一列美元折算，且同一列的币种+单位无论是否分开排版都只算
    一列。五条附注行 + 仅一条无附注合计，列数必须判 2、附注全部剥离，现金本期取 1,000 而
    不是附注号 10。"""
    pages = [
        "綜合財務狀況表\n於二零二五年十二月三十一日\n二零二五年 二零二四年\n" + unit_line + "\n"
        "現金 10 1,000 900\n應收賬款 11 2,000 1,800\n存貨 12 3,000 2,700\n物業 13 4,000 3,000\n"
        "無形資產 14 500 400\n資產總額 10,000 7,900\n"
    ]
    parsed = rs.locate_statements(pages, report_type="annual")["balance"]
    assert parsed.column_count == 2 and parsed.currency == "USD" and parsed.unit_multiplier == 1_000
    assert [(r.label, r.note, r.values) for r in parsed.rows][:2] == [
        ("現金", "10", [Decimal("1000"), Decimal("900")]),
        ("應收賬款", "11", [Decimal("2000"), Decimal("1800")]),
    ]
    assert all(len(r.values) == 2 for r in parsed.rows)
    cols = rs.period_columns(parsed, report_type="annual", end_date="20251231")
    cash = next(r for r in parsed.rows if r.label == "現金")
    assert rs.resolve_value(parsed, [cash.row_id], cols[0].column, scale=True) == Decimal("1000000")
    assert rs.resolve_value(parsed, [cash.row_id], cols[1].column, scale=True) == Decimal("900000")


def test_english_rmb_million_header_with_separated_unit_words():
    """「Notes RMB million RMB million」= 两列人民币百万元：五条附注行 + 一条合计到 resolve_value。"""
    pages = [
        "Consolidated Statement of Financial Position\n綜合財務狀況表\n2025 2024\n"
        "Notes RMB million RMB million\n"
        "Cash 10 1,000 900\nReceivables 11 2,000 1,800\nInventories 12 3,000 2,700\n"
        "Property 13 4,000 3,000\nIntangibles 14 500 400\nTotal assets 10,000 7,900\n"
    ]
    parsed = rs.locate_statements(pages, report_type="annual")["balance"]
    assert parsed.column_count == 2 and parsed.unit_multiplier == 1_000_000 and parsed.currency == "CNY"
    assert all(len(r.values) == 2 for r in parsed.rows)
    cash = next(r for r in parsed.rows if r.label == "Cash")
    assert (cash.note, cash.values) == ("10", [Decimal("1000"), Decimal("900")])
    cols = rs.period_columns(parsed, report_type="annual", end_date="20251231")
    assert rs.resolve_value(parsed, [cash.row_id], cols[0].column, scale=True) == Decimal("1000000000")
    assert rs.resolve_value(parsed, [cash.row_id], cols[1].column, scale=True) == Decimal("900000000")


@pytest.mark.parametrize("line", ["資產", "非流動資產", "附註 人民幣百萬元 人民幣百萬元", "6", "下列人士應佔："])
def test_parse_row_rejects_non_numeric_lines(line):
    assert rs.parse_row(line) is None


@pytest.mark.parametrize("line, kind", [
    ("綜合收益表", "income"), ("簡明綜合全面收益表", "income"), ("綜合損益及其他全面收益表", "income"),
    ("1、合并资产负债表", "balance"), ("綜合財務狀況表（續）", "balance"), ("合併現金流量表", "cashflow"),
    ("合併經營狀況及綜合收益表（續）", "income"), ("（三）合并现金流量表", "cashflow"),
])
def test_title_line_matches(line, kind):
    assert rs.match_title(line)[0] == kind


@pytest.mark.parametrize("line", [
    "43 綜合現金流量表附註", "(a) 於綜合財務狀況表確認的金額", "母公司资产负债表", "綜合權益變動表",
    "載於第140頁至第272頁的附註乃此等綜合財務報表的組成部分。",
])
def test_title_line_rejects_notes_and_other_statements(line):
    assert rs.match_title(line) is None


def test_prior_fiscal_year_end_for_interim_balance():
    assert rs._prior_fiscal_year_end("20260630") == "20251231"
    assert rs._prior_fiscal_year_end("20250930") == "20250331"  # 3 月财年
    assert rs._prior_fiscal_year_end("20251231") == "20250630"  # 6 月财年
    assert rs._prior_fiscal_year_end("20250331") == "20240930"  # 9 月财年


def test_resolve_value_sums_rows_and_scales_units():
    parsed = rs.ParsedStatement(
        kind="income", page_start=1, page_end=1, title="t", header=[], unit_multiplier=1_000_000,
        currency="CNY", years=[2025, 2024], column_count=2, interim_four_columns=False,
        rows=[
            rs.StatementRow("r1", "銷售開支", "", [Decimal("-10"), Decimal("-8")]),
            rs.StatementRow("r2", "行政開支", "", [Decimal("-5"), None]),
            rs.StatementRow("r3", "每股盈利", "", [Decimal("2.5"), Decimal("2.1")]),
        ],
    )
    assert rs.resolve_value(parsed, ["r1", "r2"], 0, scale=True) == Decimal("-15000000")
    assert rs.resolve_value(parsed, ["r1", "r2"], 1, scale=True) == Decimal("-8000000")  # None 分项跳过
    assert rs.resolve_value(parsed, ["r2"], 1, scale=True) is None
    assert rs.resolve_value(parsed, ["r3"], 0, scale=False) == Decimal("2.5")
    assert rs.resolve_value(parsed, ["r999"], 0, scale=True) is None
    # 序列化往返
    again = rs.ParsedStatement.from_payload(parsed.to_payload())
    assert again.rows[1].values == [Decimal("-5"), None] and again.unit_multiplier == 1_000_000


def test_locate_returns_none_when_no_statement_present():
    found = rs.locate_statements(["公司簡介\n業務回顧", "董事會報告\n收入 1,000 900"], report_type="annual")
    assert found == {"income": None, "balance": None, "cashflow": None}
