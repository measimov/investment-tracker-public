"""报告事实输入的回归：同笔分红、FCF 口径、摘要单位与未核对状态。"""

from copy import deepcopy

import pytest

from app.database import SessionLocal
from app.models.security_profile import SecurityProfileData
from app.services import analysis_signals as signals
from app.services import business_profile_service as business
from app.services import report_digest_service as digests
from app.services import security_analysis_jobs as jobs
from app.services.business_profile_prompts import PROFILE_PROMPT_VERSION
from app.services.profile_store import upsert_profile_row
from app.services.report_digest_prompts import DIGEST_PROMPT_VERSION
from app.services.report_digest_qa import check_digest_numbers, digest_for_llm, parse_amount
from app.services.report_sections import section_extractor_version
from app.services.security_analysis_prompts import build_system_prompt

from .helpers import reset_tables


def dividend(end="20240930", **kw):
    return dict(
        end_date=end,
        ex_date="20250124",
        imp_ann_date="20250117",
        cash_div_tax=1.23,
        div_proc="实施",
        **kw,
    )


def test_same_distribution_across_report_periods_is_paid_once():
    original = [dividend(), dividend("20241211"), {**dividend(), "div_proc": "预案"}]
    saved = deepcopy(original)
    (paid,) = signals.implemented_dividends(original)
    assert paid["end_date"] == "20241211"
    assert paid["source_end_dates"] == ["20240930", "20241211"]
    assert signals.a_share_dividends_per_share(original)["by_year"] == {"2024": 1.23}
    (example,) = jobs._compact_dividend_history(original)
    assert example["cash_div_tax"] == 1.23 and example["div_proc"] == "实施"
    assert original == saved


@pytest.mark.parametrize(
    "change",
    [
        {"imp_ann_date": "20250118"},
        {"ex_date": "20250125"},
        {"cash_div_tax": 1.24},
        {"stk_div": 0.1},
        {"imp_ann_date": None},
        {"ex_date": None},
        {"cash_div_tax": None},
    ],
)
def test_distinct_or_uncertain_distributions_are_not_merged(change):
    assert len(signals.implemented_dividends([dividend(), {**dividend("20241211"), **change}])) == 2


def test_revision_conflict_is_not_hidden_by_cross_period_deduplication():
    rows = [dividend(), {**dividend(), "cash_div_tax": 2.0}, dividend("20241211")]
    paid = signals.implemented_dividends(rows)
    assert len(paid) == 2 and paid[0]["conflict"] is True


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2025年收入为68.66亿元，同比增长5%", 68.66e8),
        ("亏损 5.04 亿港元", -5.04e8),
        ("营业收入（68.66亿元）", 68.66e8),
        ("虧損5.04億港元", -5.04e8),
        ("港幣4.02億元（原文402.1百萬元，截至2018年12月31日）", 4.02e8),
    ],
)
def test_amount_parser_ignores_dates_and_growth_and_handles_loss(value, expected):
    assert parse_amount(value)[0] == pytest.approx(expected)


@pytest.mark.parametrize(
    "value",
    [
        "原文未提及（仅披露同比增长97.7%）",
        "2025年，同比增长5%",
        "5%",
        "本期68亿元，上期60亿元",
        "8亿元（未明确是否归母）",
        "2025",
    ],
)
def test_unavailable_or_ambiguous_numbers_are_not_treated_as_amounts(value):
    assert parse_amount(value) is None


@pytest.mark.parametrize(
    "label,value,reference",
    [
        ("归母净利润", "49.85亿港元（扣除减值拨备后基本纯利）", 4401609000),
        ("营业收入", "油气销售收入1213.25亿元", 146490000000),
        ("归母净利润", "原文未提及（合并净利润88.18亿元）", 7683685424.8),
    ],
)
def test_other_accounting_basis_is_not_compared_as_the_core_field(label, value, reference):
    field = "total_revenue" if label == "营业收入" else "n_income_attr_p"
    qa = check_digest_numbers(
        {"核心财务": {label: value}}, {field: reference}, statement_currency="CNY"
    )
    assert qa["checked"] == [] and qa["flags"] == []


