"""持仓现价的行情所属交易日（#217）。

price_updated_at 是写库时刻：周六刷新写 9/26，而 A 股收盘价实为 9/25。
刷新时由报价源写入 price_as_of（拿不到写 None，绝不拿今天冒充），手工价写 None。
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest

from app.database import SessionLocal
from app.models.holding import Holding
from app.services import stock_price_service as sps
from app.services.stock_price_service import (
    parse_quote_date,
    parse_tencent_quote_date,
    price_result,
    quote_date_from_epoch_ms,
    update_all_holdings_prices,
)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("20260925", date(2026, 9, 25)),  # Tushare trade_date
        ("2026-09-25 16:00:00", date(2026, 9, 25)),  # Tushare hk_mins trade_time
        ("20260924161444", date(2026, 9, 24)),  # 腾讯 A 股行情时间
        ("2026/09/25 16:08:20", date(2026, 9, 25)),  # 腾讯港股行情时间
        (pd.Timestamp("2026-09-25 15:00"), date(2026, 9, 25)),
        (date(2026, 9, 25), date(2026, 9, 25)),
        (None, None),
        ("", None),
        ("nan", None),
        ("2026", None),
        ("20261399", None),  # 非法日期不抛
        (float("nan"), None),
    ],
)
def test_parse_quote_date(value, expected):
    assert parse_quote_date(value) == expected


def _tencent_payload(code: str, time_field: str) -> str:
    fields = ["1", "测试", code, "12.34"] + [""] * 26 + [time_field, "x"]
    return f'v_{code}="{"~".join(fields)}";'


def test_tencent_quote_date_reads_field_30():
    text = _tencent_payload("sh600519", "20260924161444")
    assert parse_tencent_quote_date(text, "sh600519") == date(2026, 9, 24)
    hk = _tencent_payload("hk00700", "2026/09/25 16:08:20")
    assert parse_tencent_quote_date(hk, "hk00700") == date(2026, 9, 25)


def test_tencent_quote_date_missing_field_is_none():
    assert parse_tencent_quote_date('v_sh600519="1~茅台~600519~12.3";', "sh600519") is None
    assert parse_tencent_quote_date("garbage", "sh600519") is None


def test_epoch_ms_uses_exchange_timezone():
    # 2026-09-25 20:00 UTC = 纽约 16:00 收盘；在东八区已是 9/26 凌晨
    ms = int(datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert quote_date_from_epoch_ms(ms, "美股") == date(2026, 9, 25)
    assert quote_date_from_epoch_ms(ms, "港股") == date(2026, 9, 26)
    assert quote_date_from_epoch_ms(None, "美股") is None
    assert quote_date_from_epoch_ms(0, "美股") is None
    assert quote_date_from_epoch_ms(True, "美股") is None
    assert quote_date_from_epoch_ms(ms, "火星股") is None


def test_failed_result_never_carries_as_of():
    result = price_result(price=None, source="t", success=False, as_of=date(2026, 9, 25))
    assert result["as_of"] is None


def test_tushare_daily_fallback_reports_trade_date(monkeypatch):
    frames = {
        "rt_k": RuntimeError("rt_k 无数据"),
        "daily": pd.DataFrame(
            [
                {"trade_date": "20260924", "close": 10.1},
                {"trade_date": "20260925", "close": 10.2},
            ]
        ),
    }

    def fake_query(api_name, **kwargs):
        value = frames[api_name]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(sps, "tushare_query", fake_query)
    result = sps.fetch_a_stock_price_tushare("600000")
    assert result["success"] is True
    assert result["price"] == Decimal("10.2")
    assert result["as_of"] == date(2026, 9, 25)


def _holding(symbol: str, **kwargs) -> Holding:
    return Holding(
        user_id=1, broker_account_id=None, symbol=symbol, name=symbol,
        market="A股", quantity=Decimal("100"), avg_cost=Decimal("10"),
        total_cost=Decimal("1000"), currency="CNY", **kwargs,
    )


def test_refresh_writes_as_of_and_source_and_clears_stale_date(monkeypatch):
    db = SessionLocal()
    db.query(Holding).filter(Holding.user_id == 1).delete()
    db.commit()
    try:
        old = datetime.now(timezone.utc) - timedelta(days=3)
        db.add(_holding("ASOF01", current_price=Decimal("9"), price_updated_at=old))
        # 上次刷新有日期、这次报价源拿不到：必须清成 None，不保留旧日期
        db.add(_holding(
            "ASOF02", current_price=Decimal("9"), price_updated_at=old,
            price_as_of=date(2026, 9, 1), price_source="tushare-daily",
        ))
        db.commit()

        def fake_fetch(symbol, market):
            as_of = date(2026, 9, 25) if symbol == "ASOF01" else None
            return price_result(
                price=Decimal("11"), source="tencent-quote", success=True, as_of=as_of
            )

        monkeypatch.setattr(sps, "fetch_stock_price", fake_fetch)
        result = update_all_holdings_prices(db, 1)
        assert result["success_count"] == 2

        db.expire_all()
        first = db.query(Holding).filter_by(symbol="ASOF01").one()
        assert first.price_as_of == date(2026, 9, 25)
        assert first.price_source == "tencent-quote"
        second = db.query(Holding).filter_by(symbol="ASOF02").one()
        assert second.price_as_of is None
        assert second.price_source == "tencent-quote"
    finally:
        db.query(Holding).filter(Holding.user_id == 1).delete()
        db.commit()
        db.close()


def test_batch_manual_price_update_marks_manual():
    from app.api.holdings import batch_update_prices
    from app.models.user import User
    from app.schemas.holding import PriceBatchUpdate

    db = SessionLocal()
    db.query(Holding).filter(Holding.user_id == 1).delete()
    db.commit()
    try:
        db.add(_holding(
            "ASOF03", current_price=Decimal("9"),
            price_updated_at=datetime.now(timezone.utc) - timedelta(days=3),
            price_as_of=date(2026, 9, 1), price_source="tushare-daily",
        ))
        db.commit()
        user = db.query(User).filter(User.id == 1).one()
        batch_update_prices(
            [PriceBatchUpdate(symbol="ASOF03", market="A股", price=Decimal("12"))],
            current_user=user,
            db=db,
        )
        db.expire_all()
        row = db.query(Holding).filter_by(symbol="ASOF03").one()
        assert row.current_price == Decimal("12")
        assert row.price_as_of is None
        assert row.price_source == "manual"
    finally:
        db.query(Holding).filter(Holding.user_id == 1).delete()
        db.commit()
        db.close()
