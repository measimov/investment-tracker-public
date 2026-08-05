"""Tushare 接口级频率错误的自适应冷却 + 档案同步的降级语义。

设计前提：低积分账号上 fina_audit/pledge_stat/stk_holdertrade 这类接口是
"每分钟 1 次"，批量分析连打必然撞限；但预设固定长间隔会把单标的分析从
1.5 分钟拖到 4.5 分钟，所以改为错误驱动的冷却（正常路径零开销）。
"""

import threading
import time

import pytest

from app.database import SessionLocal
from app.models.security_profile import SecurityProfileData
from app.services import security_profile_service as svc
from app.services import stock_price_service as prices

from .helpers import reset_tables


@pytest.fixture(autouse=True)
def clean_cooldowns():
    prices._tushare_cooldown_until.clear()
    prices._tushare_cooldown_strikes.clear()
    yield
    prices._tushare_cooldown_until.clear()
    prices._tushare_cooldown_strikes.clear()


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        reset_tables(session, [SecurityProfileData])
        yield session
        session.rollback()
        reset_tables(session, [SecurityProfileData])
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------


def test_classify_rate_before_fatal():
    """顺序敏感：致命串"抱歉，您"是频率消息的前缀，必须先判频率——
    判反了会把可恢复的接口限流当成 token 失效而中止整批。"""
    assert prices.classify_tushare_error(
        "抱歉，您每分钟最多访问该接口500次"
    ) == "rate"
    assert prices.classify_tushare_error("抱歉，您没有该接口权限") == "fatal"
    assert prices.classify_tushare_error("积分不足") == "fatal"
    assert prices.classify_tushare_error("每小时最多访问该接口") == "rate"
    assert prices.classify_tushare_error("connection reset") == "other"
    assert prices.classify_tushare_error(None) == "other"


# ---------------------------------------------------------------------------
# 冷却记录与清除
# ---------------------------------------------------------------------------


def test_cooldown_backoff_and_cap(monkeypatch):
    monkeypatch.setattr(prices.settings, "tushare_cooldown_base_seconds", 10.0)
    monkeypatch.setattr(prices.settings, "tushare_cooldown_max_seconds", 25.0)

    assert prices.note_tushare_rate_error("pledge_stat") == 10.0
    assert prices.note_tushare_rate_error("pledge_stat") == 20.0
    assert prices.note_tushare_rate_error("pledge_stat") == 25.0  # 封顶
    assert prices.tushare_cooldown_remaining("pledge_stat") > 0
    assert prices.tushare_cooldown_remaining("daily") == 0  # 只影响该接口

    prices.clear_tushare_cooldown("pledge_stat")
    assert prices.tushare_cooldown_remaining("pledge_stat") == 0
    # 清除后连击计数归零：下次从 base 重新开始
    assert prices.note_tushare_rate_error("pledge_stat") == 10.0


def test_tushare_query_records_rate_error_only(monkeypatch):
    """频率错误记冷却；致命错误不记（那不是接口级问题，冷却也救不了）。"""
    monkeypatch.setattr(prices, "wait_for_tushare_rate_limit", lambda api: None)

    def make_pro(message):
        class FakePro:
            def __getattr__(self, name):
                def call(**kwargs):
                    raise RuntimeError(message)

                return call

        return FakePro()

    monkeypatch.setattr(prices, "get_tushare_pro", lambda: make_pro("每分钟最多访问该接口1次"))
    with pytest.raises(RuntimeError):
        prices.tushare_query("pledge_stat")
    assert prices.tushare_cooldown_remaining("pledge_stat") > 0

    monkeypatch.setattr(prices, "get_tushare_pro", lambda: make_pro("积分不足"))
    with pytest.raises(RuntimeError):
        prices.tushare_query("fina_audit")
    assert prices.tushare_cooldown_remaining("fina_audit") == 0


def test_tushare_query_success_clears_cooldown(monkeypatch):
    import pandas as pd

    prices.note_tushare_rate_error("daily_basic")
    assert prices.tushare_cooldown_remaining("daily_basic") > 0

    monkeypatch.setattr(prices, "wait_for_tushare_rate_limit", lambda api: None)

    class FakePro:
        def __getattr__(self, name):
            return lambda **kwargs: pd.DataFrame([{"a": 1}])

    monkeypatch.setattr(prices, "get_tushare_pro", lambda: FakePro())
    prices.tushare_query("daily_basic")
    assert prices.tushare_cooldown_remaining("daily_basic") == 0


