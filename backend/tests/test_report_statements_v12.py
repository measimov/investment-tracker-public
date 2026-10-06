"""港股报表抽取器 v12 / 构建 v4（#339 #341 #342 #343）的回归。

复现样本一律走生产入口（`locate_statements` → `_parse_block`、`build_period_rows`、
`merge_comparative_row`）：#341 的教训是 `glue_decimal_tail` 只在测试调用的 `parse_row` 里生效，
生产解析从未经过它。
"""

import gzip
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.services import report_statement_build as build
from app.services import report_statement_checks as checks
from app.services import report_statement_prompts as prompts
from app.services import report_statement_service as svc
from app.services import report_statements as rs
from app.services.report_statements import ParsedStatement, StatementRow

REPORTS = Path(__file__).parent / "fixtures" / "reports"
EXTRACTS = REPORTS / "statement_extracts"


def _pages(name):
    with gzip.open(REPORTS / f"{name}.pages.txt.gz", "rt", encoding="utf-8") as handle:
        return handle.read().split("\x0c")


def _rows_by_label(parsed, label):
    return [row for row in parsed.rows if row.label == label]


def _vals(row):
    return [str(v) if v is not None else None for v in row.values]


# ---------------------------------------------------------------------------
# #339 全角破折號「－」是空值记号
# ---------------------------------------------------------------------------


def test_fullwidth_dash_is_a_null_cell_in_03900_2019_interim_balance():
    """同一页混用「–」与「－」：修复前 2019 H1 预付租赁款项 = 896,967（实为 2018 年末数），
    行尾是「－」的三行整行丢失。"""
    found = rs.locate_statements(_pages("hk_03900_20190630_interim"), report_type="interim")
    balance = found["balance"]
    assert balance.column_count == 2
    (prepaid,) = _rows_by_label(balance, "預付租賃款項")
    assert _vals(prepaid) == [None, "896967"]
    (deposit,) = _rows_by_label(balance, "收購一家聯營公司的訂金")
    assert deposit.note == "12" and _vals(deposit) == [None, "2718000"]
    (rou,) = _rows_by_label(balance, "使用權資產")
    assert rou.note == "11" and _vals(rou) == ["1050079", None]
    (receivable,) = _rows_by_label(balance, "長期應收款項")
    assert _vals(receivable) == ["956423", None]
    assert ["87895", None] in [_vals(r) for r in _rows_by_label(balance, "租賃負債")]
    # 科目名里不再粘着破折號
    assert not any(row.label.endswith("－") for row in balance.rows)


def test_fullwidth_dash_in_00799_2016_income_statement():
    found = rs.locate_statements(_pages("hk_00799_20161231"), report_type="annual")
    income = found["income"]
    (gain,) = _rows_by_label(income, "－出售時收益")
    assert _vals(gain) == [None, "67"]  # 修复前：2016 本期 = 67
    (share,) = _rows_by_label(income, "應佔聯營公司及合營企業的業績")
    assert _vals(share) == ["-1057", None]  # 修复前：整行丢失


def test_leading_fullwidth_dash_bullet_stays_part_of_the_label():
    """「－基本」是项目符号不是空值（腾讯 2025 年报）：紧贴文字时仍属科目名。"""
    found = rs.locate_statements(_pages("hk_00700_20251231"), report_type="annual")
    (eps,) = _rows_by_label(found["income"], "－基本")
    assert eps.note == "13(a)" and _vals(eps) == ["24.749", "20.938"]


def test_fullwidth_dash_is_not_squashed_into_letter_spaced_labels():
    assert rs.squash_spaced_cjk("應付股息 － － 5,000") == "應付股息 － － 5,000"
    assert rs.squash_spaced_cjk("合 併 綜 合 收 益 表") == "合併綜合收益表"


@pytest.mark.parametrize("token", ["－", "―", "−", "–", "—", "-"])
def test_null_marks(token):
    assert rs.parse_number(token) is None


