"""美股 ADS 换算比：20-F 封面解析（金样）、落库缓存、用户规则覆盖、格雷厄姆估值取用、规则 API。

金样 = tests/fixtures/ads/*.txt：SEC EDGAR 公开 20-F 主文档经 html_to_text 后的封面（至
Section 12(g) 行）与正文术语定义段的裁剪。覆盖的真实原文形态：
- PDD 2026 / JD 2026「(one American depositary share representing four/two Class A …)」
- BABA 2026「each representing eight Ordinary Shares」、BABA 2019（拆股前）「each representing one」
- BIDU 2026「(each American depositary share representing eight …)」、
  BIDU 2020「(ten American depositary shares representing one Class A ordinary share)」= 0.1
- NTES 2026「five」、NTES 2020「25」（数字）；TCOM「(each representing one ordinary share …)」；
  BILI「one Class Z ordinary share」
- 10-K（v3，#352）：ONC（BeOne）2026-02「each representing 13 Ordinary Shares」、ZLAB（再鼎医药）
  2026-02「each representing 10 Ordinary Shares」——以 ADS 交易的 10-K 申报人；NFLX 2026-01 普通股（无 ADS）
"""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.core.timeutil import local_today
from app.database import SessionLocal
from app.main import app
from app.models.security_price import SecurityPrice
from app.models.security_profile import SecurityProfileData
from app.models.security_rule import SecurityRule
from app.models.user import User
from app.services import ads_ratio_service as ads
from app.services import report_fetchers
from app.services import profile_store
from app.services import security_profile_service as svc
from tests.helpers import seed_security_rule

FIXTURES = Path(__file__).parent / "fixtures" / "ads"

COVER_GOLDEN = {
    "PDD_20-F_2026-04-29": "4",
    "BABA_20-F_2026-05-20": "8",
    "BABA_20-F_2019-06-05": "1",
    "JD_20-F_2026-04-16": "2",
    "BIDU_20-F_2026-03-17": "8",
    "BIDU_20-F_2020-03-13": "0.1",
    "NTES_20-F_2026-04-15": "5",
    "NTES_20-F_2020-04-29": "25",
    "TCOM_20-F_2026-04-28": "1",
    "BILI_20-F_2026-04-16": "1",
}


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _cover(phrase: str) -> str:
    return (
        "FORM 20-F\nSecurities registered or to be registered pursuant to Section 12(b) of the Act:\n"
        f"Title of each class\n{phrase}\nThe Nasdaq Stock Market LLC\n"
        "Securities registered or to be registered pursuant to Section 12(g) of the Act: None\n"
    )


# ---------------------------------------------------------------------------- 解析金样


@pytest.mark.parametrize("name,expected", sorted(COVER_GOLDEN.items()))
def test_cover_golden(name, expected):
    result = ads.parse_ads_ratio(_fixture(f"{name}.cover.txt"), form="20-F", filing_date="d")
    assert result is not None, name
    assert result["ratio"] == Decimal(expected)
    assert ads.format_ratio(result["ratio"]) == expected
    assert result["section"] == "cover"
    assert "depositary share" in result["source_text"]
    assert result["form"] == "20-F" and result["filing_date"] == "d"


def test_pdd_parses_to_four():
    """原 ADS_SHARE_RATIOS 代码常量（PDD 1:4）已删除，由封面解析覆盖。"""
    result = ads.parse_ads_ratio(_fixture("PDD_20-F_2026-04-29.cover.txt"))
    assert result["ratio"] == Decimal(4)


def test_body_definition_fallback():
    """封面缺失时只认正文术语定义句（"ADSs" are to …）。"""
    pdd = ads.parse_ads_ratio(_fixture("PDD_20-F_2026-04-29.body.txt"))
    assert pdd["ratio"] == Decimal(4) and pdd["section"] == "body"
    # BIDU 正文同时有定义句（8）与历史比例变更叙述（1 → 10 ADSs : 1 股）：只取定义句
    bidu = ads.parse_ads_ratio(_fixture("BIDU_20-F_2026-03-17.body.txt"))
    assert bidu["ratio"] == Decimal(8) and bidu["section"] == "body"


