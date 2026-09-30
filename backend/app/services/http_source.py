"""外部数据源 HTTP 的共用底座（#281）：分源限速与浏览器 UA。

此前中国货币网、美国财政部、港交所日报、东方财富、Tiingo 各写一份
`_throttle_lock + _last_fetch_at` 全局限速，与 `report_fetchers` 的按源分桶实现并存；
腾讯被报价、K 线、取名、美股代码探测四条路径调用，却没有任何限速。这里只有一份。

限速语义（issue #145 / PR #170 复审）：每个 key 一把锁，等待发生在**同源锁内**、按实际
monotonic 复验——cninfo 的 1s 等待不会把并发的 EDGAR(0.15s) 调用挡在外面，
而同源后来者排在锁上，放行时从**实际**上一次请求时刻重新度量，间隔恒 ≥ min_interval。
刻意不用「锁外按预约时隙 sleep」：sleep 醒来的时刻不受控，不复验就放行会出现同源
突发与顺序反转。
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Optional

# 浏览器 UA：只对要求像浏览器的公开站点用（腾讯、雅虎、东方财富）；
# EDGAR 这类要求表明身份的源各自声明 UA，不要用它
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

_state_lock = threading.Lock()  # 只保护下面两张表的创建与取用
_key_locks: Dict[str, threading.Lock] = {}
_last_request_at: Dict[str, float] = {}


def _sleep(seconds: float) -> None:
    # 调用时才解析 time.sleep：monkeypatch time.sleep 的测试照样生效；
    # 延迟唤醒场景的回归测试直接替换本函数
    time.sleep(seconds)


def throttle(key: str, min_interval: float, *, check: Optional[Callable[[], None]] = None) -> None:
    """占 key 的一个请求名额：距上一次放行不足 min_interval 则在同源锁内等待。

    check（可选）在等待前、等待后各调用一次（都在锁内），用于「排队期间状态变了就别发」
    ——Tiingo 撞 429 后的冷却：排在锁上的请求醒来后必须再查一次（PR #246 评审 P2）。
    check 抛出的异常原样传出，本次不占名额。
    """
    with _state_lock:
        key_lock = _key_locks.setdefault(key, threading.Lock())
    with key_lock:
        if check is not None:
            check()
        elapsed = time.monotonic() - _last_request_at.get(key, 0.0)
        if elapsed < min_interval:
            _sleep(min_interval - elapsed)
            if check is not None:
                check()
        _last_request_at[key] = time.monotonic()


def reset_throttle(key: Optional[str] = None) -> None:
    """清空限速时钟（测试与轮换凭证后用）；key 为 None 清空全部。"""
    with _state_lock:
        if key is None:
            _last_request_at.clear()
        else:
            _last_request_at.pop(key, None)
