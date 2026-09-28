"""港股分红同步（披露易现金股息公告）：建议生成、判重、更新取代、撤回、缓存、
事件、复权因子，以及未配置 TUSHARE_TOKEN 时港股照常同步。

清单与下载两层 monkeypatch（`list_dividend_forms` / `download_form_text`），
表格文本来自真实金样 `tests/fixtures/hkex_dividend/`，不打网络。
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models.background_job import BackgroundJob
from app.models.corporate_action import CorporateAction
from app.models.corporate_action_suggestion import CorporateActionSuggestion
from app.models.exchange_rate import ExchangeRate
from app.models.holding import Holding
from app.models.security_event import SecurityEvent
from app.models.security_price import SecurityPrice
from app.models.security_profile import SecurityProfileData
from app.models.security_rule import SecurityRule
from app.models.transaction import Transaction
from app.services import dividend_sync_service as svc
from app.services import hkex_dividend_source as src
from app.services.background_job_store import JobOwnershipLostError
from app.services.hk_adjustment_factors import recompute_hk_adj_factors

from .helpers import add_transaction, make_account, reset_tables

FIXTURES = Path(__file__).parent / "fixtures" / "hkex_dividend"
RATE_SOURCE = "test-hkdiv"

RESET_MODELS = [
    CorporateActionSuggestion,
    SecurityEvent,
    CorporateAction,
    Holding,
    Transaction,
    SecurityRule,
]


def _cleanup(session):
    reset_tables(session, RESET_MODELS)
    session.query(SecurityProfileData).filter(
        SecurityProfileData.dataset == src.DATASET
    ).delete()
    session.query(SecurityPrice).filter(SecurityPrice.market == "港股").delete()
    session.query(ExchangeRate).filter(ExchangeRate.source == RATE_SOURCE).delete()
    session.query(BackgroundJob).filter(BackgroundJob.job_type == "dividend_sync").delete()
    session.commit()


@pytest.fixture
def db(monkeypatch):
    # 港股不依赖 Tushare：整个文件默认无 token；窗口放宽到十年，金样日期不随今天漂移
    monkeypatch.setattr(svc.settings, "tushare_token", "")
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.setattr(svc.settings, "dividend_sync_lookback_days", 3650)
    session = SessionLocal()
    try:
        _cleanup(session)
        yield session
        session.rollback()
        _cleanup(session)
    finally:
        session.close()


def _text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeHkex:
    """按标的给清单；下载按 URL 取文本并计数。"""

    def __init__(self, monkeypatch):
        self.listings = {}
        self.texts = {}
        self.downloads = []
        self.fail_urls = set()
        monkeypatch.setattr(src, "list_dividend_forms", self._list)
        monkeypatch.setattr(src, "download_form_text", self._download)

    def publish(self, symbol, doc_id, listed_at, text):
        url = f"https://www1.hkexnews.hk/listedco/listconews/sehk/x/{doc_id}_c.pdf"
        self.texts[url] = text
        self.listings.setdefault(symbol, []).insert(0, {
            "doc_id": doc_id, "title": "t", "listed_at": listed_at, "url": url,
        })
        return url

    def _list(self, symbol, from_date, to_date):
        return list(self.listings.get(symbol, []))

    def _download(self, url):
        self.downloads.append(url)
        if url in self.fail_urls:
            raise TimeoutError("披露易下载超时")
        return self.texts[url]


def _seed_hk_holding(db, symbol, quantity=Decimal("100")):
    db.add(Holding(
        user_id=1, symbol=symbol, name=symbol, market="港股", quantity=quantity,
        avg_cost=Decimal("10"), total_cost=quantity * 10, currency="HKD",
    ))
    db.commit()


def _buy(db, symbol, quantity, on, account_id=None):
    add_transaction(db, symbol=symbol, market="港股", currency="HKD", name=symbol,
                    quantity=Decimal(quantity), transaction_date=on,
                    broker_account_id=account_id)
    db.commit()


def _suggestions(db, symbol):
    return (
        db.query(CorporateActionSuggestion)
        .filter(CorporateActionSuggestion.symbol == symbol)
        .order_by(CorporateActionSuggestion.ex_date, CorporateActionSuggestion.broker_account_id)
        .all()
    )


# ---------------------------------------------------------------------------
# 建议生成（无 TUSHARE_TOKEN）
# ---------------------------------------------------------------------------


def test_hk_sync_works_without_tushare_and_splits_accounts(db, monkeypatch):
    fake = FakeHkex(monkeypatch)
    fake.publish("00700", "2026031800477", "2026-03-18T17:09:00", _text("00700_final_2025.txt"))
    _seed_hk_holding(db, "00700")
    db.add(Holding(user_id=1, symbol="600036", name="招商银行", market="A股",
                   quantity=Decimal("100"), avg_cost=Decimal("30"),
                   total_cost=Decimal("3000"), currency="CNY"))
    db.commit()
    a1 = make_account(db, "IBKR", commit=True)
    a2 = make_account(db, "CMB", commit=True)
    _buy(db, "00700", "300", date(2026, 1, 10), a1.id)
    _buy(db, "00700", "100", date(2026, 5, 14), a2.id)  # 除净日前一天：享有
    _buy(db, "00700", "200", date(2026, 5, 15), a1.id)  # 除净日当天买入：不享有（T+2）
    tushare_calls = []
    monkeypatch.setattr(svc, "fetch_dividend_announcements",
                        lambda s, m: tushare_calls.append(s) or [])

    result = svc.sync_dividends_for_user(db, 1)

    assert tushare_calls == []  # 无 token：A 股整体跳过，不去打 Tushare
    assert result["tushare_unavailable"] is True
    assert result["skipped_no_tushare"] == 1
    assert result["symbols_scanned"] == 1
    assert result["failed"] == []
    assert result["new"] == 2

    rows = _suggestions(db, "00700")
    assert [(r.broker_account_id, Decimal(str(r.record_date_quantity))) for r in rows] == [
        (a1.id, Decimal("300")), (a2.id, Decimal("100")),
    ]
    first = rows[0]
    assert first.market == "港股"
    assert first.ex_date == date(2026, 5, 15)
    assert first.record_date == date(2026, 5, 20)
    assert first.pay_date == date(2026, 6, 1)
    assert first.ann_date == date(2026, 3, 18)
    assert first.currency == "HKD"
    assert Decimal(str(first.cash_div_pre_tax)) == Decimal("5.3")
    assert first.cash_div_after_tax is None  # 代扣税取决于持有渠道，不猜
    assert Decimal(str(first.estimated_total_dividend)) == Decimal("1590")
    assert first.source == "hkexnews-dividend"
    assert first.status == "NEW"
    detail = first.announcement_detail
    assert detail["source"] == "hkexnews"
    assert detail["entitlement_basis"] == "ex_date_minus_1"
    assert detail["withholding_applicable"] is False
    assert [(c["dividend_type"], c["amount"], c["currency"]) for c in detail["components"]] == [
        ("末期", "5.3", "HKD"),
    ]

    event = db.query(SecurityEvent).one()
    assert (event.symbol, event.market, event.event_type, event.event_date, event.source) == (
        "00700", "港股", "DIVIDEND_PLAN", date(2026, 5, 15), "hkexnews-dividend",
    )
    assert event.payload["currency"] == "HKD"
    assert event.payload["cash_div_tax"] == 5.3
    assert event.payload["pay_date"] == "2026-06-01"
    assert event.payload["dividend_types"] == ["末期 普通股息"]


def test_accepting_hk_suggestion_books_hkd_dividend(db, monkeypatch):
    from app.models.user import User

    fake = FakeHkex(monkeypatch)
    fake.publish("00700", "2026031800477", "2026-03-18T17:09:00", _text("00700_final_2025.txt"))
    _seed_hk_holding(db, "00700")
    _buy(db, "00700", "100", date(2026, 1, 10))
    svc.sync_dividends_for_user(db, 1)

    suggestion = _suggestions(db, "00700")[0]
    user = db.query(User).filter(User.id == 1).one()
    action = svc.accept_suggestion(db, user, suggestion.id, {"tax_withheld": Decimal("53")})
    assert action.currency == "HKD"
    assert Decimal(str(action.dividend_per_share)) == Decimal("5.3")
    assert Decimal(str(action.total_dividend)) == Decimal("530")
    assert Decimal(str(action.net_dividend)) == Decimal("477")
    assert "hkexnews-dividend" in action.notes


def test_withholding_detail_for_h_share(db, monkeypatch):
    fake = FakeHkex(monkeypatch)
    fake.publish("00728", "2026051901240", "2026-05-19T21:44:00",
                 _text("00728_final_2025_update.txt"))
    _seed_hk_holding(db, "00728")
    _buy(db, "00728", "1000", date(2026, 1, 10))
    svc.sync_dividends_for_user(db, 1)

    (row,) = _suggestions(db, "00728")
    assert row.currency == "HKD"
    assert Decimal(str(row.cash_div_pre_tax)) == Decimal("0.10391")
    component = row.announcement_detail["components"][0]
    assert (component["declared_amount"], component["declared_currency"]) == ("0.0908", "CNY")
    assert component["exchange_rate"] == {"from": "CNY", "to": "HKD", "rate": "1.144387"}
    assert component["withholding"]["non_resident_enterprise_percent"] == "10"
    assert component["withholding"]["southbound_individual_percent"] == "20"
    assert row.announcement_detail["withholding_applicable"] is True


# ---------------------------------------------------------------------------
# 判重
# ---------------------------------------------------------------------------


def _broker_dividend(db, symbol, total, currency, *, on, account_id=None):
    action = CorporateAction(
        user_id=1, symbol=symbol, market="港股", action_type="CASH_DIVIDEND",
        ex_date=on, payment_date=on, currency=currency, broker_account_id=account_id,
        total_dividend=Decimal(total), tax_withheld=Decimal("0"), net_dividend=Decimal(total),
    )
    db.add(action)
    db.commit()
    return action


def test_dedupe_by_pay_date_window_and_currency(db, monkeypatch):
    fake = FakeHkex(monkeypatch)
    fake.publish("00700", "2026031800477", "2026-03-18T17:09:00", _text("00700_final_2025.txt"))
    _seed_hk_holding(db, "00700")
    a1 = make_account(db, "IBKR", commit=True)
    a2 = make_account(db, "CMB", commit=True)
    _buy(db, "00700", "100", date(2026, 1, 10), a1.id)
    _buy(db, "00700", "200", date(2026, 1, 10), a2.id)
    # IBKR：到账日入账、港元同额；招商港股通：人民币入账（金额不可比）
    hkd = _broker_dividend(db, "00700", "530", "HKD", on=date(2026, 6, 1), account_id=a1.id)
    cny = _broker_dividend(db, "00700", "960", "CNY", on=date(2026, 6, 3), account_id=a2.id)

    result = svc.sync_dividends_for_user(db, 1)
    assert result["matched"] == 2 and result["new"] == 0

    s1, s2 = _suggestions(db, "00700")
    assert (s1.status, s1.matched_corporate_action_id) == ("MATCHED", hkd.id)
    assert "amount_diff" not in s1.match_detail
    assert (s2.status, s2.matched_corporate_action_id) == ("MATCHED", cny.id)
    assert "amount_diff" not in s2.match_detail
    assert s2.match_detail["currency_mismatch"] == {
        "recorded_currency": "CNY", "suggested_currency": "HKD", "recorded_total": 960.0,
    }


def test_same_ex_date_final_and_special_merge_into_one_suggestion(db, monkeypatch):
    special = _text("00148_special_final_2025.txt")
    ordinary = (
        special.replace("股息性質 特別股息", "股息性質 普通股息")
        .replace("每 股 0.4HKD", "每 股 0.8HKD")
    )
    fake = FakeHkex(monkeypatch)
    fake.publish("00148", "2026031600354", "2026-03-16T12:12:00", ordinary)
    fake.publish("00148", "2026031600360", "2026-03-16T12:14:00", special)
    _seed_hk_holding(db, "00148")
    _buy(db, "00148", "1000", date(2026, 1, 10))
    # 券商分两行入账（末期 800 + 特別 400）
    first = _broker_dividend(db, "00148", "800", "HKD", on=date(2026, 7, 8))
    second = _broker_dividend(db, "00148", "400", "HKD", on=date(2026, 7, 8))

    svc.sync_dividends_for_user(db, 1)

    (row,) = _suggestions(db, "00148")
    assert row.ex_date == date(2026, 6, 11)
    assert Decimal(str(row.cash_div_pre_tax)) == Decimal("1.2")
    assert Decimal(str(row.estimated_total_dividend)) == Decimal("1200")
    assert [c["dividend_nature"] for c in row.announcement_detail["components"]] == [
        "普通股息", "特別股息",
    ]
    assert row.status == "MATCHED"
    assert row.match_detail["matched_action_ids"] == sorted([first.id, second.id])
    assert "amount_diff" not in row.match_detail  # 两行合计比较
    event = db.query(SecurityEvent).one()
    assert event.payload["dividend_types"] == ["末期 普通股息", "末期 特別股息"]


# ---------------------------------------------------------------------------
# 更新取代 / 撤回 / 缓存
# ---------------------------------------------------------------------------


def _original_00728():
    return (
        _text("00728_final_2025_update.txt")
        .replace("公告狀態 更新公告", "公告狀態 新公告")
        .replace("更新/撤回理由 更新派付末期股息的匯率及末期股息港元金額\n", "")
        .replace("每 股 0.10391HKD", "每 股 0.1HKD")
        .replace("除淨日 2026年6月2日", "除淨日 2026年6月1日")
        .replace("公告日期 2026年5月19日", "公告日期 2026年3月24日")
    )


def test_update_form_supersedes_older_form_and_moves_ex_date(db, monkeypatch):
    fake = FakeHkex(monkeypatch)
    fake.publish("00728", "2026032400515", "2026-03-24T17:08:00", _original_00728())
    _seed_hk_holding(db, "00728")
    _buy(db, "00728", "1000", date(2026, 1, 10))

    svc.sync_dividends_for_user(db, 1)
    (row,) = _suggestions(db, "00728")
    assert (row.ex_date, Decimal(str(row.cash_div_pre_tax))) == (date(2026, 6, 1), Decimal("0.1"))
    assert [e.event_date for e in db.query(SecurityEvent).all()] == [date(2026, 6, 1)]

    fake.publish("00728", "2026051901240", "2026-05-19T21:44:00",
                 _text("00728_final_2025_update.txt"))
    result = svc.sync_dividends_for_user(db, 1)

    (row,) = _suggestions(db, "00728")
    assert (row.ex_date, Decimal(str(row.cash_div_pre_tax))) == (
        date(2026, 6, 2), Decimal("0.10391")
    )
    assert row.announcement_detail["components"][0]["status"] == "更新公告"
    assert result["stale_removed"] == 1
    assert result["events_removed"] == 1
    assert [e.event_date for e in db.query(SecurityEvent).all()] == [date(2026, 6, 2)]
    # 旧表格只下载过一次（缓存命中）
    assert len(fake.downloads) == 2


def test_update_keeps_accepted_old_suggestion(db, monkeypatch):
    from app.models.user import User

    fake = FakeHkex(monkeypatch)
    fake.publish("00728", "2026032400515", "2026-03-24T17:08:00", _original_00728())
    _seed_hk_holding(db, "00728")
    _buy(db, "00728", "1000", date(2026, 1, 10))
    svc.sync_dividends_for_user(db, 1)
    user = db.query(User).filter(User.id == 1).one()
    svc.accept_suggestion(db, user, _suggestions(db, "00728")[0].id, {})

    fake.publish("00728", "2026051901240", "2026-05-19T21:44:00",
                 _text("00728_final_2025_update.txt"))
    svc.sync_dividends_for_user(db, 1)
    # 已入账的旧建议是账本事实：保留；新除净日的建议判重命中刚入账的记录
    rows = _suggestions(db, "00728")
    assert [(r.ex_date, r.status) for r in rows] == [
        (date(2026, 6, 1), "ACCEPTED"), (date(2026, 6, 2), "MATCHED"),
    ]


def test_pending_form_waits_for_update(db, monkeypatch):
    """首份公告「有待公佈」：不生成建议/事件、不算解析失败；更新公告补齐后才生成。"""
    pending = _text("00728_final_2021_pending.txt")
    fake = FakeHkex(monkeypatch)
    fake.publish("00728", "2022031700680", "2022-03-17T16:43:00", pending)
    _seed_hk_holding(db, "00728")
    _buy(db, "00728", "1000", date(2021, 6, 10))

    result = svc.sync_dividends_for_user(db, 1)
    assert _suggestions(db, "00728") == []
    assert db.query(SecurityEvent).count() == 0
    assert result["hk_unparsed_forms"] == []
    assert [(p["symbol"], p["period_end"]) for p in result["hk_pending"]] == [
        ("00728", "2021-12-31"),
    ]

    update = (
        pending.replace("公告狀態 新公告", "公告狀態 更新公告")
        .replace("HKD， 金額有待公佈", "每 股 0.2063HKD")
        .replace("匯率 有待公佈", "匯率 1 RMB : 1.2135HKD")
        .replace("除淨日 有待公佈", "除淨日 2022年6月2日")
        .replace("記錄日期 有待公佈", "記錄日期 2022年6月9日")
    )
    fake.publish("00728", "2022051900995", "2022-05-19T19:31:00", update)
    result = svc.sync_dividends_for_user(db, 1)
    (row,) = _suggestions(db, "00728")
    assert (row.ex_date, row.currency, Decimal(str(row.cash_div_pre_tax))) == (
        date(2022, 6, 2), "HKD", Decimal("0.2063")
    )
    assert result["hk_pending"] == []


def test_withdrawal_form_cancels_dividend(db, monkeypatch):
    fake = FakeHkex(monkeypatch)
    fake.publish("00700", "2026031800477", "2026-03-18T17:09:00", _text("00700_final_2025.txt"))
    _seed_hk_holding(db, "00700")
    _buy(db, "00700", "100", date(2026, 1, 10))
    svc.sync_dividends_for_user(db, 1)
    assert len(_suggestions(db, "00700")) == 1

    withdrawal = (
        _text("00700_final_2025.txt")
        .replace("公告狀態 新公告", "公告狀態 撤回公告")
        .replace("除淨日 2026年5月15日", "除淨日 不適用")
    )
    fake.publish("00700", "2026040100001", "2026-04-01T09:00:00", withdrawal)
    result = svc.sync_dividends_for_user(db, 1)

    assert _suggestions(db, "00700") == []
    assert db.query(SecurityEvent).count() == 0
    assert result["stale_removed"] == 1


def test_forms_are_cached_and_parser_bump_reparses_without_download(db, monkeypatch):
    fake = FakeHkex(monkeypatch)
    fake.publish("00700", "2026031800477", "2026-03-18T17:09:00", _text("00700_final_2025.txt"))
    _seed_hk_holding(db, "00700")
    _buy(db, "00700", "100", date(2026, 1, 10))

    first = svc.sync_dividends_for_user(db, 1)
    second = svc.sync_dividends_for_user(db, 1)
    assert (first["hk_forms_downloaded"], second["hk_forms_downloaded"]) == (1, 0)

    bumped = src.HKEX_DIVIDEND_PARSER_VERSION + 1
    monkeypatch.setattr(src, "HKEX_DIVIDEND_PARSER_VERSION", bumped)
    third = svc.sync_dividends_for_user(db, 1)
    assert third["hk_forms_downloaded"] == 0
    assert len(fake.downloads) == 1
    cached = db.query(SecurityProfileData).filter_by(dataset=src.DATASET).one()
    assert cached.period_key == "2026031800477"
    assert cached.payload["parser_version"] == src.HKEX_DIVIDEND_PARSER_VERSION
    assert cached.payload["status"] == "ok"
    assert cached.payload["text"].startswith("EF001")


def _unparsable_update_of_00700():
    """同一笔股息（2025 末期普通）的更新公告，派息金额认不出。"""
    return (
        _text("00700_final_2025.txt")
        .replace("公告狀態 新公告", "公告狀態 更新公告")
        .replace("公告日期 2026年3月18日", "公告日期 2026年4月1日")
        .replace("派息金額及公司預設派發貨幣 每 股 5.3HKD", "派息金額及公司預設派發貨幣 見附件")
    )


def test_unparsable_update_blocks_that_dividend_instead_of_reviving_old_one(db, monkeypatch):
    """PR #249 评审 P2：最新的更新公告认不出时，旧公告不得静默重新成为现行值。"""
    fake = FakeHkex(monkeypatch)
    fake.publish("00700", "2026031800477", "2026-03-18T17:09:00", _text("00700_final_2025.txt"))
    _seed_hk_holding(db, "00700")
    _buy(db, "00700", "100", date(2026, 1, 10))
    svc.sync_dividends_for_user(db, 1)
    (before,) = _suggestions(db, "00700")
    event_dates = [e.event_date for e in db.query(SecurityEvent).all()]

    fake.publish("00700", "2026040100001", "2026-04-01T09:00:00", _unparsable_update_of_00700())
    result = svc.sync_dividends_for_user(db, 1)

    (blocked,) = result["hk_blocked"]
    assert (blocked["symbol"], blocked["scope"], blocked["doc_id"]) == (
        "00700", "dividend", "2026040100001",
    )
    assert (blocked["period_end"], blocked["dividend_type"]) == ("2025-12-31", "末期")
    assert blocked["ex_dates"] == ["2026-05-15"]
    assert "無法識別派息金額" in blocked["reason"]
    # 已有建议与事件原样保留：不按旧金额刷新、也不当成撤回删除
    (after,) = _suggestions(db, "00700")
    assert (after.id, after.updated_at, Decimal(str(after.cash_div_pre_tax))) == (
        before.id, before.updated_at, Decimal("5.3"),
    )
    assert [e.event_date for e in db.query(SecurityEvent).all()] == event_dates
    assert result["stale_removed"] == 0 and result["events_removed"] == 0
    # 回填同步也一样：从零开始时不产生任何建议
    _cleanup(db)
    _seed_hk_holding(db, "00700")
    _buy(db, "00700", "100", date(2026, 1, 10))
    fresh = FakeHkex(monkeypatch)
    fresh.publish("00700", "2026031800477", "2026-03-18T17:09:00", _text("00700_final_2025.txt"))
    fresh.publish("00700", "2026040100001", "2026-04-01T09:00:00", _unparsable_update_of_00700())
    result = svc.sync_dividends_for_user(db, 1)
    assert _suggestions(db, "00700") == []
    assert db.query(SecurityEvent).count() == 0
    assert len(result["hk_blocked"]) == 1