def test_body_without_definition_is_none():
    """TCOM 正文只有脚注「Each ADS represents one」与比例变更叙述（8 ADSs : 1 股 → 1:1）：
    没有定义句不猜。"""
    assert ads.parse_ads_ratio(_fixture("TCOM_20-F_2026-04-28.body.txt")) is None


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("American depositary shares, each representing four Class A ordinary shares", "4"),
        ("Each ADS represents 8 ordinary shares", "8"),
        ("One ADS represents 0.5 ordinary share", "0.5"),
        ("Every two ADSs represent one ordinary share", "0.5"),
        ("American Depositary Shares, each representing one-tenth of one ordinary share", "0.1"),
        # HTML 里的 NBSP 与非断行连字符
        ("American Depositary Shares, each representing one‑tenth of one share", "0.1"),
        ("ADSs, each representing 1/4 of an ordinary share", "0.25"),
        ("American depositary shares, each representing twenty-five ordinary shares", "25"),
        (
            "American depositary shares (three ADSs representing two ordinary shares)",
            "0.6666666667",
        ),
        ("American Depositary Shares, each representing twenty Class B shares", "20"),
    ],
)
def test_phrase_variants(phrase, expected):
    result = ads.parse_ads_ratio(_cover(phrase))
    assert result is not None, phrase
    assert ads.format_ratio(result["ratio"]) == expected


@pytest.mark.parametrize(
    "text",
    [
        # 10-K 封面：普通股，无 ADS
        _cover("Common Stock, par value $0.001 per share NFLX"),
        # 比例叙述而非换算比
        _cover("ADSs representing 5% of our outstanding shares"),
        # 过去时：历史比例
        "Prior to March 2021, each ADS represented eight ordinary shares.",
        # 没有任何 ADS 信息
        "",
    ],
)
def test_negative_cases(text):
    assert ads.parse_ads_ratio(text) is None


@pytest.mark.parametrize(
    "text",
    [
        # PR #234 评审 P2：数量前缀没被完整识别时不得按 1 个 ADS 处理
        "Section 12(b) One hundred ADSs represent one ordinary share Section 12(g)",
        "Section 12(b) One hundred and twenty ADSs represent one ordinary share Section 12(g)",
        "Section 12(b) one thousand american depositary shares represent one class a ordinary share Section 12(g)",
    ],
)
def test_unsupported_ads_quantity_is_none(text):
    assert ads.parse_ads_ratio(text) is None


@pytest.mark.parametrize(
    "text",
    [
        # 「N ADSs, each representing M shares」在语法上就是每份 M 股：前导数量（哪怕是不支持的
        # two hundred）不参与换算（v3，#352；此前按「数量没识别全」判 None）
        "Section 12(b) two hundred ADSs, each representing one ordinary share Section 12(g)",
        "Section 12(b) 2 American depositary shares, each representing one ordinary share Section 12(g)",
        "Section 12(b) 2 ADSs, each of which represents one ordinary share Section 12(g)",
    ],
)
def test_per_each_phrase_ignores_leading_quantity(text):
    result = ads.parse_ads_ratio(text)
    assert result is not None and result["ratio"] == Decimal(1)


def test_cover_footnote_number_is_not_read_as_ads_quantity():
    """#352：BABA 2026 版式——港交所那一格带脚注上标，html_to_text 把 <sup>2</sup> 转成孤立的 2，
    与下一格的 ADS 连成「… hong kong limited 2 american depositary shares, each representing
    eight …」。v2 解析成 2 ADS = 8 股（比例 4），应为 8。"""
    text = _fixture("BABA_20-F_2026-05-20.cover.txt").replace(
        "The Stock Exchange of Hong Kong Limited\n", "The Stock Exchange of Hong Kong Limited 2\n"
    )
    assert "limited 2 american depositary" in ads._normalize(text)
    assert ads.parse_ads_ratio(text)["ratio"] == Decimal(8)
    html = (
        "<p>Securities registered or to be registered pursuant to Section 12(b) of the Act:</p>"
        "<table><tr><td>Ordinary Shares</td><td>9988</td>"
        "<td>The Stock Exchange of Hong Kong Limited<sup>2</sup></td></tr>"
        "<tr><td>American Depositary Shares, each representing eight Ordinary Shares</td>"
        "<td>BABA</td><td>New York Stock Exchange</td></tr></table>"
        "<p>Securities registered or to be registered pursuant to Section 12(g) of the Act: None</p>"
    )
    assert ads.parse_ads_ratio_html(html)["ratio"] == Decimal(8)


