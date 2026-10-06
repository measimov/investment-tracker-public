"""美股 EDGAR 按报告币种取数（PR-D）。

金样（公开数据，data.sec.gov companyfacts 2026-09-27 抓取，只留概念链用到的概念与透视读取的字段）：
- fixtures/edgar/pdd_companyfacts.json —— 拼多多 20-F：CNY 全序列 + USD 便利折算（仅各年本年）；
- fixtures/edgar/aapl_companyfacts.json —— 苹果 10-K：只有 USD（2023 年起的事实）；
- fixtures/edgar/aapl_pivot_golden.json —— **改动前**的透视器对上一份固件的输出。
"""

import json
from collections import namedtuple
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.timeutil import local_today
from app.database import SessionLocal
from app.models.security_price import SecurityPrice
from app.models.security_profile import SecurityProfileData
from app.services import report_fetchers
from app.services import profile_store
from app.services import security_profile_service as svc
from app.services.edgar_facts import EDGAR_PIVOT_VERSION, edgar_reporting_currency
from app.services.earnings_quality import (
    compute_earnings_quality,
    market_statements,
)
from app.services.graham_screen import compute_graham_screen, resolve_fx_rates
from app.services.portfolio.fx import ExchangeRateLookup

FIXTURES = Path(__file__).parent / "fixtures" / "edgar"
Rate = namedtuple("Rate", "from_currency to_currency rate effective_date")
USD_CNY = Decimal("6.7126")  # 生产汇率表 2026-09-25 的 USD→CNY


def _facts(name):
    return json.loads((FIXTURES / f"{name}_companyfacts.json").read_text(encoding="utf-8"))


def _pivot(monkeypatch, facts, symbol="PDD"):
    monkeypatch.setattr(report_fetchers, "edgar_lookup", lambda s: {"cik": 1, "title": "x"})
    monkeypatch.setattr(report_fetchers, "edgar_companyfacts", lambda cik: facts)
    return svc.fetch_dataset_rows("edgar_companyfacts", symbol, "美股")


def _item(end, val, *, fp="FY", start=None):
    item = {"end": end, "fp": fp, "form": "20-F", "filed": "2026-04-29", "val": val}
    if start:
        item["start"] = start
    return item


def _by(result, key):
    return next(item for item in result["criteria"] if item["criterion"] == key)


# ---------------------------------------------------------------------------- 透视


# 只有时点（资产负债）科目的字段：v4 起季度行只剩这些字段即是 10-Q 比较资产负债表的占位行
_INSTANT_FIELDS = {
    "total_assets",
    "total_liab",
    "total_hldr_eqy_exc_min_int",
    "money_cap",
    "accounts_receiv",
    "inventories",
    "total_cur_assets",
    "total_cur_liab",
    "lt_debt",
    "fix_assets",
}
_ROW_META = {"end_date", "fp", "form", "currency", "edgar_chain_version", "edgar_missing_reasons"}


def _is_quarter_placeholder(row):
    return row["fp"] != "FY" and set(row) - _ROW_META <= _INSTANT_FIELDS


