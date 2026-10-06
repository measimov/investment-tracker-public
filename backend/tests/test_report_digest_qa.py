"""财报摘要的数字口径（#289）：摘要 prompt v3 的规则与「核心财务」、落库时与同期报表核对。"""

import json

import pytest

from app.database import SessionLocal
from app.models.security_profile import SecurityProfileData
from app.services import report_digest_prompts as prompts
from app.services import report_digest_service as svc
from app.services.profile_store import upsert_profile_row
from app.services.report_digest_qa import check_digest_numbers, parse_amount, qa_note

from .helpers import reset_tables


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("68.66 亿元", (68.66e8, None)),
        ("249,859.53 万元", (249859.53e4, None)),
        ("1,234.5 百万元", (1234.5e6, None)),
        ("4.37亿港元", (4.37e8, "HKD")),
        ("RMB 431.8 billion", (431.8e9, "CNY")),
        ("(1.2) 亿元", (-1.2e8, None)),
        ("人民币 2,498,595,300 元", (2498595300.0, "CNY")),
    ],
)
def test_parse_amount(text, expected):
    value, currency, _rounding = parse_amount(text)
    assert value == pytest.approx(expected[0]) and currency == expected[1]


def test_parse_amount_rounding_is_half_of_the_last_written_digit():
    assert parse_amount("0.01 亿元")[2] == pytest.approx(0.005e8)
    assert parse_amount("68.66 亿元")[2] == pytest.approx(0.005e8)
    assert parse_amount("24,985,953 万元")[2] == pytest.approx(0.5e4)


@pytest.mark.parametrize(
    ("text", "reference"),
    [
        ("0.01 亿元", 600000.0),  # 60 万元 = 0.006 亿元，两位小数写成 0.01 是对的
        ("0.00 亿元", 300000.0),  # 舍入为零：不是「符号相反」
        ("-0.01 亿元", -600000.0),
        ("-0.00 亿元", 200000.0),
        ("0.00 亿元", -400000.0),  # 盈亏平衡附近的小额亏损舍入为零
    ],
)
def test_small_amounts_within_rounding_are_not_flagged(text, reference):
    qa = check_digest_numbers(
        _digest("原文未提及", text), {"n_income_attr_p": reference}, statement_currency="CNY"
    )
    assert qa["checked"] == ["归母净利润"] and qa["flags"] == []


def test_small_amount_real_errors_still_flag():
    # 0.06 亿元写成 0.60 亿元：10 倍
    qa = check_digest_numbers(
        _digest("原文未提及", "0.60 亿元"), {"n_income_attr_p": 6000000.0}, statement_currency="CNY"
    )
    assert [f["reason"] for f in qa["flags"]] == ["magnitude"]
    # 亏损 1.2 亿写成盈利 1.2 亿：超出舍入区间的符号相反
    qa = check_digest_numbers(
        _digest("原文未提及", "1.20 亿元"),
        {"n_income_attr_p": -120000000.0},
        statement_currency="CNY",
    )
    assert "符号相反" in qa["flags"][0]["detail"]


@pytest.mark.parametrize("text", ["原文未提及", "", None, 3])
def test_parse_amount_unreadable(text):
    assert parse_amount(text) is None


def _digest(revenue, profit="原文未提及"):
    return {"核心财务": {"营业收入": revenue, "归母净利润": profit}}


def test_unit_error_of_ten_times_is_flagged_as_magnitude():
    """构造单位回归：「249,859.53 万元」（24.99 亿）误写为 249.99 亿元。"""
    qa = check_digest_numbers(
        _digest("249.99 亿元"), {"total_revenue": 2498595300.0}, statement_currency="CNY"
    )
    assert qa["checked"] == ["营业收入"]
    (flag,) = qa["flags"]
    assert flag["reason"] == "magnitude" and "10^1" in flag["detail"]
    assert "以报表为准" in qa_note(qa)