def test_cooldown_wait_never_blocks_the_global_gate():
    """[设计护栏] 冷却等待绝不能塞进 wait_for_tushare_rate_limit——那里的
    sleep 在 _tushare_rate_lock 临界区内，十几分钟的冷却会让全进程所有
    Tushare 调用（行情刷新/汇率/历史同步）一起阻塞。

    本用例锁死这一点：某接口处于长冷却时，其他接口的全局闸仍按正常间隔放行。
    """
    prices.note_tushare_rate_error("pledge_stat")
    assert prices.tushare_cooldown_remaining("pledge_stat") > 60

    elapsed: list = []

    def call_gate():
        started = time.monotonic()
        prices.wait_for_tushare_rate_limit("daily")
        elapsed.append(time.monotonic() - started)

    thread = threading.Thread(target=call_gate)
    thread.start()
    thread.join(timeout=5)
    assert elapsed and elapsed[0] < 2  # 未被别的接口的冷却拖住


# ---------------------------------------------------------------------------
# 档案同步的降级语义
# ---------------------------------------------------------------------------


def test_sync_skips_dataset_in_long_cooldown(db, monkeypatch):
    """长冷却的数据集进 skipped 而非 failed，其余数据集照常同步——
    批量分析里一个受限接口不该毁掉整只标的。"""
    called: list = []

    def fetch(dataset, symbol, market):
        called.append(dataset)
        return [{"end_date": "20251231"}]

    monkeypatch.setattr(svc, "fetch_dataset_rows", fetch)
    prices.note_tushare_rate_error("pledge_stat")  # 默认 65s > 内联等待阈值

    result = svc.sync_symbol_profile(db, "600036", "A股")

    assert result["supported"] is True
    assert "pledge_stat" not in called  # 冷却中不外呼
    skipped = {item["dataset"]: item for item in result["skipped"]}
    assert "pledge_stat" in skipped
    assert skipped["pledge_stat"]["reason"] == "rate_cooldown"
    assert skipped["pledge_stat"]["retry_after_seconds"] > 0
    assert result["failed"] == []  # 冷却不是失败
    assert "fina_indicator" in result["datasets"]  # 其余数据集不受影响


def test_sync_waits_inline_for_short_cooldown(db, monkeypatch):
    """短冷却（≤阈值）就地等一下再照常同步，不跳过。"""
    monkeypatch.setattr(prices.settings, "tushare_cooldown_base_seconds", 0.05)
    prices.note_tushare_rate_error("pledge_stat")

    called: list = []
    monkeypatch.setattr(
        svc, "fetch_dataset_rows",
        lambda dataset, symbol, market: called.append(dataset) or [{"end_date": "20251231"}],
    )
    result = svc.sync_symbol_profile(db, "600036", "A股")

    assert "pledge_stat" in called
    assert result["skipped"] == []


def test_degraded_datasets_reach_the_llm_input(db, monkeypatch):
    """[口径回归] 降级必须如实进 LLM 输入：模型不能把"没取到质押数据"
    当成"没有质押"——那是把限流伪装成利好。"""
    from app.services import security_analysis_jobs as jobs
    from app.services.security_analysis_prompts import build_system_prompt

    monkeypatch.setattr(
        svc, "fetch_dataset_rows",
        lambda dataset, symbol, market: [{"end_date": "20251231"}],
    )
    prices.note_tushare_rate_error("pledge_stat")
    monkeypatch.setattr(
        jobs, "chat_completion",
        lambda messages, **kw: {
            "content": (
                '{"tags":["数据不足"],"risk_level":"medium","summary":"s",'
                '"report_markdown":"r"}'
            ),
            "model": "m", "usage": {},
        },
    )
    monkeypatch.setattr(jobs, "resolve_public_security_name", lambda s, m: None)

    outcome = jobs.analyze_one(db, "600036", "A股", digest_max_new=0)
    assert outcome["status"] == "succeeded"
    assert any("pledge_stat" in item for item in outcome["degraded"])

    from app.models.security_profile import SecurityAnalysis

    payload = db.query(SecurityAnalysis).one().input_payload
    assert any("pledge_stat" in gap for gap in payload["profile_data_gaps"])
    # prompt 明确禁止把缺数据读成"无异常"
    assert "profile_data_gaps" in build_system_prompt("A股")
    assert "不等于没有质押" in build_system_prompt("A股")