@pytest.mark.parametrize(
    "name, expected",
    [("ONC_10-K_2026-02-26", "13"), ("ZLAB_10-K_2026-02-26", "10")],
)
def test_10k_ads_issuer_covers(name, expected):
    text = _fixture(f"{name}.cover.txt")
    assert ads.ads_listed_on_cover(text) is True
    result = ads.parse_ads_ratio(text, form="10-K")
    assert result["ratio"] == Decimal(expected) and result["section"] == "cover"


def test_10k_common_stock_cover_has_no_ads():
    text = _fixture("NFLX_10-K_2026-01-23.cover.txt")
    assert ads.ads_listed_on_cover(text) is False
    assert ads.parse_ads_ratio(text, form="10-K") is None
    assert ads.ads_listed_on_cover("no cover here") is None


def test_unsupported_quantity_in_body_definition_is_none():
    text = (
        'Annual report. Unless otherwise indicated, "ADSs" are to one hundred American '
        "depositary shares representing one ordinary share."
    )
    assert ads.parse_ads_ratio(text) is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Section 12(b) 10000 ADSs represent one ordinary share Section 12(g)", "0.0001"),
        ("Section 12(b) 10,000 ADSs represent one ordinary share Section 12(g)", "0.0001"),
        ("Section 12(b) 100 ADSs represent one ordinary share Section 12(g)", "0.01"),
        # 脚注编号被括号隔开，不算未消费的数量前缀
        (
            "Section 12(b) (1) American depositary shares, each representing four ordinary shares Section 12(g)",
            "4",
        ),
        # 「and」前一个词不是数量词：正常识别
        (
            "Section 12(b) Class A ordinary shares and American depositary shares, each representing "
            "eight Class A ordinary shares Section 12(g)",
            "8",
        ),
    ],
)
def test_multi_digit_quantities_and_non_quantity_prefixes(text, expected):
    result = ads.parse_ads_ratio(text)
    assert result is not None and result["ratio"] == Decimal(expected)


def test_conflicting_cover_is_none_even_with_body_definition():
    text = (
        _cover(
            "American depositary shares, each representing four ordinary shares; "
            "American depositary shares, each representing two ordinary shares"
        )
        + '"ADSs" are to the American depositary shares, each of which represents four shares'
    )
    assert ads.parse_ads_ratio(text) is None


def test_cover_wins_over_body():
    text = _cover("American depositary shares, each representing eight ordinary shares") + (
        '"ADSs" are to the American depositary shares, each of which represents one share'
    )
    result = ads.parse_ads_ratio(text)
    assert result["ratio"] == Decimal(8) and result["section"] == "cover"


def test_parse_html_wrapper():
    html = (
        "<html><body><p>Securities registered pursuant to Section 12(b) of the Act:</p>"
        "<table><tr><td>American Depositary Shares (one American<br/>depositary share "
        "representing four Class A<br/>ordinary shares)</td><td>PDD</td></tr></table>"
        "<p>Section 12(g) of the Act: None</p></body></html>"
    )
    assert ads.parse_ads_ratio_html(html)["ratio"] == Decimal(4)


def test_resolved_from_precedence_pure():
    parsed = {"ratio": "4", "filing_date": "2026-04-29", "section": "cover"}
    assert ads.resolved_from(None, parsed) == {
        "ratio": Decimal(4),
        "source": "20-F",
        "filing_date": "2026-04-29",
        "note": "1 ADS = 4 股（20-F 封面 2026-04-29）",
    }
    rule = ads.resolved_from(Decimal("0.5"), parsed)
    assert rule["source"] == "rule" and rule["ratio"] == Decimal("0.5")
    assert rule["note"] == "1 ADS = 0.5 股（用户规则）"
    assert ads.resolved_from(None, {"ratio": None, "status": "not_found"}) is None
    assert ads.resolved_from(None, None) is None


# ---------------------------------------------------------------------------- 落库与缓存

SYMBOL = "ZZADS"


@pytest.fixture
def db():
    session = SessionLocal()

    def clean():
        session.query(SecurityProfileData).filter(SecurityProfileData.symbol == SYMBOL).delete(
            synchronize_session=False
        )
        session.query(SecurityPrice).filter(SecurityPrice.symbol == SYMBOL).delete(
            synchronize_session=False
        )
        session.query(SecurityRule).filter(SecurityRule.symbol == SYMBOL).delete(
            synchronize_session=False
        )
        session.commit()

    clean()
    try:
        yield session
    finally:
        clean()
        session.close()


