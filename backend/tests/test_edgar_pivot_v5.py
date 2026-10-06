"""EDGAR 透视 v5：季度身份按期末日（#359）、拆股口径统一（#289）。

真实固件（公开数据，data.sec.gov companyfacts 2026-09-30 抓取，只留透视用到的概念、拆股比例
概念与透视读取的字段）：
- fixtures/edgar/sndk_companyfacts.json —— Sandisk（2025-02 分拆上市，52/53 周财年，6 月底结账）：
  上一季度的净利润在后续两份 10-Q 的权益变动表里以本期 fp 重复出现；
- fixtures/edgar/nflx_companyfacts.json（2012 年起）—— Netflix：2015-07 7:1 与 2025-11 10:1 两次拆股，
  FY2025 10-K 按新股本重述了 2023/2024 的 EPS，更早年份只有旧口径。
"""

import json
from pathlib import Path

import pytest

from app.services import edgar_facts, report_fetchers
from app.services import security_profile_service as svc
from app.services.earnings_quality import market_statements
from app.services.graham_screen import compute_graham_screen

FIXTURES = Path(__file__).parent / "fixtures" / "edgar"


def _facts(name):
    return json.loads((FIXTURES / f"{name}_companyfacts.json").read_text(encoding="utf-8"))


def _pivot(monkeypatch, facts, symbol="X"):
    monkeypatch.setattr(report_fetchers, "edgar_lookup", lambda s: {"cik": 1, "title": "x"})
    monkeypatch.setattr(report_fetchers, "edgar_companyfacts", lambda cik: facts)
    return svc.fetch_dataset_rows("edgar_companyfacts", symbol, "美股")


def _fact(end, val, *, fp, filed, start=None, form=None):
    item = {"end": end, "val": val, "fp": fp, "filed": filed, "form": form or "10-Q"}
    if start:
        item["start"] = start
    return item


# ---------------------------------------------------------------------------- 季度身份（#359）


def test_sndk_quarter_comparatives_collapse_into_one_row(monkeypatch):
    rows = _pivot(monkeypatch, _facts("sndk"), "SNDK")
    quarterly = [row for row in rows if row["fp"] != "FY"]
    ends = [row["end_date"] for row in quarterly]
    assert len(ends) == len(set(ends)), "同一期末日只留一行"
    by_end = {row["end_date"]: row for row in quarterly}
    q1 = by_end["20251003"]
    # 2025-10-03 结束的一季度：净利润在 Q1/Q2/Q3 三份 10-Q 里各出现一次，v4 透视成三行
    assert q1["fp"] == "Q1"
    assert q1["total_revenue"] == 2308000000 and q1["n_income_attr_p"] == 112000000
    # 标签按财年末推算：分拆后首份 10-Q 是 FY2025 Q2，其中 2024-09-27 那一季挂的 fp 是 Q2
    assert by_end["20240927"]["fp"] == "Q1"
    assert by_end["20241227"]["fp"] == "Q2"
    assert by_end["20260102"]["fp"] == "Q2" and by_end["20260403"]["fp"] == "Q3"
    # 腾出的额度给了真实季度：8 行都有营收
    assert len(quarterly) == edgar_facts.EDGAR_QUARTERLY_KEEP
    assert all(row.get("total_revenue") for row in quarterly[:7])


def test_quarter_label_falls_back_to_earliest_filing_without_fiscal_year_end(monkeypatch):
    """没有年度事实推不出季度序号：取最早报告这一季度单季数的申报的 fp。"""
    facts = {
        "facts": {
            "us-gaap": {
                "NetIncomeLoss": {
                    "units": {
                        "USD": [
                            _fact(
                                "2026-03-31", 10.0, fp="Q1", filed="2026-05-01", start="2026-01-01"
                            ),
                            # 后续 10-Q 的比较数挂着本期 fp
                            _fact(
                                "2026-03-31", 10.0, fp="Q2", filed="2026-08-01", start="2026-01-01"
                            ),
                            _fact(
                                "2026-06-30", 12.0, fp="Q2", filed="2026-08-01", start="2026-04-01"
                            ),
                        ]
                    }
                }
            }
        }
    }
    rows = _pivot(monkeypatch, facts)
    assert [(row["end_date"], row["fp"]) for row in rows] == [
        ("20260630", "Q2"),
        ("20260331", "Q1"),
    ]