def bad_digest():
    return {
        "核心财务": {"营业收入": "249.99亿元"},
        "主营收入结构": "收入249.99亿元，临床业务增长",
        "业务分部占比": "临床业务占比40%",
    }


def test_conflicting_digest_is_quarantined_everywhere_without_rewriting_source():
    source = bad_digest()
    qa = check_digest_numbers(source, {"total_revenue": 2498595300}, statement_currency="CNY")
    entry = {"digest": source, "end_date": "20260630", "qa": qa}
    saved = deepcopy(entry)
    projection = digest_for_llm(entry)
    (serialized,) = digests.serialize_digest_for_analysis([entry], compact_older_than_years=0)
    assert projection["digest"] == serialized["digest"] == {}
    assert "249.99" not in str(serialized)
    assert projection["numeric_qa"]["quarantined"]
    assert entry == saved
    unverified = digest_for_llm({"digest": source})
    assert unverified["numeric_qa"]["checked"] == []
    assert unverified["numeric_qa"]["unverified"] == ["营业收入", "归母净利润"]


@pytest.fixture
def db():
    session = SessionLocal()
    reset_tables(session, [SecurityProfileData])
    try:
        yield session
    finally:
        session.rollback()
        reset_tables(session, [SecurityProfileData])
        session.close()


def seed_digest(db):
    upsert_profile_row(
        db,
        "603259",
        "A股",
        "income",
        "20260630",
        {"end_date": "20260630", "total_revenue": 2498595300},
    )
    upsert_profile_row(
        db,
        "603259",
        "A股",
        "report_digest",
        "20260630|semi",
        {
            "status": "ok",
            "report_type": "semi",
            "end_date": "20260630",
            "digest": bad_digest(),
            "prompt_version": DIGEST_PROMPT_VERSION,
            "extractor_version": section_extractor_version("A股"),
        },
    )
    db.commit()


def test_business_slices_do_not_reintroduce_quarantined_digest(db):
    seed_digest(db)
    (item,) = business.build_business_profile_input(db, "603259", "A股")["report_digest_slices"]
    assert item["numeric_qa"]["quarantined"] is True
    assert item["主营收入结构"] is None and item["业务分部占比"] is None
    # 旧画像即使仍是 ok，也不能在刷新失败后继续传播。
    upsert_profile_row(
        db,
        "603259",
        "A股",
        "business_profile",
        "current",
        {
            "status": "ok",
            "prompt_version": PROFILE_PROMPT_VERSION,
            "input_fingerprint": "old",
            "profile": {"商业模式": "收入249.99亿元"},
        },
    )
    db.commit()
    assert business.load_business_profile(db, "603259", "A股", for_analysis=True)["profile"] is None
    assert business.load_business_profile(db, "603259", "A股")["profile"]


def test_latest_interim_fcf_and_main_input_use_one_definition(db):
    for dataset, fields in {
        "income": {"total_revenue": 10e8, "n_income_attr_p": 1e8},
        "cashflow": {
            "n_cashflow_act": 2e8,
            "c_pay_acq_const_fiolta": 2.61e8,
            "free_cashflow": 1.56e8,
        },
    }.items():
        upsert_profile_row(
            db, "605016", "A股", dataset, "20260630", {"end_date": "20260630", **fields}
        )
    db.commit()
    payload = jobs.build_analysis_input(db, "605016", "A股", digest_gaps=[])
    fcf = payload["signals"]["period_signals"]["latest_interim"]["free_cashflow"]
    assert fcf["value_yi"] == -0.61 and "购建" in fcf["basis"]
    assert "free_cashflow" not in payload["profile"]["cashflow"][0]
    raw = db.query(SecurityProfileData).filter_by(symbol="605016", dataset="cashflow").one().payload
    assert raw["free_cashflow"] == 1.56e8


def test_raw_fcf_is_excluded_in_all_compaction_levels():
    for dataset in ("cashflow", "report_statements", "yahoo_fundamentals"):
        raw = {dataset: [{"end_date": "20251231", "free_cashflow": 100, "n_cashflow_act": 500}]}
        assert "free_cashflow" not in jobs._compact_profile(raw)[dataset][0]


