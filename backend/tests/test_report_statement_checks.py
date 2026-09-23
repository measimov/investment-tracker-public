"""港股报表行的确定性校验层（纯函数）：恒等式 / 合理性 / 币种 / 交叉核对 / 清洗。

金样来自真实年报（tests/fixtures/reports/hk_*），映射用标签直接构造（模拟只会照抄行 id 的
模型），这样每条断言都对应一份真实报表的真实数字。
"""

import gzip
import json
from pathlib import Path

import pytest

from app.services import report_statement_checks as checks
from app.services import report_statement_service as svc
from app.services.report_statement_prompts import parse_statement_mapping
from app.services.report_statements import locate_statements

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "reports"


def _pages(name):
    with gzip.open(FIXTURE_DIR / f"{name}.pages.txt.gz", "rt", encoding="utf-8") as handle:
        return handle.read().split("\x0c")


def _ids(parsed, *labels, note=None):
    out = []
    for row in parsed.rows:
        if row.label in labels or (note is not None and row.label == "" and row.note == note):
            out.append(row.row_id)
    return out


def _rows_for(name, mapping_by_label, *, report_type="annual", end_date):
    located = {k: v for k, v in locate_statements(_pages(name), report_type=report_type).items() if v}
    raw = {
        kind: {field: _ids(located[kind], *labels) if isinstance(labels, tuple) else labels(located[kind])
               for field, labels in fields.items()}
        for kind, fields in mapping_by_label.items()
    }
    mapping, _ = parse_statement_mapping(
        json.dumps(raw), {kind: [r.row_id for r in parsed.rows] for kind, parsed in located.items()}
    )
    target = {"end_date": end_date, "report_type": report_type, "period_key": f"{end_date}|{report_type}",
              "url": "u", "ann_date": "01/04/2026 16:30", "title": name}
    rows = svc.build_period_rows(located, mapping, target, fingerprint="f")
    return {f"{r['end_date']}|{r['fp']}": r for r in rows}


TENCENT_2025 = {
    "income": {
        "total_revenue": lambda p: _ids(p, note="6"), "cost_of_revenue": ("收入成本",),
        "gross_profit": ("毛利",), "operating_income": ("經營盈利",),
        "n_income_attr_p": lambda p: _ids(p, "本公司權益持有人")[:1],
    },
    "balance": {
        # 腾讯的分项合计行没有标签（r15 非流动资产 / r25 流动资产 / r33 归母权益 / r43 非流动负债 /
        # r53 流动负债），按行 id 指定
        "total_assets": ("資產總額",), "total_nca": lambda p: ["r15"], "total_cur_assets": lambda p: ["r25"],
        "total_liab": ("負債總額",), "total_cur_liab": lambda p: ["r53"], "total_ncl": lambda p: ["r43"],
        "total_hldr_eqy_exc_min_int": lambda p: ["r33"],
        "minority_int": ("非控制性權益",), "total_equity": ("權益總額",),
    },
    "cashflow": {
        "n_cashflow_act": ("經營活動所得現金流量淨額",),
        "capex": ("購買物業、設備及器材、在建工程與投資物業的付款╱預付款項",),
    },
}


def test_tencent_2025_passes_every_identity_including_balance_sheet():
    rows = _rows_for("hk_00700_20251231", TENCENT_2025, end_date="20251231")
    fy = rows["20251231|FY"]
    validation = fy["validation"]
    assert validation["status"] == "ok" and validation["suspect_fields"] == []
    by_id = {c["id"]: c for c in validation["checks"]}
    assert by_id["gross_profit_identity"]["status"] == "ok"
    assert by_id["total_assets_identity"]["status"] == "ok"
    assert by_id["total_liab_identity"]["status"] == "ok"
    assert by_id["total_equity_identity"]["status"] == "ok"
    assert by_id["balance_sheet_identity"]["status"] == "ok"  # 资产 = 负债 + 权益总额（含少数股东）
    assert by_id["free_cashflow_identity"]["status"] == "ok"
    assert by_id["currency_consistency"]["status"] == "ok"
    assert fy["currency"] == "CNY" and fy["currency_by_kind"] == {"income": "CNY", "balance": "CNY", "cashflow": "CNY"}
    assert fy["free_cashflow"] == fy["n_cashflow_act"] - abs(fy["capex"])
    assert validation["version"] == checks.STATEMENT_VALIDATION_VERSION