def test_later_comparative_restatement_wins_within_merged_row(monkeypatch):
    """同一季度的比较数被后续申报重述：并入同一行后 filed 最新者胜（与年度行一致）。"""
    fy = _fact("2025-12-31", 400.0, fp="FY", filed="2026-02-01", start="2025-01-01", form="10-K")
    facts = {
        "facts": {
            "us-gaap": {
                "NetIncomeLoss": {
                    "units": {
                        "USD": [
                            fy,
                            _fact(
                                "2026-03-31", 10.0, fp="Q1", filed="2026-05-01", start="2026-01-01"
                            ),
                            _fact(
                                "2026-03-31", 11.0, fp="Q2", filed="2026-08-01", start="2026-01-01"
                            ),
                        ]
                    }
                }
            }
        }
    }
    rows = {(row["end_date"], row["fp"]): row for row in _pivot(monkeypatch, facts)}
    assert set(rows) == {("20260331", "Q1"), ("20251231", "FY")}
    assert rows[("20260331", "Q1")]["n_income_attr_p"] == 11.0


# ---------------------------------------------------------------------------- 拆股口径（#289）


def test_nflx_eps_unified_to_post_split_basis_with_evidence(monkeypatch):
    rows = _pivot(monkeypatch, _facts("nflx"), "NFLX")
    annual = {row["end_date"][:4]: row for row in rows if row["fp"] == "FY"}
    # FY2022 只有拆股前申报（10.1 / 9.95）：按 10:1 折算，原值与依据留在行上
    fy22 = annual["2022"]
    assert fy22["basic_eps"] == 1.01 and fy22["diluted_eps"] == 0.995
    adjustment = fy22["eps_split_adjustment"]
    assert adjustment["original"] == {"basic_eps": 10.1, "diluted_eps": 9.95}
    assert adjustment["factor"] == 10.0
    (event,) = adjustment["events"]
    assert event["effective_date"] == "2025-11-14"
    assert event["basis"] == "ratio_fact+restated"
    # 证据是原文：同一期 EPS 在拆股前后两份年报里的值（FY2025 10-K 重述了 2023 年）
    assert event["evidence"]["before"]["value"] / event["evidence"]["after"]["value"] > 9.9
    # FY2023/2024 最新申报已按新股本重述：不再调整
    assert annual["2023"]["basic_eps"] == 1.22 and "eps_split_adjustment" not in annual["2023"]
    assert annual["2024"]["basic_eps"] == 2.03 and "eps_split_adjustment" not in annual["2024"]
    assert annual["2025"]["basic_eps"] == 2.58
    # FY2014 的最新申报在 2015 年 7:1 拆股之后（已重述）：只折 2025 年这一次
    fy14 = annual["2014"]
    assert fy14["eps_split_adjustment"]["factor"] == 10.0
    assert [e["effective_date"] for e in fy14["eps_split_adjustment"]["events"]] == ["2025-11-14"]
    # 2025Q3 季报只有拆股前口径：同样折算（TTM 的上年同期季度会读到它）
    q3 = next(row for row in rows if row["end_date"] == "20250930")
    assert q3["basic_eps"] == 0.6 and q3["eps_split_adjustment"]["original"]["basic_eps"] == 6
    # 口径统一后，隐含股数（净利润 / EPS）逐年同一量级
    implied = [row["n_income_attr_p"] / row["basic_eps"] for row in annual.values()]
    assert max(implied) / min(implied) < 1.5


def test_nflx_graham_growth_uses_consistent_eps(monkeypatch):
    rows = _pivot(monkeypatch, _facts("nflx"), "NFLX")
    statements = market_statements(
        "美股", {"edgar_companyfacts": [r for r in rows if r["fp"] == "FY"]}
    )
    result = compute_graham_screen("美股", statements)
    growth = next(c for c in result["criteria"] if c["criterion"] == "earnings_growth")
    # v4 混口径：首年 2016 年 0.44（旧股本）→ 2025 年 2.58（新股本），被算成 +486.4%；
    # 统一口径后首年 0.044，十年增长 +5763.6%
    assert growth["verdict"] == "pass"
    assert growth["value"] == pytest.approx((2.58 / 0.044 - 1) * 100, abs=0.01)
    assert growth["reason"].startswith("2016→2025 每股盈利累计增幅 5763.6%")