def test_unicode_minus_attached_to_digits_is_a_negative_sign():
    assert rs.parse_row("匯兌虧損 −1,234 −56", expected_columns=2)[2] == [
        Decimal("-1234"),
        Decimal("-56"),
    ]
    assert rs.parse_row("匯兌虧損 − 56", expected_columns=2)[2] == [None, Decimal("56")]


# ---------------------------------------------------------------------------
# #341 小数尾数粘合走生产路径
# ---------------------------------------------------------------------------


def test_decimal_tail_glue_runs_in_production_parse_for_01133_2023():
    found = rs.locate_statements(_pages("hk_01133_20231231"), report_type="annual")
    (other_cash,) = _rows_by_label(found["cashflow"], "收到其他與經營活動有關的現金")
    assert _vals(other_cash) == ["1648565774.61", "978684382.48"]
    (non_op,) = _rows_by_label(found["income"], "減：營業外支出 註釋64")
    assert _vals(non_op) == ["165627024.85", "6095779.97"]


def test_parse_row_and_parse_block_share_one_token_resolver():
    tokens = ["5", "1,234,567,890.1", "2", "1,100,000,000.00"]
    assert rs.resolve_row_tokens("", tokens, 2) == ("5", ["1,234,567,890.12", "1,100,000,000.00"])
    # 附注号 + 恰好列数：不得再粘（PR #207 评审 P1）
    assert rs.resolve_row_tokens("", ["12", "1,234.5", "6"], 2) == ("12", ["1,234.5", "6"])
    # 两位附注号被拆开（#263）
    assert rs.resolve_row_tokens("", ["2", "6", "2,105,184", "1,815,678"], 2) == (
        "26",
        ["2,105,184", "1,815,678"],
    )
    # 同一行经 parse_row 与经 resolve_row_tokens 结果一致
    label, note, values = rs.parse_row(
        "營業成本 5 1,234,567,890.1 2 1,100,000,000.00", expected_columns=2
    )
    assert (label, note) == ("營業成本", "5")
    assert values == [Decimal("1234567890.12"), Decimal("1100000000.00")]


# ---------------------------------------------------------------------------
# #342 映射与构建
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label",
    [
        "收入",
        "總收入",
        "总收入",
        "銷售收入",
        "來自客戶合約之收益",
        "客戶合約收益",
        "Total revenues",
        "總 收 入",
        "營業額",
        "Revenue",
    ],
)
def test_revenue_labels(label):
    assert prompts.is_revenue_label(label)


@pytest.mark.parametrize(
    "label",
    ["其他收入", "其他收入及收益淨額", "利息收入", "財務收入", "公平值收益", "Other income"],
)
def test_not_revenue_labels(label):
    """未有收入的公司（09926 2020 中报）表内照样有这些行：它们不能让无收入映射判失败。"""
    assert not prompts.is_revenue_label(label)


def test_duplicate_row_ids_are_deduplicated_and_reported():
    content = json.dumps({"income": {"total_revenue": ["r1"], "sga_exp": ["r4", "r4", "r5"]}})
    mapping, unresolved = prompts.parse_statement_mapping(content, {"income": ["r1", "r4", "r5"]})
    assert mapping["income"]["sga_exp"] == ["r4", "r5"]
    assert "income.sga_exp:dup:r4" in unresolved


def _statement(rows, *, unit=1):
    return ParsedStatement(
        kind="income",
        page_start=1,
        page_end=1,
        title="綜合損益表",
        header=[],
        unit_multiplier=unit,
        currency="HKD",
        years=[2025, 2024],
        column_count=2,
        interim_four_columns=False,
        rows=rows,
    )


def test_resolve_value_counts_each_row_once():
    parsed = _statement([StatementRow("r4", "銷售開支", "", [Decimal("100"), None])])
    assert rs.resolve_value(parsed, ["r4", "r4"], 0, scale=False) == Decimal("100")


