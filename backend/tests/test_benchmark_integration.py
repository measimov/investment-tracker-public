"""基准对比：路由/目录/编排/API 集成。"""

from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.core.security import get_password_hash
from app.database import SessionLocal
from app.main import app
from app.models.security_price import SecurityPrice
from app.models.transaction import Transaction
from app.models.user import User
from app.services import benchmark_service
from app.services.market_data_service import resolve_tushare_history_api
from app.services.performance_history_jobs import get_history_sync_targets

from .helpers import add_transaction, reset_tables


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        reset_tables(session, [Transaction])
        session.query(SecurityPrice).filter(
            SecurityPrice.market == benchmark_service.BENCHMARK_MARKET
        ).delete()
        session.query(SecurityPrice).filter(SecurityPrice.symbol == "600036").delete()
        session.commit()
        yield session
        session.rollback()
        reset_tables(session, [Transaction])
        session.query(SecurityPrice).filter(
            SecurityPrice.market == benchmark_service.BENCHMARK_MARKET
        ).delete()
        session.query(SecurityPrice).filter(SecurityPrice.symbol == "600036").delete()
        session.commit()
    finally:
        session.close()


def _seed_index_prices(db, code, closes):
    for price_date, close in closes.items():
        db.add(SecurityPrice(
            symbol=code, market=benchmark_service.BENCHMARK_MARKET,
            price_date=price_date, close_price=close,
            currency=benchmark_service.BENCHMARKS[code]["currency"],
            source="tushare-index_daily",
        ))
    db.commit()


def test_routing_and_catalog():
    assert resolve_tushare_history_api("000300.SH", "指数") == {
        "api": "index_daily", "adjust_api": "", "ts_code": "000300.SH",
    }
    assert resolve_tushare_history_api("HSI", "指数") == {
        "api": "index_global", "adjust_api": "", "ts_code": "HSI",
    }
    assert resolve_tushare_history_api("UNKNOWN", "指数") is None

    catalog = benchmark_service.benchmark_catalog()
    assert [entry["code"] for entry in catalog] == ["000300.SH", "HSI", "SPX"]
    assert all({"code", "name", "currency"} <= set(entry) for entry in catalog)


def test_history_sync_targets_include_benchmarks(db):
    add_transaction(db, symbol="600036", market="A股", currency="CNY",
                    transaction_date=date(2026, 1, 5))
    db.commit()

    info = get_history_sync_targets(db, 1)
    benchmark_rows = [t for t in info["targets"] if t["market"] == "指数"]
    assert {t["symbol"] for t in benchmark_rows} == {"000300.SH", "HSI", "SPX"}
    for target in benchmark_rows:
        # 窗口 = 用户首笔交易日 ~ 全局终点；日历市场来自目录
        assert target["start_date"] == date(2026, 1, 5)
        assert target["calendar_market"] in {"A股", "港股", "美股"}


def test_load_benchmark_closes_includes_anchor_before_start(db):
    _seed_index_prices(db, "000300.SH", {
        date(2026, 1, 3): Decimal("4000"),
        date(2026, 1, 6): Decimal("4100"),
        date(2026, 1, 7): Decimal("4200"),
    })
    closes = benchmark_service.load_benchmark_closes(
        db, "000300.SH", date(2026, 1, 5), date(2026, 1, 7)
    )
    # 区间内两行 + 起点前最近一行（基点锚）
    assert set(closes) == {date(2026, 1, 3), date(2026, 1, 6), date(2026, 1, 7)}


@pytest.fixture
def api_user():
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "demo").one()
        original = user.hashed_password
        user.hashed_password = get_password_hash("benchmark-api-password")
        db.commit()
        yield user.id
        user.hashed_password = original
        db.commit()
    finally:
        db.close()


@pytest.mark.anyio
async def test_first_available_benchmark_has_no_comparison(db, api_user):
    """[评审回归] 基准数据晚于区间起点（first_available）：不产出超额收益，
    响应携带 alignment 与中文警告，不得输出误导性的全区间超额。"""
    add_transaction(db, user_id=api_user, symbol="600036", market="A股",
                    currency="CNY", transaction_date=date(2026, 1, 5),
                    quantity=Decimal("100"), price=Decimal("10"))
    db.add(SecurityPrice(symbol="600036", market="A股", price_date=date(2026, 1, 5),
                         close_price=Decimal("10"), currency="CNY", source="test"))
    db.add(SecurityPrice(symbol="600036", market="A股", price_date=date(2026, 1, 7),
                         close_price=Decimal("11"), currency="CNY", source="test"))
    db.commit()
    # 基准数据从区间中段（1/6）才开始，起点前无锚点
    _seed_index_prices(db, "000300.SH", {
        date(2026, 1, 6): Decimal("4100"),
        date(2026, 1, 7): Decimal("4200"),
    })

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/api/auth/token",
            json={"username": "demo", "password": "benchmark-api-password"},
        )
        auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
        response = await client.get(
            "/api/statistics/performance-analytics"
            "?benchmarks=000300.SH&start_date=2026-01-05&end_date=2026-01-07",
            headers=auth,
        )
        block = response.json()["benchmarks"][0]
        assert block["status"] == "ok"
        assert block["alignment"] == "first_available"
        assert block["comparison"] is None
        assert any(
            "晚于区间起点" in w for w in response.json()["data_quality"]["warnings"]
        )