def test_unparsed_form_of_another_dividend_does_not_block_the_rest(db, monkeypatch):
    fake = FakeHkex(monkeypatch)
    fake.publish("00700", "2026031800477", "2026-03-18T17:09:00", _text("00700_final_2025.txt"))
    other = (
        _text("00700_final_2025.txt")
        .replace("宣派股息的報告期末 2025年12月31日", "宣派股息的報告期末 2026年6月30日")
        .replace("股息類型 末期", "股息類型 中期（半年期）")
        .replace("除淨日 2026年5月15日", "除淨日 待定")
    )
    fake.publish("00700", "2026082000001", "2026-08-20T17:00:00", other)
    _seed_hk_holding(db, "00700")
    _buy(db, "00700", "100", date(2026, 1, 10))

    result = svc.sync_dividends_for_user(db, 1)
    assert [(u["doc_id"], u["reason"]) for u in result["hk_unparsed_forms"]] == [
        ("2026082000001", "缺少除淨日"),
    ]
    assert [(b["scope"], b["period_end"]) for b in result["hk_blocked"]] == [
        ("dividend", "2026-06-30"),
    ]
    (row,) = _suggestions(db, "00700")
    assert row.ex_date == date(2026, 5, 15)


def test_form_without_identity_blocks_the_whole_symbol(db, monkeypatch):
    """认不出是哪一笔股息的公告（含身份缺失的撤回）：整个标的本次不写不删。"""
    fake = FakeHkex(monkeypatch)
    fake.publish("00700", "2026031800477", "2026-03-18T17:09:00", _text("00700_final_2025.txt"))
    _seed_hk_holding(db, "00700")
    _buy(db, "00700", "100", date(2026, 1, 10))
    svc.sync_dividends_for_user(db, 1)
    (before,) = _suggestions(db, "00700")

    anonymous_withdrawal = (
        _text("00700_final_2025.txt")
        .replace("公告狀態 新公告", "公告狀態 撤回公告")
        .replace("宣派股息的報告期末 2025年12月31日", "宣派股息的報告期末 不適用")
    )
    fake.publish("00700", "2026040100002", "2026-04-01T10:00:00", anonymous_withdrawal)
    result = svc.sync_dividends_for_user(db, 1)

    (blocked,) = result["hk_blocked"]
    assert (blocked["symbol"], blocked["scope"]) == ("00700", "symbol")
    assert [f["doc_id"] for f in blocked["forms"]] == ["2026040100002"]
    (after,) = _suggestions(db, "00700")
    assert after.id == before.id and after.status == before.status
    assert db.query(SecurityEvent).count() == 1