class _FakeEdgar:
    def __init__(self, monkeypatch, *, form="20-F", accession="0001-26-000001", html=None):
        self.form = form
        self.accession = accession
        self.html = html if html is not None else _fixture("PDD_20-F_2026-04-29.cover.txt")
        self.downloads = 0
        self.fail = False
        monkeypatch.setattr(report_fetchers, "edgar_lookup", lambda symbol: {"cik": 123})
        monkeypatch.setattr(
            report_fetchers, "edgar_recent_annual_filings", lambda cik, limit=1: [self.filing()]
        )
        monkeypatch.setattr(report_fetchers, "edgar_download_filing", self.download)

    def filing(self):
        return {
            "form": self.form,
            "accession": self.accession,
            "primary_document": "doc.htm",
            "filing_date": "2026-04-29",
            "report_date": "2025-12-31",
        }

    def download(self, cik, accession, document):
        self.downloads += 1
        if self.fail:
            raise TimeoutError("slow edge")
        return self.html


def _stored(db):
    db.expire_all()
    row = ads._load_row(db, SYMBOL)
    return row.payload if row else None


def test_ensure_persists_and_caches(db, monkeypatch):
    fake = _FakeEdgar(monkeypatch)
    first = ads.ensure_ads_ratio(db, SYMBOL)
    assert first["status"] == "ok" and first["ratio"] == "4"
    stored = _stored(db)
    assert stored["ratio"] == "4" and stored["section"] == "cover"
    assert stored["accession"] == "0001-26-000001"
    assert stored["parser_version"] == ads.ADS_PARSER_VERSION
    assert "four class a" in stored["source_text"]

    # 同一份 20-F、同一解析器版本：零下载
    assert ads.ensure_ads_ratio(db, SYMBOL)["status"] == "cached"
    assert fake.downloads == 1

    # 新一份 20-F：重新下载解析，比例变化留痕
    fake.accession = "0001-27-000001"
    fake.html = _cover("American depositary shares, each representing eight ordinary shares")
    second = ads.ensure_ads_ratio(db, SYMBOL)
    assert second["status"] == "ok" and second["ratio"] == "8"
    stored = _stored(db)
    assert stored["previous_ratio"] == "4" and fake.downloads == 2


def test_ensure_not_found_is_cached_and_version_bump_retries(db, monkeypatch):
    fake = _FakeEdgar(monkeypatch, html=_cover("Ordinary shares, par value US$0.0001"))
    assert ads.ensure_ads_ratio(db, SYMBOL)["status"] == "not_found"
    assert _stored(db)["ratio"] is None
    assert ads.ensure_ads_ratio(db, SYMBOL)["status"] == "cached"
    assert fake.downloads == 1
    monkeypatch.setattr(ads, "ADS_PARSER_VERSION", ads.ADS_PARSER_VERSION + 1)
    assert ads.ensure_ads_ratio(db, SYMBOL)["status"] == "not_found"
    assert fake.downloads == 2


def test_ensure_10k_without_ads_is_no_ads_and_cached(db, monkeypatch):
    """v3（#352）：10-K 也下载封面；普通股封面记 no_ads（估值 1:1），同一份 10-K 只下载一次。"""
    fake = _FakeEdgar(monkeypatch, form="10-K", html=_fixture("NFLX_10-K_2026-01-23.cover.txt"))
    assert ads.ensure_ads_ratio(db, SYMBOL)["status"] == "no_ads"
    stored = _stored(db)
    assert stored["status"] == "no_ads" and stored["ads_listed"] is False
    assert stored["ratio"] is None
    assert ads.ensure_ads_ratio(db, SYMBOL)["status"] == "cached"
    assert fake.downloads == 1
    assert ads.resolve_ads_ratio(db, SYMBOL) is None  # 1:1，不标缺


def test_ensure_10k_ads_issuer_parses_ratio(db, monkeypatch):
    _FakeEdgar(monkeypatch, form="10-K", html=_fixture("ONC_10-K_2026-02-26.cover.txt"))
    outcome = ads.ensure_ads_ratio(db, SYMBOL)
    assert outcome["status"] == "ok" and outcome["ratio"] == "13"
    resolved = ads.resolve_ads_ratio(db, SYMBOL)
    assert resolved["ratio"] == Decimal(13) and resolved["source"] == "10-K"
    assert resolved["note"] == "1 ADS = 13 股（10-K 封面 2026-04-29）"