def _income(rows):
    return _statement([StatementRow(rid, label, "", [Decimal(v)]) for rid, label, v in rows])


def _tax_mapping(**extra):
    return {"total_profit": ["r1"], "income_tax": ["r2"], **extra}


@pytest.mark.parametrize(
    ("tax_label", "tax", "net", "attr", "expected_sign"),
    [
        # 国际准则开支（括号负数）；少数股东亏损 60 使归母净利 140（#371 评审 P2 的反例）
        ("所得稅開支", "-20", "80", "140", -1),
        # 国际准则抵免；少数股东盈利 60 使归母净利 60
        ("所得稅抵免", "20", "120", "60", -1),
        # 中国准则「減：所得稅費用」开支为正
        ("減：所得稅費用", "20", "80", "80", 1),
        # 开支写正数、行名无「減：」：按报表自己的税后利润判断
        ("所得稅開支", "20", "80", "200", 1),
    ],
)
def test_tax_sign_follows_the_statements_own_after_tax_profit(
    tax_label, tax, net, attr, expected_sign
):
    income = _income(
        [
            ("r1", "除稅前溢利", "100"),
            ("r2", tax_label, tax),
            ("r3", "年內溢利", net),
            ("r4", "本公司權益持有人應佔", attr),
        ]
    )
    mapping = _tax_mapping(n_income_attr_p=["r4"])
    assert build.tax_sign_convention(income, mapping, 0) == expected_sign


def test_tax_sign_ignores_a_next_row_that_is_not_consolidated_profit():
    """PR #371 复审 P2：合并净利行没抽到、所得税下一行直接是归母净利（已映射）时，不能拿它做恒等式。"""
    income = _income(
        [
            ("r0", "銷售成本", "-500"),
            ("r1", "除稅前溢利", "100"),
            ("r2", "所得稅開支", "-20"),
            ("r3", "本公司權益持有人應佔溢利", "120"),
            ("r4", "非控股權益", "-40"),
        ]
    )
    mapping = _tax_mapping(n_income_attr_p=["r3"], cost_of_revenue=["r0"])
    assert build.tax_sign_convention(income, mapping, 0) == -1  # 存为 +20（开支）
    # 同样的行没被映射、只靠行名也要排除
    assert build.tax_sign_convention(income, _tax_mapping(cost_of_revenue=["r0"]), 0) == -1


@pytest.mark.parametrize(
    "label",
    [
        "年內溢利",
        "期內溢利╱（虧損）",
        "四、淨利潤（淨虧損以「－」號填列）",
        "Profit for the year",
        "本期利潤",
        "本期利 潤",
        "年內溢利及全面收益總額",
    ],
)
def test_consolidated_profit_labels(label):
    assert build.is_consolidated_profit_label(label)


@pytest.mark.parametrize(
    "label",
    [
        "本公司權益持有人應佔溢利",
        "歸屬於母公司股東的淨利潤",
        "非控股權益",
        "少數股東損益",
        "除稅前溢利",
        "年內全面收益總額",
        "Profit attributable to owners of the Company",
        "每股盈利",
        "本公司擁有人應佔年內溢利",
        "年內其他全面收益",
        "",
    ],
)
def test_not_consolidated_profit_labels(label):
    assert not build.is_consolidated_profit_label(label)


def test_tax_sign_without_after_tax_row_uses_label_then_expense_convention():
    cas = _income([("r1", "利潤總額", "100"), ("r2", "減：所得稅費用", "20")])
    assert build.tax_sign_convention(cas, _tax_mapping(), 0) == 1
    ifrs = _income(
        [("r0", "銷售成本", "-500"), ("r1", "除稅前溢利", "100"), ("r2", "所得稅", "-20")]
    )
    assert build.tax_sign_convention(ifrs, _tax_mapping(cost_of_revenue=["r0"]), 0) == -1
    positive = _income(
        [("r0", "銷售成本", "500"), ("r1", "除稅前溢利", "100"), ("r2", "所得稅", "20")]
    )
    assert build.tax_sign_convention(positive, _tax_mapping(cost_of_revenue=["r0"]), 0) == 1
    # 什么都判不出：按国际准则惯例（开支为负）
    bare = _income([("r1", "除稅前溢利", "100"), ("r2", "所得稅", "-20")])
    assert build.tax_sign_convention(bare, _tax_mapping(), 0) == -1


