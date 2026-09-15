"""港交所《每日行情报表》：解析金样（真实报表裁剪）、写库口径、周期窗口。"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models.hkex_dayquot_report import HkexDayquotReport
from app.models.security_price import SecurityPrice
from app.services import hkex_dayquot_source as dq

FIXTURE = Path(__file__).parent / "fixtures" / "hkex" / "dayquot_d260901e_sample.htm"
REPORT_DATE = date(2026, 9, 1)
TEST_SYMBOLS = ("00700", "00003", "00007", "00046", "00127", "99999")
WINDOW_START, WINDOW_END = date(2026, 8, 20), date(2026, 9, 4)


@pytest.fixture
def sample_text() -> str:
    return FIXTURE.read_text(encoding="latin-1")


@pytest.fixture
def clean_rows():
    db = SessionLocal()

    def _purge():
        db.query(SecurityPrice).filter(
            SecurityPrice.market == dq.HK_MARKET,
            SecurityPrice.symbol.in_(TEST_SYMBOLS),
            SecurityPrice.price_date == REPORT_DATE,
        ).delete(synchronize_session=False)
        db.query(HkexDayquotReport).filter(
            HkexDayquotReport.report_date.between(WINDOW_START, WINDOW_END)
        ).delete(synchronize_session=False)
        db.commit()

    _purge()
    try:
        yield db
    finally:
        _purge()
        db.close()


def test_report_url_uses_yymmdd():
    assert dq.report_url(REPORT_DATE) == (
        "https://www.hkex.com.hk/eng/stat/smstat/dayquot/d260901e.htm"
    )


def test_parse_sample_rows(sample_text):
    quotes = dq.parse_dayquot(sample_text, expected_date=REPORT_DATE)
    assert len(quotes) == 14

    tencent = quotes[700]
    assert tencent.name == "TENCENT"
    assert tencent.currency == "HKD"
    assert tencent.prev_close == Decimal("453.00")
    assert tencent.close == Decimal("441.40")
    assert tencent.high == Decimal("447.60")
    assert tencent.low == Decimal("440.60")
    assert tencent.ask == Decimal("441.60")
    assert tencent.bid == Decimal("441.40")
    assert tencent.shares_traded == 20_289_781
    assert tencent.turnover == 8_990_301_596
    assert tencent.most_active and not tencent.halt_from_today and not tencent.suspended

    # HTML 实体反转义后名称正确，且不影响列对齐
    assert quotes[3].name == "HK & CHINA GAS"
    assert quotes[3].close == Decimal("7.08")
    # 停牌行只有一行、无价
    assert quotes[7].suspended and quotes[7].close is None and quotes[7].name == "WISDOM WEALTH"
    # 无成交：最高/最低/成交为 "-"，但仍有官方收盘（名义价）
    assert quotes[46].close == Decimal("1.30")
    assert quotes[46].high is None and quotes[46].low is None
    assert quotes[46].shares_traded is None and quotes[46].turnover is None
    # N/A 前收/收盘 → 无价但非停牌
    assert quotes[89009].close is None and not quotes[89009].suspended
    # 分页标签 </font></pre><pre> 直接前缀在第二行上
    assert quotes[127].close == Decimal("1.08") and quotes[127].low == Decimal("1.05")
    # 人民币柜台按报表币种
    assert quotes[80737].currency == "CNY" and quotes[80737].close == Decimal("1.75")
    # SALES RECORDS 栏目之后的 "1 CKH HOLDINGS < 1,000-70.50 >" 不得污染代码 1
    assert quotes[1].close == Decimal("69.05")


def test_parse_rejects_wrong_date(sample_text):
    with pytest.raises(dq.DayquotFormatError):
        dq.parse_dayquot(sample_text, expected_date=date(2026, 9, 2))


def test_parse_rejects_missing_section():
    with pytest.raises(dq.DayquotFormatError):
        dq.parse_dayquot("<html><pre>DATE: 01 SEP 2026 (TUESDAY)\nnothing</pre></html>")
    with pytest.raises(dq.DayquotFormatError):
        dq.dayquot_report_date("<html>no header</html>")


def test_store_preserves_open_and_reports_conflicts(sample_text, clean_rows):
    db = clean_rows
    quotes = dq.parse_dayquot(sample_text)
    db.add(
        SecurityPrice(
            symbol="00700", market=dq.HK_MARKET, ts_code="00700.HK", price_date=REPORT_DATE,
            currency="HKD", open_price=Decimal("445"), high_price=Decimal("447.6"),
            low_price=Decimal("440.6"), close_price=Decimal("441.5"),
            adj_factor=Decimal("1"), adj_close_price=Decimal("441.5"), source="tushare-hk_daily",
        )
    )
    db.commit()

    result = dq.store_dayquot_closes(
        db, report_date=REPORT_DATE, quotes=quotes, symbols=set(TEST_SYMBOLS)
    )
    assert result["stored"] == 4  # 00700 00003 00046 00127
    assert result["suspended"] == ["00007"]
    assert result["missing"] == ["99999"]
    assert result["unpriced"] == []
    assert [c["symbol"] for c in result["conflicts"]] == ["00700"]
    assert result["conflicts"][0]["existing_source"] == "tushare-hk_daily"
    assert result["conflicts"][0]["existing_close"] == Decimal("441.5")
    assert result["conflicts"][0]["official_close"] == Decimal("441.40")

    db.expire_all()
    tencent = (
        db.query(SecurityPrice)
        .filter_by(symbol="00700", market=dq.HK_MARKET, price_date=REPORT_DATE)
        .one()
    )
    assert tencent.close_price == Decimal("441.40")
    assert tencent.pre_close_price == Decimal("453.00")
    assert tencent.open_price == Decimal("445")  # 报表无开盘价：保留原值
    assert tencent.adj_factor == Decimal("1")  # 复权字段不碰
    assert tencent.source == dq.SOURCE
    assert tencent.ts_code == "00700.HK"

    gas = (
        db.query(SecurityPrice)
        .filter_by(symbol="00003", market=dq.HK_MARKET, price_date=REPORT_DATE)
        .one()
    )
    assert gas.close_price == Decimal("7.08") and gas.open_price is None
    assert gas.currency == "HKD" and gas.source == dq.SOURCE

    marker = db.query(HkexDayquotReport).filter_by(report_date=REPORT_DATE).one()
    assert (marker.universe_size, marker.parsed_count, marker.stored_count) == (6, 14, 4)
    assert marker.detail["suspended"] == ["00007"] and marker.detail["missing"] == ["99999"]
    assert marker.detail["conflicts"][0]["official_close"] == "441.40"

    # 再跑一次：官方对官方，无冲突、幂等
    again = dq.store_dayquot_closes(
        db, report_date=REPORT_DATE, quotes=quotes, symbols=set(TEST_SYMBOLS)
    )
    assert again["stored"] == 4 and again["conflicts"] == []


def _purge_source_rows(db, days):
    db.query(SecurityPrice).filter(
        SecurityPrice.market == dq.HK_MARKET,
        SecurityPrice.source == dq.SOURCE,
        SecurityPrice.price_date.in_(days),
    ).delete(synchronize_session=False)
    db.query(HkexDayquotReport).filter(HkexDayquotReport.report_date.in_(days)).delete(
        synchronize_session=False
    )
    db.commit()


def test_report_without_storable_rows_is_done_and_window_advances(
    sample_text, clean_rows, monkeypatch
):
    """跟踪集当天全停牌 / 全 N/A / 全未匹配：写入 0 行仍算已处理，第二轮零外呼
    且预算推进到更早日期；标记在库里，跨进程（新会话 + 清空休市缓存）仍有效。"""
    db = clean_rows
    today = date(2026, 9, 1)  # 周二 = 固件报表日
    window = [date(2026, 9, 1), date(2026, 8, 31), date(2026, 8, 28), date(2026, 8, 27)]
    _purge_source_rows(db, window)
    calls = []

    def fake_fetch(day):
        calls.append(day)
        return sample_text if day == REPORT_DATE else None

    monkeypatch.setattr(dq, "fetch_dayquot_text", fake_fetch)
    monkeypatch.setattr(dq, "hk_universe_symbols", lambda _db: {"00007", "89009", "99999"})
    dq._missing_reports.clear()
    try:
        first = dq.sync_recent_dayquots(db, today=today, lookback_days=7, max_reports=1)
        assert calls == [REPORT_DATE]
        assert first["processed"][0]["stored"] == 0
        assert first["processed"][0]["suspended"] == ["00007"]
        assert first["processed"][0]["unpriced"] == ["89009"]
        assert first["processed"][0]["missing"] == ["99999"]
        assert db.query(SecurityPrice).filter(
            SecurityPrice.source == dq.SOURCE, SecurityPrice.price_date == REPORT_DATE
        ).count() == 0
        marker = db.query(HkexDayquotReport).filter_by(report_date=REPORT_DATE).one()
        assert (marker.stored_count, marker.parsed_count, marker.universe_size) == (0, 14, 3)

        # 模拟进程重启：新会话、休市缓存清空——只靠库内标记
        calls.clear()
        dq._missing_reports.clear()
        fresh = SessionLocal()
        try:
            second = dq.sync_recent_dayquots(fresh, today=today, lookback_days=7, max_reports=1)
        finally:
            fresh.close()
        assert second["skipped_done"] == [REPORT_DATE]
        assert calls == [date(2026, 8, 31)]  # 预算花在更早的日期上，不再重下 09-01

        calls.clear()
        third = dq.sync_recent_dayquots(db, today=today, lookback_days=7, max_reports=1)
        assert third["skipped_done"] == [REPORT_DATE]
        assert calls == [date(2026, 8, 28)]  # 08-31 休市已缓存，继续向前推进
    finally:
        dq._missing_reports.clear()
        _purge_source_rows(db, window)


def test_sync_window_holiday_cache_and_done_skip(sample_text, clean_rows, monkeypatch):
    db = clean_rows
    today = date(2026, 9, 3)  # 周四；窗口 5 天 = 09-03 09-02 09-01 08-31 (08-30 周日跳过)
    window = [date(2026, 9, 3), date(2026, 9, 2), date(2026, 9, 1), date(2026, 8, 31)]
    _purge_source_rows(db, window)
    calls = []

    def fake_fetch(day):
        calls.append(day)
        return sample_text if day == REPORT_DATE else None

    monkeypatch.setattr(dq, "fetch_dayquot_text", fake_fetch)
    monkeypatch.setattr(dq, "hk_universe_symbols", lambda _db: {"00700", "00003"})
    dq._missing_reports.clear()
    try:
        result = dq.sync_recent_dayquots(db, today=today, lookback_days=5, max_reports=10)
        assert calls == window
        assert [p["report_date"] for p in result["processed"]] == [REPORT_DATE]
        assert result["processed"][0]["stored"] == 2
        assert result["no_report"] == [date(2026, 9, 3), date(2026, 9, 2), date(2026, 8, 31)]
        # 今天的 404 可能只是尚未发布，不缓存；过去的 404 = 休市，缓存
        assert dq._missing_reports == {date(2026, 9, 2), date(2026, 8, 31)}

        calls.clear()
        second = dq.sync_recent_dayquots(db, today=today, lookback_days=5, max_reports=10)
        assert calls == [date(2026, 9, 3)]  # 只重试今天；已处理日与休市日零外呼
        assert second["skipped_done"] == [REPORT_DATE]
        assert second["processed"] == []
    finally:
        dq._missing_reports.clear()
        _purge_source_rows(db, window)


def test_sync_caps_downloads_per_tick(clean_rows, monkeypatch):
    calls = []
    monkeypatch.setattr(dq, "fetch_dayquot_text", lambda day: calls.append(day))
    monkeypatch.setattr(dq, "hk_universe_symbols", lambda _db: {"00700"})
    monkeypatch.setattr(dq, "_date_already_synced", lambda _db, _day: False)
    dq._missing_reports.clear()
    try:
        result = dq.sync_recent_dayquots(
            clean_rows, today=date(2026, 9, 3), lookback_days=14, max_reports=2
        )
        assert calls == [date(2026, 9, 3), date(2026, 9, 2)]
        assert result["processed"] == []
    finally:
        dq._missing_reports.clear()


def test_sync_skips_everything_without_tracked_hk_securities(clean_rows, monkeypatch):
    monkeypatch.setattr(dq, "hk_universe_symbols", lambda _db: set())
    monkeypatch.setattr(
        dq, "fetch_dayquot_text", lambda day: pytest.fail("无跟踪标的时不得下载报表")
    )
    result = dq.sync_recent_dayquots(clean_rows, today=date(2026, 9, 3))
    assert result["universe"] == 0 and result["processed"] == []


def test_periodic_entry_respects_switch_and_swallows_errors(monkeypatch):
    monkeypatch.setattr(dq.settings, "hkex_dayquot_sync_enabled", False)
    monkeypatch.setattr(
        dq, "sync_recent_dayquots", lambda *a, **k: pytest.fail("开关关闭时不得同步")
    )
    assert dq.refresh_hk_dayquot() == 0

    monkeypatch.setattr(dq.settings, "hkex_dayquot_sync_enabled", True)

    def boom(*_a, **_k):
        raise RuntimeError("network down")

    monkeypatch.setattr(dq, "sync_recent_dayquots", boom)
    assert dq.refresh_hk_dayquot() == 0  # 周期线程上不上抛
