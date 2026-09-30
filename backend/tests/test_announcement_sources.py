"""官方公告三源的解析（纯函数，真实响应裁剪的金样）与抓取分派（外呼打桩）。"""

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.services import announcement_sources as sources
from app.services import report_fetchers

FIXTURES = Path(__file__).parent / "fixtures" / "announcements"


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parse_cninfo_rows_fixture():
    body = _load("cninfo_600298_page.json")
    records = sources.parse_cninfo_rows(body["announcements"], "600298", "A股")
    assert len(records) == len(body["announcements"]) == 15
    first = records[0]
    assert first["source"] == "cninfo" and first["source_id"].isdigit()
    assert first["published_at"].tzinfo is not None
    assert first["url"].startswith("https://static.cninfo.com.cn/finalpage/")
    assert "||" in first["category_raw"]
    assert len({r["source_id"] for r in records}) == 15
    assert any("可转换公司债券预案" in r["title"] for r in records)


def test_cninfo_stock_param_maps_b_share_to_a_share_code(monkeypatch):
    """B 股的 orgId 对应 A 股公司（实测 900926 → gssh0600845），按 B 股代码检索恒为 0 条。"""
    orgs = {"900926": "gssh0600845", "600298": "gssh0600298", "159307": None}
    monkeypatch.setattr(report_fetchers, "cninfo_org_id", lambda s, **kw: orgs.get(s))
    assert sources.cninfo_stock_param("900926") == "600845,gssh0600845"
    assert sources.cninfo_stock_param("600298") == "600298,gssh0600298"
    with pytest.raises(sources.UnsupportedSymbol):
        sources.cninfo_stock_param("159307")  # ETF 无 orgId


def test_fetch_cninfo_pages_until_no_more(monkeypatch):
    monkeypatch.setattr(report_fetchers, "cninfo_org_id", lambda s, **kw: "gssh0600298")
    rows = _load("cninfo_600298_page.json")["announcements"]
    pages = []

    def fake_page(stock, *, se_date, page, page_size=30):
        pages.append((stock, se_date, page))
        return {"announcements": rows[:8] if page == 1 else rows[8:], "hasMore": page == 1}

    monkeypatch.setattr(report_fetchers, "cninfo_announcement_page", fake_page)
    records = sources.fetch_cninfo("600298", "A股", date(2026, 9, 20), date(2026, 9, 28))
    assert len(records) == 15
    assert pages == [
        ("600298,gssh0600298", "2026-09-20~2026-09-28", 1),
        ("600298,gssh0600298", "2026-09-20~2026-09-28", 2),
    ]


def test_parse_hkex_rows_fixture():
    rows = _load("hkex_00700_rows.json")
    records = sources.parse_hkex_rows(rows, "00700")
    assert len(records) == len(rows)
    sample = records[0]
    assert sample["source"] == "hkexnews" and sample["source_id"] == rows[0]["NEWS_ID"]
    # DATE_TIME 是香港时间：28/09/2026 18:01 → UTC 10:01
    local = datetime.strptime(rows[0]["DATE_TIME"], "%d/%m/%Y %H:%M")
    assert sample["published_at"] == local.replace(tzinfo=timezone.utc) - (
        datetime(2000, 1, 1, 8) - datetime(2000, 1, 1)
    )
    assert "&#x2f;" not in "".join(r["category_raw"] for r in records)
    assert sample["url"].startswith("https://www1.hkexnews.hk/listedco/")


def test_fetch_hkex_splits_into_windows_and_flags_unknown_stock(monkeypatch):
    calls = []

    def fake_raw(symbol, *, from_date, to_date, row_range=500):
        calls.append((from_date, to_date))
        return []

    monkeypatch.setattr(report_fetchers, "hkex_announcements_raw", fake_raw)
    sources.fetch_hkex("00700", date(2025, 9, 29), date(2026, 9, 29))
    assert calls[0] == ("20260801", "20260929")
    assert calls[-1][0] == "20250929"
    assert all(a <= b for a, b in calls) and len(calls) == 7
    monkeypatch.setattr(report_fetchers, "hkex_announcements_raw", lambda *a, **k: None)
    with pytest.raises(sources.UnsupportedSymbol):
        sources.fetch_hkex("99999", date(2026, 9, 1), date(2026, 9, 29))


def test_parse_edgar_recent_fixture():
    sub = _load("edgar_nflx_submissions_min.json")
    records = sources.parse_edgar_recent(sub, "NFLX", since=date(2026, 1, 1))
    forms = [r["payload"]["form"] for r in records]
    assert "SC 13G/A" not in forms  # 2024 年的，早于 since
    assert {"8-K", "10-Q", "10-K", "4", "144", "DEF 14A"} <= set(forms)
    earnings = next(r for r in records if r["category_raw"].startswith("8-K|2.02"))
    assert "经营业绩（2.02）" in earnings["title"]
    assert earnings["url"].startswith("https://www.sec.gov/Archives/edgar/data/")
    assert earnings["published_at"].tzinfo is not None
    assert sources.EDGAR_FORMS.match("SD") is None