def test_concurrent_price_upsert_is_atomic(db):
    """[评审回归] 两个会话并发 upsert 同一基准同一交易日：双方成功、最终一份。"""
    import threading

    from app.services.market_data_service import upsert_security_prices

    barrier = threading.Barrier(2)
    errors: list = []

    def worker():
        session = SessionLocal()
        try:
            rows = [
                SecurityPrice(
                    symbol="000300.SH", market=benchmark_service.BENCHMARK_MARKET,
                    price_date=date(2026, 1, 6), close_price=Decimal("4100"),
                    currency="CNY", source="tushare-index_daily",
                ),
                SecurityPrice(
                    symbol="000300.SH", market=benchmark_service.BENCHMARK_MARKET,
                    price_date=date(2026, 1, 7), close_price=Decimal("4200"),
                    currency="CNY", source="tushare-index_daily",
                ),
            ]
            barrier.wait(timeout=10)
            upsert_security_prices(session, rows)
        except Exception as exc:  # noqa: BLE001 - 断言用
            session.rollback()
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    rows = db.query(SecurityPrice).filter(
        SecurityPrice.symbol == "000300.SH",
        SecurityPrice.market == benchmark_service.BENCHMARK_MARKET,
    ).all()
    assert len(rows) == 2  # 两个交易日各一份，无重复无回滚


@pytest.mark.anyio
async def test_analytics_benchmark_block_and_validation(db, api_user):
    # 用户持仓：1/5 买入 100 股 @10；行情 1/5=10、1/7=11 → 组合 +10%
    add_transaction(db, user_id=api_user, symbol="600036", market="A股",
                    currency="CNY", transaction_date=date(2026, 1, 5),
                    quantity=Decimal("100"), price=Decimal("10"))
    db.add(SecurityPrice(symbol="600036", market="A股", price_date=date(2026, 1, 5),
                         close_price=Decimal("10"), currency="CNY", source="test"))
    db.add(SecurityPrice(symbol="600036", market="A股", price_date=date(2026, 1, 7),
                         close_price=Decimal("11"), currency="CNY", source="test"))
    db.commit()
    # 基准：同区间 4000 → 4200（+5%）
    _seed_index_prices(db, "000300.SH", {
        date(2026, 1, 3): Decimal("4000"),
        date(2026, 1, 7): Decimal("4200"),
    })

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/api/auth/token",
            json={"username": "demo", "password": "benchmark-api-password"},
        )
        auth = {"Authorization": f"Bearer {login.json()['access_token']}"}

        # 带基准请求：块形状 + 超额收益
        response = await client.get(
            "/api/statistics/performance-analytics"
            "?benchmarks=000300.SH,HSI&start_date=2026-01-05&end_date=2026-01-07",
            headers=auth,
        )
        assert response.status_code == 200
        payload = response.json()
        blocks = {block["code"]: block for block in payload["benchmarks"]}

        hs300 = blocks["000300.SH"]
        assert hs300["status"] == "ok"
        assert hs300["name"] == "沪深300"
        assert hs300["fx_basis"] == "index_native"
        assert hs300["return_basis"] == "price_index_excl_dividends"
        assert hs300["total_return_rate"] == pytest.approx(5.0)
        comparison = hs300["comparison"]
        assert comparison["benchmark_total_return_rate"] == pytest.approx(5.0)
        # 组合 +10% − 基准 +5% = +5pp
        assert comparison["excess_return_rate"] == pytest.approx(5.0, abs=0.01)

        # 无数据基准：no_data + 中文警告，不影响曲线主体
        hsi = blocks["HSI"]
        assert hsi["status"] == "no_data"
        assert any("恒生指数" in w for w in payload["data_quality"]["warnings"])
        assert payload["curve"]

        # 不带参数：响应无 benchmarks 键（向后兼容）
        plain = await client.get(
            "/api/statistics/performance-analytics", headers=auth
        )
        assert "benchmarks" not in plain.json()

        # 未知 code → 422；超上限 → 422
        unknown = await client.get(
            "/api/statistics/performance-analytics?benchmarks=FAKE", headers=auth
        )
        assert unknown.status_code == 422
        over = await client.get(
            "/api/statistics/performance-analytics?benchmarks=000300.SH,HSI,SPX,FAKE",
            headers=auth,
        )
        assert over.status_code == 422

        # 目录端点
        catalog = await client.get("/api/statistics/benchmarks", headers=auth)
        assert [row["code"] for row in catalog.json()] == ["000300.SH", "HSI", "SPX"]