def test_direct_input_build_carries_cached_coverage_gaps(db):
    upsert_profile_row(
        db,
        "00700",
        "港股",
        "report_target_plan",
        "current",
        {
            "status": "ok",
            "targets": [
                {
                    "period_key": "20201231|annual",
                    "end_date": "20201231",
                    "report_type": "annual",
                    "url": "https://example.test/report.pdf",
                }
            ],
        },
    )
    db.commit()
    payload = jobs.build_analysis_input(db, "00700", "港股")
    assert any("20201231" in gap for gap in payload["report_digest_gaps"])
    assert "不得推断年报没有风险章节" in build_system_prompt("港股")


@pytest.mark.parametrize("currency", ["HKD", "USD", None])
def test_hk_official_values_are_not_competing_with_yahoo(currency):
    source = {
        "report_statements": [
            {
                "end_date": "20251231",
                "currency": currency,
                "n_cashflow_act": 10,
                "capex": 3,
                "money_cap": None,
            }
        ],
        "yahoo_fundamentals": [
            {
                "end_date": "20251231",
                "currency": "HKD",
                "n_cashflow_act": 12,
                "capex": 5,
                "money_cap": 8,
            }
        ],
    }
    saved = deepcopy(source)
    rows = jobs._compact_profile(source)["yahoo_fundamentals"]
    if currency == "HKD":
        assert rows == [{"end_date": "20251231", "currency": "HKD", "money_cap": 8}]
    else:
        assert rows == []
    assert source == saved


@pytest.mark.parametrize("value", ["68..66亿元", "1,23万元"])
def test_malformed_amounts_do_not_produce_false_comparisons(value):
    assert parse_amount(value) is None


def test_business_refresh_is_resumable_after_current_input_is_saved(db):
    import runpy
    from pathlib import Path

    seed_digest(db)
    stale_profiles = runpy.run_path(
        str(Path(__file__).parents[1] / "scripts/refresh_business_profiles.py")
    )["stale_profiles"]
    payload = business.build_business_profile_input(db, "603259", "A股")
    upsert_profile_row(
        db,
        "603259",
        "A股",
        "business_profile",
        "current",
        {
            "status": "ok",
            "prompt_version": PROFILE_PROMPT_VERSION,
            "input_fingerprint": business.input_fingerprint(payload),
            "profile": {"商业模式": "已核对输入"},
        },
    )
    db.commit()
    assert stale_profiles(db) == []
    assert business.load_business_profile(db, "603259", "A股", for_analysis=True)["profile"]
    # 报表修正让原先被隔离的摘要可用，画像必须重新生成而不能误判缓存命中。
    upsert_profile_row(
        db,
        "603259",
        "A股",
        "income",
        "20260630",
        {
            "end_date": "20260630",
            "total_revenue": 249.99e8,
        },
    )
    db.commit()
    assert stale_profiles(db) == [("603259", "A股")]
    assert business.load_business_profile(db, "603259", "A股", for_analysis=True)["profile"] is None


def test_old_digest_chapter_claim_is_limited_to_excerpt_coverage():
    source = {
        "风险要点": "原文节选未设专门风险章节，未披露主要风险因素。可确认的挑战包括：汇率波动、投资减值；2025年现金768亿元。",
    }
    saved = deepcopy(source)
    projected = digest_for_llm({"digest": source})["digest"]["风险要点"]
    assert "未设专门风险章节" not in projected
    assert "节选" in projected and "未核验" in projected
    assert "汇率波动" in projected and "投资减值" in projected and "现金768亿元" in projected
    assert source == saved


def test_positive_risk_description_is_preserved():
    text = "主要风险包括：客户需求下降可能导致无法消化产能，汇率波动。"
    assert digest_for_llm({"digest": {"风险要点": text}})["digest"]["风险要点"] == text


def test_all_quarantined_slices_are_not_sources_for_profile_generation(db, monkeypatch):
    seed_digest(db)

    def unexpected_completion(*args, **kwargs):
        raise AssertionError("被隔离的摘要不能充当商业画像来源")

    monkeypatch.setattr(business, "chat_completion", unexpected_completion)
    assert business.ensure_business_profile(db, "603259", "A股") is None
    assert not db.query(SecurityProfileData).filter_by(dataset="business_profile").all()