def test_usd_issuer_pivot_unchanged_against_pre_change_golden(monkeypatch):
    """[回归锁] 美国本土 10-K 发行人只有 USD 单位：与 v3 改动前的金样相比，唯一差别是
    季度占位行（只有时点事实，#351-2）不再生成——其余行除版本标记外逐字节一致。

    金样是 v3 生产输出，保持原样不重生成：差异由这里显式剔除，读测试就知道改了什么。"""
    rows = _pivot(monkeypatch, _facts("aapl"), "AAPL")
    golden = json.loads((FIXTURES / "aapl_pivot_golden.json").read_text(encoding="utf-8"))
    assert {row.pop("edgar_chain_version") for row in rows} == {EDGAR_PIVOT_VERSION}
    for row in golden:
        row.pop("edgar_chain_version")
    placeholders = [(r["end_date"], r["fp"]) for r in golden if _is_quarter_placeholder(r)]
    # 金样里的五行占位：Q3 10-Q 的上季末权益、Q2 10-Q 的上季末权益、三份 10-Q 的上年末资产负债表
    assert placeholders == [
        ("20260328", "Q3"),
        ("20251227", "Q2"),
        ("20250927", "Q1"),
        ("20250927", "Q2"),
        ("20250927", "Q3"),
    ]
    assert not any(_is_quarter_placeholder(row) for row in rows)
    kept = {(r["end_date"], r["fp"]): r for r in golden if not _is_quarter_placeholder(r)}
    new = {(r["end_date"], r["fp"]): r for r in rows}
    # 金样里的真实行一行不少、逐字节一致
    for key, row in kept.items():
        assert json.dumps(new[key], sort_keys=False) == json.dumps(row, sort_keys=False), key
    # 腾出的季度额度由更早的**真实**季度补上（有营收与净利）——上年同期季度回到窗口，TTM 可滚动
    added = sorted(set(new) - set(kept))
    assert added == [
        ("20240330", "Q2"),
        ("20240629", "Q3"),
        ("20241228", "Q1"),
        ("20250329", "Q2"),
        ("20250628", "Q3"),
    ]
    assert all(new[key]["total_revenue"] and new[key]["n_income_attr_p"] for key in added)
    quarterly = [row for row in rows if row["fp"] != "FY"]
    assert len(quarterly) == 8


def test_pdd_pivot_uses_cny_reporting_currency(monkeypatch):
    facts = _facts("pdd")
    assert edgar_reporting_currency(facts["facts"]["us-gaap"]) == "CNY"
    rows = _pivot(monkeypatch, facts)
    assert {row["currency"] for row in rows} == {"CNY"}
    annual = {row["end_date"][:4]: row for row in rows if row["fp"] == "FY"}
    # 十个财年都有营收（此前 USD 口径只有 2018 起 8 年，且逐年折算率不同）
    assert sorted(y for y, row in annual.items() if row.get("total_revenue")) == [
        str(y) for y in range(2016, 2026)
    ]
    latest = annual["2025"]
    assert latest["total_revenue"] == 431845713000  # 人民币原值，不是 USD 折算 61753116000
    assert latest["n_income_attr_p"] == 97842539000
    assert latest["basic_eps"] == 17.5  # CNY/shares，不是 USD/shares 的 2.5
    assert latest["total_assets"] == 630044327000
    # 分项求和同样按报告币种（营销费 + 管理费）
    assert latest["sga_exp"] == 133445665000
    # USD 便利折算没有的年份/科目在 CNY 下齐了：2021 可转债
    assert annual["2021"]["lt_debt"] == 11788907000
    # 2021 股东权益：CNY 值（USD 折算约为其 1/6.4）
    assert annual["2021"]["total_hldr_eqy_exc_min_int"] > 5 * 10**10


def test_reporting_currency_rules():
    both = {
        "Revenues": {
            "units": {
                "USD": [_item("2025-12-31", 1.0, start="2025-01-01")],
                "EUR": [_item("2025-12-31", 1.0, start="2025-01-01")],
            }
        }
    }
    assert edgar_reporting_currency(both) == "USD"  # 并列 → USD
    cny = {
        "NetIncomeLoss": {
            "units": {
                "CNY": [_item(f"{y}-12-31", 1.0, start=f"{y}-01-01") for y in (2024, 2025)],
                "USD": [_item("2025-12-31", 1.0, start="2025-01-01")],
            }
        },
        # 每股单位不参与判定
        "EarningsPerShareBasic": {
            "units": {
                "USD/shares": [
                    _item(f"{y}-12-31", 1.0, start=f"{y}-01-01") for y in range(2015, 2026)
                ],
            }
        },
    }
    assert edgar_reporting_currency(cny) == "CNY"
    # 单季事实、期间不符的 FY 事实不计
    quarterly = {
        "Assets": {
            "units": {
                "CNY": [_item("2025-12-31", 1.0, fp="Q3"), _item("2025-09-30", 1.0, fp="Q2")],
                "USD": [_item("2025-12-31", 1.0)],
            }
        }
    }
    assert edgar_reporting_currency(quarterly) == "USD"
    assert edgar_reporting_currency({}) == "USD"


