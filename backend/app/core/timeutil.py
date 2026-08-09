"""存储时间戳（UTC）→ 业务日期的统一换算。

DB 的时间戳列都是 `DateTime(timezone=True)`（存 UTC），而"哪一天"在业务与 UI
里指**业务时区**（`settings.display_timezone`，默认 Asia/Shanghai）的日期。

两条铁律：

1. **不能用进程系统时区**。生产后端容器实测是 UTC，无参数 `astimezone()` /
   `date.today()` 在那里得到的都是 UTC 口径——东八区跨日转换根本不会发生，
   `data_fetched_at` 照样显示成前一天（#150 在部署环境原样复现）。
2. **"今天"必须与日期换算来自同一时区**。一边把时间戳转成东八区日期、
   一边用 UTC 容器的 `date.today()` 当今天，价格新鲜度的天数差就会错一天。
   需要与存储时间戳比较的"今天"一律用本模块的 `local_today()`。
"""

from datetime import date, datetime
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo

from ..config import settings


@lru_cache(maxsize=1)
def business_timezone() -> ZoneInfo:
    return ZoneInfo(settings.display_timezone)


def to_local_date(value: Optional[datetime]) -> Optional[date]:
    """tz-aware 时间戳 → 业务时区日期；naive 值按业务时区语义直取日期。

    timestamptz 列经 psycopg2 读出恒为 tz-aware；naive 只会来自测试构造，
    视作已是业务时区的时刻，不再二次偏移。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.date()
    return value.astimezone(business_timezone()).date()


def local_today() -> date:
    """业务时区的"今天"。凡与存储时间戳换算出的日期比较，禁用 date.today()。"""
    return datetime.now(business_timezone()).date()
