"""估值取价：持仓现价与最新收盘按行情日期择优，陈旧现价不再制造虚假当日损益（#267）。

复现：持仓现价停在 8/15（错过收盘后刷新），日线尾部已推进到 9/25。旧实现无条件优先持仓现价，
当日期初按 9/25 收盘、期末按 8/15 现价，两者之差被当成「当日损益」并标为精确。
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.database import SessionLocal
from app.models.corporate_action import CorporateAction
from app.models.exchange_rate import ExchangeRate
from app.models.holding import Holding
from app.models.security_price import SecurityPrice
from app.models.transaction import Transaction
from app.models.watchlist_item import WatchlistItem
from app.services.statistics import calculate_period_pnl, resolve_server_prices
from app.services.statistics.period_pnl import assess_closing_prices
from app.services.statistics.pricing import history_close_wins, quote_market_date
from app.services.stock_price_service import apply_latest_closes
from tests.helpers import add_transaction, reset_tables

RESET_MODELS = (SecurityPrice, WatchlistItem, Holding, CorporateAction, Transaction, ExchangeRate)
TODAY = date(2026, 9, 26)


@pytest.fixture
def db():
    session = SessionLocal()
    reset_tables(session, RESET_MODELS)
    try:
        yield session
    finally:
        reset_tables(session, RESET_MODELS)
        session.close()


def _close(db, day, close, symbol="600000", market="A股"):
    db.add(
        SecurityPrice(
            symbol=symbol,
            market=market,
            price_date=day,
            currency="CNY",
            close_price=Decimal(str(close)),
            source="audit",
        )
    )


def _holding(db, *, price, as_of=None, updated_at=None, source=None, symbol="600000", market="A股"):
    row = Holding(
        user_id=1,
        broker_account_id=None,
        symbol=symbol,
        name=symbol,
        market=market,
        quantity=Decimal("100"),
        avg_cost=Decimal("10"),
        total_cost=Decimal("1000"),
        currency="CNY",
        current_price=Decimal(str(price)),
        price_as_of=as_of,
        price_updated_at=updated_at or datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc),
        price_source=source,
    )
    db.add(row)
    return row


# --------------------------------------------------------------------------- 纯规则


@pytest.mark.parametrize(
    "close_date, quote_date, quote_source, expected",
    [
        (date(2026, 9, 25), date(2026, 8, 15), "tencent-quote", True),  # 收盘更新
        (date(2026, 9, 25), date(2026, 9, 26), "tencent-quote", False),  # 现价更新（盘中）
        (date(2026, 9, 25), date(2026, 9, 25), "tencent-quote", True),  # 同日收盘是终值
        (date(2026, 9, 25), date(2026, 9, 25), "manual", False),  # 同日手工价胜出
        (date(2026, 9, 25), date(2026, 8, 15), "manual", True),  # 旧手工价照样被取代
        (date(2026, 9, 25), None, None, True),  # 现价无日期
        (None, date(2026, 8, 15), "manual", False),  # 没有收盘
    ],
)
def test_history_close_wins(close_date, quote_date, quote_source, expected):
    assert history_close_wins(close_date, quote_date, quote_source) is expected


def test_quote_market_date_uses_the_market_timezone():
    # 纽约周五 22:00 = UTC 周六 03:00 = 北京周六 11:00：美股行情日是周五
    written = datetime(2026, 3, 7, 3, 0, tzinfo=timezone.utc)
    assert quote_market_date("美股", None, written) == date(2026, 3, 6)
    assert quote_market_date("A股", None, written) == date(2026, 3, 7)
    # 报价源给出的行情日期优先
    assert quote_market_date("美股", date(2026, 3, 5), written) == date(2026, 3, 5)
    assert quote_market_date("A股", None, None) is None


def test_assess_closing_prices_flags_only_positions_older_than_their_basis():
    quality = {
        "terminal_positions": [
            {"symbol": "OLD", "market": "A股", "quantity": 100},
            {"symbol": "OK", "market": "A股", "quantity": 100},
            {"symbol": "NEWBUY", "market": "A股", "quantity": 100},
        ],
        "opening_price_basis": {
            "OLD:A股": {"date": "2026-09-25", "source": "history"},
            "OK:A股": {"date": "2026-09-25", "source": "history"},
        },
    }
    price_dates = {"OLD:A股": "2026-08-15", "OK:A股": "2026-09-25", "NEWBUY:A股": "2026-01-01"}
    assert assess_closing_prices(quality, price_dates) == [
        {"symbol": "OLD", "market": "A股", "price_date": "2026-08-15", "basis_date": "2026-09-25"}
    ]


# --------------------------------------------------------------------------- 取价


def test_resolve_prefers_newer_close_over_stale_holding_price(db):
    _holding(db, price=11, as_of=date(2026, 8, 15), source="tencent-quote")
    _close(db, date(2026, 9, 24), 12.5)
    _close(db, date(2026, 9, 25), 13)
    db.commit()

    prices, sources, freshness = resolve_server_prices(db, 1)

    assert prices["600000:A股"] == 13.0
    assert sources["600000:A股"] == "latest_history"
    assert freshness["600000:A股"]["price_date"] == "2026-09-25"


def test_resolve_keeps_holding_price_when_newer_or_manual_same_day(db):
    _holding(db, price=14, as_of=date(2026, 9, 26), source="tencent-quote")
    _holding(
        db,
        price=9,
        symbol="MAN1",
        source="manual",
        updated_at=datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc),
    )
    _close(db, date(2026, 9, 25), 13)
    _close(db, date(2026, 9, 25), 8, symbol="MAN1")
    db.commit()

    prices, sources, freshness = resolve_server_prices(db, 1)

    assert (prices["600000:A股"], sources["600000:A股"]) == (14.0, "holding")
    assert freshness["600000:A股"]["price_date"] == "2026-09-26"
    assert (prices["MAN1:A股"], sources["MAN1:A股"]) == (9.0, "holding")


# --------------------------------------------------------------------------- 当日损益


def test_period_pnl_no_longer_books_the_stale_price_gap(db):
    """100 股，9/25 收盘 13，持仓现价停在 8/15 的 11。
    旧口径：当日 = 100×11 − 100×13 = −200 且标为精确；新口径按 9/25 收盘估值，当日 0。"""
    add_transaction(
        db,
        symbol="600000",
        name="审计标的",
        market="A股",
        currency="CNY",
        transaction_type="BUY",
        quantity=Decimal("100"),
        price=Decimal("10"),
        fee=Decimal("0"),
        transaction_date=date(2025, 12, 15),
    )
    _holding(db, price=11, as_of=date(2026, 8, 15), source="tencent-quote")
    _close(db, date(2026, 9, 25), 13)
    db.commit()

    prices, _sources, freshness = resolve_server_prices(db, 1)
    daily = calculate_period_pnl(db, 1, prices, today=TODAY, price_freshness=freshness)["periods"][
        "daily"
    ]
    assert daily["pnl_cny"] == 0
    assert daily["status"] == "exact"
    assert daily["stale_closing_prices"] == []


def test_period_pnl_marks_estimated_when_closing_price_predates_basis(db):
    """调用方给了更旧的期末价（例如 POST 手工口径之外的遗留路径）：必须标估算并列出。"""
    add_transaction(
        db,
        symbol="600000",
        name="审计标的",
        market="A股",
        currency="CNY",
        transaction_type="BUY",
        quantity=Decimal("100"),
        price=Decimal("10"),
        fee=Decimal("0"),
        transaction_date=date(2025, 12, 15),
    )
    _close(db, date(2026, 9, 25), 13)
    db.commit()

    result = calculate_period_pnl(
        db,
        1,
        {"600000:A股": 11.0},
        today=TODAY,
        price_freshness={"600000:A股": {"price_date": "2026-08-15"}},
    )
    daily = result["periods"]["daily"]
    assert daily["status"] == "estimated"
    assert daily["stale_closing_prices"] == [
        {
            "symbol": "600000",
            "market": "A股",
            "price_date": "2026-08-15",
            "basis_date": "2026-09-25",
        }
    ]
    assert any("估值价早于期初基准" in warning for warning in result["data_quality"]["warnings"])


# --------------------------------------------------------------------------- 现价追平


def test_apply_latest_closes_catches_up_holdings_and_watchlist(db):
    stale = _holding(db, price=11, as_of=date(2026, 8, 15), source="tencent-quote")
    fresh = _holding(db, price=14, as_of=date(2026, 9, 26), source="tencent-quote", symbol="FRESH")
    manual = _holding(
        db,
        price=9,
        symbol="MAN1",
        source="manual",
        updated_at=datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc),
    )
    watch = WatchlistItem(
        user_id=1,
        symbol="600000",
        market="A股",
        name="审计标的",
        current_price=Decimal("11"),
        price_as_of=date(2026, 8, 15),
        added_price=Decimal("10"),
    )
    db.add(watch)
    for symbol, close in (("600000", 13), ("FRESH", 12), ("MAN1", 8)):
        _close(db, date(2026, 9, 25), close, symbol=symbol)
    db.commit()

    updated = apply_latest_closes(db, [("600000", "A股"), ("FRESH", "A股"), ("MAN1", "A股")])
    db.commit()
    db.expire_all()

    assert updated == 2  # 持仓 600000 + 自选 600000
    assert (stale.current_price, stale.price_as_of) == (Decimal("13"), date(2026, 9, 25))
    assert stale.price_source == "close:audit"
    assert fresh.current_price == Decimal("14")
    assert manual.current_price == Decimal("9")
    assert watch.current_price == Decimal("13")
    assert watch.added_price == Decimal("10")  # 基准价不动

    # 幂等（PR #293 评审）：已追平的行不再每个 tick 被重写、price_updated_at 不被推到 now
    first_refresh = stale.price_updated_at
    assert apply_latest_closes(db, [("600000", "A股"), ("FRESH", "A股"), ("MAN1", "A股")]) == 0
    db.commit()
    db.expire_all()
    assert stale.price_updated_at == first_refresh


def test_apply_latest_closes_gives_up_when_the_row_changed_after_reading(db, monkeypatch):
    """PR #293 复审：写回是按 id 的比较交换——读取之后有新报价写入（price_updated_at 变了），
    这一行放弃，不拿更旧的收盘覆盖。"""
    import app.services.statistics.pricing as pricing

    stale = _holding(db, price=11, as_of=date(2026, 8, 15), source="tencent-quote")
    _close(db, date(2026, 9, 25), 13)
    db.commit()
    real_wins = pricing.history_close_wins

    def racing_wins(close_date, row_date, source):
        other = SessionLocal()
        try:  # 读取之后、写回之前：另一个会话写入了更新的盘中报价
            other.query(Holding).filter(Holding.id == stale.id).update(
                {
                    Holding.current_price: Decimal("14"),
                    Holding.price_as_of: date(2026, 9, 26),
                    Holding.price_updated_at: datetime(2026, 9, 26, 2, 0, tzinfo=timezone.utc),
                    Holding.price_source: "tencent-quote",
                }
            )
            other.commit()
        finally:
            other.close()
        return real_wins(close_date, row_date, source)

    monkeypatch.setattr(pricing, "history_close_wins", racing_wins)
    assert apply_latest_closes(db, [("600000", "A股")]) == 0
    db.commit()
    db.expire_all()
    assert (stale.current_price, stale.price_as_of) == (Decimal("14"), date(2026, 9, 26))