@pytest.mark.parametrize("kind", ["update", "withdrawal"])
def test_form_missing_nature_blocks_whole_symbol_and_keeps_old_rows(db, monkeypatch, kind):
    """缺「股息性質」的更新/撤回公告：认不出对应哪一笔，整标的不写不删，旧金额不刷新。"""
    fake = FakeHkex(monkeypatch)
    fake.publish("00700", "2026031800477", "2026-03-18T17:09:00", _text("00700_final_2025.txt"))
    _seed_hk_holding(db, "00700")
    _buy(db, "00700", "100", date(2026, 1, 10))
    svc.sync_dividends_for_user(db, 1)
    (before,) = _suggestions(db, "00700")

    text = _text("00700_final_2025.txt").replace("股息性質 普通股息\n", "")
    if kind == "update":
        text = text.replace("公告狀態 新公告", "公告狀態 更新公告").replace(
            "派息金額及公司預設派發貨幣 每 股 5.3HKD", "派息金額及公司預設派發貨幣 每 股 6.1HKD"
        )
    else:
        text = text.replace("公告狀態 新公告", "公告狀態 撤回公告").replace(
            "除淨日 2026年5月15日", "除淨日 不適用"
        )
    fake.publish("00700", "2026040100005", "2026-04-01T11:00:00", text)
    result = svc.sync_dividends_for_user(db, 1)

    (blocked,) = result["hk_blocked"]
    assert (blocked["scope"], [f["doc_id"] for f in blocked["forms"]]) == (
        "symbol", ["2026040100005"],
    )
    (after,) = _suggestions(db, "00700")
    assert (after.id, after.updated_at, Decimal(str(after.cash_div_pre_tax))) == (
        before.id, before.updated_at, Decimal("5.3"),
    )
    assert db.query(SecurityEvent).count() == 1


