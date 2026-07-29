"""性能热点修复的行为回归：XIRR float 求解与 Session 级汇率缓存。

不做计时断言（CI 环境计时不稳定）；只锁定优化不得改变的行为语义。
基线（重建库 2103 笔交易实测）：仪表盘 2954ms→36ms，摘要 2985ms→28ms。
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import event

from app.database import SessionLocal
from app.models.exchange_rate import ExchangeRate
from app.services import exchange_rate_service
from app.services.portfolio.metrics import xirr


def test_xirr_same_date_flows_merge_without_changing_result():
    """同日现金流合并是纯优化：拆分录入与合并录入结果一致。"""
    split = [
        (date(2025, 1, 1), Decimal("-600")),
        (date(2025, 1, 1), Decimal("-400")),
        (date(2026, 1, 1), Decimal("500")),
        (date(2026, 1, 1), Decimal("600")),
    ]
    merged = [
        (date(2025, 1, 1), Decimal("-1000")),
        (date(2026, 1, 1), Decimal("1100")),
    ]
    assert float(xirr(split)) == pytest.approx(float(xirr(merged)), abs=1e-9)
    assert float(xirr(merged)) == pytest.approx(0.10, abs=2e-4)


def test_xirr_degenerate_inputs_still_return_none():
    assert xirr([]) is None
    assert xirr([(date(2025, 1, 1), Decimal("-1000"))]) is None
    assert xirr([(date(2025, 1, 1), Decimal("1000"))]) is None
    # 同日正负相抵为零 → 无有效流
    assert xirr([
        (date(2025, 1, 1), Decimal("-1000")),
        (date(2025, 1, 1), Decimal("1000")),
    ]) is None


@pytest.fixture
def rate_db():
    db = SessionLocal()
    db.query(ExchangeRate).delete()
    db.commit()
    try:
        yield db
    finally:
        db.query(ExchangeRate).delete()
        db.commit()
        db.close()


def _count_queries(db, fn):
    counter = {"n": 0}

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            counter["n"] += 1

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
    return counter["n"]


def test_latest_rate_is_cached_per_session(rate_db):
    db = rate_db
    exchange_rate_service.update_or_create_rate(
        db, "USD", "CNY", Decimal("7.0"), effective_date=date(2026, 1, 1)
    )

    queries = _count_queries(
        db,
        lambda: [exchange_rate_service.convert_to_cny(db, Decimal("1"), "USD") for _ in range(50)],
    )
    # 50 次换算最多触发 1 次汇率查询（首查入缓存，其余命中）
    assert queries <= 1

    other = SessionLocal()
    try:
        # 新会话（新请求）不复用旧会话缓存
        assert exchange_rate_service.convert_to_cny(other, Decimal("1"), "USD") == Decimal("7.0")
    finally:
        other.close()


def test_rate_cache_invalidated_on_write(rate_db):
    db = rate_db
    exchange_rate_service.update_or_create_rate(
        db, "USD", "CNY", Decimal("7.0"), effective_date=date(2026, 1, 1)
    )
    assert exchange_rate_service.convert_to_cny(db, Decimal("1"), "USD") == Decimal("7.0")

    # 同一会话内更新汇率：写路径必须使缓存失效，读到新值
    exchange_rate_service.update_or_create_rate(
        db, "USD", "CNY", Decimal("7.5"), effective_date=date(2026, 1, 2)
    )
    assert exchange_rate_service.convert_to_cny(db, Decimal("1"), "USD") == Decimal("7.5")


def test_xirr_long_horizon_does_not_underflow():
    """检视意见回归：81 年跨度在搜索下界 rate≈-0.9999 处 base**years 下溢，
    float 版曾除零抛 ZeroDivisionError（Decimal 版正常）。"""
    flows = [(date(1900, 1, 1), Decimal("-1000")), (date(1981, 1, 1), Decimal("1100"))]
    rate = xirr(flows)
    days = (date(1981, 1, 1) - date(1900, 1, 1)).days
    expected = 1.1 ** (365.25 / days) - 1
    assert float(rate) == pytest.approx(expected, abs=5e-5)

    # 更极端：120 年跨度 + 多笔同向/反向流，只要求不抛异常且解有限
    flows = [
        (date(1900, 1, 1), Decimal("-1000")),
        (date(1950, 6, 15), Decimal("-500")),
        (date(2020, 1, 1), Decimal("9000")),
    ]
    rate = xirr(flows)
    assert rate is not None
    assert 0 < float(rate) < 1


def test_xirr_long_horizon_mixed_signs_preserves_relative_magnitudes():
    """检视意见回归：逐项钳制会让两个极端项同值饱和后互相抵消，把搜索下界
    -0.9999 误判成根。对数域求和保留相对量级，须与 Decimal 参考解一致。"""
    flows = [
        (date(1900, 1, 1), Decimal("-1000")),
        (date(1981, 1, 1), Decimal("-500")),
        (date(1982, 1, 1), Decimal("2000")),
    ]
    rate = xirr(flows)
    # Decimal 参考实现（main 版本）在同一输入上的收敛值
    assert float(rate) == pytest.approx(0.0049368537, abs=1e-6)
    assert float(rate) != pytest.approx(-0.9999, abs=1e-4)
