"""#276：security_prices.currency 由唯一的解析函数决定，不再随写入方来回翻转。"""

from datetime import date

import pandas as pd
import pytest

from app.database import SessionLocal
from app.models.security_catalog import SecurityCatalogEntry
from app.models.security_price import SecurityPrice
from app.services import market_data_service as mds
from app.services.market_data_service import resolve_price_currency


@pytest.fixture
def db():
    session = SessionLocal()
    keys = [
        ("900926", "B股"),
        ("200596", "B股"),
        ("80700", "港股"),
        ("00700", "港股"),
        ("600000", "A股"),
        ("09988", "港股"),
        ("09999", "港股"),
    ]

    def clean():
        for symbol, market in keys:
            session.query(SecurityPrice).filter_by(symbol=symbol, market=market).delete()
            session.query(SecurityCatalogEntry).filter_by(symbol=symbol, market=market).delete()
        session.commit()

    clean()
    try:
        yield session
    finally:
        clean()
        session.close()


@pytest.mark.parametrize(
    "symbol, market, fallback, expected",
    [
        ("900926", "B股", None, "USD"),  # 沪 B：此前一律写成 CNY
        ("200596", "B股", "CNY", "HKD"),  # 深 B：交易币种不能覆盖代码规则
        ("80700", "港股", None, "CNY"),  # 人民币柜台
        ("00700", "港股", "CNY", "HKD"),  # 港股通按 CNY 记账，行情仍是港元
        ("600000", "A股", None, "CNY"),
        ("AAPL", "美股", "CNY", "USD"),
        ("PCT", "新加坡股", None, "SGD"),
        ("F001", "场外基金", "CNY", "CNY"),  # 没有确定规则的市场才用交易币种
    ],
)
def test_resolution_rules(db, symbol, market, fallback, expected):
    assert resolve_price_currency(db, symbol, market, fallback) == expected


def test_catalog_currency_wins(db):
    # 港交所日报的官方 CUR 会回填进标的全集：它优先于一切推断
    db.add(
        SecurityCatalogEntry(
            symbol="09988", market="港股", name="阿里巴巴-W", currency="CNY", source="hkex-dayquot"
        )
    )
    db.commit()
    assert resolve_price_currency(db, "09988", "港股", "HKD") == "CNY"


def test_hkex_dayquot_currency_beats_inference_when_catalog_lacks_the_symbol(db):
    """PR #299 评审：标的全集缺这只港股时，港交所日报（官方 CUR）写过的币种优先于推断，
    否则 9xxxx 美元柜台会被日报写 USD、history-sync 按报价币种改回 HKD，来回翻转。"""
    db.add(
        SecurityPrice(
            symbol="09999",
            market="港股",
            price_date=date(2026, 9, 25),
            close_price=1,
            currency="USD",
            source="hkex-dayquot",
        )
    )
    db.add(
        SecurityPrice(
            symbol="09999",
            market="港股",
            price_date=date(2026, 9, 26),
            close_price=1,
            currency="HKD",
            source="tushare",
        )
    )
    db.commit()
    assert resolve_price_currency(db, "09999", "港股", "HKD") == "USD"


def test_history_sync_writes_the_resolved_currency_and_survives_adj_factor_errors(db, monkeypatch):
    """尾部同步不传币种：B 股此前被写成 CNY。复权因子拉取失败此前会丢掉已拿到的日线、
    整段改走腾讯不复权兜底。"""
    frame = pd.DataFrame(
        [
            {
                "trade_date": "20260925",
                "open": 0.9,
                "high": 0.92,
                "low": 0.89,
                "close": 0.91,
                "pre_close": 0.9,
            },
        ]
    )

    def history(api_name, **kwargs):
        if api_name == "adj_factor":
            raise Exception("抱歉，您没有访问该接口的权限")
        return frame

    monkeypatch.setattr(mds, "_tushare_history_query", history)
    monkeypatch.setattr(
        mds,
        "resolve_tushare_history_api",
        lambda symbol, market: {"api": "daily", "ts_code": "900926.SH", "adjust_api": "adj_factor"},
    )
    tencent_calls = []
    monkeypatch.setattr(
        mds, "_fetch_and_store_tencent_history", lambda *a, **k: tencent_calls.append(1)
    )

    result = mds.fetch_and_store_security_price_history(
        db,
        symbol="900926",
        market="B股",
        start_date=date(2026, 9, 25),
        end_date=date(2026, 9, 25),
    )

    assert result["success"] is True and result["source"] == "tushare-daily"
    assert tencent_calls == []
    row = db.query(SecurityPrice).filter_by(symbol="900926", market="B股").one()
    assert row.currency == "USD"
    assert row.adj_factor is None