def _split_facts(*, ratio_fact=True, restated_periods=("2024-12-31", "2023-12-31")):
    eps = [
        _fact("2024-12-31", 20.0, fp="FY", filed="2025-02-01", start="2024-01-01", form="10-K"),
        _fact("2023-12-31", 12.0, fp="FY", filed="2025-02-01", start="2023-01-01", form="10-K"),
        _fact("2022-12-31", 8.0, fp="FY", filed="2024-02-01", start="2022-01-01", form="10-K"),
        _fact("2025-12-31", 3.0, fp="FY", filed="2026-02-01", start="2025-01-01", form="10-K"),
    ]
    for end in restated_periods:
        before = next(i for i in eps if i["end"] == end)["val"]
        eps.append(
            _fact(
                end, before / 10, fp="FY", filed="2026-02-01", start=f"{end[:4]}-01-01", form="10-K"
            )
        )
    us_gaap = {"EarningsPerShareBasic": {"units": {"USD/shares": eps}}}
    if ratio_fact:
        us_gaap["StockholdersEquityNoteStockSplitConversionRatio1"] = {
            "units": {"pure": [{"end": "2025-11-14", "val": 10, "fp": "FY", "filed": "2026-02-01"}]}
        }
    return {"facts": {"us-gaap": us_gaap}}


def test_split_ratio_fact_without_restatement_only_annotates(monkeypatch):
    rows = {r["end_date"]: r for r in _pivot(monkeypatch, _split_facts(restated_periods=()))}
    fy22 = rows["20221231"]
    assert fy22["basic_eps"] == 8.0 and "eps_split_adjustment" not in fy22
    note = fy22["eps_basis_unverified"]["basic_eps"]
    assert note["split_ratio_facts"] == [{"effective_date": "2025-11-14", "ratio": 10.0}]
    assert "未获重述数据证实" in note["note"]


def test_restatement_only_evidence_needs_two_clean_periods(monkeypatch):
    two = {r["end_date"]: r for r in _pivot(monkeypatch, _split_facts(ratio_fact=False))}
    assert two["20221231"]["basic_eps"] == 0.8
    (event,) = two["20221231"]["eps_split_adjustment"]["events"]
    assert event["basis"] == "restated" and event["effective_date"] is None
    one = {
        r["end_date"]: r
        for r in _pivot(
            monkeypatch, _split_facts(ratio_fact=False, restated_periods=("2023-12-31",))
        )
    }
    assert one["20221231"]["basic_eps"] == 8.0 and "eps_split_adjustment" not in one["20221231"]


def test_non_split_restatement_is_ignored(monkeypatch):
    """比值不干净（非整数倍）的重述是业务重述（如终止经营），不是拆股。"""
    facts = _split_facts(ratio_fact=False)
    for item in facts["facts"]["us-gaap"]["EarningsPerShareBasic"]["units"]["USD/shares"]:
        if item["filed"] == "2026-02-01" and item["end"] < "2025-01-01":
            item["val"] = round(item["val"] * 4.2, 4)  # 20 → 8.4、12 → 5.04：比值 2.38
    rows = {r["end_date"]: r for r in _pivot(monkeypatch, facts)}
    assert rows["20221231"]["basic_eps"] == 8.0 and "eps_split_adjustment" not in rows["20221231"]


# ---------------------------------------------------------------------------- 多次拆股（PR #381 评审）


def _eps(end, val, filed, *, fp="FY"):
    """日历财年的 EPS 事实：FY 为全年，季度为当季三个月。"""
    if fp == "FY":
        start, form = f"{end[:4]}-01-01", "10-K"
    else:
        first_month = {"03": "01", "06": "04", "09": "07", "12": "10"}[end[5:7]]
        start, form = f"{end[:4]}-{first_month}-01", "10-Q"
    return _fact(end, val, fp=fp, filed=filed, start=start, form=form)


def _ratio(end, val):
    return {"end": end, "val": val, "fp": "FY", "filed": "2026-02-01", "form": "10-K"}