def test_download_failure_fails_only_that_symbol(db, monkeypatch):
    fake = FakeHkex(monkeypatch)
    url = fake.publish("00700", "2026031800477", "2026-03-18T17:09:00",
                       _text("00700_final_2025.txt"))
    fake.fail_urls.add(url)
    fake.publish("00883", "2026082600001", "2026-08-26T17:00:00",
                 _text("00883_interim_2026.txt"))
    for symbol in ("00700", "00883"):
        _seed_hk_holding(db, symbol)
        _buy(db, symbol, "100", date(2026, 1, 10))

    result = svc.sync_dividends_for_user(db, 1)
    assert [(f["symbol"], f["market"]) for f in result["failed"]] == [("00700", "港股")]
    assert _suggestions(db, "00700") == []
    assert len(_suggestions(db, "00883")) == 1
    # 失败标的没有落任何缓存行（下次整标的重来）
    assert db.query(SecurityProfileData).filter_by(symbol="00700").count() == 0


def test_lost_job_ownership_aborts_instead_of_counting_as_failure(db, monkeypatch):
    fake = FakeHkex(monkeypatch)
    fake.publish("00700", "2026031800477", "2026-03-18T17:09:00", _text("00700_final_2025.txt"))
    _seed_hk_holding(db, "00700")
    _buy(db, "00700", "100", date(2026, 1, 10))
    calls = []

    def progress(**fields):
        calls.append(fields)
        if len(calls) > 1:  # 第一份表格下载完的续租回写：已被接管
            raise JobOwnershipLostError("job")

    with pytest.raises(JobOwnershipLostError):
        svc.sync_dividends_for_user(db, 1, progress=progress)
    assert _suggestions(db, "00700") == []