AKESO_2025 = {
    "income": {"total_revenue": ("收入",), "n_income_attr_p": lambda p: _ids(p, "本公司擁有人")[:1]},
    "balance": {
        "total_nca": ("非流動資產總值",), "total_cur_assets": ("流動資產總值",),
        "total_cur_liab": ("流動負債總額",), "total_ncl": ("非流動負債總額",),
        "total_hldr_eqy_exc_min_int": lambda p: _ids(p, "本公司擁有人應佔權益")[:1],
        "minority_int": ("非控股權益",), "total_equity": ("權益總額",),
    },
    "cashflow": {"n_cashflow_act": lambda p: _ids(p, "經營活動所用現金流量淨額", "經營活動所得現金流量淨額")[:1]},
}


def test_negative_minority_interest_closes_only_with_total_equity():
    """09926 少数股东权益为负：有权益总额时资产恒等式闭合；没有时 skipped 而不是假阳性。"""
    rows = _rows_for("hk_09926_20251231", AKESO_2025, end_date="20251231")
    fy = rows["20251231|FY"]
    assert fy["minority_int"] < 0 and fy["total_equity"] == 8_945_957_000.0
    by_id = {c["id"]: c for c in fy["validation"]["checks"]}
    assert by_id["balance_sheet_identity"]["status"] == "ok"
    assert fy["validation"]["status"] == "ok"

    without_equity = {**fy, "total_equity": None, "minority_int": None}
    validation = checks.validate_period_row(without_equity)
    by_id = {c["id"]: c for c in validation["checks"]}
    assert by_id["balance_sheet_identity"]["status"] == "skipped"
    assert validation["status"] == "ok"


def test_gross_profit_mapped_to_a_sub_line_is_flagged_field_level():
    rows = _rows_for("hk_00700_20251231", TENCENT_2025, end_date="20251231")
    fy = dict(rows["20251231|FY"])
    fy["cost_of_revenue"] = fy["cost_of_revenue"] * 0.7  # 映射到了子项
    validation = checks.validate_period_row(fy)
    assert validation["status"] == "suspect"
    assert set(validation["suspect_fields"]) == {"gross_profit", "total_revenue", "cost_of_revenue"}
    scrubbed = checks.scrub_suspect_fields({**fy, "validation": validation})
    assert scrubbed["gross_profit"] is None and scrubbed["total_assets"] == fy["total_assets"]


def test_cross_check_with_yahoo_marks_only_the_mismatching_field():
    rows = _rows_for("hk_00700_20251231", TENCENT_2025, end_date="20251231")
    fy = rows["20251231|FY"]
    yahoo = {"end_date": "20251231", "fp": "FY", "currency": "CNY",
             "total_revenue": fy["total_revenue"] * 1.03, "total_assets": fy["total_assets"],
             "n_cashflow_act": fy["n_cashflow_act"] * 1.004}
    extra = checks.cross_check_row(fy, yahoo_row=yahoo)
    by_id = {c["id"]: c for c in extra}
    assert by_id["yahoo_total_revenue"]["status"] == "suspect"
    assert by_id["yahoo_total_assets"]["status"] == "ok"
    assert by_id["yahoo_n_cashflow_act"]["status"] == "ok"  # 0.4% 在容差内
    assert by_id["yahoo_n_income_attr_p"]["status"] == "skipped"
    validation = checks.validate_period_row(fy, extra_checks=extra)
    assert validation["status"] == "suspect" and validation["suspect_fields"] == ["total_revenue"]
    # 币种不同：全部 skipped，不判存疑
    extra = checks.cross_check_row(fy, yahoo_row={**yahoo, "currency": "HKD"})
    assert all(c["status"] == "skipped" for c in extra)
    assert checks.validate_period_row(fy, extra_checks=extra)["status"] == "ok"


def test_comparative_column_drift_tiers():
    rows = _rows_for("hk_00700_20251231", TENCENT_2025, end_date="20251231")
    fy = rows["20251231|FY"]
    base = {"currency": "CNY", "source_period_key": "20261231|annual", "total_revenue": fy["total_revenue"]}
    restated = checks.cross_check_row(fy, comparative_row={**base, "total_revenue": fy["total_revenue"] * 1.03})
    check = next(c for c in restated if c["id"] == "comparative_total_revenue")
    assert check["status"] == "suspect" and check["severity"] == "info"  # 1-5%：可能是重述，只记不判
    assert checks.validate_period_row(fy, extra_checks=restated)["status"] == "ok"
    wrong = checks.cross_check_row(fy, comparative_row={**base, "total_revenue": fy["total_revenue"] * 1.2})
    check = next(c for c in wrong if c["id"] == "comparative_total_revenue")
    assert check["status"] == "suspect" and check["severity"] == "error"