def test_ensure_10k_ads_without_ratio_is_marked_missing(db, monkeypatch):
    html = _cover("American Depositary Shares ONC The Nasdaq Global Select Market")
    _FakeEdgar(monkeypatch, form="10-K", html=html)
    assert ads.ensure_ads_ratio(db, SYMBOL)["status"] == "not_found"
    assert _stored(db)["ads_listed"] is True
    assert ads.resolve_ads_ratio(db, SYMBOL) == {"ratio": None, "missing": True, "form": "10-K"}


def test_ensure_10k_ignores_body_definition_without_cover_ads(db, monkeypatch):
    """10-K 只认封面登记表：普通股封面 + 正文里的 ADS 术语定义句不足以按 ADS 换算。"""
    html = _fixture("NFLX_10-K_2026-01-23.cover.txt") + (
        '\n"ADSs" are to the American depositary shares, each of which represents four shares.'
    )
    _FakeEdgar(monkeypatch, form="10-K", html=html)
    assert ads.ensure_ads_ratio(db, SYMBOL)["status"] == "no_ads"


def test_ensure_skips_other_annual_forms(db, monkeypatch):
    fake = _FakeEdgar(monkeypatch, form="40-F")
    assert ads.ensure_ads_ratio(db, SYMBOL)["status"] == "not_20f"
    assert fake.downloads == 0 and _stored(db) is None


def test_ensure_download_failure_keeps_previous_and_caps(db, monkeypatch):
    fake = _FakeEdgar(monkeypatch)
    ads.ensure_ads_ratio(db, SYMBOL)
    fake.accession = "0001-27-000001"
    fake.fail = True
    for _ in range(ads.MAX_FETCH_ATTEMPTS):
        assert ads.ensure_ads_ratio(db, SYMBOL)["status"] == "failed"
    stored = _stored(db)
    # 旧结果保留（仍可用于估值），失败只记在 pending_* 上
    assert stored["ratio"] == "4" and stored["accession"] == "0001-26-000001"
    assert stored["pending_accession"] == "0001-27-000001"
    assert stored["fetch_attempts"] == ads.MAX_FETCH_ATTEMPTS
    assert ads.ensure_ads_ratio(db, SYMBOL)["status"] == "capped"
    assert fake.downloads == 1 + ads.MAX_FETCH_ATTEMPTS
    # 恢复后照常解析新一份
    fake.fail = False
    assert ads.ensure_ads_ratio(db, SYMBOL, force=True)["status"] == "ok"


def test_ensure_unregistered_symbol(db, monkeypatch):
    monkeypatch.setattr(report_fetchers, "edgar_lookup", lambda symbol: None)
    assert ads.ensure_ads_ratio(db, SYMBOL)["status"] == "not_registered"


# ---------------------------------------------------------------------------- 解析 + 规则 → 生效


def _user_ids(db):
    admin = db.query(User).filter(User.username == "admin").one().id
    demo = db.query(User).filter(User.username == "demo").one().id
    return admin, demo


def _store_parsed(db, ratio="4"):
    profile_store.upsert_profile_row(
        db,
        SYMBOL,
        "美股",
        ads.DATASET,
        ads.PERIOD_KEY,
        {
            "status": "ok",
            "ratio": ratio,
            "filing_date": "2026-04-29",
            "section": "cover",
            "form": "20-F",
            "parser_version": ads.ADS_PARSER_VERSION,
        },
    )
    db.commit()


def test_rule_overrides_parsed_only_for_its_user(db):
    admin, demo = _user_ids(db)
    _store_parsed(db)
    seed_security_rule(db, demo, "ADS_RATIO", SYMBOL, "美股", {"ratio": "2"})
    db.commit()

    assert ads.resolve_ads_ratio(db, SYMBOL)["source"] == "20-F"
    mine = ads.resolve_ads_ratio(db, SYMBOL, user_id=demo)
    assert mine["source"] == "rule" and mine["ratio"] == Decimal(2)
    other = ads.resolve_ads_ratio(db, SYMBOL, user_id=admin)
    assert other["source"] == "20-F" and other["ratio"] == Decimal(4)


def test_rule_without_parsed_value(db):
    _, demo = _user_ids(db)
    assert ads.resolve_ads_ratio(db, SYMBOL, user_id=demo) is None
    seed_security_rule(db, demo, "ADS_RATIO", SYMBOL, "美股", {"ratio": "0.1"})
    db.commit()
    assert ads.resolve_ads_ratio(db, SYMBOL, user_id=demo)["ratio"] == Decimal("0.1")