def test_fetch_announcements_dispatch(monkeypatch):
    monkeypatch.setattr(sources, "fetch_cninfo", lambda s, m, a, b: [f"cninfo:{m}"])
    monkeypatch.setattr(sources, "fetch_hkex", lambda s, a, b: ["hkex"])
    assert sources.fetch_announcements("900926", "B股", date(2026, 9, 1), date(2026, 9, 2)) == [
        "cninfo:B股"
    ]
    assert sources.fetch_announcements("00700", "港股", date(2026, 9, 1), date(2026, 9, 2)) == [
        "hkex"
    ]
    with pytest.raises(sources.UnsupportedSymbol):
        sources.fetch_announcements("D05", "新加坡", date(2026, 9, 1), date(2026, 9, 2))


def test_cninfo_org_id_strict_distinguishes_load_failure_from_not_found(monkeypatch):
    """PR #309 评审 P2-1：映射表加载失败不得被当成「无官方源」。"""
    monkeypatch.setattr(report_fetchers, "_org_id_cache", {})

    def boom():
        raise TimeoutError("szse_stock.json 超时")

    monkeypatch.setattr(report_fetchers, "_load_org_id_map", boom)
    # 非严格（年报检索）：沿用回退规则
    assert report_fetchers.cninfo_org_id("600298") == "gssh0600298"
    assert report_fetchers.cninfo_org_id("000651") is None
    # 严格：深市代码与 B 股都不得靠回退规则判定，原样抛出
    for code in ("000651", "900926"):
        with pytest.raises(TimeoutError):
            report_fetchers.cninfo_org_id(code, strict=True)
    with pytest.raises(TimeoutError):
        sources.cninfo_stock_param("000651")
    # 加载失败但旧缓存里有：用旧缓存
    monkeypatch.setattr(report_fetchers, "_org_id_cache", {"900926": "gssh0600845"})
    assert report_fetchers.cninfo_org_id("900926", strict=True) == "gssh0600845"
    # 加载成功且查无此码：才是 None → UnsupportedSymbol
    monkeypatch.setattr(report_fetchers, "_load_org_id_map", lambda: {"600298": "gssh0600298"})
    assert report_fetchers.cninfo_org_id("159307", strict=True) is None
    with pytest.raises(sources.UnsupportedSymbol):
        sources.cninfo_stock_param("159307")


def test_org_id_map_reloads_after_max_age(monkeypatch):
    calls = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            calls.append(1)
            return {"stockList": [{"code": "600298", "orgId": "gssh0600298"}]}

    monkeypatch.setattr(report_fetchers, "_org_id_cache", {})
    monkeypatch.setattr(report_fetchers, "_org_id_cache_loaded", False)
    monkeypatch.setattr(report_fetchers, "_throttle", lambda *a, **k: None)
    monkeypatch.setattr(report_fetchers.requests, "get", lambda *a, **k: Resp())
    report_fetchers._load_org_id_map()
    report_fetchers._load_org_id_map()
    assert len(calls) == 1
    monkeypatch.setattr(
        report_fetchers,
        "_org_id_cache_loaded_at",
        report_fetchers._org_id_cache_loaded_at - report_fetchers._MAPPING_MAX_AGE_SECONDS - 1,
    )
    report_fetchers._load_org_id_map()
    assert len(calls) == 2


def test_hkex_stock_id_does_not_cache_misses(monkeypatch):
    bodies = iter(['c({"stockInfo": []})', 'c({"stockInfo": [{"stockId": 7609}]})'])

    class Resp:
        def __init__(self):
            self.text = next(bodies)

        def raise_for_status(self):
            pass

    monkeypatch.setattr(report_fetchers, "_hkex_stock_id_cache", {})
    monkeypatch.setattr(report_fetchers, "_throttle", lambda *a, **k: None)
    monkeypatch.setattr(report_fetchers.requests, "get", lambda *a, **k: Resp())
    assert report_fetchers.hkex_stock_id("09999") is None
    assert report_fetchers.hkex_stock_id("09999") == "7609"  # 未缓存 None，建档后能查到


def test_edgar_lookup_raises_on_map_failure_unless_cached(monkeypatch):
    def boom():
        raise ConnectionError("sec.gov 超时")

    monkeypatch.setattr(report_fetchers, "_load_cik_map", boom)
    monkeypatch.setattr(report_fetchers, "_cik_cache", {})
    with pytest.raises(ConnectionError):
        report_fetchers.edgar_lookup("NFLX")
    monkeypatch.setattr(report_fetchers, "_cik_cache", {"NFLX": {"cik": 1065280, "title": "x"}})
    assert report_fetchers.edgar_lookup("nflx")["cik"] == 1065280