# ---------------------------------------------------------------------------
# 复权因子（编排：缓存表格 + security_prices + 汇率表）
# ---------------------------------------------------------------------------


def _price(db, symbol, on, close):
    db.add(SecurityPrice(symbol=symbol, market="港股", price_date=on, currency="HKD",
                         close_price=Decimal(close), source="test"))


def test_adj_factors_written_for_usd_dividend_converted_to_hkd(db, monkeypatch):
    fake = FakeHkex(monkeypatch)
    fake.publish("09618", "2026030501475", "2026-03-05T18:10:00",
                 _text("09618_final_2025_usd.txt"))
    _seed_hk_holding(db, "09618")
    _buy(db, "09618", "100", date(2026, 1, 10))
    for on, close in [(date(2026, 4, 2), "120"), (date(2026, 4, 7), "125"),
                      (date(2026, 4, 8), "121"), (date(2026, 4, 9), "122")]:
        _price(db, "09618", on, close)
    db.add_all([
        ExchangeRate(from_currency="USD", to_currency="CNY", rate=Decimal("7.2"),
                     effective_date=date(2026, 4, 1), source=RATE_SOURCE, is_active=True),
        ExchangeRate(from_currency="HKD", to_currency="CNY", rate=Decimal("0.9"),
                     effective_date=date(2026, 4, 1), source=RATE_SOURCE, is_active=True),
        # 除净日之后的汇率不得参与
        ExchangeRate(from_currency="USD", to_currency="CNY", rate=Decimal("9"),
                     effective_date=date(2026, 4, 9), source=RATE_SOURCE, is_active=True),
    ])
    db.commit()

    result = svc.sync_dividends_for_user(db, 1)
    assert result["hk_adj_rows_updated"] == 4

    dividend_hkd = Decimal("0.5") * Decimal("7.2") / Decimal("0.9")  # 4 HKD
    expected = (1 / ((Decimal("125") - dividend_hkd) / Decimal("125"))).quantize(
        Decimal("0.00000001")
    )
    rows = {
        r.price_date: r for r in db.query(SecurityPrice).filter_by(symbol="09618").all()
    }
    assert Decimal(str(rows[date(2026, 4, 2)].adj_factor)) == Decimal("1")
    assert Decimal(str(rows[date(2026, 4, 7)].adj_factor)) == Decimal("1")
    assert Decimal(str(rows[date(2026, 4, 8)].adj_factor)) == expected
    assert Decimal(str(rows[date(2026, 4, 9)].adj_factor)) == expected
    assert Decimal(str(rows[date(2026, 4, 8)].adj_close_price)) == (
        Decimal("121") * expected
    ).quantize(Decimal("0.00000001"))
    assert Decimal(str(rows[date(2026, 4, 2)].adj_close_price)) == Decimal("120")

    # 幂等：无变化不写
    assert recompute_hk_adj_factors(db, "09618")["updated"] == 0