# ---------------------------------------------------------------------------- 格雷厄姆取用


def _seed_us_20f(db, close="77.57", form="20-F"):
    for year, eps in ((2025, 4.0), (2024, 3.0), (2023, 2.0)):
        row = {
            "end_date": f"{year}1231",
            "fp": "FY",
            "form": form,
            "currency": "USD",
            "basic_eps": eps,
            "n_income_attr_p": eps * 1000.0,
            "total_cur_assets": 5000.0,
            "total_cur_liab": 2000.0,
            "total_hldr_eqy_exc_min_int": 20000.0,
            "n_cashflow_act": 900.0,
        }
        profile_store.upsert_profile_row(
            db, SYMBOL, "美股", "edgar_companyfacts", f"{year}1231|FY", row
        )
    db.add(
        SecurityPrice(
            symbol=SYMBOL,
            market="美股",
            price_date=local_today() - timedelta(days=1),
            currency="USD",
            close_price=Decimal(close),
            source="test",
        )
    )
    db.commit()


def _pe(result):
    return next(item for item in result["criteria"] if item["criterion"] == "pe")


def test_graham_uses_parsed_ratio(db, monkeypatch):
    monkeypatch.setattr(svc, "_rate_lookup_for", lambda _db, _markets: None)
    _seed_us_20f(db)
    _store_parsed(db)
    pe = _pe(svc.compute_graham_for(db, SYMBOL, "美股"))
    assert pe["basis"]["eps_ttm"] == pytest.approx(16.0)
    assert pe["basis"]["share_ratio"] == 4.0
    assert pe["basis"]["share_ratio_source"] == "20-F"
    assert pe["basis"]["share_ratio_note"] == "1 ADS = 4 股（20-F 封面 2026-04-29）"


def test_graham_user_rule_overrides_and_summaries(db, monkeypatch):
    monkeypatch.setattr(svc, "_rate_lookup_for", lambda _db, _markets: None)
    _, demo = _user_ids(db)
    _seed_us_20f(db)
    _store_parsed(db)
    seed_security_rule(db, demo, "ADS_RATIO", SYMBOL, "美股", {"ratio": "2"})
    db.commit()
    pe = _pe(svc.compute_graham_for(db, SYMBOL, "美股", user_id=demo))
    assert pe["basis"]["eps_ttm"] == pytest.approx(8.0)
    assert pe["basis"]["share_ratio_source"] == "rule"
    # 批量与单标的同口径
    key = (SYMBOL, "美股")
    single = svc.graham_summary_for(db, SYMBOL, "美股", user_id=demo)
    assert svc.graham_summaries_for(db, [key], user_id=demo)[key] == single


def test_graham_without_ratio_is_indeterminate_with_hint(db, monkeypatch):
    monkeypatch.setattr(svc, "_rate_lookup_for", lambda _db, _markets: None)
    _seed_us_20f(db)
    pe = _pe(svc.compute_graham_for(db, SYMBOL, "美股"))
    assert pe["verdict"] == "indeterminate"
    assert "20-F 封面未解析出 ADS 换算比，可在特例规则中手动填写" in pe["reason"]


def _store_10k(db, *, ads_listed, ratio=None):
    status = "ok" if ratio else ("not_found" if ads_listed else "no_ads")
    profile_store.upsert_profile_row(
        db,
        SYMBOL,
        "美股",
        ads.DATASET,
        ads.PERIOD_KEY,
        {
            "status": status,
            "ratio": ratio,
            "ads_listed": ads_listed,
            "filing_date": "2026-02-26",
            "section": "cover" if ratio else None,
            "form": "10-K",
            "parser_version": ads.ADS_PARSER_VERSION,
        },
    )
    db.commit()


def test_graham_10k_ads_issuer_without_ratio_is_indeterminate(db, monkeypatch):
    """#352：10-K 封面登记了 ADS 却无比例 → 不按 1:1 估值。"""
    monkeypatch.setattr(svc, "_rate_lookup_for", lambda _db, _markets: None)
    _seed_us_20f(db, form="10-K")
    _store_10k(db, ads_listed=True)
    pe = _pe(svc.compute_graham_for(db, SYMBOL, "美股"))
    assert pe["verdict"] == "indeterminate"
    assert "10-K 发行人以 ADS 交易" in pe["reason"]
    assert "10-K 封面未解析出 ADS 换算比，可在特例规则中手动填写" in pe["reason"]