def test_hard_failures_catch_split_digit_rows_and_currency_conflicts():
    garbled = {"total_assets": 9_000_000.0, "total_cur_assets": 4_000_000.0, "total_liab": 4_000_000.0,
               "total_hldr_eqy_exc_min_int": 3_185_546_000.0, "currency_by_kind": {"balance": "CNY"}}
    reasons = checks.hard_failures(garbled)
    assert any("一半" in reason for reason in reasons)
    assert checks.hard_failures({"total_assets": 0.0}) == ["总资产 0 ≤ 0"]
    conflict = {"total_assets": 10.0, "currency_by_kind": {"income": "USD", "balance": "CNY"}}
    assert any("币种不一致" in reason for reason in checks.hard_failures(conflict))
    assert checks.hard_failures({"total_assets": 100.0, "total_cur_assets": 40.0}) == []


def test_build_period_rows_raises_on_primary_hard_failure_and_drops_comparative():
    located = {k: v for k, v in locate_statements(_pages("hk_09926_20251231"), report_type="annual").items() if v}
    balance = located["balance"]
    by_label = {row.label: row.row_id for row in balance.rows}
    target = {"end_date": "20251231", "report_type": "annual", "period_key": "20251231|annual", "url": "u"}
    # 主行：总资产映射到「流動資產淨值」→ 流动资产 > 总资产 → 抛
    mapping = {"balance": {"total_assets": [by_label["流動資產淨值"]], "total_cur_assets": [by_label["流動資產總值"]]}}
    with pytest.raises(ValueError, match="报表校验失败"):
        svc.build_period_rows({"balance": balance}, mapping, target, fingerprint="f")
    # 只有比较列坏（用只在比较列出问题的构造很难，这里验证丢弃路径：把两列都坏掉时主行抛在前）
    rows = svc.build_period_rows(
        {"balance": balance},
        {"balance": {"total_nca": [by_label["非流動資產總值"]], "total_cur_assets": [by_label["流動資產總值"]]}},
        target, fingerprint="f",
    )
    assert {r["end_date"] for r in rows} == {"20251231", "20241231"}
    assert all(r["validation"]["status"] == "ok" for r in rows)
    # 总资产由分项推导 → 记入 derived_fields（清洗分项时它随之失效）
    assert all(r["derived_fields"] == {"total_assets": ["total_nca", "total_cur_assets"]} for r in rows)


def test_positive_capex_is_info_and_fcf_uses_absolute_value():
    row = {"n_cashflow_act": 100.0, "capex": 30.0, "free_cashflow": 70.0}
    validation = checks.validate_period_row(row)
    by_id = {c["id"]: c for c in validation["checks"]}
    assert by_id["free_cashflow_identity"]["status"] == "ok"
    assert by_id["capex_sign"]["severity"] == "info" and validation["status"] == "ok"


def test_scrub_row_level_only_for_hard_failures():
    # 旧版 validation（无 row_level）且无字段：唯一可能的含义是整行不可信
    payload = {"total_revenue": 1.0, "total_assets": 2.0, "currency": "CNY",
               "validation": {"status": "suspect", "suspect_fields": []}}
    scrubbed = checks.scrub_suspect_fields(payload)
    assert scrubbed["total_revenue"] is None and scrubbed["total_assets"] is None
    assert scrubbed["currency"] == "CNY"
    assert checks.scrub_suspect_fields({"total_revenue": 1.0})["total_revenue"] == 1.0
    assert checks.statement_row_usable({"validation": {"status": "suspect"}}) is False
    # 币种冲突 → row_level=True → 全清；字段级存疑 → 只清字段
    conflict = {"total_assets": 10.0, "money_cap": 3.0, "currency_by_kind": {"income": "USD", "balance": "CNY"}}
    validation = checks.validate_period_row(conflict)
    assert validation["status"] == "suspect" and validation["row_level"] is True
    scrubbed = checks.scrub_suspect_fields({**conflict, "validation": validation})
    assert scrubbed["total_assets"] is None and scrubbed["money_cap"] is None


def test_conflicting_external_evidence_blames_only_that_field():
    """评审 P2：雅虎说收入对、比较列说收入错——收入存疑，但资产/现金不能被整行清空。"""
    row = {"currency": "CNY", "total_revenue": 100.0, "total_assets": 1000.0, "money_cap": 70.0}
    extra = checks.cross_check_row(
        row,
        yahoo_row={"currency": "CNY", "total_revenue": 100.0},
        comparative_row={"currency": "CNY", "source_period_key": "20261231|annual", "total_revenue": 80.0},
    )
    validation = checks.validate_period_row(row, extra_checks=extra)
    assert validation["status"] == "suspect" and validation["row_level"] is False
    assert validation["suspect_fields"] == ["total_revenue"]
    scrubbed = checks.scrub_suspect_fields({**row, "validation": validation})
    assert scrubbed["total_revenue"] is None
    assert scrubbed["total_assets"] == 1000.0 and scrubbed["money_cap"] == 70.0