def test_adj_factor_null_after_unresolved_ex_date(db, monkeypatch):
    fake = FakeHkex(monkeypatch)
    fake.publish("09618", "2026030501475", "2026-03-05T18:10:00",
                 _text("09618_final_2025_usd.txt"))
    _seed_hk_holding(db, "09618")
    _buy(db, "09618", "100", date(2026, 1, 10))
    _price(db, "09618", date(2026, 4, 7), "125")
    _price(db, "09618", date(2026, 4, 8), "121")
    db.commit()

    svc.sync_dividends_for_user(db, 1)  # 汇率表里没有 USD：无法折港元
    rows = {
        r.price_date: r for r in db.query(SecurityPrice).filter_by(symbol="09618").all()
    }
    assert Decimal(str(rows[date(2026, 4, 7)].adj_factor)) == Decimal("1")
    assert rows[date(2026, 4, 8)].adj_factor is None
    assert rows[date(2026, 4, 8)].adj_close_price is None


# ---------------------------------------------------------------------------
# 周期入口
# ---------------------------------------------------------------------------


def test_periodic_entry_without_token_enqueues_only_hk_users(db, monkeypatch):
    from app.services import dividend_sync_jobs as jobs

    monkeypatch.setattr(jobs.settings, "dividend_sync_periodic_enabled", True)
    db.add(Holding(user_id=2, symbol="600036", name="招商银行", market="A股",
                   quantity=Decimal("100"), avg_cost=Decimal("30"),
                   total_cost=Decimal("3000"), currency="CNY"))
    db.commit()
    assert jobs.enqueue_periodic_dividend_sync() == 0

    _seed_hk_holding(db, "00700")
    assert jobs.enqueue_periodic_dividend_sync() == 1
    job = db.query(BackgroundJob).filter(BackgroundJob.job_type == "dividend_sync").one()
    assert job.user_id == 1
