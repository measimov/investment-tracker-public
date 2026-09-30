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


def test_usd_issuer_pivot_unchanged_against_pre_change_golden(monkeypatch):
    """[回归锁] 美国本土 10-K 发行人只有 USD 单位：除版本标记外与改动前逐字节一致。"""
    rows = _pivot(monkeypatch, _facts("aapl"), "AAPL")
    golden = json.loads((FIXTURES / "aapl_pivot_golden.json").read_text(encoding="utf-8"))
    assert {row.pop("edgar_chain_version") for row in rows} == {EDGAR_PIVOT_VERSION}
    for row in golden:
        row.pop("edgar_chain_version")
    assert json.dumps(rows, sort_keys=False) == json.dumps(golden, sort_keys=False)


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