def test_graham_10k_ads_issuer_uses_parsed_ratio(db, monkeypatch):
    monkeypatch.setattr(svc, "_rate_lookup_for", lambda _db, _markets: None)
    _seed_us_20f(db, form="10-K")
    _store_10k(db, ads_listed=True, ratio="13")
    pe = _pe(svc.compute_graham_for(db, SYMBOL, "美股"))
    assert pe["basis"]["share_ratio"] == 13.0
    assert pe["basis"]["share_ratio_source"] == "10-K"
    assert pe["basis"]["eps_ttm"] == pytest.approx(4.0 * 13)


def test_graham_10k_common_stock_stays_one_to_one(db, monkeypatch):
    monkeypatch.setattr(svc, "_rate_lookup_for", lambda _db, _markets: None)
    _seed_us_20f(db, form="10-K")
    _store_10k(db, ads_listed=False)
    pe = _pe(svc.compute_graham_for(db, SYMBOL, "美股"))
    assert pe["verdict"] != "indeterminate"
    assert pe["basis"]["eps_ttm"] == pytest.approx(4.0)
    assert "share_ratio" not in pe["basis"]


# 10-K 主文档里找不到 Section 12(b) 登记表（下载截断、SEC 返回错误页、版式没认出来）
_UNRECOGNIZED_10K = "FORM 10-K\nAnnual report of Example Corp.\nItem 1. Business\nWe sell things.\n"


def test_unrecognized_10k_cover_is_unknown_not_one_to_one_end_to_end(db, monkeypatch):
    """PR #358 评审 P2：封面无法识别 ≠ 普通股。走真实 ensure_ads_ratio（只桩 EDGAR 下载）：
    不落 no_ads、不进永久缓存、按次数重试到上限；解析器升版重新计数；估值两项 indeterminate。"""
    monkeypatch.setattr(svc, "_rate_lookup_for", lambda _db, _markets: None)
    _seed_us_20f(db, form="10-K")
    fake = _FakeEdgar(monkeypatch, form="10-K", html=_UNRECOGNIZED_10K)

    first = ads.ensure_ads_ratio(db, SYMBOL)
    assert first["status"] == "cover_unknown" and "12(b)" in first["error"]
    stored = _stored(db)
    assert stored["status"] == "cover_unknown" and stored["ads_listed"] is None
    assert stored["ratio"] is None and stored["fetch_attempts"] == 1

    # 不缓存：同一份年报再次调用会重新下载，直到重试上限
    for _ in range(ads.MAX_FETCH_ATTEMPTS - 1):
        assert ads.ensure_ads_ratio(db, SYMBOL)["status"] == "cover_unknown"
    assert ads.ensure_ads_ratio(db, SYMBOL)["status"] == "capped"
    assert fake.downloads == ads.MAX_FETCH_ATTEMPTS

    # 解析器 / 估值：不可确定，而不是按 1:1
    assert ads.resolve_ads_ratio(db, SYMBOL) == {
        "ratio": None,
        "missing": True,
        "form": "10-K",
        "unknown": True,
    }
    pe = _pe(svc.compute_graham_for(db, SYMBOL, "美股"))
    assert pe["verdict"] == "indeterminate"
    assert "10-K 封面未识别出证券登记表，无法确认是否以 ADS 交易" in pe["reason"]
    pb = next(
        i
        for i in svc.compute_graham_for(db, SYMBOL, "美股")["criteria"]
        if i["criterion"] == "pb_or_product"
    )
    assert pb["verdict"] == "indeterminate"

    # 解析器升版：同一份年报重新计数、重新尝试（修好的解析器不会被上限挡住）
    monkeypatch.setattr(ads, "ADS_PARSER_VERSION", ads.ADS_PARSER_VERSION + 1)
    fake.html = _fixture("NFLX_10-K_2026-01-23.cover.txt")
    assert ads.ensure_ads_ratio(db, SYMBOL)["status"] == "no_ads"
    assert fake.downloads == ads.MAX_FETCH_ATTEMPTS + 1
    stored = _stored(db)
    assert stored["ads_listed"] is False and "fetch_attempts" not in stored
    # 明确普通股 → 1:1
    assert ads.resolve_ads_ratio(db, SYMBOL) is None
    pe = _pe(svc.compute_graham_for(db, SYMBOL, "美股"))
    assert pe["verdict"] != "indeterminate" and pe["basis"]["eps_ttm"] == pytest.approx(4.0)