def test_income_tax_credit_in_comparative_column_is_negative_for_01023():
    """真实固件 01023 2021 中报「所得稅（開支）╱抵免」(10,576) / 7,112：比较期 2020 H1 是抵免。"""
    data = json.loads((EXTRACTS / "hk_01023.extracts.json").read_text(encoding="utf-8"))
    payload = data["extracts"]["20211231|interim"]
    located = {k: ParsedStatement.from_payload(v) for k, v in payload["statements"].items()}
    target = svc._extract_target("20211231|interim", payload)
    rows = {
        r["end_date"]: r
        for r in build.build_period_rows(located, payload["mapping"], target, fingerprint="f")
    }
    current, prior = (
        rows[target["end_date"]],
        [r for k, r in rows.items() if k != target["end_date"]],
    )
    assert current["income_tax"] > 0
    assert prior and prior[0]["income_tax"] < 0


# ---------------------------------------------------------------------------
# #343 服务与构建
# ---------------------------------------------------------------------------


def _extract_payload(**extra):
    return {"status": "failed", "attempts": svc.MAX_ATTEMPTS, **extra}


def test_failures_under_an_older_prompt_are_not_capped():
    stale = _extract_payload(prompt_version=prompts.STATEMENT_PROMPT_VERSION - 1)
    current = _extract_payload(prompt_version=prompts.STATEMENT_PROMPT_VERSION)
    assert not svc.statement_capped(stale)
    assert svc.statement_capped(current)
    assert not svc.statement_capped({**current, "status": "ok"})


def test_no_revenue_line_mapping_is_stale_when_a_revenue_row_exists():
    def payload(label):
        return {
            "unresolved": ["income.total_revenue:no_revenue_line"],
            "statements": {"income": {"rows": [{"id": "r1", "label": label}]}},
        }

    assert svc.no_revenue_line_stale(payload("總收入"))
    assert not svc.no_revenue_line_stale(payload("其他收入及收益淨額"))
    assert not svc.no_revenue_line_stale({"unresolved": [], "statements": {}})


def _comparative(source_key, report_type, *, kinds, **fields):
    end = source_key.split("|")[0]
    return {
        "end_date": "20241231",
        "fp": "FY",
        "currency": "CNY",
        "is_comparative": True,
        "source_period_key": source_key,
        "source_report_type": report_type,
        "source_end_date": end,
        "source_pages": {kind: [1, 2] for kind in kinds},
        "source_by_kind": {
            kind: {"period_key": source_key, "end_date": end, "report_type": report_type}
            for kind in kinds
        },
        "extractor_version": rs.STATEMENT_EXTRACTOR_VERSION,
        "prompt_version": prompts.STATEMENT_PROMPT_VERSION,
        "build_version": prompts.STATEMENT_BUILD_VERSION,
        **fields,
    }