def test_concept_missing_reporting_currency_is_left_empty_not_mixed(monkeypatch):
    """报告币种缺某概念时留空，不回退到 USD——一行只能有一个币种。"""
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "CNY": [
                            _item(f"{y}-12-31", 1000.0 * y, start=f"{y}-01-01")
                            for y in (2024, 2025)
                        ],
                        "USD": [_item("2025-12-31", 140.0, start="2025-01-01")],
                    }
                },
                "CostOfRevenue": {
                    "units": {"USD": [_item("2025-12-31", 80.0, start="2025-01-01")]}
                },
                "EarningsPerShareBasic": {
                    "units": {
                        "CNY/shares": [_item("2025-12-31", 7.0, start="2025-01-01")],
                        "USD/shares": [_item("2025-12-31", 1.0, start="2025-01-01")],
                    }
                },
            }
        }
    }
    row = _pivot(monkeypatch, facts)[0]
    assert row["currency"] == "CNY"
    assert row["total_revenue"] == 1000.0 * 2025
    assert row["basic_eps"] == 7.0
    assert row.get("cost_of_revenue") is None


# ---------------------------------------------------------------------------- 下游


def _pdd_rows(monkeypatch):
    return _pivot(monkeypatch, _facts("pdd"))


def test_pdd_earnings_quality_is_currency_neutral(monkeypatch):
    statements = market_statements("美股", {"edgar_companyfacts": _pdd_rows(monkeypatch)})
    quality = compute_earnings_quality(
        statements["income"],
        statements["balancesheet"],
        statements["cashflow"],
        statements["fina_indicator"],
    )
    assert quality["status"] == "ok"
    # 比率与币种无关，按同一行的人民币值算（窗口 max_years=8）
    assert quality["years"] == [str(y) for y in range(2025, 2017, -1)]
    assert quality["per_year"]["2025"]["cfo_ni_ratio"] is not None
    assert quality["cfo_ni_ratio_5y"] is not None


def _pdd_valuation(rows, close=77.57):
    lookup = ExchangeRateLookup([Rate("USD", "CNY", USD_CNY, date(2026, 1, 1))])
    return {
        "price": {
            "close": close,
            "currency": "USD",
            "date": "2026-09-25",
            "stale": False,
            "age_days": 2,
            "source": "tencent-kline",
        },
        "fx_rates": resolve_fx_rates({"CNY"}, "USD", date(2026, 9, 25), lookup),
        "interim_rows": [row for row in rows if row["fp"] != "FY"],
        "annual_form": "20-F",
        "share_ratio": 4,
        "share_ratio_note": "1 ADS = 4 股",
    }


def test_pdd_graham_pe_converts_cny_eps_to_usd_per_ads(monkeypatch):
    """#232 估值：CNY 每股 → USD（价格日汇率）× ADS 比例 4。

    预期：17.5 CNY / 6.7126 × 4 = 10.428 USD/ADS，PE = 77.57 / 10.428 ≈ 7.44
    （旧 USD 口径 = 77.57 / (2.5 × 4) = 7.76——折算率是 20-F 的便利汇率 6.99 而非价格日汇率）。
    """
    rows = _pdd_rows(monkeypatch)
    statements = market_statements("美股", {"edgar_companyfacts": rows})
    result = compute_graham_screen("美股", statements, valuation=_pdd_valuation(rows))
    pe = _by(result, "pe")
    eps_per_ads = 17.5 / float(USD_CNY) * 4
    assert pe["basis"]["label"] == "20251231 年报（20-F 发行人不披露季报）"
    assert pe["basis"]["eps_ttm"] == pytest.approx(eps_per_ads, rel=1e-5)
    assert pe["value"] == pytest.approx(77.57 / eps_per_ads, abs=1e-3)
    assert pe["value"] == pytest.approx(7.4385, abs=1e-3)
    assert pe["basis"]["components"][0]["currency"] == "CNY"
    pb = _by(result, "pb_or_product")
    # 每 ADS 净资产 = 人民币权益 / 隐含股数 × 4 折美元
    assert pb["basis"]["equity_currency"] == "CNY"
    assert pb["value"] is not None and pb["value"] > 0
    # 长期债务/流动比率在同一行同一币种内比较
    assert _by(result, "current_ratio")["verdict"] in ("pass", "fail")