def test_positive_external_evidence_only_exempts_identity_blame():
    """雅虎核对通过的收入不因毛利恒等式另一端可疑而被清空；毛利/成本仍被指认。"""
    row = {"currency": "CNY", "total_revenue": 100.0, "cost_of_revenue": 60.0, "gross_profit": 55.0}
    extra = checks.cross_check_row(row, yahoo_row={"currency": "CNY", "total_revenue": 100.0})
    validation = checks.validate_period_row(row, extra_checks=extra)
    assert validation["status"] == "suspect"
    assert set(validation["suspect_fields"]) == {"gross_profit", "cost_of_revenue"}
    # 恒等式失败但责任科目全部有正面证据 → 不留"存疑却无字段"的空状态
    row = {"currency": "CNY", "total_assets": 100.0, "total_nca": 50.0, "total_cur_assets": 40.0}
    extra = checks.cross_check_row(row, yahoo_row={"currency": "CNY", "total_assets": 100.0})
    validation = checks.validate_period_row(row, extra_checks=extra)
    assert validation["suspect_fields"] == ["total_nca", "total_cur_assets"]


def test_scrubbing_an_input_also_clears_its_derived_fields_until_rederived():
    """评审 P1 端到端：CFO 被雅虎判存疑 → FCF 一并清空（它只是由错误 CFO 算出来的）；
    雅虎补上 CFO 后重新推导 FCF。"""
    row = {"currency": "CNY", "n_cashflow_act": 100.0, "capex": -20.0, "free_cashflow": 80.0,
           "derived_fields": {"free_cashflow": ["n_cashflow_act", "capex"]}}
    extra = checks.cross_check_row(row, yahoo_row={"currency": "CNY", "n_cashflow_act": 50.0})
    validation = checks.validate_period_row(row, extra_checks=extra)
    assert validation["suspect_fields"] == ["n_cashflow_act"]
    scrubbed = checks.scrub_suspect_fields({**row, "validation": validation})
    assert scrubbed["n_cashflow_act"] is None and scrubbed["free_cashflow"] is None
    assert scrubbed["capex"] == -20.0
    scrubbed["n_cashflow_act"] = 50.0
    checks.rederive_fields(scrubbed)
    assert scrubbed["free_cashflow"] == 30.0
    # 派生链两级：总资产由分项推导、分项存疑 → 总资产也清；没有元数据的旧行只认 FCF
    row = {"total_assets": 90.0, "total_nca": 50.0, "total_cur_assets": 40.0,
           "derived_fields": {"total_assets": ["total_nca", "total_cur_assets"]},
           "validation": {"status": "suspect", "row_level": False, "suspect_fields": ["total_cur_assets"]}}
    assert checks.scrub_suspect_fields(row)["total_assets"] is None
    legacy = {"total_assets": 90.0, "total_nca": 50.0, "total_cur_assets": 40.0,
              "validation": {"status": "suspect", "row_level": False, "suspect_fields": ["total_cur_assets"]}}
    assert checks.scrub_suspect_fields(legacy)["total_assets"] == 90.0


def test_merge_hk_rows_rederives_fcf_after_yahoo_fills_scrubbed_cfo():
    from app.services.earnings_quality import merge_hk_statement_rows

    pdf = {"end_date": "20251231", "fp": "FY", "currency": "CNY", "n_cashflow_act": 100.0, "capex": -20.0,
           "free_cashflow": 80.0, "derived_fields": {"free_cashflow": ["n_cashflow_act", "capex"]},
           "validation": {"status": "suspect", "row_level": False, "suspect_fields": ["n_cashflow_act"]}}
    yahoo = {"end_date": "20251231", "fp": "FY", "currency": "CNY", "n_cashflow_act": 50.0}
    (merged,) = merge_hk_statement_rows({"report_statements": [pdf], "yahoo_fundamentals": [yahoo]})
    assert merged["n_cashflow_act"] == 50.0 and merged["capex"] == -20.0
    assert merged["free_cashflow"] == 30.0
    # 雅虎也没有 CFO：FCF 保持空，不复活旧值
    (merged,) = merge_hk_statement_rows({"report_statements": [pdf], "yahoo_fundamentals": []})
    assert merged["free_cashflow"] is None


def test_validation_summary_lists_error_reasons_only():
    payload = {"validation": {"checks": [
        {"severity": "error", "status": "suspect", "detail": "A"},
        {"severity": "info", "status": "suspect", "detail": "B"},
        {"severity": "error", "status": "ok", "detail": ""},
    ]}}
    assert checks.validation_summary(payload) == "A"
