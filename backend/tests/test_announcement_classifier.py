"""公告分类器（纯函数）金样：来自 2026-09 跟踪范围近 180 天真实公告（巨潮/披露易）的人工复核样本，
外加 EDGAR form/items 组合。改规则须同步复核本金样并 bump ANNOUNCEMENT_CLASSIFIER_VERSION。"""

import json
from pathlib import Path

import pytest

from app.services.announcement_classifier import (
    CATEGORIES,
    IMPORTANCE_ORDER,
    classify,
    normalize_text,
    representative_index,
)
from app.services.announcement_sources import parse_cninfo_rows

FIXTURES = Path(__file__).parent / "fixtures" / "announcements"
GOLDEN = json.loads((FIXTURES / "classifier_golden.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "case",
    GOLDEN,
    ids=[f"{c['market']}-{c['title'][:18]}-{c['category_raw'][:12]}" for c in GOLDEN],
)
def test_classifier_golden(case):
    result = classify(case["market"], case["title"], case["category_raw"])
    assert (result.category, result.importance) == (case["category"], case["importance"]), (
        result.rule_id
    )


def test_golden_covers_every_major_category():
    majors = {c["category"] for c in GOLDEN if c["importance"] == "major"}
    assert {
        "financing",
        "restructuring",
        "earnings_alert",
        "legal",
        "buyback",
        "shareholding",
    } <= majors
    assert {c["category"] for c in GOLDEN} <= set(CATEGORIES)
    assert set(IMPORTANCE_ORDER) == {"major", "normal", "minor"}


def test_hk_traditional_titles_are_normalized():
    assert (
        normalize_text("須予披露的交易 - 股份購回&#x2f;盈利預喜")
        == "须予披露的交易-股份购回/盈利预喜"
    )
    # 已发行股份变动的翌日披露报表不是「发行股份」（生产误判过）
    result = classify("港股", "翌日披露報表 - 已發行股份變動", "翌日披露報表 - [其他]")
    assert (result.category, result.importance) == ("governance", "minor")


def test_angel_yeast_convertible_bond_day_is_one_major_financing_group():
    """#306 起因：安琪酵母 2026-09 可转债预案一天 15 份文件，须归入同一个 major 融资组，
    代表标题取「预案」。"""
    body = json.loads((FIXTURES / "cninfo_600298_page.json").read_text(encoding="utf-8"))
    records = parse_cninfo_rows(body["announcements"], "600298", "A股")
    financing = [
        r for r in records if classify("A股", r["title"], r["category_raw"]).category == "financing"
    ]
    assert len(financing) >= 5
    assert all(
        classify("A股", r["title"], r["category_raw"]).importance in ("major", "normal")
        for r in financing
    )
    assert any(
        classify("A股", r["title"], r["category_raw"]).importance == "major" for r in financing
    )
    titles = [r["title"] for r in financing]
    assert "预案" in titles[representative_index(titles)]


def test_representative_prefers_the_main_document_over_companion_reports():
    """生产实测（2026-09-25 同步）：同组 6 份文件里「方案的论证分析报告」排在预案前面，
    只按「含方案/预案」判会把它选成代表。"""
    titles = [
        "安琪酵母股份有限公司向不特定对象发行可转换公司债券方案的论证分析报告",
        "安琪酵母股份有限公司向不特定对象发行可转换公司债券募集资金使用的可行性分析报告",
        "安琪酵母股份有限公司关于向不特定对象发行可转换公司债券预案披露的提示性公告",
        "安琪酵母股份有限公司向不特定对象发行可转换公司债券持有人会议规则（2026年9月）",
        "安琪酵母股份有限公司向不特定对象发行可转换公司债券预案",
    ]
    assert titles[representative_index(titles)].endswith("可转换公司债券预案")
    # 没有主文件时退到提示性公告，再退到论证报告
    assert "提示性公告" in titles[representative_index(titles[:4])]
    assert "论证" in titles[representative_index(titles[:1])]
    assert (
        representative_index(["回购股份实施进展的公告", "关于回购股份方案的公告暨回购报告书"]) == 1
    )