@pytest.fixture
def db():
    session = SessionLocal()
    symbols = ("PDD",)

    def clean():
        session.query(SecurityProfileData).filter(SecurityProfileData.symbol.in_(symbols)).delete(
            synchronize_session=False
        )
        session.query(SecurityPrice).filter(SecurityPrice.symbol.in_(symbols)).delete(
            synchronize_session=False
        )
        session.commit()

    clean()
    try:
        yield session
    finally:
        clean()
        session.close()


def test_sync_replaces_old_usd_rows_and_graham_end_to_end(db, monkeypatch):
    # 旧 USD 透视行：一个与新 CNY 行同键（被 upsert 覆盖），一个只在旧透视里出现的键（须删除）
    profile_store.upsert_profile_row(
        db,
        "PDD",
        "美股",
        "edgar_companyfacts",
        "20251231|FY",
        {"end_date": "20251231", "fp": "FY", "currency": "USD", "basic_eps": 2.5},
    )
    profile_store.upsert_profile_row(
        db,
        "PDD",
        "美股",
        "edgar_companyfacts",
        "20141231|FY",
        {"end_date": "20141231", "fp": "FY", "currency": "USD"},
    )
    db.commit()
    facts = _facts("pdd")
    monkeypatch.setattr(report_fetchers, "edgar_lookup", lambda s: {"cik": 1, "title": "x"})
    monkeypatch.setattr(report_fetchers, "edgar_companyfacts", lambda cik: facts)

    result = svc.sync_symbol_profile(db, "PDD", "美股")
    assert result["failed"] == []
    stored = (
        db.query(SecurityProfileData)
        .filter(
            SecurityProfileData.symbol == "PDD",
            SecurityProfileData.dataset == "edgar_companyfacts",
        )
        .all()
    )
    assert {row.payload["currency"] for row in stored} == {"CNY"}
    assert "20141231|FY" not in {row.period_key for row in stored}
    assert all(row.payload["edgar_chain_version"] == EDGAR_PIVOT_VERSION for row in stored)

    db.add(
        SecurityPrice(
            symbol="PDD",
            market="美股",
            price_date=local_today() - timedelta(days=2),
            currency="USD",
            close_price=Decimal("77.57"),
            source="tencent-kline",
        )
    )
    db.commit()
    lookup = ExchangeRateLookup([Rate("USD", "CNY", USD_CNY, date(2026, 1, 1))])
    monkeypatch.setattr(svc, "_rate_lookup_for", lambda _db, _markets: lookup)
    # ADS 换算比来自 20-F 封面解析（ads_ratio_service.ensure_ads_ratio 落库的形状）
    profile_store.upsert_profile_row(
        db,
        "PDD",
        "美股",
        "ads_ratio",
        "current",
        {
            "status": "ok",
            "ratio": "4",
            "section": "cover",
            "form": "20-F",
            "filing_date": "2026-04-29",
        },
    )
    db.commit()
    pe = _by(svc.compute_graham_for(db, "PDD", "美股"), "pe")
    assert pe["basis"]["share_ratio"] == 4
    assert pe["value"] == pytest.approx(77.57 / (17.5 / float(USD_CNY) * 4), abs=1e-3)


def test_sync_keeps_rows_when_fetch_is_empty(db, monkeypatch):
    profile_store.upsert_profile_row(
        db,
        "PDD",
        "美股",
        "edgar_companyfacts",
        "20141231|FY",
        {"end_date": "20141231", "fp": "FY", "currency": "USD"},
    )
    db.commit()
    monkeypatch.setattr(svc, "fetch_dataset_rows", lambda *args: [])
    svc.sync_symbol_profile(db, "PDD", "美股")
    assert db.query(SecurityProfileData).filter(SecurityProfileData.symbol == "PDD").count() == 1