def _two_split_facts(*, ratio_facts=True, extra_eps=()):
    """评审复现：2025-03-01 的 2:1 与 2025-09-01 的 3:1 两次拆股。

    - 2024Q1 EPS 6 → 3（2024-05-01 → 2025-05-01 两份申报之间只有第一次）证实 2:1；
    - 2025Q1 EPS 9 → 3（2025-05-01 → 2026-05-01 之间只有第二次）证实 3:1；
    - FY2023 12 → 2、FY2024 18 → 3（2025-02-01 → 2026-02-01，跨两次）是累计 6 倍；
    - FY2022 EPS 60 只在 2025-02-01 申报过：应折 6 倍为 10，不能折 36 倍。"""
    eps = [
        _eps("2024-03-31", 6.0, "2024-05-01", fp="Q1"),
        _eps("2024-03-31", 3.0, "2025-05-01", fp="Q1"),
        _eps("2025-03-31", 9.0, "2025-05-01", fp="Q1"),
        _eps("2025-03-31", 3.0, "2026-05-01", fp="Q1"),
        _eps("2023-12-31", 12.0, "2025-02-01"),
        _eps("2023-12-31", 2.0, "2026-02-01"),
        _eps("2024-12-31", 18.0, "2025-02-01"),
        _eps("2024-12-31", 3.0, "2026-02-01"),
        _eps("2022-12-31", 60.0, "2025-02-01"),
        _eps("2025-12-31", 4.0, "2026-02-01"),
        *extra_eps,
    ]
    us_gaap = {"EarningsPerShareBasic": {"units": {"USD/shares": eps}}}
    if ratio_facts:
        us_gaap["StockholdersEquityNoteStockSplitConversionRatio1"] = {
            "units": {"pure": [_ratio("2025-03-01", 2), _ratio("2025-09-01", 3)]}
        }
    return {"facts": {"us-gaap": us_gaap}}


def test_two_splits_cumulative_restatement_is_not_a_third_split(monkeypatch):
    rows = {(r["end_date"], r["fp"]): r for r in _pivot(monkeypatch, _two_split_facts())}
    fy22 = rows[("20221231", "FY")]
    assert fy22["basic_eps"] == 10.0  # 60 / (2 × 3)，不是 60 / 36 = 1.666667
    adjustment = fy22["eps_split_adjustment"]
    assert adjustment["factor"] == 6.0
    assert [(e["effective_date"], e["factor"]) for e in adjustment["events"]] == [
        ("2025-03-01", 2),
        ("2025-09-01", 3),
    ]
    # 两次拆股各由只跨它自己的那笔重述证实（逐次独立）
    evidence = {e["effective_date"]: e["evidence"]["period"] for e in adjustment["events"]}
    assert evidence == {"2025-03-01": "20240331|Q1", "2025-09-01": "20250331|Q1"}
    # 已按新股本重述过的值不动
    assert rows[("20231231", "FY")]["basic_eps"] == 2.0
    assert "eps_split_adjustment" not in rows[("20231231", "FY")]
    assert rows[("20241231", "FY")]["basic_eps"] == 3.0


def test_value_restated_for_first_split_only_gets_later_split(monkeypatch):
    """被后续申报按第一次拆股重述过的值（2025-05-01 申报的 2024Q1 = 3）只补第二次（÷3）。"""
    rows = {(r["end_date"], r["fp"]): r for r in _pivot(monkeypatch, _two_split_facts())}
    # 2024Q1 最新申报是 2025-05-01（第一次之后、第二次之前）：3 → 1
    q1_24 = rows[("20240331", "Q1")]
    assert q1_24["basic_eps"] == 1.0
    assert [e["effective_date"] for e in q1_24["eps_split_adjustment"]["events"]] == ["2025-09-01"]
    # 2025Q1 最新申报 2026-05-01（两次之后）：不动
    assert rows[("20250331", "Q1")]["basic_eps"] == 3.0


def test_cumulative_pair_does_not_confirm_when_other_split_unproven(monkeypatch):
    """跨两次拆股的累计重述，只有另一次已被独立证实时才能证实这一次；两次都只有累计证据 →
    都不证实，只标注，不折算。"""
    facts = _two_split_facts()
    eps = facts["facts"]["us-gaap"]["EarningsPerShareBasic"]["units"]["USD/shares"]
    # 去掉两笔只跨单次拆股的季度重述
    facts["facts"]["us-gaap"]["EarningsPerShareBasic"]["units"]["USD/shares"] = [
        item for item in eps if item["fp"] == "FY"
    ]
    rows = {(r["end_date"], r["fp"]): r for r in _pivot(monkeypatch, facts)}
    fy22 = rows[("20221231", "FY")]
    assert fy22["basic_eps"] == 60.0 and "eps_split_adjustment" not in fy22
    assert [
        f["effective_date"] for f in fy22["eps_basis_unverified"]["basic_eps"]["split_ratio_facts"]
    ] == [
        "2025-03-01",
        "2025-09-01",
    ]