def test_revenue_mismatch_beyond_two_percent_is_flagged():
    """01995 2025：摘要 66.862 亿与报表 68.66 亿不符（差 2.6%）。"""
    qa = check_digest_numbers(
        _digest("66.86 亿元"), {"total_revenue": 6866231000.0}, statement_currency="CNY"
    )
    assert [f["reason"] for f in qa["flags"]] == ["mismatch"]


def test_rounding_to_two_decimals_passes():
    qa = check_digest_numbers(
        _digest("68.66 亿元", "4.37 亿元"),
        {"total_revenue": 6866231000.0, "n_income_attr_p": 437449000.0},
        statement_currency="CNY",
    )
    assert qa["checked"] == ["营业收入", "归母净利润"] and qa["flags"] == []
    assert qa_note(qa) is None


def test_different_currency_or_missing_data_is_not_compared():
    statement = {"total_revenue": 6866231000.0, "n_income_attr_p": 437449000.0}
    assert (
        check_digest_numbers(_digest("9.5 亿美元"), statement, statement_currency="CNY")["checked"]
        == []
    )
    assert (
        check_digest_numbers(_digest("68.66 亿元"), None, statement_currency=None)["checked"] == []
    )
    assert check_digest_numbers({}, statement, statement_currency="CNY")["checked"] == []


def test_opposite_sign_is_a_mismatch():
    qa = check_digest_numbers(
        _digest("原文未提及", "1.2 亿元"),
        {"n_income_attr_p": -120000000.0},
        statement_currency="CNY",
    )
    assert qa["flags"][0]["reason"] == "mismatch" and "符号相反" in qa["flags"][0]["detail"]


def test_prompts_carry_number_rules_and_core_finance():
    for prompt in (prompts.DIGEST_SYSTEM_PROMPT, prompts.COMPACT_DIGEST_SYSTEM_PROMPT):
        assert prompts.DIGEST_NUMBER_RULES in prompt
        assert "核心财务" in prompt
    assert prompts.DIGEST_PROMPT_VERSION == 3


def test_parse_digest_keeps_core_finance_and_tolerates_its_absence():
    base = {
        "主营收入结构": "a",
        "一次性项目": "b",
        "会计信号": "c",
        "关键数字": ["x"],
    }
    parsed = prompts.parse_digest_output(
        json.dumps({**base, "核心财务": {"营业收入": "68.66 亿元", "其他": "忽略"}}), tier="C"
    )
    assert parsed["核心财务"] == {"营业收入": "68.66 亿元"}
    assert "核心财务" not in prompts.parse_digest_output(json.dumps(base), tier="C")
    assert "核心财务" not in prompts.parse_digest_output(
        json.dumps({**base, "核心财务": "68 亿"}), tier="C"
    )


# ---------------------------------------------------------------------------
# 落库与分析输入（真实测试库）
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        reset_tables(session, [SecurityProfileData])
        yield session
        session.rollback()
        reset_tables(session, [SecurityProfileData])
    finally:
        session.close()


DIGEST_JSON = json.dumps(
    {
        "主营收入结构": "a",
        "一次性项目": "b",
        "会计信号": "c",
        "经营回顾": "d",
        "业务分部占比": "e",
        "上下游与产业链": "f",
        "成本与费用": "g",
        "风险要点": "h",
        "展望": "i",
        "关键数字": ["x"],
        "核心财务": {"营业收入": "249.99 亿元", "归母净利润": "110.80 亿元"},
    },
    ensure_ascii=False,
)


