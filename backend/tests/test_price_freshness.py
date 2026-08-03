"""股价刷新新鲜度：时区正确性（未来时间戳自愈）与可配置窗口。

背景：price_updated_at 曾以 naive 本地时间写入 timestamptz，UTC+8 环境下
被解释成"未来 8 小时"，elapsed 恒为负 → 主动刷新长期全部跳过。
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.database import SessionLocal
from app.models.holding import Holding
from app.services import stock_price_service
from app.services.stock_price_service import price_result, update_all_holdings_prices


def _make_holding(db, symbol, price_updated_at):
    holding = Holding(
        user_id=1, broker_account_id=None, symbol=symbol, name=symbol,
        market="A股", quantity=Decimal("100"), avg_cost=Decimal("10"),
        total_cost=Decimal("1000"), currency="CNY",
        current_price=Decimal("10"), price_updated_at=price_updated_at,
    )
    db.add(holding)
    return holding


def _run_refresh(db, monkeypatch):
    monkeypatch.setattr(
        stock_price_service, "fetch_stock_price",
        lambda symbol, market: price_result(
            price=Decimal("11"), source="test", success=True
        ),
    )
    return update_all_holdings_prices(db, 1)


def test_price_result_timestamp_is_aware_utc():
    ts = price_result(price=Decimal("1"), source="t", success=True)["timestamp"]
    assert ts.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - ts).total_seconds()) < 5


def test_future_timestamp_is_treated_as_stale_and_healed(monkeypatch):
    """未来时间戳（历史 naive 写入的脏数据）必须刷新而非跳过，并被 aware UTC 覆盖。"""
    db = SessionLocal()
    db.query(Holding).filter(Holding.user_id == 1).delete()
    db.commit()
    try:
        future = datetime.now(timezone.utc) + timedelta(hours=7)
        _make_holding(db, "FUTURE1", future)
        db.commit()

        result = _run_refresh(db, monkeypatch)
        assert result["success_count"] == 1
        assert result["skipped_count"] == 0

        db.expire_all()
        healed = db.query(Holding).filter_by(symbol="FUTURE1").one()
        ts = healed.price_updated_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        assert abs((datetime.now(timezone.utc) - ts).total_seconds()) < 60  # 已自愈
    finally:
        db.query(Holding).filter(Holding.user_id == 1).delete()
        db.commit()
        db.close()


def test_freshness_window_skips_and_is_configurable(monkeypatch):
    db = SessionLocal()
    db.query(Holding).filter(Holding.user_id == 1).delete()
    db.commit()
    try:
        # 5 分钟前更新：600s 默认窗口内 → 跳过
        _make_holding(db, "FRESH01", datetime.now(timezone.utc) - timedelta(seconds=300))
        db.commit()
        result = _run_refresh(db, monkeypatch)
        assert result["skipped_count"] == 1
        assert result["success_count"] == 0

        # 窗口缩到 120s → 同一持仓应刷新
        monkeypatch.setattr(
            stock_price_service.settings, "price_refresh_freshness_seconds", 120
        )
        result = _run_refresh(db, monkeypatch)
        assert result["success_count"] == 1
        assert result["skipped_count"] == 0
    finally:
        db.query(Holding).filter(Holding.user_id == 1).delete()
        db.commit()
        db.close()


def test_compose_passes_freshness_setting():
    from pathlib import Path

    compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
    assert "PRICE_REFRESH_FRESHNESS_SECONDS=${PRICE_REFRESH_FRESHNESS_SECONDS:-600}" in compose
