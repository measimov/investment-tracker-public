"""交易时段自动刷新实时价（持仓 + 自选）与自选「加入以来涨跌幅」。

覆盖：交易时段判定（午休、收盘余量、夏令时）；昨收解析；美股盘中 Tiingo 提前；
refresh_quotes 跨用户按标的去重、写回持仓与自选、基准价三种口径、新鲜度窗口内共享报价、
异动候选；周期入口的 PeriodicOutcome；加入自选即时报价；存量基准价回填。
全部外呼打桩。
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from app.config import settings
from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.holding import Holding
from app.models.security_price import SecurityPrice
from app.models.user import User
from app.models.watchlist_item import WatchlistItem
from app.services import market_sessions, price_refresh_jobs, tiingo_source
from app.services import stock_price_service as sps
from app.services.stock_price_service import price_result, refresh_quotes
from app.services.watchlist_price_service import backfill_added_prices

SYMBOLS = ("QRA001", "QRA002", "QRA003")


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 交易时段
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "market, moment, expected",
    [
        ("A股", _utc(2026, 9, 28, 2, 0), True),  # 周一 10:00 北京
        ("A股", _utc(2026, 9, 28, 4, 0), False),  # 12:00 午休
        ("A股", _utc(2026, 9, 28, 7, 10), True),  # 15:10 收盘余量内
        ("A股", _utc(2026, 9, 28, 7, 25), True),  # 15:25 收盘余量内
        ("A股", _utc(2026, 9, 28, 7, 35), False),  # 15:35
        ("A股", _utc(2026, 9, 27, 2, 0), False),  # 周日
        ("B股", _utc(2026, 9, 28, 1, 30), True),  # 09:30
        ("港股", _utc(2026, 9, 28, 8, 20), True),  # 16:20 香港，收盘余量内
        ("港股", _utc(2026, 9, 28, 8, 35), True),  # 16:35 收盘余量内
        ("港股", _utc(2026, 9, 28, 8, 45), False),  # 16:45
        ("港股", _utc(2026, 9, 28, 4, 30), False),  # 12:30 午休
        ("美股", _utc(2026, 7, 15, 13, 30), True),  # 09:30 EDT
        ("美股", _utc(2026, 12, 15, 14, 0), False),  # 09:00 EST（冬令时）
        ("美股", _utc(2026, 12, 15, 14, 30), True),  # 09:30 EST
        ("美股", _utc(2026, 12, 15, 21, 15), True),  # 16:15 EST 收盘余量内
        ("美股", _utc(2026, 12, 15, 21, 25), True),  # 16:25 EST
        ("美股", _utc(2026, 12, 15, 21, 35), False),  # 16:35 EST
        ("新加坡股", _utc(2026, 9, 28, 2, 0), False),  # 未登记时段
    ],
)
def test_session_windows(market, moment, expected):
    assert market_sessions.is_session_open(market, moment) is expected


def test_session_requires_aware_time_and_lists_open_markets():
    with pytest.raises(ValueError):
        market_sessions.is_session_open("A股", datetime(2026, 9, 28, 10, 0))
    # 北京 09:30：A/B 股开市，港股 09:30 也在时段内，美股（前一日 21:30 ET）已收
    assert market_sessions.open_markets(_utc(2026, 9, 28, 1, 30)) == ["A股", "B股", "港股"]


# --------------------------------------------------------------------------- #
# 昨收与美股报价顺序
# --------------------------------------------------------------------------- #
def test_tencent_prev_close_parsed_and_tolerant():
    code = "sh600000"
    fields = ["1", "浦发银行", "600000", "10.50", "10.00"] + [""] * 26
    text = f'v_{code}="{"~".join(fields)}";'
    assert sps.parse_tencent_quote_prev_close(text, code) == Decimal("10.00")
    broken = f'v_{code}="1~x~600000~10.5~-";'
    assert sps.parse_tencent_quote_prev_close(broken, code) is None


def test_tiingo_iex_quote_carries_prev_close(monkeypatch):
    payload = [
        {
            "ticker": "PDD",
            "timestamp": "2026-09-25T20:00:00+00:00",
            "tngoLast": 120.0,
            "prevClose": 121.05,
        }
    ]
    assert tiingo_source.parse_iex_quote(payload)["prev_close"] == Decimal("121.05")
    payload[0]["prevClose"] = None
    assert tiingo_source.parse_iex_quote(payload)["prev_close"] is None


def test_failed_price_result_has_no_prev_close():
    result = price_result(price=None, source="x", success=False, prev_close=Decimal("1"))
    assert result["prev_close"] is None


@pytest.fixture
def us_chain(monkeypatch):
    from app.services import xueqiu_source

    calls = []

    def make(name, ok):
        def fetch(symbol, *args, **kwargs):
            calls.append(name)
            if ok:
                return price_result(price=Decimal("1"), source=name, success=True)
            return price_result(price=None, source=name, success=False, error=f"E-{name}")

        return fetch

    def install(tushare_ok=True, tiingo_ok=True, xueqiu_ok=True):
        monkeypatch.setattr(sps, "fetch_us_stock_price_tushare", make("tushare", tushare_ok))
        monkeypatch.setattr(tiingo_source, "fetch_tiingo_stock_price", make("tiingo", tiingo_ok))
        monkeypatch.setattr(xueqiu_source, "fetch_xueqiu_stock_price", make("xueqiu", xueqiu_ok))

    return calls, install


def test_us_in_session_prefers_tiingo(monkeypatch, us_chain):
    calls, install = us_chain
    install()
    monkeypatch.setattr(sps, "us_session_open", lambda now=None: True)
    assert sps.fetch_us_stock_price("PDD")["source"] == "tiingo"
    assert calls == ["tiingo"]


def test_us_in_session_falls_back_in_order(monkeypatch, us_chain):
    calls, install = us_chain
    install(tushare_ok=False, tiingo_ok=False, xueqiu_ok=False)
    monkeypatch.setattr(sps, "us_session_open", lambda now=None: True)
    result = sps.fetch_us_stock_price("PDD")
    assert calls == ["tiingo", "tushare", "xueqiu"]
    assert result["error"] == "E-tiingo; E-tushare; E-xueqiu"


def test_us_out_of_session_keeps_tushare_first(monkeypatch, us_chain):
    calls, install = us_chain
    install()
    monkeypatch.setattr(sps, "us_session_open", lambda now=None: False)
    assert sps.fetch_us_stock_price("PDD")["source"] == "tushare"
    assert calls == ["tushare"]


# --------------------------------------------------------------------------- #
# refresh_quotes
# --------------------------------------------------------------------------- #
def _cleanup(db):
    db.query(Holding).filter(Holding.symbol.in_(SYMBOLS)).delete(synchronize_session=False)
    db.query(WatchlistItem).filter(WatchlistItem.symbol.in_(SYMBOLS)).delete(
        synchronize_session=False
    )
    db.query(SecurityPrice).filter(SecurityPrice.symbol.in_(SYMBOLS)).delete(
        synchronize_session=False
    )
    db.commit()


@pytest.fixture
def db():
    session = SessionLocal()
    _cleanup(session)
    try:
        yield session
    finally:
        session.rollback()
        _cleanup(session)
        session.query(User).update({User.is_active: True})
        session.commit()
        session.close()


def _holding(user_id, symbol, quantity="100", market="A股", **kwargs):
    return Holding(
        user_id=user_id,
        broker_account_id=None,
        symbol=symbol,
        name=f"名{symbol}",
        market=market,
        quantity=Decimal(quantity),
        avg_cost=Decimal("10"),
        total_cost=Decimal("1000"),
        currency="CNY",
        **kwargs,
    )


def _watch(user_id, symbol, market="A股", **kwargs):
    return WatchlistItem(user_id=user_id, symbol=symbol, market=market, **kwargs)


def _fake_fetch(monkeypatch, prices, prev=None, fail=()):
    calls = []

    def fetch(symbol, market):
        calls.append((symbol, market))
        if symbol in fail:
            return price_result(price=None, source="t", success=False, error=f"E-{symbol}")
        return price_result(
            price=Decimal(prices[symbol]),
            source="tencent-quote",
            success=True,
            as_of=date(2026, 9, 28),
            prev_close=Decimal(prev[symbol]) if prev and symbol in prev else None,
        )

    monkeypatch.setattr(sps, "fetch_stock_price", fetch)
    return calls


def test_refresh_dedupes_across_users_and_writes_holdings_and_watchlist(db, monkeypatch):
    db.add_all(
        [
            _holding(1, "QRA001"),
            _holding(2, "QRA001"),
            _watch(2, "QRA001"),
            _watch(1, "QRA002"),
        ]
    )
    db.commit()
    calls = _fake_fetch(
        monkeypatch, {"QRA001": "11", "QRA002": "5"}, prev={"QRA001": "10", "QRA002": "4"}
    )

    result = refresh_quotes(db)

    assert sorted(calls) == [("QRA001", "A股"), ("QRA002", "A股")]  # 每标的只请求一次
    assert result["success_count"] == 2 and result["failed_count"] == 0
    db.expire_all()
    for row in db.query(Holding).filter(Holding.symbol == "QRA001"):
        assert row.current_price == Decimal("11")
        assert row.price_as_of == date(2026, 9, 28) and row.price_source == "tencent-quote"
    watch = db.query(WatchlistItem).filter_by(user_id=2, symbol="QRA001").one()
    assert watch.current_price == Decimal("11") and watch.price_source == "tencent-quote"
    # 刚加入的条目：这次报价就是加入时报价
    assert watch.added_price == Decimal("11")
    assert watch.added_price_basis == "quote"
    assert watch.added_price_date == date(2026, 9, 28)
    # 异动候选只来自有持仓的标的（QRA002 只在自选里）
    assert [(m["symbol"], round(m["pct"], 6)) for m in result["moves"]] == [("QRA001", 10.0)]
    assert result["moves"][0]["name"] == "名QRA001"


def test_added_price_is_never_overwritten_and_late_quote_is_left_for_backfill(db, monkeypatch):
    """加入 1 小时后的首次报价不直接作基准（周末加入、周一才刷新到的是周一的价）——
    留给按加入日收盘回填。"""
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    db.add_all(
        [
            _watch(
                1,
                "QRA001",
                added_price=Decimal("8"),
                added_price_basis="close_on_add",
                added_price_date=date(2026, 9, 1),
            ),
            _watch(1, "QRA002", created_at=old),
        ]
    )
    db.commit()
    _fake_fetch(monkeypatch, {"QRA001": "11", "QRA002": "5"})

    refresh_quotes(db, user_id=1)

    db.expire_all()
    kept = db.query(WatchlistItem).filter_by(symbol="QRA001").one()
    assert kept.added_price == Decimal("8") and kept.added_price_basis == "close_on_add"
    late = db.query(WatchlistItem).filter_by(symbol="QRA002").one()
    assert late.current_price == Decimal("5")
    assert late.added_price is None and late.added_price_basis is None


def test_old_watchlist_item_is_left_for_close_on_add_backfill(db, monkeypatch):
    """上线前加入的存量条目不能被首次报价写成今天的价格——留给按加入日收盘回填。"""
    long_ago = datetime.now(timezone.utc) - timedelta(days=40)
    db.add(_watch(1, "QRA001", created_at=long_ago))
    db.commit()
    _fake_fetch(monkeypatch, {"QRA001": "11"})

    refresh_quotes(db, user_id=1)

    db.expire_all()
    item = db.query(WatchlistItem).filter_by(symbol="QRA001").one()
    assert item.current_price == Decimal("11")
    assert item.added_price is None and item.added_price_basis is None


def test_row_deleted_during_fetch_does_not_lose_the_round(db, monkeypatch):
    """外呼期间某行被删除（清仓/移出自选）：只影响那一行，其余行照常写入。"""
    db.add_all([_holding(1, "QRA001"), _watch(1, "QRA002"), _holding(2, "QRA003")])
    db.commit()

    def fetch(symbol, market):
        if symbol == "QRA001":
            other = SessionLocal()
            other.query(Holding).filter_by(symbol="QRA001").delete()
            other.query(WatchlistItem).filter_by(symbol="QRA002").delete()
            other.commit()
            other.close()
        return price_result(price=Decimal("7"), source="t", success=True, as_of=date(2026, 9, 28))

    monkeypatch.setattr(sps, "fetch_stock_price", fetch)
    monkeypatch.setattr(settings, "price_refresh_max_workers", 1)

    result = refresh_quotes(db)

    assert result["success"] is True
    db.expire_all()
    assert db.query(Holding).filter_by(symbol="QRA003").one().current_price == Decimal("7")


def test_older_trading_day_never_overwrites_newer_quote(db, monkeypatch):
    """兜底源拿到上一交易日收盘（如 Tiingo 限流后退到 us_daily）不能盖掉今天的价格。"""
    earlier = datetime.now(timezone.utc) - timedelta(hours=2)
    db.add(
        _holding(
            1,
            "QRA001",
            current_price=Decimal("12"),
            price_updated_at=earlier,
            price_as_of=date(2026, 9, 28),
            price_source="tiingo-iex",
        )
    )
    db.commit()

    def fetch(symbol, market):
        return price_result(
            price=Decimal("10"), source="tushare", success=True, as_of=date(2026, 9, 25)
        )

    monkeypatch.setattr(sps, "fetch_stock_price", fetch)
    refresh_quotes(db, user_id=1)

    db.expire_all()
    row = db.query(Holding).filter_by(symbol="QRA001").one()
    assert row.current_price == Decimal("12") and row.price_as_of == date(2026, 9, 28)


def test_periodic_refresh_ignores_freshness_window(db, monkeypatch):
    """收盘前几分钟的手动刷新不能让收盘后的那次周期刷新被跳过。"""
    recent = datetime.now(timezone.utc) - timedelta(seconds=60)
    db.add(_holding(1, "QRA001", current_price=Decimal("9"), price_updated_at=recent))
    db.commit()
    calls = _fake_fetch(monkeypatch, {"QRA001": "9.5"})

    refresh_quotes(db, markets=["A股"], only_open=True, ignore_freshness=True)

    assert calls == [("QRA001", "A股")]


def test_only_open_skips_closed_positions_and_inactive_users(db, monkeypatch):
    db.add_all([_holding(1, "QRA001", quantity="0"), _holding(2, "QRA002")])
    db.commit()
    db.query(User).filter(User.id == 2).update({User.is_active: False})
    db.commit()
    calls = _fake_fetch(monkeypatch, {"QRA001": "1", "QRA002": "1"})

    result = refresh_quotes(db, markets=["A股"], only_open=True)

    assert calls == [] and result["success_count"] == 0
    # 手动刷新（指定用户）沿用历史行为：清仓的行也刷新
    refresh_quotes(db, user_id=1)
    assert calls == [("QRA001", "A股")]


def test_markets_filter(db, monkeypatch):
    db.add_all([_holding(1, "QRA001"), _holding(1, "QRA002", market="港股")])
    db.commit()
    calls = _fake_fetch(monkeypatch, {"QRA001": "1", "QRA002": "2"})
    refresh_quotes(db, markets=["港股"], only_open=True)
    assert calls == [("QRA002", "港股")]


def test_fresh_quote_is_shared_without_fetch(db, monkeypatch):
    recent = datetime.now(timezone.utc) - timedelta(seconds=60)
    db.add_all(
        [
            _holding(
                1,
                "QRA001",
                current_price=Decimal("9.5"),
                price_updated_at=recent,
                price_as_of=date(2026, 9, 28),
                price_source="tencent-quote",
            ),
            _watch(1, "QRA001"),
        ]
    )
    db.commit()
    calls = _fake_fetch(monkeypatch, {"QRA001": "99"})

    result = refresh_quotes(db, user_id=1)

    assert calls == [] and result["skipped_count"] == 1
    db.expire_all()
    watch = db.query(WatchlistItem).filter_by(symbol="QRA001").one()
    assert watch.current_price == Decimal("9.5") and watch.price_as_of == date(2026, 9, 28)
    assert watch.added_price == Decimal("9.5") and watch.added_price_basis == "quote"


def test_failures_are_reported_per_symbol(db, monkeypatch):
    db.add_all([_holding(1, "QRA001"), _holding(1, "QRA002")])
    db.commit()
    _fake_fetch(monkeypatch, {"QRA001": "1"}, fail=("QRA002",))
    result = refresh_quotes(db, user_id=1)
    assert result["success_count"] == 1 and result["failed_count"] == 1
    assert result["failed_list"][0]["error"] == "E-QRA002"


# --------------------------------------------------------------------------- #
# 周期入口
# --------------------------------------------------------------------------- #
def test_periodic_disabled_and_closed_market_are_skipped(monkeypatch):
    monkeypatch.setattr(settings, "quote_auto_refresh_enabled", False)
    assert price_refresh_jobs.periodic_refresh_quotes().status == "skipped"
    monkeypatch.setattr(settings, "quote_auto_refresh_enabled", True)
    monkeypatch.setattr(price_refresh_jobs, "open_markets", lambda now: [])
    assert price_refresh_jobs.periodic_refresh_quotes().status == "skipped"


def test_periodic_refresh_notifies_moves_and_reports_failures(db, monkeypatch):
    from app.services import event_notifications

    monkeypatch.setattr(settings, "quote_auto_refresh_enabled", True)
    monkeypatch.setattr(price_refresh_jobs, "open_markets", lambda now: ["A股"])
    db.add_all([_holding(1, "QRA001"), _holding(1, "QRA002", quantity="0")])
    db.commit()
    notified = []
    monkeypatch.setattr(
        event_notifications, "notify_price_moves", lambda session, moves: notified.append(moves)
    )
    _fake_fetch(monkeypatch, {"QRA001": "12"}, prev={"QRA001": "10"})

    outcome = price_refresh_jobs.periodic_refresh_quotes()

    assert outcome.status == "succeeded" and outcome.count == 1
    assert [m["symbol"] for m in notified[0]] == ["QRA001"]

    # 全部失败 → failed（计入周期任务连续失败告警）
    db.query(Holding).filter(Holding.symbol == "QRA001").update({Holding.price_updated_at: None})
    db.commit()
    _fake_fetch(monkeypatch, {}, fail=("QRA001",))
    outcome = price_refresh_jobs.periodic_refresh_quotes()
    assert outcome.status == "failed" and "E-QRA001" in outcome.reason


def test_periodic_notification_errors_do_not_fail_refresh(db, monkeypatch):
    from app.services import event_notifications

    monkeypatch.setattr(settings, "quote_auto_refresh_enabled", True)
    monkeypatch.setattr(price_refresh_jobs, "open_markets", lambda now: ["A股"])
    db.add(_holding(1, "QRA001"))
    db.commit()

    def boom(session, moves):
        raise RuntimeError("bark down")

    monkeypatch.setattr(event_notifications, "notify_price_moves", boom)
    _fake_fetch(monkeypatch, {"QRA001": "12"}, prev={"QRA001": "10"})
    assert price_refresh_jobs.periodic_refresh_quotes().status == "succeeded"


# --------------------------------------------------------------------------- #
# 加入自选即时报价 + API 字段
# --------------------------------------------------------------------------- #
@pytest.fixture
def api_user():
    session = SessionLocal()
    try:
        user = session.query(User).filter(User.username == "demo").one()
        original = user.hashed_password
        user.hashed_password = get_password_hash("quote-refresh-password")
        session.commit()
        yield user.id
        user = session.query(User).filter(User.username == "demo").one()
        user.hashed_password = original
        session.commit()
    finally:
        session.close()


@pytest.mark.anyio
async def test_add_to_watchlist_fetches_quote_and_reports_change(db, api_user, monkeypatch):
    monkeypatch.setattr(settings, "quote_auto_refresh_enabled", True)
    calls = _fake_fetch(monkeypatch, {"QRA003": "20"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = (
            await client.post(
                "/api/auth/token",
                json={"username": "demo", "password": "quote-refresh-password"},
            )
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        created = await client.post(
            "/api/watchlist", json={"symbol": "QRA003", "market": "A股"}, headers=headers
        )
        assert created.status_code == 201
        assert calls == [("QRA003", "A股")]

        # 行情变化后 change_since_added_pct 相对加入时报价
        session = SessionLocal()
        session.query(WatchlistItem).filter_by(symbol="QRA003").update(
            {WatchlistItem.current_price: Decimal("22")}
        )
        session.commit()
        session.close()

        listed = (await client.get("/api/watchlist", headers=headers)).json()
        row = next(item for item in listed if item["symbol"] == "QRA003")
        assert Decimal(row["added_price"]) == Decimal("20")
        assert row["added_price_basis"] == "quote"
        assert row["price_source"] == "tencent-quote"
        assert row["price_as_of"] == "2026-09-28"
        assert row["change_since_added_pct"] == pytest.approx(0.1)


@pytest.mark.anyio
async def test_add_to_watchlist_without_auto_refresh_does_not_fetch(db, api_user, monkeypatch):
    monkeypatch.setattr(settings, "quote_auto_refresh_enabled", False)
    calls = _fake_fetch(monkeypatch, {"QRA003": "20"})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        token = (
            await client.post(
                "/api/auth/token",
                json={"username": "demo", "password": "quote-refresh-password"},
            )
        ).json()["access_token"]
        created = await client.post(
            "/api/watchlist",
            json={"symbol": "QRA003", "market": "A股"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert created.status_code == 201
        assert calls == []
        assert created.json()["change_since_added_pct"] is None


# --------------------------------------------------------------------------- #
# 存量基准价回填
# --------------------------------------------------------------------------- #
def _close(symbol, day, close):
    return SecurityPrice(
        symbol=symbol, market="A股", price_date=day, close_price=Decimal(close), source="test"
    )


def test_backfill_uses_close_on_or_before_add_date_within_7_days(db):
    # 北京时间 2026-09-20 10:00 加入（UTC 02:00）
    added = _utc(2026, 9, 20, 2, 0)
    db.add_all(
        [
            _watch(1, "QRA001", created_at=added),
            _watch(1, "QRA002", created_at=added),
            _watch(
                1, "QRA003", created_at=added, added_price=Decimal("1"), added_price_basis="quote"
            ),
            _close("QRA001", date(2026, 9, 18), "7.5"),
            _close("QRA001", date(2026, 9, 21), "9.9"),  # 加入之后，不用
            _close("QRA002", date(2026, 9, 10), "3"),  # 早于加入日 7 天以上，不用
        ]
    )
    db.commit()

    preview = backfill_added_prices(db, dry_run=True, head_probed={})
    assert [e["symbol"] for e in preview["filled"]] == ["QRA001"]
    # QRA002 的历史早于加入日 − 7 天（已探过），加入日前后仍没有收盘 → 等首次报价
    assert [e["symbol"] for e in preview["pending_quote"]] == ["QRA002"]
    db.expire_all()
    assert db.query(WatchlistItem).filter_by(symbol="QRA001").one().added_price is None

    result = backfill_added_prices(db, head_probed={})
    assert result["candidates"] == 2
    db.expire_all()
    item = db.query(WatchlistItem).filter_by(symbol="QRA001").one()
    assert item.added_price == Decimal("7.5")
    assert item.added_price_date == date(2026, 9, 18)
    assert item.added_price_basis == "close_on_add"
    assert db.query(WatchlistItem).filter_by(symbol="QRA002").one().added_price_basis == (
        "pending_quote"
    )
    # 幂等：再跑只剩取不到的那条
    assert backfill_added_prices(db, head_probed={})["candidates"] == 1


def test_old_item_with_history_gap_gets_a_stable_baseline_eventually(db, monkeypatch):
    """老条目 + 加入日前后的历史缺口：探过历史前不动；探过后取加入后首个收盘；
    连这也没有则下一次成功报价写入基准（first_quote），之后不再变。"""
    added = _utc(2026, 8, 1, 2, 0)
    db.add_all([_watch(1, "QRA001", created_at=added), _watch(1, "QRA002", created_at=added)])
    db.add(_close("QRA001", date(2026, 8, 20), "6"))  # 只有加入之后的行情
    db.commit()

    # 头部历史还没探过：不能抢先用加入后的价格
    waiting = backfill_added_prices(db, head_probed={})
    assert {e["symbol"] for e in waiting["missing"]} == {"QRA001", "QRA002"}

    probed = {"QRA001|A股": "2026-07-25", "QRA002|A股": "2026-07-25"}
    result = backfill_added_prices(db, head_probed=probed)
    assert [(e["symbol"], e["basis"]) for e in result["filled"]] == [("QRA001", "close_after_add")]
    assert [e["symbol"] for e in result["pending_quote"]] == ["QRA002"]

    _fake_fetch(monkeypatch, {"QRA001": "7", "QRA002": "5"})
    refresh_quotes(db, user_id=1)
    refresh_quotes(db, user_id=1, ignore_freshness=True)  # 再刷一次，基准不变
    db.expire_all()
    first = db.query(WatchlistItem).filter_by(symbol="QRA001").one()
    assert first.added_price == Decimal("6") and first.added_price_basis == "close_after_add"
    second = db.query(WatchlistItem).filter_by(symbol="QRA002").one()
    assert second.added_price == Decimal("5") and second.added_price_basis == "first_quote"


def test_backfill_waits_for_quote_within_first_hour_then_uses_close_on_add(db):
    """刚加入（1 小时内）先让加入时报价去填；之后按加入日收盘补（周末加入取周五收盘）。"""
    just_now = datetime.now(timezone.utc) - timedelta(minutes=10)
    weekend = _utc(2026, 9, 26, 12, 0)  # 周六
    db.add_all(
        [
            _watch(1, "QRA001", created_at=just_now),
            _watch(1, "QRA002", created_at=weekend),
            _close("QRA001", date(2026, 9, 24), "3"),
            _close("QRA002", date(2026, 9, 24), "8.8"),
        ]
    )
    db.commit()
    result = backfill_added_prices(db, head_probed={})
    assert [e["symbol"] for e in result["missing"]] == ["QRA001"]
    assert [(e["symbol"], e["basis"]) for e in result["filled"]] == [("QRA002", "close_on_add")]
