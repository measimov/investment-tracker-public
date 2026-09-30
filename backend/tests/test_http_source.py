"""外部源分源限速（http_source，#281；语义源自 issue #145 / PR #170 复审）。"""

import threading
import time
from typing import Dict

import pytest

from app.services import http_source


def test_throttle_sources_do_not_block_each_other(monkeypatch):
    """跨源不再 head-of-line blocking：A 源的预约等待不拖 B 源。

    旧实现持锁 sleep 且四源共用一把锁——A 源第二次调用在锁内睡 0.4s 时，
    B 源的首次调用会被挡在锁外白等。新实现每源一把锁，A 只占 A 的锁。
    """
    monkeypatch.setattr(http_source, "_key_locks", {})
    monkeypatch.setattr(http_source, "_last_request_at", {})

    http_source.throttle("source-a", 0.4)  # 首次：不等待，预约下一时隙

    results: Dict[str, float] = {}

    def second_a():
        start = time.monotonic()
        http_source.throttle("source-a", 0.4)  # 同源第二次：要等 ~0.4s
        results["a"] = time.monotonic() - start

    thread = threading.Thread(target=second_a)
    thread.start()
    time.sleep(0.05)  # 让 A 的等待先发生
    start = time.monotonic()
    http_source.throttle("source-b", 0.4)  # 异源首次：必须立刻通过
    results["b"] = time.monotonic() - start
    thread.join()

    assert results["b"] < 0.2, f"异源调用被拖了 {results['b']:.3f}s（跨源阻塞未修）"
    assert results["a"] >= 0.25, "同源第二次调用应等到预约时隙"


def test_same_source_waiters_keep_order_and_spacing_after_delayed_wakeup(monkeypatch):
    """[PR #170 复审回归] 较早的同源 waiter 延迟唤醒后，实际请求仍按序且间隔达标。

    预约制的失效模式：B 预约 t+i、C 预约 t+2i；B 因调度延迟到 t+2.2i 才醒，
    C 在 t+2i 先返回——顺序反转且实际间隔只有 0.2i。同源锁内按实际时刻
    校验后：C 排在锁上等 B 放行，再从 B 的**实际**请求时刻重新度量。
    """
    interval = 0.3
    real_sleep = time.sleep
    lagged = {"done": False}

    def lagging_sleep(wait):
        # 第一个进入等待的 waiter（B）被模拟成延迟唤醒：多睡 1.2 个间隔
        if not lagged["done"] and wait > 0:
            lagged["done"] = True
            real_sleep(wait + interval * 1.2)
        else:
            real_sleep(wait)

    monkeypatch.setattr(http_source, "_sleep", lagging_sleep)
    monkeypatch.setattr(http_source, "_key_locks", {})
    monkeypatch.setattr(http_source, "_last_request_at", {})

    grants = []
    grants_lock = threading.Lock()

    def call(tag):
        http_source.throttle("lag-src", interval)
        with grants_lock:
            grants.append((tag, time.monotonic()))

    http_source.throttle("lag-src", interval)  # 占住起点时刻
    thread_b = threading.Thread(target=call, args=("B",))
    thread_b.start()
    real_sleep(0.05)  # 确保 B 先进入等待
    thread_c = threading.Thread(target=call, args=("C",))
    thread_c.start()
    thread_b.join()
    thread_c.join()

    assert [tag for tag, _ in grants] == ["B", "C"], f"顺序反转: {grants}"
    gap = grants[1][1] - grants[0][1]
    assert gap >= interval * 0.9, f"实际间隔只有 {gap:.3f}s（突发未修）"


def test_check_runs_before_and_after_wait_and_blocks_grant(monkeypatch):
    """check 在等待前后各查一次；等待后抛出则本次不占名额（Tiingo 冷却的语义）。"""
    monkeypatch.setattr(http_source, "_key_locks", {})
    monkeypatch.setattr(http_source, "_last_request_at", {})
    clock = {"now": 1000.0}
    monkeypatch.setattr(http_source.time, "monotonic", lambda: clock["now"])
    state = {"blocked": False, "checks": 0}

    def check():
        state["checks"] += 1
        if state["blocked"]:
            raise RuntimeError("冷却中")

    def sleep_and_block(seconds):
        state["blocked"] = True
        clock["now"] += seconds

    monkeypatch.setattr(http_source, "_sleep", sleep_and_block)
    http_source.throttle("k", 1.0, check=check)
    granted_at = http_source._last_request_at["k"]
    clock["now"] += 0.2
    with pytest.raises(RuntimeError, match="冷却"):
        http_source.throttle("k", 1.0, check=check)
    assert state["checks"] == 3
    assert http_source._last_request_at["k"] == granted_at


def test_reset_throttle_clears_one_or_all(monkeypatch):
    monkeypatch.setattr(http_source, "_last_request_at", {"a": 1.0, "b": 2.0})
    http_source.reset_throttle("a")
    assert http_source._last_request_at == {"b": 2.0}
    http_source.reset_throttle()
    assert http_source._last_request_at == {}


def test_exchange_timezones_are_defined_once():
    """交易所时区只在 market_sessions.MARKET_TIMEZONES 定义（#281，此前四份）。"""
    import ast
    from pathlib import Path

    from app.services.market_sessions import MARKET_TIMEZONES, market_timezone

    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        if path.name == "market_sessions.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", None)) == "ZoneInfo"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in set(MARKET_TIMEZONES.values())
            ):
                offenders.append(f"{path.relative_to(app_dir)}:{node.lineno}")
    assert offenders == []
    assert str(market_timezone("美股")) == "America/New_York"
    assert market_timezone("场外开基") is None


def test_edgar_submissions_short_ttl_cache(monkeypatch):
    """同一 CIK 的 submissions 在 TTL 内只拉一次；过期或清空后重拉（#281）。"""
    from app.services import report_fetchers

    calls = []
    monkeypatch.setattr(
        report_fetchers, "_edgar_get_json", lambda url: calls.append(url) or {"n": len(calls)}
    )
    clock = {"now": 1000.0}
    monkeypatch.setattr(report_fetchers, "_monotonic", lambda: clock["now"])
    assert report_fetchers.edgar_submissions(1) == {"n": 1}
    assert report_fetchers.edgar_submissions("1") == {"n": 1}
    clock["now"] += report_fetchers._SUBMISSIONS_TTL_SECONDS + 1
    assert report_fetchers.edgar_submissions(1) == {"n": 2}
    report_fetchers.clear_edgar_submissions_cache()
    assert report_fetchers.edgar_submissions(1) == {"n": 3}
