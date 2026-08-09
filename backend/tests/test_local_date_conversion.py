"""存储时间戳（UTC）→ 业务时区日期（issue #150，两轮）。

第一轮实现用了无参数 astimezone()——它取的是**进程系统时区**：生产后端容器
实测是 UTC，转换在那里空转，#150 在部署环境原样复现；CI runner 同为 UTC，
旧测试断言 helper 等于同一系统时区的 astimezone()（恒真），东时区守护又直接
skip，于是全绿假通过。

本文件按复审要求重写：**不依赖也不跳过运行环境的系统时区**——业务时区来自
settings.display_timezone（默认 Asia/Shanghai），断言全部是无条件的固定值，
在 UTC 容器/CI 上必须原样成立。
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.timeutil import business_timezone, local_today, to_local_date


def test_business_timezone_defaults_to_shanghai():
    assert str(business_timezone()) == "Asia/Shanghai"


def test_utc_evening_rolls_to_next_shanghai_date():
    """复审点名的固定断言：2026-08-05 16:07Z → 2026-08-06（无条件，UTC 环境也成立）。"""
    assert to_local_date(datetime(2026, 8, 5, 16, 7, tzinfo=timezone.utc)) == date(2026, 8, 6)


@pytest.mark.parametrize(
    "utc_dt,expected",
    [
        # 15:59Z 仍是上海 23:59 → 同日；16:00Z 起进入上海次日
        (datetime(2026, 8, 5, 15, 59, tzinfo=timezone.utc), date(2026, 8, 5)),
        (datetime(2026, 8, 5, 16, 0, tzinfo=timezone.utc), date(2026, 8, 6)),
        (datetime(2026, 8, 5, 23, 59, tzinfo=timezone.utc), date(2026, 8, 6)),
        (datetime(2026, 8, 6, 0, 0, tzinfo=timezone.utc), date(2026, 8, 6)),
        # 非 UTC 的 aware 值同样按业务时区换算（东十二区 04:00 = UTC 前一日 16:00）
        (datetime(2026, 8, 6, 4, 0, tzinfo=timezone(timedelta(hours=12))), date(2026, 8, 6)),
    ],
)
def test_aware_timestamps_convert_by_business_timezone(utc_dt, expected):
    assert to_local_date(utc_dt) == expected


def test_naive_timestamp_is_taken_as_business_local():
    """naive 只会来自测试构造：视作业务时区时刻，不做二次偏移。"""
    assert to_local_date(datetime(2026, 8, 6, 9, 30)) == date(2026, 8, 6)


def test_none_passes_through():
    assert to_local_date(None) is None


def test_local_today_matches_business_timezone_now():
    """local_today 与换算同源：把"现在"转出来的日期必须等于 local_today。"""
    now_utc = datetime.now(timezone.utc)
    assert local_today() == to_local_date(now_utc)


def test_display_timezone_is_configurable(monkeypatch):
    """时区可配置——换成 UTC 后同一时间戳落回 8-05，证明不是写死的偏移。"""
    from app.core import timeutil

    monkeypatch.setattr(timeutil.settings, "display_timezone", "UTC")
    timeutil.business_timezone.cache_clear()
    try:
        assert to_local_date(datetime(2026, 8, 5, 16, 7, tzinfo=timezone.utc)) == date(2026, 8, 5)
    finally:
        timeutil.business_timezone.cache_clear()


# ---------------------------------------------------------------------------
# 陈价边界（复审要求）：today 与 as_of 必须同一时区，否则天数差错一天
# ---------------------------------------------------------------------------


def test_price_staleness_boundary_uses_business_dates(monkeypatch):
    """UTC 深夜更新的价格：按业务时区恰好 PRICE_STALE_DAYS 天 → 不陈价。

    混用口径（UTC 的 as_of 日期 + 任一 today）会把天数差多算一天、误判陈价。
    这里在 UTC 环境下直接跑 resolve_server_prices 的真实路径断言。
    """
    from app.database import SessionLocal
    from app.models.corporate_action import CorporateAction
    from app.models.holding import Holding
    from app.models.transaction import Transaction
    from app.services.statistics import pricing as ss
    from tests.helpers import reset_tables

    frozen_today = date(2026, 8, 6)
    monkeypatch.setattr(ss, "local_today", lambda: frozen_today)

    # UTC 2026-07-29 22:00 = 上海 2026-07-30 06:00 → 距 8-06 恰 7 天（阈值内）；
    # 按 UTC 日期算是 7-29 → 8 天（会被误判陈价）
    boundary_utc = datetime(2026, 7, 29, 22, 0, tzinfo=timezone.utc)

    db = SessionLocal()
    try:
        reset_tables(db, [Holding, CorporateAction, Transaction])
        db.add(Holding(
            user_id=1, symbol="PCT", name="柏能集团", market="新加坡股",
            quantity=Decimal("100"), avg_cost=Decimal("1"), total_cost=Decimal("100"),
            currency="SGD", current_price=Decimal("2"),
            price_updated_at=boundary_utc,
        ))
        db.commit()

        _, _, freshness = ss.resolve_server_prices(db, 1)

        info = freshness["PCT:新加坡股"]
        assert info["stale"] is False, (
            f"业务时区口径下恰 {ss.PRICE_STALE_DAYS} 天不应陈价；"
            f"stale=True 说明 as_of 或 today 混用了 UTC 口径：{info}"
        )
    finally:
        reset_tables(db, [Holding, CorporateAction, Transaction])
        db.close()


def test_price_staleness_flags_beyond_threshold(monkeypatch):
    """超过阈值一天必须判陈价——守住修复没有把判定放得过松。"""
    from app.database import SessionLocal
    from app.models.corporate_action import CorporateAction
    from app.models.holding import Holding
    from app.models.transaction import Transaction
    from app.services.statistics import pricing as ss
    from tests.helpers import reset_tables

    monkeypatch.setattr(ss, "local_today", lambda: date(2026, 8, 6))
    # 上海 2026-07-29（UTC 7-28 22:00）→ 距 8-06 为 8 天 > 阈值
    stale_utc = datetime(2026, 7, 28, 22, 0, tzinfo=timezone.utc)

    db = SessionLocal()
    try:
        reset_tables(db, [Holding, CorporateAction, Transaction])
        db.add(Holding(
            user_id=1, symbol="PCT", name="柏能集团", market="新加坡股",
            quantity=Decimal("100"), avg_cost=Decimal("1"), total_cost=Decimal("100"),
            currency="SGD", current_price=Decimal("2"),
            price_updated_at=stale_utc,
        ))
        db.commit()

        _, _, freshness = ss.resolve_server_prices(db, 1)

        assert freshness["PCT:新加坡股"]["stale"] is True
    finally:
        reset_tables(db, [Holding, CorporateAction, Transaction])
        db.close()