def test_unrecognized_new_10k_keeps_previous_explicit_result(db, monkeypatch):
    """上一份年报有明确结论时，新一份封面无法识别只记重试、沿用旧结论（不被覆盖成未知）。"""
    fake = _FakeEdgar(monkeypatch, form="10-K", html=_fixture("ONC_10-K_2026-02-26.cover.txt"))
    assert ads.ensure_ads_ratio(db, SYMBOL)["status"] == "ok"
    fake.accession = "0001-27-000001"
    fake.html = _UNRECOGNIZED_10K
    assert ads.ensure_ads_ratio(db, SYMBOL)["status"] == "cover_unknown"
    stored = _stored(db)
    assert stored["status"] == "ok" and stored["ratio"] == "13"
    assert stored["accession"] == "0001-26-000001"
    assert stored["pending_accession"] == "0001-27-000001" and stored["fetch_attempts"] == 1
    assert ads.resolve_ads_ratio(db, SYMBOL)["ratio"] == Decimal(13)


def test_analysis_gap_names_unrecognized_cover(db, monkeypatch):
    from app.services.security_analysis_jobs import _ensure_ads_ratio_gap

    _FakeEdgar(monkeypatch, form="10-K", html=_UNRECOGNIZED_10K)
    assert _ensure_ads_ratio_gap(db, SYMBOL, "美股") == [
        "ADS 换算比无法确定（年报封面未识别出证券登记表），美股估值两项不可确定"
    ]


# ---------------------------------------------------------------------------- 规则 API


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_ads_ratio_rule_api_validation():
    from app.core.security import get_password_hash

    db = SessionLocal()
    user = db.query(User).filter(User.username == "demo").one()
    original = user.hashed_password
    user.hashed_password = get_password_hash("ads-rule-password")
    db.commit()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            token = await client.post(
                "/api/auth/token",
                json={"username": "demo", "password": "ads-rule-password"},
            )
            auth = {"Authorization": f"Bearer {token.json()['access_token']}"}

            async def post(body):
                return await client.post("/api/security-rules", headers=auth, json=body)

            ok = await post(
                {
                    "rule_type": "ADS_RATIO",
                    "symbol": "zzads",
                    "market": "美股",
                    "payload": {"ratio": 0.1},
                }
            )
            assert ok.status_code == 201, ok.text
            assert ok.json()["symbol"] == SYMBOL
            assert Decimal(ok.json()["payload"]["ratio"]) == Decimal("0.1")

            for body in (
                {
                    "rule_type": "ADS_RATIO",
                    "symbol": "ZZADS2",
                    "market": "港股",
                    "payload": {"ratio": 4},
                },
                {
                    "rule_type": "ADS_RATIO",
                    "symbol": "ZZADS2",
                    "market": "美股",
                    "payload": {"ratio": 0},
                },
                {"rule_type": "ADS_RATIO", "symbol": "ZZADS2", "market": "美股"},
                {
                    "rule_type": "ADS_RATIO",
                    "symbol": "ZZADS2",
                    "market": "美股",
                    "payload": {"ratio": 4, "note": "x"},
                },
            ):
                assert (await post(body)).status_code == 422, body
    finally:
        user.hashed_password = original
        db.query(SecurityRule).filter(SecurityRule.symbol.in_([SYMBOL, "ZZADS2"])).delete(
            synchronize_session=False
        )
        db.commit()
        db.close()


def test_analysis_job_hook_degrades_without_blocking(db, monkeypatch):
    """分析 job 同步档案后顺带 ensure：失败只进缺口，不阻断；非美股不外呼。"""
    from app.services import security_analysis_jobs as jobs

    calls = []

    def boom(_db, symbol, **_kw):
        calls.append(symbol)
        raise ConnectionError("sec.gov down")

    monkeypatch.setattr(ads, "ensure_ads_ratio", boom)
    assert jobs._ensure_ads_ratio_gap(db, "600036", "A股") == []
    gaps = jobs._ensure_ads_ratio_gap(db, SYMBOL, "美股")
    assert calls == [SYMBOL] and len(gaps) == 1 and "ADS 换算比" in gaps[0]
    monkeypatch.setattr(ads, "ensure_ads_ratio", lambda _db, symbol, **_kw: {"status": "cached"})
    assert jobs._ensure_ads_ratio_gap(db, SYMBOL, "美股") == []