# ---------------------------------------------------------------------------- 缺失原因（评审 P2）


def _cny_issuer(**extra):
    """两年 CNY 营收 → 报告币种 CNY；两年都有 CNY 经营现金流与流动项。"""

    def duration(concept, val):
        return {
            concept: {
                "units": {
                    "CNY": [_item(f"{y}-12-31", val, start=f"{y}-01-01") for y in (2024, 2025)]
                }
            }
        }

    concepts = {
        **duration("Revenues", 1000.0),
        **duration("NetIncomeLoss", 100.0),
        **duration("NetCashProvidedByUsedInOperatingActivities", 100.0),
        "AssetsCurrent": {"units": {"CNY": [_item(f"{y}-12-31", 500.0) for y in (2024, 2025)]}},
        "LiabilitiesCurrent": {
            "units": {"CNY": [_item(f"{y}-12-31", 200.0) for y in (2024, 2025)]}
        },
    }
    concepts.update(extra)
    return {"facts": {"us-gaap": concepts}}


def _screen(monkeypatch, facts):
    """真实取数（透视器）→ market_statements → compute_graham_screen。"""
    rows = _pivot(monkeypatch, facts)
    statements = market_statements("美股", {"edgar_companyfacts": rows})
    return rows, statements, compute_graham_screen("美股", statements)


def _latest(rows):
    return next(row for row in rows if row["end_date"] == "20251231" and row["fp"] == "FY")


def test_dividend_only_in_other_currency_stays_unknown(monkeypatch):
    """PaymentsOfDividends 只有 USD（无 CNY 单位）：不混币所以留空，但不能推断为「未列 → 0」
    去否定已披露的分红。"""
    rows, statements, result = _screen(
        monkeypatch,
        _cny_issuer(
            PaymentsOfDividends={"units": {"USD": [_item("2025-12-31", 10.0, start="2025-01-01")]}},
        ),
    )
    latest = _latest(rows)
    assert latest["currency"] == "CNY" and latest.get("div_paid_owners") is None
    assert latest["edgar_missing_reasons"]["div_paid_owners"] == "other_currency_only"
    # 该概念整条没有 CNY 单位：2024 的空值同样不可知
    assert {row["div_paid_status"] for row in statements["cashflow"]} == {"other_currency_only"}
    item = _by(result, "dividend_record")
    assert item["verdict"] == "indeterminate"
    assert "只以非报告币种披露" in item["reason"]


def test_recent_dividend_only_in_other_currency_does_not_count_as_interrupted(monkeypatch):
    """与 #232「近期未知不判中断」衔接：2016-2023 以 CNY 报过分红，2024-2025 的股息事实只有 USD
    → 这两年是「未知」而非 not_listed，锚定窗口内有未知年份 → indeterminate，不判「已中断」。"""
    years = range(2016, 2026)

    def duration(values):
        return [_item(f"{y}-12-31", v, start=f"{y}-01-01") for y, v in values]

    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"CNY": duration((y, 1000.0) for y in years)}},
                "NetIncomeLoss": {"units": {"CNY": duration((y, 100.0) for y in years)}},
                "NetCashProvidedByUsedInOperatingActivities": {
                    "units": {"CNY": duration((y, 120.0) for y in years)},
                },
                "PaymentsOfDividends": {
                    "units": {
                        "CNY": duration((y, 30.0) for y in range(2016, 2024)),
                        "USD": duration((y, 4.0) for y in (2024, 2025)),
                    }
                },
            }
        }
    }
    rows, statements, result = _screen(monkeypatch, facts)
    status = {row["end_date"][:4]: row["div_paid_status"] for row in statements["cashflow"]}
    assert status["2023"] == "reported"
    assert status["2024"] == status["2025"] == "other_currency_only"
    item = _by(result, "dividend_record")
    assert item["verdict"] == "indeterminate"
    assert "已中断" not in item["reason"]
    assert "2024、2025 年已付股息只以非报告币种披露" in item["reason"]


