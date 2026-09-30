"""各市场的交易时段判定（纯函数，不查交易日历）。

只判「工作日 + 当地时段」：节假日照样判为开市，这时报价源返回的是上一交易日收盘，
写回的价格与行情日期都不变，多一次请求而已——为此引入交易日历（各市场一个接口、
要 token、要缓存）得不偿失。每个时段在收盘后多留约 30 分钟，15 分钟一次的刷新
因此至少有一次落在收盘之后，拿到的是收盘价而不是最后一笔盘中价。
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

Window = Tuple[time, time]

# 交易所本地时区：全仓唯一定义（#281，此前四份）。美东由 zoneinfo 自动处理夏令时。
# 毫秒时间戳与无日期的报价时间换算成交易日一律用它：美股 16:00 ET 收盘在东八区已是
# 次日凌晨，按业务时区换算会把周五收盘记成周六。
MARKET_TIMEZONES: Dict[str, str] = {
    "A股": "Asia/Shanghai",
    "B股": "Asia/Shanghai",
    "港股": "Asia/Hong_Kong",
    "美股": "America/New_York",
    "新加坡股": "Asia/Singapore",
}

# 交易时段 [(开始, 结束)]，结束已含收盘后的余量；未登记的市场不判开市
_SESSIONS: Dict[str, List[Window]] = {
    "A股": [(time(9, 25), time(11, 35)), (time(12, 55), time(15, 30))],
    "B股": [(time(9, 25), time(11, 35)), (time(12, 55), time(15, 30))],
    "港股": [(time(9, 25), time(12, 5)), (time(12, 55), time(16, 40))],
    "美股": [(time(9, 25), time(16, 30))],
}

SESSION_MARKETS = tuple(_SESSIONS)


def market_timezone(market: str) -> Optional[ZoneInfo]:
    name = MARKET_TIMEZONES.get(market)
    return ZoneInfo(name) if name else None


def is_session_open(market: str, now_utc: datetime) -> bool:
    """now_utc 须带时区。未登记交易时段的市场（新加坡股、场外基金…）一律 False。"""
    windows = _SESSIONS.get(market)
    if windows is None:
        return False
    if now_utc.tzinfo is None:
        raise ValueError("now_utc 必须带时区")
    local = now_utc.astimezone(market_timezone(market))
    if local.weekday() >= 5:
        return False
    moment = local.time()
    return any(start <= moment <= end for start, end in windows)


def open_markets(now_utc: datetime, markets: Iterable[str] = SESSION_MARKETS) -> List[str]:
    return [market for market in markets if is_session_open(market, now_utc)]
