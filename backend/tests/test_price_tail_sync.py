"""跟踪标的日线尾部同步 + A股 daily_basic 每日刷新（services/price_tail_sync.py）。

外呼全部打桩：交易日历、增量历史同步、tushare_query。
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from app.config import settings
from app.core.timeutil import business_timezone
from app.database import SessionLocal
from app.models.holding import Holding
from app.models.scheduled_task_state import ScheduledTaskState
from app.models.security_price import SecurityPrice
from app.models.security_profile import SecurityProfileData
from app.models.user import User
from app.models.watchlist_item import WatchlistItem
from app.services import market_data_service, price_tail_sync, stock_price_service
from app.services.price_tail_sync import plan_tail_targets

from .helpers import reset_tables

SYMBOLS = ["600519", "000001", "600036", "PDD", "900901"]
TARGET = date(2026, 9, 25)
TODAY = date(2026, 9, 28)


def _cleanup(session):
    reset_tables(session, [WatchlistItem, Holding])
    session.query(SecurityPrice).filter(SecurityPrice.symbol.in_(SYMBOLS)).delete(
        synchronize_session=False
    )
    session.query(SecurityProfileData).filter(SecurityProfileData.symbol.in_(SYMBOLS)).delete(
        synchronize_session=False
    )
    session.query(ScheduledTaskState).delete()
    session.query(User).filter(User.username == "tail_inactive").delete()
    session.commit()


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        _cleanup(session)
        yield session
        session.rollback()
        _cleanup(session)
    finally:
        session.close()


def _uid(db):
    return db.query(User).filter(User.username == "demo").one().id


def _hold(db, symbol, market, user_id=None, quantity="100"):
    db.add(
        Holding(
            user_id=user_id or _uid(db),
            symbol=symbol,
            market=market,
            quantity=Decimal(quantity),
            avg_cost=Decimal("1"),
            total_cost=Decimal(quantity),
            currency="CNY",
        )
    )


def _price(db, symbol, market, day, close="10"):
    db.add(
        SecurityPrice(
            symbol=symbol,
            market=market,
            price_date=day,
            close_price=Decimal(close),
            currency="CNY",
            source="test",
        )
    )


@pytest.fixture
def calendar(monkeypatch):
    monkeypatch.setattr(
        market_data_service,
        "get_last_completed_trading_day",
        lambda market, today=None: TARGET,
    )
    monkeypatch.setattr(price_tail_sync, "local_today", lambda: TODAY)


@pytest.fixture
def fetcher(monkeypatch):
    calls = []
    outcomes = {}

    def fake(db, *, symbol, market, start_date, end_date, **_kwargs):
        calls.append((symbol, market, start_date, end_date))
        outcome = outcomes.get(symbol, {"success": True, "rows": 1})
        if outcome.get("success") and outcome.get("rows"):
            # 成功即视作目标日已入库（真实增量同步的跨层行为另有用例覆盖）
            _price(db, symbol, market, end_date)
            db.commit()
        return outcome

    monkeypatch.setattr(
        market_data_service, "fetch_and_store_security_price_history_incremental", fake
    )
    return calls, outcomes


# --------------------------------------------------------------------------- #
# 纯函数：计划
# --------------------------------------------------------------------------- #
def test_plan_skips_up_to_date_and_attempted_keys():
    keys = [("A", "A股"), ("B", "A股"), ("C", "美股"), ("D", "A股")]
    coverage = {
        ("A", "A股"): (date(2025, 1, 1), TARGET),  # 已最新
        ("B", "A股"): (date(2025, 1, 1), date(2026, 9, 24)),  # 落后
        ("D", "A股"): (date(2025, 1, 1), date(2026, 9, 20)),
    }
    plan = plan_tail_targets(
        keys,
        coverage,
        {},
        {"A股": TARGET, "美股": TARGET},
        {"D|A股": TARGET.isoformat()},
        TODAY,
    )
    assert [(p["symbol"], p["start_date"]) for p in plan] == [
        ("B", date(2025, 1, 1)),  # 只补尾：起点 = 覆盖起点
        ("C", TODAY - timedelta(days=30)),  # 无行：近 30 天
    ]


def test_plan_backfills_watchlist_head_before_added_date():
    keys = [("W", "美股"), ("N", "美股")]
    added = {("W", "美股"): date(2026, 3, 10), ("N", "美股"): date(2025, 6, 1)}
    coverage = {("W", "美股"): (date(2026, 8, 1), TARGET)}  # 尾部最新但起点晚于加入日
    plan = plan_tail_targets(keys, coverage, added, {"美股": TARGET}, {}, TODAY)
    assert [(p["symbol"], p["start_date"]) for p in plan] == [
        ("W", date(2026, 3, 3)),
        ("N", date(2025, 5, 25)),  # 无行：加入日 − 7 早于今天 − 30
    ]


def test_plan_probes_watchlist_head_only_once():
    """加入日 − 7 天落在非交易日/上市前时覆盖起点永远晚于它：探过一次就不再每天重打。"""
    keys = [("W", "美股")]
    added = {("W", "美股"): date(2026, 3, 10)}
    coverage = {("W", "美股"): (date(2026, 3, 4), TARGET)}  # 3-3 是休市日，覆盖从 3-4 起
    probed = {"W|美股": date(2026, 3, 3).isoformat()}
    assert plan_tail_targets(keys, coverage, added, {"美股": TARGET}, {}, TODAY, probed) == []
    # 尾部落后时照常只补尾
    behind = {("W", "美股"): (date(2026, 3, 4), date(2026, 9, 20))}
    plan = plan_tail_targets(keys, behind, added, {"美股": TARGET}, {}, TODAY, probed)
    assert [(p["symbol"], p["start_date"]) for p in plan] == [("W", date(2026, 3, 4))]


# --------------------------------------------------------------------------- #
# 尾部同步
# --------------------------------------------------------------------------- #
def test_up_to_date_keys_make_no_fetch(db, calendar, fetcher):
    calls, _ = fetcher
    _hold(db, "600519", "A股")
    _price(db, "600519", "A股", TARGET)
    _hold(db, "00700", "港股")  # 港股不归这里管
    db.commit()
    summary = price_tail_sync.sync_price_tails(db)
    assert calls == [] and summary["planned"] == 0


def test_behind_key_fetched_once_per_target_day(db, calendar, fetcher):
    calls, _ = fetcher
    _hold(db, "600519", "A股")
    _price(db, "600519", "A股", date(2026, 1, 5))
    _price(db, "600519", "A股", date(2026, 9, 24))
    db.commit()

    summary = price_tail_sync.sync_price_tails(db)
    assert summary["synced"] == 1
    assert calls == [("600519", "A股", date(2026, 1, 5), TARGET)]

    # 同一目标交易日不再重打（例如停牌、源里就是没有这一天）
    price_tail_sync.sync_price_tails(db)
    assert len(calls) == 1


def test_quota_error_stops_round_and_is_retried(db, calendar, fetcher, monkeypatch):
    calls, outcomes = fetcher
    for symbol in ("000001", "600036", "600519"):
        _hold(db, symbol, "A股")
    db.commit()
    outcomes["000001"] = {"success": False, "error": "抱歉，您每分钟最多访问该接口500次"}

    summary = price_tail_sync.sync_price_tails(db)
    assert summary["quota_error"] and len(calls) == 1  # 第一只命中配额即中止

    outcomes.clear()
    price_tail_sync.sync_price_tails(db)
    assert [c[0] for c in calls[1:]] == ["000001", "600036", "600519"]  # 配额失败的会重试


def test_periodic_entry_outcomes(db, calendar, fetcher, monkeypatch):
    _, outcomes = fetcher
    monkeypatch.setattr(settings, "price_tail_sync_enabled", False)
    assert price_tail_sync.periodic_sync_price_tails().status == "skipped"
    monkeypatch.setattr(settings, "price_tail_sync_enabled", True)

    _hold(db, "600519", "A股")
    _hold(db, "PDD", "美股")
    db.commit()
    outcomes["600519"] = {"success": False, "error": "腾讯K线: HTTP 503"}
    outcome = price_tail_sync.periodic_sync_price_tails()
    # 部分成功也报 failed：PDD 成功不能把 600519 的失败告警清掉
    assert outcome.status == "failed" and outcome.count == 1 and "600519" in outcome.reason

    # 失败的标的下个 tick 重试，满 MAX_TAIL_ATTEMPTS 次当日不再试 → skipped（不算恢复）
    for _ in range(price_tail_sync.MAX_TAIL_ATTEMPTS - 1):
        assert price_tail_sync.periodic_sync_price_tails().status == "failed"
    assert price_tail_sync.periodic_sync_price_tails().status == "skipped"


def test_transient_failure_is_retried_next_tick_and_head_not_marked(db, calendar, fetcher):
    """一个成功 + 一个瞬时失败：下个 tick 只重试失败的那只，成功后才记头部已探。"""
    calls, outcomes = fetcher
    _hold(db, "600519", "A股")
    _hold(db, "000001", "A股")
    db.commit()
    outcomes["000001"] = {"success": False, "error": "ReadTimeout"}

    first = price_tail_sync.sync_price_tails(db)
    assert first["synced"] == 1 and first["failed"] == 1
    detail = price_tail_sync.scheduled_state.get_detail(db, price_tail_sync.TAIL_TASK_NAME)
    assert "000001|A股" not in detail["attempted"]
    assert detail["failures"]["000001|A股"]["count"] == 1

    calls.clear()
    outcomes.pop("000001")
    second = price_tail_sync.sync_price_tails(db)
    assert [c[0] for c in calls] == ["000001"]  # 600519 已成功，不再请求
    assert second["synced"] == 1 and second["failed"] == 0
    detail = price_tail_sync.scheduled_state.get_detail(db, price_tail_sync.TAIL_TASK_NAME)
    assert "000001|A股" in detail["attempted"] and "000001|A股" not in detail["failures"]


def test_inactive_users_are_out_of_scope(db, calendar, fetcher):
    calls, _ = fetcher
    inactive = User(username="tail_inactive", hashed_password="x", is_active=False)
    db.add(inactive)
    db.flush()
    _hold(db, "600036", "A股", user_id=inactive.id)
    db.commit()
    price_tail_sync.sync_price_tails(db)
    assert calls == []


# --------------------------------------------------------------------------- #
# daily_basic
# --------------------------------------------------------------------------- #
def _local(hour):
    return datetime(2026, 9, 25, hour, 0, tzinfo=business_timezone())


def test_daily_basic_one_call_filtered_to_tracked(db, monkeypatch):
    seen = []
    monkeypatch.setattr(
        market_data_service,
        "get_last_completed_trading_day",
        lambda market, today=None: seen.append(today) or (today - timedelta(days=1)),
    )
    calls = []

    def fake_query(api, **params):
        calls.append((api, params))
        return pd.DataFrame(
            [
                {"ts_code": "600519.SH", "trade_date": "20260925", "pe_ttm": 20.5, "pb": 7.1},
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20260925",
                    "pe_ttm": 5.0,
                    "pb": float("nan"),
                },
                {"ts_code": "688981.SH", "trade_date": "20260925", "pe_ttm": 90.0, "pb": 4.0},
            ]
        )

    monkeypatch.setattr(stock_price_service, "tushare_query", fake_query)
    _hold(db, "600519", "A股")
    db.add(WatchlistItem(user_id=_uid(db), symbol="000001", market="A股"))
    db.commit()

    result = price_tail_sync.refresh_daily_basic(db, now_local=_local(19))
    assert seen == [date(2026, 9, 26)]  # 18 点后：含当天
    assert calls == [("daily_basic", {"trade_date": "20260925"})]
    assert result["status"] == "succeeded" and result["symbols"] == 2
    rows = {
        row.symbol: row.payload
        for row in db.query(SecurityProfileData).filter(
            SecurityProfileData.dataset == "daily_basic"
        )
    }
    assert set(rows) == {"600519", "000001"}
    assert rows["000001"]["pb"] is None  # NaN → None

    again = price_tail_sync.refresh_daily_basic(db, now_local=_local(20))
    assert again["status"] == "skipped" and len(calls) == 1  # 同一交易日不重复外呼


def test_daily_basic_before_six_uses_previous_day_and_empty_is_not_marked(db, monkeypatch):
    seen = []
    monkeypatch.setattr(
        market_data_service,
        "get_last_completed_trading_day",
        lambda market, today=None: seen.append(today) or (today - timedelta(days=1)),
    )
    monkeypatch.setattr(stock_price_service, "tushare_query", lambda api, **p: pd.DataFrame())
    _hold(db, "600519", "A股")
    db.commit()

    result = price_tail_sync.refresh_daily_basic(db, now_local=_local(10))
    assert seen == [date(2026, 9, 25)]
    assert result["status"] == "skipped"
    assert db.get(ScheduledTaskState, price_tail_sync.DAILY_BASIC_TASK_NAME) is None


def test_daily_basic_prunes_to_keep_rows(db, monkeypatch):
    from app.services.security_profile_service import DAILY_BASIC_KEEP_ROWS

    monkeypatch.setattr(
        market_data_service,
        "get_last_completed_trading_day",
        lambda market, today=None: date(2026, 9, 25),
    )
    monkeypatch.setattr(
        stock_price_service,
        "tushare_query",
        lambda api, **p: pd.DataFrame([{"ts_code": "600519.SH", "trade_date": "20260925"}]),
    )
    _hold(db, "600519", "A股")
    for offset in range(DAILY_BASIC_KEEP_ROWS):
        day = date(2026, 8, 1) + timedelta(days=offset)
        db.add(
            SecurityProfileData(
                symbol="600519",
                market="A股",
                dataset="daily_basic",
                period_key=day.strftime("%Y%m%d"),
                payload={},
            )
        )
    db.commit()
    price_tail_sync.refresh_daily_basic(db, now_local=_local(19))
    keys = [
        row.period_key
        for row in db.query(SecurityProfileData).filter(
            SecurityProfileData.symbol == "600519",
            SecurityProfileData.dataset == "daily_basic",
        )
    ]
    assert len(keys) == DAILY_BASIC_KEEP_ROWS and "20260925" in keys and "20260801" not in keys


def test_daily_basic_periodic_entry(db, monkeypatch):
    monkeypatch.setattr(settings, "daily_basic_refresh_enabled", False)
    assert price_tail_sync.periodic_refresh_daily_basic().status == "skipped"
    monkeypatch.setattr(settings, "daily_basic_refresh_enabled", True)
    monkeypatch.setattr(settings, "tushare_token", "")
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    assert price_tail_sync.periodic_refresh_daily_basic().status == "skipped"

    monkeypatch.setattr(settings, "tushare_token", "fake")
    _hold(db, "600519", "A股")
    db.commit()
    monkeypatch.setattr(
        market_data_service,
        "get_last_completed_trading_day",
        lambda market, today=None: date(2026, 9, 25),
    )

    def boom(api, **params):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(stock_price_service, "tushare_query", boom)
    assert price_tail_sync.periodic_refresh_daily_basic().status == "failed"


def test_tail_target_includes_today_after_local_close():
    """A股北京时间 16 点后、美股纽约 17 点后，当天算作已完成交易日（不再等到次日早上）。"""
    from datetime import datetime, timezone

    seen = []

    def last_completed(market, today=None):
        seen.append((market, today))
        return today - timedelta(days=1) if today else date(2000, 1, 1)

    # 2026-09-28 17:00 北京 = 09:00 UTC；此时纽约是 05:00
    moment = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
    assert price_tail_sync.tail_target_date("A股", last_completed, now_utc=moment) == date(
        2026, 9, 28
    )
    assert price_tail_sync.tail_target_date("美股", last_completed, now_utc=moment) == date(
        2026, 9, 27
    )
    # 北京 15:00（07:00 UTC）：今天还没收完
    early = datetime(2026, 9, 28, 7, 0, tzinfo=timezone.utc)
    assert price_tail_sync.tail_target_date("A股", last_completed, now_utc=early) == date(
        2026, 9, 27
    )


def test_after_close_real_incremental_sync_fetches_today_and_retries_until_published(
    db, monkeypatch
):
    """跨层回归（PR #261 评审 P1）：真实的增量同步函数，只 mock 行情源与交易日历/时钟。
    北京时间 09-28 收盘后：必须请求并入库 09-28；数据源还没发布当天时不能记「已处理」。"""
    from datetime import datetime, timezone

    def last_completed(market, today=None):
        # 真实行为：缺省以 UTC 今天为界且不含今天（09-28 当天只能到 09-25，09-25 前为节假日）
        if today is None:
            return date(2026, 9, 25)
        return today - timedelta(days=1)

    monkeypatch.setattr(market_data_service, "get_last_completed_trading_day", last_completed)
    monkeypatch.setattr(price_tail_sync, "local_today", lambda: date(2026, 9, 28))
    published = {"today": False}
    requested = []

    def source(db_, *, symbol, market, start_date, end_date, currency=None, **_kwargs):
        requested.append((start_date, end_date))
        if published["today"] and end_date >= date(2026, 9, 28):
            _price(db_, symbol, market, date(2026, 9, 28))
            db_.commit()
            return {"success": True, "rows": 1}
        return {"success": True, "rows": 0, "message": "没有新增交易日数据"}

    monkeypatch.setattr(market_data_service, "fetch_and_store_security_price_history", source)
    _hold(db, "600519", "A股")
    _price(db, "600519", "A股", date(2026, 9, 1))
    _price(db, "600519", "A股", date(2026, 9, 25))
    db.commit()
    after_close = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)  # 北京 17:00

    first = price_tail_sync.sync_price_tails(db, now_utc=after_close)
    assert requested == [(date(2026, 9, 26), date(2026, 9, 28))]  # 真的请求了当天
    assert first["synced"] == 0 and first["incomplete"] == 1
    detail = price_tail_sync.scheduled_state.get_detail(db, price_tail_sync.TAIL_TASK_NAME)
    assert "600519|A股" not in detail["attempted"]  # 没取到当天：不能记已处理

    published["today"] = True
    second = price_tail_sync.sync_price_tails(db, now_utc=after_close)
    assert len(requested) == 2 and second["synced"] == 1
    assert _max_date(db, "600519", "A股") == date(2026, 9, 28)
    detail = price_tail_sync.scheduled_state.get_detail(db, price_tail_sync.TAIL_TASK_NAME)
    assert detail["attempted"]["600519|A股"] == "2026-09-28"

    # 已入库：后续 tick 不再请求
    price_tail_sync.sync_price_tails(db, now_utc=after_close)
    assert len(requested) == 2


def test_unpublished_target_gives_up_after_max_attempts(db, monkeypatch):
    """停牌/数据源当天缺失：成功但始终没有目标日，满 MAX_TAIL_ATTEMPTS 次后当日不再请求。"""
    from datetime import datetime, timezone

    monkeypatch.setattr(
        market_data_service,
        "get_last_completed_trading_day",
        lambda market, today=None: (today - timedelta(days=1)) if today else date(2026, 9, 25),
    )
    monkeypatch.setattr(price_tail_sync, "local_today", lambda: date(2026, 9, 28))
    requested = []

    def source(db_, *, symbol, market, start_date, end_date, currency=None, **_kwargs):
        requested.append(end_date)
        return {"success": True, "rows": 0}

    monkeypatch.setattr(market_data_service, "fetch_and_store_security_price_history", source)
    _hold(db, "600519", "A股")
    _price(db, "600519", "A股", date(2026, 9, 25))
    db.commit()
    after_close = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
    outcomes = [
        price_tail_sync.sync_price_tails(db, now_utc=after_close)
        for _ in range(price_tail_sync.MAX_TAIL_ATTEMPTS + 1)
    ]
    assert len(requested) == price_tail_sync.MAX_TAIL_ATTEMPTS
    assert all(o["failed"] == 0 for o in outcomes)  # 未发布不计失败


def _max_date(db, symbol, market):
    from sqlalchemy import func

    return (
        db.query(func.max(SecurityPrice.price_date))
        .filter(SecurityPrice.symbol == symbol, SecurityPrice.market == market)
        .scalar()
    )