def test_dividend_concept_absent_in_all_units_still_counts_as_zero(monkeypatch):
    """对照：该发行人所有单位里都没有股息概念 → 仍按「现金流量表未列 → 0」。"""
    rows, _, result = _screen(monkeypatch, _cny_issuer())
    assert all("edgar_missing_reasons" not in row for row in rows)
    item = _by(result, "dividend_record")
    assert item["verdict"] == "fail"
    assert "现金流量表均未列已付股东股息" in item["reason"]


def test_lt_debt_only_in_other_currency_is_not_zero(monkeypatch):
    """2024 报过 CNY 长期债务，2025 只有 USD 的可转债事实：不得走「本期缺概念 → 0」。"""
    rows, _, result = _screen(
        monkeypatch,
        _cny_issuer(
            LongTermDebtNoncurrent={"units": {"CNY": [_item("2024-12-31", 50.0)]}},
            ConvertibleDebtNoncurrent={"units": {"USD": [_item("2025-12-31", 7.0)]}},
        ),
    )
    latest = _latest(rows)
    assert latest.get("lt_debt") is None
    assert latest["edgar_missing_reasons"]["lt_debt"] == "other_currency_only"
    item = _by(result, "lt_debt_vs_net_current_assets")
    assert item["verdict"] == "indeterminate"
    assert "只以非报告币种披露" in item["reason"]


def test_lt_debt_other_currency_in_another_period_only_keeps_zero_inference(monkeypatch):
    """别币种事实只落在别的期间、且概念本身有报告币种单位：本期空值仍是「没报」→ 0。"""
    _, _, result = _screen(
        monkeypatch,
        _cny_issuer(
            LongTermDebtNoncurrent={
                "units": {
                    "CNY": [_item("2024-12-31", 50.0)],
                    "USD": [_item("2024-12-31", 7.0)],
                }
            },
        ),
    )
    item = _by(result, "lt_debt_vs_net_current_assets")
    assert item["verdict"] == "pass"
    assert "往年报过" in item["reason"]


def test_lt_debt_concept_absent_this_period_still_counts_as_zero(monkeypatch):
    """对照：往年报过、本期所有单位都没有 → 仍视为已清偿按 0。"""
    _, _, result = _screen(
        monkeypatch,
        _cny_issuer(
            LongTermDebtNoncurrent={"units": {"CNY": [_item("2024-12-31", 50.0)]}},
        ),
    )
    item = _by(result, "lt_debt_vs_net_current_assets")
    assert item["verdict"] == "pass"
    assert "往年报过" in item["reason"]


def test_pdd_real_facts_have_no_other_currency_gaps_on_graham_fields(monkeypatch):
    """PDD 真实数据：每个 USD 事实都有 CNY 对应，股息/长债的空值都是真的「没报」。"""
    for row in _pdd_rows(monkeypatch):
        reasons = row.get("edgar_missing_reasons") or {}
        assert "div_paid_owners" not in reasons and "lt_debt" not in reasons


# ---------------------------------------------------------------------------- v4（#351）


def _fy(end, val, *, filed, form="20-F"):
    return {
        "end": end,
        "start": f"{end[:4]}-01-01",
        "fp": "FY",
        "form": form,
        "filed": filed,
        "val": val,
    }


def _instant(end, val, *, filed, fp="FY", form="20-F"):
    return {"end": end, "fp": fp, "form": form, "filed": filed, "val": val}


def _q1(end, start, val, *, filed):
    return {"end": end, "start": start, "fp": "Q1", "form": "10-Q", "filed": filed, "val": val}