def test_qa_reflects_statements_that_arrive_after_the_digest(db, monkeypatch):
    """评审 P2：分析 job 与每周刷新都是先摘要、后报表。核对在读取时按当前报表现算，同一轮里
    后抽到/被修正的报表，最终进分析输入的摘要就带着最新结论。"""
    target = {
        "period_key": "20260630|semi",
        "report_type": "semi",
        "end_date": "20260630",
        "title": "2026年半年度报告",
        "ann_date": "2026-08-28",
        "url": "http://static.cninfo.com.cn/final/h1.PDF",
    }
    monkeypatch.setattr(
        svc,
        "cached_report_targets_detailed",
        lambda db, symbol, market, **kw: {"targets": [target], "complete": True},
    )
    monkeypatch.setattr(svc, "_ensure_section", lambda db, s, m, t: {"mdna": "经营分析"})
    monkeypatch.setattr(
        svc,
        "chat_completion",
        lambda messages, **kw: {"content": DIGEST_JSON, "model": "m", "usage": {}},
    )
    # 1) 先摘要：此时还没有报表行，没法核对
    svc.ensure_report_digests(db, "603259", "A股", max_new=2)
    (loaded,) = svc.load_report_digests(db, "603259", "A股")
    assert loaded["qa"]["checked"] == [] and loaded["qa"]["flags"] == []
    # 2) 同一轮随后报表落库（收入 24.99 亿，摘要写成 249.99 亿）
    upsert_profile_row(
        db,
        "603259",
        "A股",
        "income",
        "20260630",
        {"end_date": "20260630", "total_revenue": 2498595300.0, "n_income_attr_p": 11080173661.42},
    )
    db.commit()
    (loaded,) = svc.load_report_digests(db, "603259", "A股")
    assert [f["reason"] for f in loaded["qa"]["flags"]] == ["magnitude"]
    (item,) = svc.serialize_digest_for_analysis([loaded])
    assert "以报表为准" in item["数字核对"]
    # 3) 报表被修正后，旧的标记随之消失（不残留误报）
    upsert_profile_row(
        db,
        "603259",
        "A股",
        "income",
        "20260630",
        {"end_date": "20260630", "total_revenue": 24998595300.0, "n_income_attr_p": 11080173661.42},
    )
    db.commit()
    (loaded,) = svc.load_report_digests(db, "603259", "A股")
    assert loaded["qa"]["flags"] == []
    assert "数字核对" not in svc.serialize_digest_for_analysis([loaded])[0]
    # 落库的摘要行里不存核对结论
    row = (
        db.query(SecurityProfileData)
        .filter_by(symbol="603259", market="A股", dataset="report_digest")
        .one()
    )
    assert "qa" not in row.payload


@pytest.mark.parametrize("report_type", ["semi", "interim"])
def test_interim_qa_uses_h1_and_never_falls_back_to_annual_yahoo(db, report_type):
    from app.services.report_statement_prompts import (
        STATEMENT_BUILD_VERSION,
        STATEMENT_EXTRACTOR_VERSION,
        STATEMENT_PROMPT_VERSION,
    )

    statement = {
        "total_revenue": 100_000_000,
        "currency": "HKD",
        "extractor_version": STATEMENT_EXTRACTOR_VERSION,
        "prompt_version": STATEMENT_PROMPT_VERSION,
        "build_version": STATEMENT_BUILD_VERSION,
    }
    for dataset, key, payload in (
        ("report_statements", "20260630|H1", statement),
        ("report_statements", "20260630|FY", {**statement, "total_revenue": 900_000_000}),
        ("yahoo_fundamentals", "20260630", {"total_revenue": 800_000_000, "currency": "HKD"}),
    ):
        upsert_profile_row(db, "00700", "港股", dataset, key, payload)
    db.commit()
    qa = svc.digest_qa(db, "00700", "港股", "20260630", report_type, _digest("1 亿港元"))
    assert qa["checked"] == ["营业收入"] and qa["flags"] == []

    db.query(SecurityProfileData).filter_by(
        symbol="00700", market="港股", dataset="report_statements", period_key="20260630|H1"
    ).delete()
    db.commit()
    qa = svc.digest_qa(db, "00700", "港股", "20260630", report_type, _digest("1 亿港元"))
    assert qa["checked"] == [] and qa["flags"] == []