def test_derived_fields_follow_the_winning_source_per_statement():
    """#343-2：中报（只有资产负债表、总资产由分项推导）先写比较期，年报（三张表、总资产直接列示、
    FCF 推导）后到——合并后 FCF 必须记为派生，总资产不再是派生。"""
    interim = _comparative(
        "20250630|interim",
        "interim",
        kinds=["balance"],
        total_assets=90.0,
        total_nca=50.0,
        total_cur_assets=40.0,
        derived_fields={"total_assets": ["total_nca", "total_cur_assets"]},
    )
    annual = _comparative(
        "20251231|annual",
        "annual",
        kinds=["income", "balance", "cashflow"],
        total_revenue=100.0,
        total_assets=95.0,
        n_cashflow_act=240.0,
        capex=-20.0,
        free_cashflow=220.0,
        derived_fields={"free_cashflow": ["n_cashflow_act", "capex"]},
    )
    merged = build.merge_comparative_row(interim, annual)
    assert merged["derived_fields"] == {"free_cashflow": ["n_cashflow_act", "capex"]}
    # 存疑 CFO 被清洗时 FCF 随之失效
    merged["validation"] = {
        "status": "suspect",
        "row_level": False,
        "suspect_fields": ["n_cashflow_act"],
    }
    scrubbed = checks.scrub_suspect_fields(merged)
    assert scrubbed["n_cashflow_act"] is None and scrubbed["free_cashflow"] is None
    assert scrubbed["total_assets"] == 95.0


def test_same_source_reextraction_replaces_the_whole_statement():
    """#343-5：同一份报告重映射后不再映射的科目不能残留。"""
    old = _comparative(
        "20251231|annual",
        "annual",
        kinds=["balance"],
        total_assets=100.0,
        total_debt=777.0,
        lt_borr=700.0,
    )
    new = _comparative("20251231|annual", "annual", kinds=["balance"], total_assets=100.0)
    merged = build.merge_comparative_row(old, new)
    assert merged["total_debt"] is None and merged["lt_borr"] is None
    assert merged["total_assets"] == 100.0


def test_older_source_only_fills_gaps_and_keeps_its_derivation_record():
    annual = _comparative(
        "20251231|annual",
        "annual",
        kinds=["cashflow"],
        n_cashflow_act=None,
        capex=None,
        derived_fields={},
    )
    interim = _comparative(
        "20250630|interim",
        "interim",
        kinds=["cashflow"],
        n_cashflow_act=10.0,
        capex=-2.0,
        free_cashflow=8.0,
        derived_fields={"free_cashflow": ["n_cashflow_act", "capex"]},
    )
    merged = build.merge_comparative_row(annual, interim)
    assert merged["free_cashflow"] == 8.0
    assert merged["derived_fields"] == {"free_cashflow": ["n_cashflow_act", "capex"]}


def test_suspect_derived_field_is_not_rederived():
    """#343-3：存疑的派生科目是否复活不能取决于碰巧有没有雅虎行。"""
    row = {
        "total_hldr_eqy_exc_min_int": 800.0,
        "minority_int": 50.0,
        "total_equity": None,
        "derived_fields": {"total_equity": ["total_hldr_eqy_exc_min_int", "minority_int"]},
        "validation": {
            "status": "suspect",
            "row_level": False,
            "suspect_fields": ["total_liab", "total_equity"],
        },
    }
    assert checks.rederive_fields(dict(row))["total_equity"] is None
    row["validation"] = {"status": "ok", "suspect_fields": []}
    assert checks.rederive_fields(dict(row))["total_equity"] == 850.0


def test_comparative_evidence_only_from_current_versions():
    """#343-4：旧版本比较列（可能正是被修掉的错）不作交叉核对证据，旧证据随行延续时丢弃。"""
    current = _comparative("20251231|annual", "annual", kinds=["income"], total_revenue=100.0)
    stale = {**current, "build_version": prompts.STATEMENT_BUILD_VERSION - 1}
    row = {"end_date": "20241231", "fp": "FY"}
    build.attach_comparative_evidence(row, stale)
    assert "comparative_evidence" not in row
    build.attach_comparative_evidence(row, current)
    evidence = row["comparative_evidence"]
    assert evidence["total_revenue"] == 100.0 and build.evidence_current(evidence)
    carried = {"comparative_evidence": {**evidence, "build_version": 0}}
    build.attach_comparative_evidence(carried, None)
    assert "comparative_evidence" not in carried