def _switching_issuer():
    """2014–2022 报 CNY、FY2023 起改报 USD 的 20-F 发行人（#351 复现）。

    每份年报带两年比较数：改币后第一份（FY2023，2024 年申报）把 2021–2022 重述成 USD。
    CNY 期数（9 年）多于 USD（5 年），v3 全史取 CNY，FY2023–2025 整行消失。"""
    revenue_cny = [_fy(f"{y}-12-31", 1000.0 * y, filed=f"{y + 1}-04-20") for y in range(2014, 2023)]
    eps_cny = [_fy(f"{y}-12-31", 7.0, filed=f"{y + 1}-04-20") for y in range(2014, 2023)]
    ni_cny = [_fy(f"{y}-12-31", 100.0 * y, filed=f"{y + 1}-04-20") for y in range(2014, 2023)]
    revenue_usd, eps_usd = [], []
    for filing_year in range(2024, 2027):  # FY2023–FY2025 三份 USD 年报
        filed = f"{filing_year}-04-20"
        for y in range(filing_year - 3, filing_year):
            revenue_usd.append(_fy(f"{y}-12-31", 150.0 * y, filed=filed))
            eps_usd.append(_fy(f"{y}-12-31", 0.5 * (y - 2021), filed=filed))
    return {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"CNY": revenue_cny, "USD": revenue_usd}},
                "NetIncomeLoss": {"units": {"CNY": ni_cny}},
                "EarningsPerShareBasic": {"units": {"CNY/shares": eps_cny, "USD/shares": eps_usd}},
            }
        }
    }


def test_currency_switch_keeps_latest_years_in_new_currency(monkeypatch):
    facts = _switching_issuer()
    # 全史口径仍是 CNY（9 年 vs 5 年）——这正是 v3 丢掉最新年度的原因
    assert edgar_reporting_currency(facts["facts"]["us-gaap"]) == "CNY"
    rows = _pivot(monkeypatch, facts, "SWCH")
    annual = {row["end_date"][:4]: row for row in rows if row["fp"] == "FY"}
    assert max(annual) == "2025"
    latest = annual["2025"]
    assert latest["currency"] == "USD"
    assert latest["basic_eps"] == 2.0 and latest["total_revenue"] == 150.0 * 2025
    # 改币后的比较数随新币种（最新申报胜）；更早年份仍是 CNY 原值
    assert [annual[y]["currency"] for y in ("2021", "2022", "2023", "2024")] == ["USD"] * 4
    assert annual["2020"]["currency"] == "CNY"
    assert annual["2020"]["total_revenue"] == 1000.0 * 2020 and annual["2020"]["basic_eps"] == 7.0
    # 行内不混币：USD 行没有 CNY 才有的净利，且如实标「别币种才有」
    assert annual["2022"].get("n_income_attr_p") is None
    assert annual["2022"]["edgar_missing_reasons"]["n_income_attr_p"] == "other_currency_only"


def test_convenience_translation_does_not_flip_latest_year(monkeypatch):
    """同一份申报里的美元便利折算只折本年：报告币种覆盖更多期间，最新年度仍取报告币种。"""
    filed = "2026-04-29"
    cny = [_fy(f"{y}-12-31", 7000.0, filed=filed) for y in (2023, 2024, 2025)]
    facts = {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"CNY": cny, "USD": [_fy("2025-12-31", 1000.0, filed=filed)]}}
            }
        }
    }
    rows = _pivot(monkeypatch, facts)
    assert {row["currency"] for row in rows} == {"CNY"}
    assert rows[0]["total_revenue"] == 7000.0


def _quarter_facts():
    return {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            _fy("2025-12-31", 400.0, filed="2026-02-20", form="10-K"),
                            _q1("2026-03-31", "2026-01-01", 110.0, filed="2026-05-01"),
                        ]
                    }
                },
                "Assets": {
                    "units": {
                        "USD": [
                            _instant("2025-12-31", 900.0, filed="2026-02-20", form="10-K"),
                            _instant("2026-03-31", 950.0, filed="2026-05-01", fp="Q1", form="10-Q"),
                            # 同一份 10-Q 的上年末比较列：fp 是 Q1，期末却是上年末
                            _instant("2025-12-31", 900.0, filed="2026-05-01", fp="Q1", form="10-Q"),
                        ]
                    }
                },
            }
        }
    }