def test_independent_later_split_without_ratio_fact_still_found(monkeypatch):
    """累计比值先用已证实事件解释，剩下的干净比值仍能证实一次真正独立的新拆股（5:1，无比例事实）。"""
    extra = (
        _eps("2025-12-31", 20.0, "2026-06-01"),  # 被第三次拆股前的申报再报一次
        _eps("2025-12-31", 4.0, "2027-02-01"),
        _eps("2026-03-31", 10.0, "2026-06-01", fp="Q1"),
        _eps("2026-03-31", 2.0, "2027-02-01", fp="Q1"),
    )
    facts = _two_split_facts(extra_eps=extra)
    # 2025-12-31 原值 4.0（2026-02-01）与 2026-06-01 的 20.0 冲突不合常理，改成先 20 后 4：
    eps = facts["facts"]["us-gaap"]["EarningsPerShareBasic"]["units"]["USD/shares"]
    facts["facts"]["us-gaap"]["EarningsPerShareBasic"]["units"]["USD/shares"] = [
        item for item in eps if not (item["end"] == "2025-12-31" and item["filed"] == "2026-02-01")
    ]
    rows = {(r["end_date"], r["fp"]): r for r in _pivot(monkeypatch, facts)}
    fy22 = rows[("20221231", "FY")]
    # 2:1、3:1 两次有比例事实；第三次 5:1 只有两期重述证据：60 / (2 × 3 × 5) = 2
    assert fy22["basic_eps"] == 2.0
    bases = [(e["basis"], e["factor"]) for e in fy22["eps_split_adjustment"]["events"]]
    assert bases == [("ratio_fact+restated", 2), ("ratio_fact+restated", 3), ("restated", 5.0)]
    # 累计 6 倍的两笔重述已被解释，没有被当成第四次拆股
    assert fy22["eps_split_adjustment"]["factor"] == 30.0


def test_nflx_two_real_splits_confirmed_once_each(monkeypatch):
    """NFLX 真实数据：2015 年 7:1（三个日期归为一次）与 2025 年 10:1，各由自己的重述证实。"""
    facts = edgar_facts.normalize_quarter_periods(_facts("nflx")["facts"]["us-gaap"])
    confirmed, unconfirmed = edgar_facts.edgar_split_events(facts, lambda key: "USD")
    assert unconfirmed == []
    assert [(e["effective_date"], e["factor"], e["basis"]) for e in confirmed] == [
        ("2015-06-23", 7.0, "ratio_fact+restated"),
        ("2025-11-14", 10.0, "ratio_fact+restated"),
    ]
    assert confirmed[0]["evidence"]["period"] == "20131231|FY"
    assert confirmed[0]["evidence"]["before"]["value"] == 1.93
    assert confirmed[0]["evidence"]["after"]["value"] == 0.28
    # 拆股前申报的值折两次（7 × 10），7:1 之后、10:1 之前申报的值只折 10
    rows = {
        ("2013-12-31", "FY"): {"basic_eps": 1.93},
        ("2016-12-31", "FY"): {"basic_eps": 0.44},
    }
    marks = {
        (("2013-12-31", "FY"), "basic_eps"): (0, "2015-01-29"),
        (("2016-12-31", "FY"), "basic_eps"): (0, "2019-01-29"),
    }
    edgar_facts.apply_split_adjustments(rows, marks, confirmed, unconfirmed)
    assert rows[("2013-12-31", "FY")]["basic_eps"] == pytest.approx(1.93 / 70, abs=1e-6)
    assert rows[("2013-12-31", "FY")]["eps_split_adjustment"]["factor"] == 70.0
    assert rows[("2016-12-31", "FY")]["basic_eps"] == pytest.approx(0.044)
    assert rows[("2016-12-31", "FY")]["eps_split_adjustment"]["factor"] == 10.0
