"""三张报表定位/解析纯函数：真实港股年报/中报文本金样（\\x0c 分页，与 report_sections 同源）。

固件覆盖的版式：00700 2025（标准 IFRS，每页重复标题，五年財務概要页同名标题，附註里
「43 綜合現金流量表附註」）、00700 2014（老版式，附註节含「42 綜合現金流量表」整行小节
标题）、00700 2026 中报（簡明报表，损益四列 = 三个月+六个月）、01995 2018（「綜合損益及全面
收益表」变体，千元）、02156 2025（中英双语）、09926 2025（附注列在左）、09618 2025（美国
准则口径：年份升序 + 美元折算列，「合併經營狀況及綜合收益表」）。

第一轮生产回填（2026-09）失败的版式，裁成只保留报表附近页的金样（其余页置空保住页码）：
00728 2025（标题字间空格「合 併 綜 合 收 益 表」、损益表排在財務狀況表之后）、00883 2025 年报
（「合併損益及其他綜合收益表」、財務摘要页的「（已經審計）」同名表）与 2025 中报（「（未經審計）」
及错位的「（未經審計（）續）」）、01133 2025（中国准则「合併利潤表」+「母公司利潤表」终止、
单位元）、09618 2023（亏损年份「…綜合收益╱（損失）表」）、02669 2026 中报（目录页带页码的
「28 簡明綜合損益表」）、02313 2025 中报（「中期簡明綜合損益表」+ 独立全面收益表）、02156 2025
中报（标题当页眉印在业绩公告首页）、01023 2026 中报（6 月财年：期末实为 2025-12-31）。
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
    # 第一轮生产失败后补的版式
    "hk_00728_20251231": ((150, 152), (148, 150), (153, 155), 1_000_000, "CNY", [2025, 2024]),
    "hk_00883_20251231": ((80, 81), (82, 83), (85, 85), 1_000_000, "CNY", [2025, 2024]),
    "hk_01133_20251231": ((85, 87), (75, 79), (90, 92), 1, "CNY", [2025, 2024]),
    "hk_09618_20231231": ((267, 268), (264, 266), (269, 272), 1_000_000, "CNY", None),
    "hk_02313_20251231": ((40, 41), (42, 43), (45, 47), 1_000, "CNY", [2025, 2024]),
    # 第二轮生产回填后补的版式
    "hk_01133_20231231": ((73, 75), (63, 67), (78, 80), 1, "CNY", []),
    "hk_01995_20191231": ((83, 83), (84, 85), (88, 89), 1_000, "CNY", [2019, 2018]),
    "hk_01023_20230630": ((62, 64), (64, 65), (67, 69), 1_000, "HKD", [2023, 2022]),
    "hk_09618_20201231": ((254, 256), (251, 253), (257, 260), 1_000, "CNY", None),
    # 第三轮（全量重抽后）补的版式：利潤表/現金流量表列标题「本期發生額 上期發生額」
    "hk_01133_20171231": ((73, 75), (67, 69), (79, 81), 1, "CNY", []),
}
# 行在主导列数上的最低占比。中国准则报表把零值格留空（01133 2017 現金流量表 39 行里 6 行只有
# 一期有数，33/39 = 84.6%）——纯文本里分不出空的是哪一列，单值行按本期列取；跨年比较列核对
# （report_statement_checks）会抓住因此错期的关键科目。只对这份金样放宽，不降全局门槛
ON_GRID_MIN = {"hk_01133_20171231": 0.8}

INTERIM = {
    # name: (income_pages, balance_pages, cashflow_pages, unit, currency, years)
    "hk_00883_20250630_interim": ((39, 40), (41, 42), (44, 44), 1_000_000, "CNY", [2025, 2024]),
    "hk_02669_20260630_interim": ((29, 31), (31, 33), (35, 41), 1_000, "CNY", [2026, 2025]),
    "hk_02313_20250630_interim": ((15, 16), (17, 18), (20, 22), 1_000, "CNY", [2025, 2024]),
    "hk_02156_20250630_interim": ((6, 7), (8, 9), (12, 12), 1_000, "CNY", [2025, 2024]),
    "hk_01023_20260630_interim": ((27, 30), (30, 32), (34, 36), 1_000, "HKD", None),
    "hk_03900_20190630_interim": ((35, 35), (36, 37), (40, 42), 1_000, "CNY", [2019, 2018]),
    # 第三轮：目录页的「目錄」在页眉两行之后（第 2 页不得被认成损益表）；未有收入的 18A 公司
    "hk_01023_20221231_interim": ((27, 30), (30, 32), (34, 36), 1_000, "HKD", None),
    "hk_09926_20200630_interim": ((46, 47), (48, 49), (52, 53), 1_000, "CNY", [2020, 2019]),
}


@pytest.mark.parametrize("name", sorted(INTERIM))
def test_interim_reports_locate_all_three_statements(name):
    income, balance, cashflow, unit, currency, years = INTERIM[name]
    found = rs.locate_statements(_pages(name), report_type="interim")
    for kind, expected in (("income", income), ("balance", balance), ("cashflow", cashflow)):
        parsed = found[kind]
        assert parsed is not None, f"{name} 未定位 {kind}"
        assert (parsed.page_start, parsed.page_end) == expected, (name, kind, parsed.page_start, parsed.page_end)
        assert parsed.unit_multiplier == unit
        assert parsed.currency == currency
        assert parsed.column_count == 2
        if years is not None:
            assert parsed.years == years


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
        assert on_grid >= len(parsed.rows) * ON_GRID_MIN.get(name, 0.85), (name, kind)


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


# ---------------------------------------------------------------------------
# 第一轮生产回填暴露的版式（每条对应一家持仓公司的真实报告）
# ---------------------------------------------------------------------------


def test_letter_spaced_titles_are_squashed_but_column_unit_words_are_kept():
    assert rs.squash_spaced_cjk("合 併 綜 合 收 益 表") == "合併綜合收益表"
    assert rs.squash_spaced_cjk("合 併 財 務 狀 況 表（ 續 ）") == "合併財務狀況表（續）"
    # 表头的列单位由空格分隔，两字词之间的单个空格不是字间空格
    assert rs.squash_spaced_cjk("附註 人民幣千元 人民幣千元") == "附註 人民幣千元 人民幣千元"
    assert rs.squash_spaced_cjk("美元 千元 美元 千元") == "美元 千元 美元 千元"
    assert rs.squash_spaced_cjk("經營收入 27 529,559 529,417") == "經營收入 27 529,559 529,417"


def test_china_telecom_income_statement_follows_balance_sheet():
    """00728：三张表标题全是字间空格版，且損益表排在財務狀況表之后——修复前財務狀況表把
    后面两页损益表整个吞成自己的续页（103 行），损益表判定位失败。"""
    found = rs.locate_statements(_pages("hk_00728_20251231"), report_type="annual")
    assert found["income"].title == "合併綜合收益表"
    assert (found["balance"].page_start, found["balance"].page_end) == (148, 150)
    assert len(found["balance"].rows) < 60
    revenue = next(r for r in found["income"].rows if r.label == "經營收入")
    assert revenue.note == "27" and revenue.values == [Decimal("529559"), Decimal("529417")]


def test_cnooc_summary_table_with_audited_suffix_is_not_the_statement():
    """00883 財務摘要页有「合併損益及其他綜合收益表（已經審計）」，正表在 80 页。"""
    found = rs.locate_statements(_pages("hk_00883_20251231"), report_type="annual")
    assert found["income"].page_start == 80
    assert rs.match_title("合併損益及其他綜合收益表（已經審計）") is None
    assert rs.match_title("中期簡明合併損益及其他綜合收益表（未經審計（）續）")[0] == "income"


def test_cas_format_h_share_uses_profit_table_and_parent_terminator():
    """01133 按中国准则：「合併利潤表」三页，「母公司利潤表」终止；金额单位为元。"""
    found = rs.locate_statements(_pages("hk_01133_20251231"), report_type="annual")
    income = found["income"]
    assert income.title == "合併利潤表" and income.page_end == 87  # 88 页起是母公司利潤表
    revenue = next(r for r in income.rows if r.label.startswith("一.") or "營業總收入" in r.label)
    assert revenue.values == [Decimal("46068852537.30"), Decimal("38721429041.12")]
    assert income.unit_multiplier == 1


def test_jd_2023_loss_year_title_variant():
    found = rs.locate_statements(_pages("hk_09618_20231231"), report_type="annual")
    assert found["income"].title == "合併經營狀況及綜合收益╱（損失）表"
    assert found["income"].years == [2021, 2022, 2023] and found["income"].column_count == 4
    cols = rs.period_columns(found["income"], report_type="annual", end_date="20231231")
    assert [(c.column, c.end_date) for c in cols] == [(2, "20231231"), (1, "20221231")]


def test_table_of_contents_page_is_not_a_statement():
    """02669 目录页「28 簡明綜合損益表」与编号标题同形：只能按页首「目錄」排除。"""
    pages = _pages("hk_02669_20260630_interim")
    assert any("目錄" in line for line in pages[1].splitlines()[:2])
    found = rs.locate_statements(pages, report_type="interim")
    assert found["income"].page_start == 29
    assert found["income"].years == [2026, 2025]


def test_statement_printed_on_announcement_page_starts_after_the_board_paragraph():
    """02156 中报：损益表印在业绩公告首页——标题下先是董事会声明段落，年份/单位行被推到第
    13 行之后，再往下才是「收益 Revenue 4 1,822,878 1,602,395」。表头取到第一条金额行为止，
    首页作为报表首块被接受，与下一页页首重复标题的续页合并。"""
    found = rs.locate_statements(_pages("hk_02156_20250630_interim"), report_type="interim")
    income = found["income"]
    assert (income.page_start, income.page_end) == (6, 7)
    assert income.years == [2025, 2024] and income.unit_multiplier == 1_000
    assert "Revenue" in income.rows[0].label and income.rows[0].values == [Decimal("1822878"), Decimal("1602395")]
    eps = next(r for r in income.rows if "Basic" in r.label)
    assert eps.values == [Decimal("0.16"), Decimal("0.14")]


def test_interim_prefix_and_separate_comprehensive_income_statement():
    """02313：「中期簡明綜合損益表」+ 下一页独立的「中期簡明綜合全面收益表」并入同一 income 块。"""
    found = rs.locate_statements(_pages("hk_02313_20250630_interim"), report_type="interim")
    income = found["income"]
    assert income.title == "中期簡明綜合損益表" and (income.page_start, income.page_end) == (15, 16)
    assert found["balance"].title == "中期簡明綜合財務狀況表"


@pytest.mark.parametrize("line, kind", [
    ("中期簡明綜合損益表", "income"), ("未經審核簡明綜合財務狀況表", "balance"),
    ("中期簡明合併現金流量表（未經審計）", "cashflow"), ("合併損益及其他綜合收益表", "income"),
    ("合併綜合收益表", "income"), ("合併利潤表", "income"), ("合併經營狀況及綜合收益╱（損失）表", "income"),
    ("簡明綜合中期財務狀況表", "balance"), ("綜合損益表（未經審核）（續）", "income"),
])
def test_title_variants_from_first_production_round(line, kind):
    assert rs.match_title(line)[0] == kind


@pytest.mark.parametrize("line", [
    "合併損益及其他綜合收益表（已經審計）", "母公司利潤表", "中期簡明綜合權益變動表",
    "28 簡明綜合損益表 96 Condensed Consolidated",
])
def test_title_variants_still_rejected(line):
    assert rs.match_title(line) is None
    assert line != "母公司利潤表" or rs._is_terminator(line)


def test_detect_period_end_from_statement_header():
    found = rs.locate_statements(_pages("hk_01023_20260630_interim"), report_type="interim")
    # 6 月财年：「截至二零二五年十二月三十一日止六個月」/「於二零二五年十二月三十一日」
    assert rs.detect_period_end(found["income"]) == "20251231"
    assert rs.detect_period_end(found["balance"]) == "20251231"
    cols = rs.period_columns(found["balance"], report_type="interim", end_date="20251231")
    assert [(c.end_date, c.fp, c.is_primary) for c in cols] == [("20251231", "H1", True), ("20250630", "FY", False)]
    # 阿拉伯数字 / 整行日期 / 英文
    make = lambda header: rs.ParsedStatement(  # noqa: E731
        kind="income", page_start=1, page_end=1, title="t", header=header, unit_multiplier=1,
        currency=None, years=[], column_count=2, interim_four_columns=False, rows=[],
    )
    assert rs.detect_period_end(make(["截至2026年3月31日止年度"])) == "20260331"
    assert rs.detect_period_end(make(["二零二五年十二月三十一日"])) == "20251231"
    assert rs.detect_period_end(make(["FOR THE YEAR ENDED 31 MARCH 2026"])) == "20260331"
    assert rs.detect_period_end(make(["截至12月31日止年度", "2025年 2024年"])) is None



def test_shenzhou_rows_are_labeled_after_baseline_clustering():
    """02313：金样由按基线聚行的抽取生成，科目名回到数字所在行——此前 pdfplumber 默认按
    字形框顶边聚行时三张表 115 行全部无标签（见 report_statement_service.baseline_text）。"""
    found = rs.locate_statements(_pages("hk_02313_20251231"), report_type="annual")
    for kind in ("income", "balance", "cashflow"):
        rows = found[kind].rows
        unlabeled = [r for r in rows if not r.label]
        assert len(unlabeled) <= 1, (kind, [(r.note, r.values) for r in unlabeled])
    revenue = next(r for r in found["income"].rows if r.label == "收入")
    assert revenue.note == "5" and revenue.values == [Decimal("30993732"), Decimal("28662938")]
    cfo = next(r for r in found["cashflow"].rows if r.label == "經營業務所得現金流量淨額")
    assert cfo.values == [Decimal("5549401"), Decimal("5272964")]
    interim = rs.locate_statements(_pages("hk_02313_20250630_interim"), report_type="interim")
    assert all(r.label for r in interim["income"].rows)


def test_page_numbers_at_page_edges_are_not_rows():
    """页眉「120」（09926 按基线聚行后页码与公司名分行）与页脚「2025 39」（02313）不是数据行；
    正文里的单值行（每股股息）照常保留。"""
    pages = [
        "綜合損益表\n截至2025年12月31日止年度\n2025年 2024年\n人民幣千元 人民幣千元\n"
        "收入 100 90\n毛利 50 40\n經營盈利 30 20\n除稅前盈利 28 18\n年度盈利 20 15\n每股股息 0.5\n2025 39",
        "120\n綜合損益表\n2025年 2024年\n其他全面收益 5 4\n全面收益總額 25 19\n120 康方生物科技 | 2025年年度報告",
    ]
    found = rs.locate_statements(pages, report_type="annual")
    labels = [(r.label, [str(v) for v in r.values]) for r in found["income"].rows]
    assert ("每股股息", ["0.5"]) in labels
    assert all(vals not in (["2025", "39"], ["120"]) for _, vals in labels), labels
    assert rs._is_page_number_row("", "", [Decimal("2025"), Decimal("39")])
    assert rs._is_page_number_row("", "", [Decimal("120")])
    assert not rs._is_page_number_row("", "", [Decimal("0.5")])
    assert not rs._is_page_number_row("", "6", [Decimal("120")])


_SHORT_HEAD_PAGE = (
    "綜合收益表\n截至2025年12月31日止年度\n2025年 2024年\n人民幣千元 人民幣千元\n"
    "收入 1,000 900\n銷售成本 (600) (550)\n毛利 400 350\n其他收入 20 10\n經營盈利 420 360"
)
_CONTINUATION_PAGE = (
    "綜合收益表（續）\n截至2025年12月31日止年度\n2025年 2024年\n人民幣千元 人民幣千元\n"
    "財務成本 (20) (15)\n除稅前盈利 400 345\n所得稅 (80) (70)\n年度盈利 320 275\n"
    "每股盈利－基本 0.32 0.28\n每股盈利－攤薄 0.31 0.27"
)


def test_short_first_page_is_kept_when_continuation_page_completes_the_statement():
    """PR #202 评审：首页只有 5 个金额行、其余在「（續）」页——总行数门槛要在组装完连续块
    之后再判，否则首页（含收入）整个丢失。"""
    found = rs.locate_statements([_SHORT_HEAD_PAGE, _CONTINUATION_PAGE], report_type="annual")
    income = found["income"]
    assert (income.page_start, income.page_end) == (1, 2)
    assert [r.label for r in income.rows][:3] == ["收入", "銷售成本", "毛利"]
    assert len(income.rows) == 11 and income.column_count == 2
    assert [r.row_id for r in income.rows] == [f"r{i}" for i in range(1, 12)]
    assert income.years == [2025, 2024] and income.unit_multiplier == 1_000


def test_two_short_pages_together_reach_the_row_threshold():
    head = _SHORT_HEAD_PAGE.rsplit("\n", 2)[0]  # 3 行
    tail = "\n".join(_CONTINUATION_PAGE.splitlines()[:8])  # 4 行
    found = rs.locate_statements([head, tail], report_type="annual")
    assert found["income"] is not None and len(found["income"].rows) == 7
    assert found["income"].rows[0].label == "收入"
    # 单独一页 3 行、没有续页：仍不够门槛
    assert rs.locate_statements([head, "附註\n1. 一般資料"], report_type="annual")["income"] is None


def test_short_head_only_merges_with_the_adjacent_next_page():
    """中间隔着附註页：短首块不再与两页之外的同类块拼接，后者单独够 6 行则单独成表。"""
    notes = "綜合財務報表附註\n1. 一般資料\n本公司於開曼群島註冊成立。"
    found = rs.locate_statements([_SHORT_HEAD_PAGE, notes, _CONTINUATION_PAGE], report_type="annual")
    assert found["income"] is not None and (found["income"].page_start, found["income"].page_end) == (3, 3)
    assert found["income"].rows[0].label == "財務成本"


def test_continuation_page_without_repeated_header_inherits_head_metadata():
    """PR #202 评审：「綜合收益表（續）」下面直接是金额行、不重复日期/年份/单位——续页不得
    因自身没有表头证据被拒（那会连带丢掉挂起的短首块），年份/单位/币种从首块继承。"""
    bare_continuation = "\n".join(
        line for line in _CONTINUATION_PAGE.splitlines()
        if line not in ("截至2025年12月31日止年度", "2025年 2024年", "人民幣千元 人民幣千元")
    )
    found = rs.locate_statements([_SHORT_HEAD_PAGE, bare_continuation], report_type="annual")
    income = found["income"]
    assert income is not None and (income.page_start, income.page_end) == (1, 2)
    assert len(income.rows) == 11 and income.rows[0].label == "收入"
    assert income.years == [2025, 2024] and income.unit_multiplier == 1_000 and income.currency == "CNY"


def test_years_split_across_two_header_lines_is_still_a_statement():
    """PR #202 评审：列年份（同一行 ≥2 个年份）为空 ≠ 表头没有年份。「2025年」「2024年」被抽成
    两行的合法报表要接受，数值列退回按位置对应。"""
    page = _SHORT_HEAD_PAGE.replace("2025年 2024年", "2025年\n2024年") + "\n年度盈利 320 275"
    found = rs.locate_statements([page], report_type="annual")
    income = found["income"]
    assert income is not None and len(income.rows) == 6 and income.years == []
    cols = rs.period_columns(income, report_type="annual", end_date="20251231")
    assert [(c.column, c.end_date) for c in cols] == [(0, "20251231"), (1, "20241231")]
    # 只有年份、没有列年份行也没有单位/币种：仍是要排除的公告页（02156 首页的形态）
    bare = "綜合收益表\n截至2025年12月31日止年度\n董事會欣然宣佈\n" + "\n".join(
        f"項目{i} {i * 100} {i * 90}" for i in range(1, 8)
    )
    assert rs.locate_statements([bare], report_type="annual")["income"] is None


# ---------------------------------------------------------------------------
# 第二轮生产回填暴露的版式
# ---------------------------------------------------------------------------


def test_cas_headers_without_years_use_period_captions_as_evidence():
    """01133 2023 按中国准则：表头没有年份，只有「本期金額 上期金額」「期末餘額 期初餘額」，
    列年份为空、按位置对应（本期在前）；母公司「利潤表」「資產負債表」不被当成合并报表。"""
    found = rs.locate_statements(_pages("hk_01133_20231231"), report_type="annual")
    income = found["income"]
    assert income.years == [] and income.title == "合併利潤表"
    assert income.rows[0].values == [Decimal("29250349896.53"), Decimal("24984261415.23")]
    cols = rs.period_columns(income, report_type="annual", end_date="20231231")
    assert [(c.column, c.end_date) for c in cols] == [(0, "20231231"), (1, "20221231")]
    # 紧随其后的母公司报表只叫「資產負債表」「利潤表」「現金流量表」：裸标题终止合并报表块
    assert (found["balance"].page_start, found["balance"].page_end) == (63, 67)
    assert (income.page_start, income.page_end) == (73, 75)
    assert (found["cashflow"].page_start, found["cashflow"].page_end) == (78, 80)
    assert rs._is_terminator("資產負債表") and rs._is_terminator("利潤表（續）") and rs._is_terminator("現金流量表 81")


def test_title_split_across_two_lines_is_rejoined():
    """03900 2019 中报：「簡明綜合」独占一行、下一行「損益及其他全面收益表」；權益變動表同样拆行，
    拼回后才能作为终止标题，否则財務狀況表块会一路吞到十几列的權益變動表。"""
    found = rs.locate_statements(_pages("hk_03900_20190630_interim"), report_type="interim")
    assert found["income"].title == "簡明綜合損益及其他全面收益表"
    assert found["balance"].column_count == 2 and (found["balance"].page_start, found["balance"].page_end) == (36, 37)
    stream = rs._line_stream(["簡明綜合\n權益變動表\n截至2019年6月30日止六個月"])
    assert [line.text for line in stream][:2] == ["簡明綜合權益變動表", "截至2019年6月30日止六個月"]
    assert rs._is_terminator("簡明綜合權益變動表")


@pytest.mark.parametrize("line, kind", [
    ("綜合全面收益表 82", "income"), ("83 綜合財務狀況表", "balance"), ("綜合現金流量表 108", "cashflow"),
    ("101 綜合損益表（續） 102", "income"),
])
def test_title_line_tolerates_page_numbers(line, kind):
    assert rs.match_title(line)[0] == kind


def test_title_line_rejects_amount_rows_that_end_with_numbers():
    assert rs.match_title("綜合收益表 1,234") is None
    assert rs.match_title("綜合收益表 2025 2024") is None


def test_unit_tokens_with_currency_after_magnitude_count_as_columns():
    """09618 2020：「附註 人民幣千元 人民幣千元 千美元」= 三个金额列（美元折算列写成「千美元」）。"""
    assert rs._unit_token_columns(["附註 人民幣千元 人民幣千元 千美元"]) == 3
    assert rs._unit_token_columns(["附註 人民幣千元 人民幣千元 百萬美元"]) == 3
    found = rs.locate_statements(_pages("hk_01133_20231231"), report_type="annual")
    # 页脚「2023年度報告 79」不是数据行
    assert all(not r.label.endswith("年度報告") for r in found["cashflow"].rows)


def test_jd_2020_usd_column_written_as_qian_meiyuan_keeps_three_columns():
    found = rs.locate_statements(_pages("hk_09618_20201231"), report_type="annual")
    balance = found["balance"]
    assert balance.column_count == 3 and balance.years == [2019, 2020]
    total = next(r for r in balance.rows if r.label == "資產總額")
    assert total.values == [Decimal("259723704"), Decimal("422287794"), Decimal("64718436")]
    cols = rs.period_columns(balance, report_type="annual", end_date="20201231")
    assert [(c.column, c.end_date) for c in cols] == [(1, "20201231"), (0, "20191231")]


def test_overdrawn_running_header_page_locates_after_dedupe():
    """01023 2023：金样文本由 baseline_text 去掉被盖住的模板页眉后生成，三张表全部定位。"""
    found = rs.locate_statements(_pages("hk_01023_20230630"), report_type="annual")
    assert found["income"].title == "綜合損益表" and found["income"].currency == "HKD"


# ---------------------------------------------------------------------------
# 被空格拆开的数字：只在有证据时粘（PR #207 评审 P1）
# ---------------------------------------------------------------------------
def test_malformed_thousands_group_is_glued_with_or_without_column_count():
    from app.services.report_statements import parse_row

    # 09926 2020：「854,84」不是合法数值，行内证据充分，不知道列数也粘
    assert parse_row("非流動資產總值 854,84 3 416,97 5")[2] == [Decimal("854843"), Decimal("416975")]
    assert parse_row("非流動資產總值 854,84 3 416,97 5", expected_columns=2)[2] == [
        Decimal("854843"), Decimal("416975"),
    ]


def test_legal_two_column_decimals_are_never_glued():
    from app.services.report_statements import parse_row

    # 千元报表里合法的两列：一位小数 + 单个整数。列数吻合/未知都不能改写
    assert parse_row("現金 1,234.5 6", expected_columns=2)[2] == [Decimal("1234.5"), Decimal("6")]
    assert parse_row("現金 1,234.5 6")[2] == [Decimal("1234.5"), Decimal("6")]
    assert parse_row("其他 157.0 6.4", expected_columns=2)[2] == [Decimal("157.0"), Decimal("6.4")]


def test_decimal_tail_is_glued_only_when_token_count_exceeds_known_columns():
    from app.services.report_statements import parse_row

    # 01133 形态：两列表里出现三个 token 且尾数被拆 → 粘回恰好两列
    assert parse_row("營業收入 1,648,565,774.6 1 1,500,000,000.00", expected_columns=2)[2] == [
        Decimal("1648565774.61"), Decimal("1500000000.00"),
    ]
    # 多出的 token 是附注号：粘完落到 列数+1，再由附注剥离
    label, note, values = parse_row("營業收入 5 1,648,565,774.6 1 1,500,000,000.00", expected_columns=2)
    assert note == "5" and values == [Decimal("1648565774.61"), Decimal("1500000000.00")]
    # 粘完仍对不上列数 → 原样不动（宁可多列也不错列）
    assert parse_row("x 1,234.5 6 7 8", expected_columns=2)[2] == [
        Decimal("1234.5"), Decimal("6"), Decimal("7"), Decimal("8"),
    ]


def test_note_column_plus_exact_columns_is_never_glued():
    """评审 P1：附注号 + 恰好列数的合法金额（末列单个数字）——多出的 token 是附注号，
    不是断字证据。粘了会把附注顶成本期、上期粘进本期。"""
    from app.services.report_statements import parse_row

    assert parse_row("收入 12 1,234.5 6", expected_columns=2) == ("收入", "12", [Decimal("1234.5"), Decimal("6")])
    assert parse_row("收入 12(a) 1,234.5 6", expected_columns=2) == (
        "收入", "12(a)", [Decimal("1234.5"), Decimal("6")],
    )
    # 无标签合计行同样：首 token 是附注号形态
    assert parse_row("12 1,234.5 6", expected_columns=2) == ("", "12", [Decimal("1234.5"), Decimal("6")])
    # 附注号 + 真断字（四个 token）：先粘再剥附注
    assert parse_row("收入 12 1,648,565,774.6 1 2,000.00", expected_columns=2) == (
        "收入", "12", [Decimal("1648565774.61"), Decimal("2000.00")],
    )
    # 标签里已带附注号（「所得稅開支 12(a) …」形态由 _parse_row_tokens 剥出）→ 列数吻合不动
    assert parse_row("所得稅開支 12(a) (47,448) (45,018)", expected_columns=2)[2] == [
        Decimal("-47448"), Decimal("-45018"),
    ]


def test_cas_occurred_amount_captions_are_period_evidence():
    """01133 2017-2021：CAS 利潤表/現金流量表列标题写「本期發生額 上期發生額」，此前不算期间证据，
    损益表整份定位失败（5 份年报封顶）。"""
    assert rs._CAS_PERIOD_CAPTION_RE.search("項目 附註 本期發生額 上期發生額")
    assert rs._CAS_PERIOD_CAPTION_RE.search("項目 本年發生額 上年發生額")
    found = rs.locate_statements(_pages("hk_01133_20171231"), report_type="annual")
    assert found["income"].title == "合併利潤表"
    assert found["cashflow"].title == "合併現金流量表"


def test_toc_page_with_masthead_lines_is_not_a_statement():
    """01023 2023 中报第 2 页：页眉两行之后第三行才是「目錄」，目录行「中期簡明綜合全面收益表 28」
    与带行尾页码的标题同形——此前被认成损益表（表头年份 [2023, 2023]，与报告期不符而封顶）。"""
    pages = _pages("hk_01023_20221231_interim")
    assert "目錄" in pages[1].splitlines()[2]
    found = rs.locate_statements(pages, report_type="interim")
    assert found["income"].page_start == 27 and found["income"].title == "中期簡明綜合損益表"