def test_quarter_rows_with_only_instant_facts_are_not_generated(monkeypatch):
    """10-Q 比较资产负债表的占位行（#351-2）不再生成，不再占季度额度。"""
    rows = _pivot(monkeypatch, _quarter_facts(), "QTR")
    assert [(row["end_date"], row["fp"]) for row in rows] == [
        ("20260331", "Q1"),
        ("20251231", "FY"),
    ]
    assert rows[0]["total_assets"] == 950.0 and rows[0]["total_revenue"] == 110.0
    assert rows[1]["total_assets"] == 900.0


def test_eps_falls_back_to_combined_basic_and_diluted_tag(monkeypatch):
    filed = "2026-02-20"
    facts = {
        "facts": {
            "us-gaap": {
                "NetIncomeLoss": {
                    "units": {
                        "USD": [
                            _fy("2025-12-31", -50.0, filed=filed, form="10-K"),
                            _fy("2024-12-31", -40.0, filed=filed, form="10-K"),
                        ]
                    }
                },
                "EarningsPerShareBasicAndDiluted": {
                    "units": {
                        "USD/shares": [
                            _fy("2025-12-31", -0.5, filed=filed, form="10-K"),
                            _fy("2024-12-31", -0.4, filed=filed, form="10-K"),
                        ]
                    }
                },
                # 某年单独打了基本 EPS：专用概念优先
                "EarningsPerShareBasic": {
                    "units": {"USD/shares": [_fy("2024-12-31", -0.41, filed=filed, form="10-K")]}
                },
            }
        }
    }
    rows = {row["end_date"]: row for row in _pivot(monkeypatch, facts, "LOSS")}
    assert rows["20251231"]["basic_eps"] == -0.5 and rows["20251231"]["diluted_eps"] == -0.5
    assert rows["20241231"]["basic_eps"] == -0.41 and rows["20241231"]["diluted_eps"] == -0.4


def _dividend_facts(**concepts):
    filed = "2026-02-20"
    base = {
        "NetCashProvidedByUsedInOperatingActivities": {
            "units": {"USD": [_fy("2025-12-31", 500.0, filed=filed, form="10-K")]}
        }
    }
    for name, value in concepts.items():
        base[name] = {"units": {"USD": [_fy("2025-12-31", value, filed=filed, form="10-K")]}}
    return {"facts": {"us-gaap": base}}


def test_common_stock_dividends_preferred_over_total_payments(monkeypatch):
    """PaymentsOfDividends 含优先股与非控股股息（us-gaap 定义）：两个都打时取普通股概念。"""
    both = _dividend_facts(PaymentsOfDividends=120.0, PaymentsOfDividendsCommonStock=80.0)
    assert _pivot(monkeypatch, both, "DIV")[0]["div_paid_owners"] == 80.0
    total_only = _dividend_facts(PaymentsOfDividends=120.0)
    assert _pivot(monkeypatch, total_only, "DIV")[0]["div_paid_owners"] == 120.0


def test_sync_prunes_stored_quarter_placeholders_but_keeps_older_history(db, monkeypatch):
    for period_key, payload in (
        # v3 留下的占位行：在本次季度窗口内、却不再产出 → 删
        ("20251231|Q1", {"end_date": "20251231", "fp": "Q1", "currency": "USD"}),
        # 本次窗口之前的深历史季度行：保留（upsert 从不删）
        (
            "20180331|Q1",
            {"end_date": "20180331", "fp": "Q1", "currency": "USD", "total_revenue": 90.0},
        ),
    ):
        profile_store.upsert_profile_row(
            db, "PDD", "美股", "edgar_companyfacts", period_key, payload
        )
    db.commit()
    facts = _quarter_facts()
    monkeypatch.setattr(report_fetchers, "edgar_lookup", lambda s: {"cik": 1, "title": "x"})
    monkeypatch.setattr(report_fetchers, "edgar_companyfacts", lambda cik: facts)
    assert svc.sync_symbol_profile(db, "PDD", "美股")["failed"] == []
    keys = {
        row.period_key
        for row in db.query(SecurityProfileData).filter(
            SecurityProfileData.symbol == "PDD",
            SecurityProfileData.dataset == "edgar_companyfacts",
        )
    }
    assert keys == {"20260331|Q1", "20251231|FY", "20180331|Q1"}
