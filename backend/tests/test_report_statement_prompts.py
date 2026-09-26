"""科目→行 id 映射契约：LLM 只能给 id；不存在的 id 剔除并记 unresolved；必需科目缺失判失败。"""

import json

import pytest

from app.services import report_statement_prompts as prompts

ROWS = {"income": ["r1", "r2", "r3"], "balance": ["r1", "r2"], "cashflow": ["r1"]}


def test_valid_mapping_resolves_lists_strings_and_nulls():
    content = json.dumps({
        "income": {"total_revenue": ["r1"], "sga_exp": ["r2", "r3"], "ebitda": None, "basic_eps": "r3"},
        "balance": {"total_assets": ["r2"], "total_debt": []},
        "cashflow": {"n_cashflow_act": ["r1"], "not_a_field": ["r1"]},
    })
    mapping, unresolved = prompts.parse_statement_mapping(content, ROWS)
    assert mapping["income"] == {"total_revenue": ["r1"], "sga_exp": ["r2", "r3"], "basic_eps": ["r3"]}
    assert mapping["balance"] == {"total_assets": ["r2"]}
    assert mapping["cashflow"] == {"n_cashflow_act": ["r1"]}
    assert unresolved == []


def test_unknown_ids_are_dropped_and_reported():
    content = json.dumps({
        "income": {"total_revenue": ["r1", "r99"], "int_exp": ["r42"], "income_tax": 12},
        "balance": {"total_assets": ["r1"]},
    })
    mapping, unresolved = prompts.parse_statement_mapping(content, ROWS)
    assert mapping["income"] == {"total_revenue": ["r1"]}
    assert sorted(unresolved) == [
        "cashflow.n_cashflow_act:required", "income.income_tax:type", "income.int_exp:r42",
        "income.total_revenue:r99",
    ]
    # 输出里没有现金流量表 → 软必需缺失：空映射 + unresolved 记一笔，不是错误
    assert mapping["cashflow"] == {}


@pytest.mark.parametrize("content, message", [
    ("not json", "不是合法 JSON"),
    ("[]", "必须是 JSON 对象"),
    (json.dumps({"income": {"gross_profit": ["r1"]}, "balance": {"total_assets": ["r1"]}}), "income.total_revenue"),
    (json.dumps({"income": {"total_revenue": ["r1"]}, "balance": {"money_cap": ["r1"]}}), "balance.total_assets"),
    (json.dumps({"income": {"total_revenue": ["r7"]}, "balance": {"total_assets": ["r1"]}}), "income.total_revenue"),
    (json.dumps({"income": "r1", "balance": {"total_assets": ["r1"]}}), "映射必须是对象"),
])
def test_invalid_outputs_raise(content, message):
    with pytest.raises(ValueError, match=message):
        prompts.parse_statement_mapping(content, ROWS)


def test_required_fields_only_apply_to_present_statements():
    content = json.dumps({"income": {"total_revenue": ["r1"]}})
    mapping, _ = prompts.parse_statement_mapping(content, {"income": ["r1"]})
    assert mapping == {"income": {"total_revenue": ["r1"]}}


def test_messages_carry_rows_targets_and_schema():
    statements = {
        "income": {"unit_multiplier": 1000, "currency": "CNY", "columns": ["20251231 FY", "20241231 FY"],
                   "rows": [{"id": "r1", "label": "收入", "values": ["100", "90"]}]},
    }
    messages = prompts.build_statement_messages(
        symbol="00700", market="港股", report_type="annual", end_date="20251231", statements=statements
    )
    assert messages[0]["role"] == "system" and "只输出行 id" in messages[0]["content"]
    user = messages[1]["content"]
    assert '"id":"r1"' in user and '"total_revenue"' in user and "output_schema" in user
    assert "balance" not in json.loads(user.split("```json\n")[1].rsplit("\n```", 1)[0])["targets"]
    assert prompts.STATEMENT_PROMPT_VERSION >= 1
    assert set(prompts.STATEMENT_FIELDS) == {"income", "balance", "cashflow"}
    assert "free_cashflow" not in prompts.STATEMENT_FIELDS["cashflow"]  # 由代码推导，不让模型映射


def test_required_balance_fields_accept_net_asset_format_components():
    """港股净资产格式没有「資產總值」行：total_nca + total_cur_assets 齐全即满足必需科目。"""
    from app.services.report_statement_prompts import parse_statement_mapping

    rows = {"income": ["r1"], "balance": ["r1", "r2", "r3"]}
    content = json.dumps({
        "income": {"total_revenue": ["r1"]},
        "balance": {"total_assets": None, "total_nca": ["r1"], "total_cur_assets": ["r2"], "total_cur_liab": ["r3"]},
    })
    mapping, unresolved = parse_statement_mapping(content, rows)
    assert mapping["balance"] == {"total_nca": ["r1"], "total_cur_assets": ["r2"], "total_cur_liab": ["r3"]}
    assert unresolved == []
    # 只有其中一个分项 → 仍判缺失，错误信息列出备选组
    content = json.dumps({"income": {"total_revenue": ["r1"]}, "balance": {"total_nca": ["r1"]}})
    with pytest.raises(ValueError, match="total_assets 或 total_nca\\+total_cur_assets"):
        parse_statement_mapping(content, rows)


def test_pre_revenue_income_statement_is_accepted_without_revenue():
    """09926 2020 中报（18A 未有收入）：損益表没有任何收入行，只有「其他收入及收益淨額」。
    映射出净利即可接受，并记 unresolved；判据看行标签——有收入行却没映射仍是确定性失败。"""
    content = json.dumps({"income": {"n_income_attr_p": ["r9"], "total_profit": ["r7"]}})
    rows = {"income": ["r1", "r2", "r7", "r9"]}
    labels = {"income": ["其他收入及收益淨額", "行政開支", "除稅前虧損", "期內虧損"]}
    mapping, unresolved = prompts.parse_statement_mapping(content, rows, labels=labels)
    assert mapping["income"] == {"n_income_attr_p": ["r9"], "total_profit": ["r7"]}
    assert "income.total_revenue:no_revenue_line" in unresolved
    # 有收入行（「收益」）却没映射：仍判失败
    with pytest.raises(ValueError, match="total_revenue"):
        prompts.parse_statement_mapping(content, rows, labels={"income": ["收益", "行政開支", "除稅前虧損", "期內虧損"]})
    # 不给 labels（旧调用方）保持原行为
    with pytest.raises(ValueError, match="total_revenue"):
        prompts.parse_statement_mapping(content, rows)
    # 连净利/税前利润都没映射出来：仍判失败
    with pytest.raises(ValueError, match="total_revenue"):
        prompts.parse_statement_mapping(json.dumps({"income": {}}), rows, labels=labels)


@pytest.mark.parametrize("label, is_revenue", [
    ("收益", True), ("收入", True), ("營業總收入", True), ("一. 營業總收入", True), ("營業額", True),
    ("Revenue", True), ("其他收入及收益淨額", False), ("其他收益", False), ("利息收入", False),
])
def test_revenue_label_detection(label, is_revenue):
    assert bool(prompts.REVENUE_LABEL_RE.match(label)) is is_revenue
