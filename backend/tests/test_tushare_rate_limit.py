"""Tushare 全局限速、配额快速失败与同步增量化的行为回归。"""

import time
from datetime import date, datetime

import pytest

from app.database import SessionLocal
from app.models.background_job import BackgroundJob
from app.services import performance_history_jobs, stock_price_service
from app.services.stock_price_service import retry_with_backoff, wait_for_tushare_rate_limit


@pytest.fixture
def reset_rate_gate():
    stock_price_service._tushare_last_call_by_api.clear()
    yield
    stock_price_service._tushare_last_call_by_api.clear()


def test_global_gate_spaces_all_apis(monkeypatch, reset_rate_gate):
    """全局闸对所有 API（含跨接口、无 per-API 间隔的接口）统一生效。"""
    monkeypatch.setattr(
        stock_price_service.settings, "tushare_global_min_interval_seconds", 0.12
    )
    start = time.monotonic()
    wait_for_tushare_rate_limit("daily")
    wait_for_tushare_rate_limit("stock_basic")  # 不同 API 也要隔开
    wait_for_tushare_rate_limit("rt_k")
    elapsed = time.monotonic() - start
    assert elapsed >= 0.24  # 3 次调用 ≥ 2 个全局间隔


def test_global_gate_disabled_when_zero(monkeypatch, reset_rate_gate):
    monkeypatch.setattr(
        stock_price_service.settings, "tushare_global_min_interval_seconds", 0.0
    )
    start = time.monotonic()
    for _ in range(5):
        wait_for_tushare_rate_limit("daily")
    assert time.monotonic() - start < 0.05


def test_quota_errors_fail_fast_without_retry():
    """配额/权限类错误命中即抛，不再退避重试（重试只会火上浇油）。"""
    for message in (
        "抱歉，您每分钟最多访问该接口500次",
        "您没有访问该接口的权限",
        "积分不足",
    ):
        calls = {"n": 0}

        def failing():
            calls["n"] += 1
            raise RuntimeError(message)

        with pytest.raises(RuntimeError):
            retry_with_backoff(failing, max_retries=3, initial_delay=0.01, max_delay=0.02)
        assert calls["n"] == 1, message

    # 普通瞬时错误仍然重试
    calls = {"n": 0}

    def transient():
        calls["n"] += 1
        raise RuntimeError("connection reset")

    with pytest.raises(RuntimeError):
        retry_with_backoff(transient, max_retries=3, initial_delay=0.01, max_delay=0.02)
    assert calls["n"] == 3


@pytest.fixture
def clear_history_jobs():
    db = SessionLocal()
    try:
        db.query(BackgroundJob).filter(
            BackgroundJob.user_id == 2,
            BackgroundJob.job_type == performance_history_jobs.JOB_TYPE,
        ).delete(synchronize_session=False)
        db.commit()
        yield
        db.query(BackgroundJob).filter(
            BackgroundJob.user_id == 2,
            BackgroundJob.job_type == performance_history_jobs.JOB_TYPE,
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_history_job_aborts_immediately_on_quota_error(monkeypatch, clear_history_jobs):
    """配额错误对全批标的等价：首个命中即中止整个 job（确定性失败，不重排、
    不再逐标的烧配额），不等 5 连败。"""
    monkeypatch.setattr(
        performance_history_jobs,
        "get_history_sync_targets",
        lambda db, user_id, start_date, end_date: {
            "start_date": datetime(2026, 7, 1).date(),
            "end_date": datetime(2026, 7, 10).date(),
            "targets": [
                {"symbol": f"S{i}", "market": "美股", "currency": "USD"} for i in range(10)
            ],
        },
    )
    calls = {"n": 0}

    def quota_fetch(*args, **kwargs):
        calls["n"] += 1
        return {
            "symbol": kwargs["symbol"],
            "market": kwargs["market"],
            "success": False,
            "error": "tushare daily 失败: 抱歉，您每分钟最多访问该接口500次",
        }

    monkeypatch.setattr(
        performance_history_jobs,
        "fetch_and_store_security_price_history_incremental",
        quota_fetch,
    )
    job = performance_history_jobs.start_performance_history_sync_job(2)
    performance_history_jobs.run_performance_history_sync_job(job["id"])

    stored = performance_history_jobs.get_performance_history_sync_job(job["id"], 2)
    assert stored["status"] == "failed"
    assert "配额受限" in stored["error"]
    assert calls["n"] == 1  # 首个标的命中即停，不再扫剩余 9 只

    db = SessionLocal()
    try:
        row = db.get(BackgroundJob, job["id"])
        assert row.attempt_count == 1  # 确定性失败不重排
    finally:
        db.close()


def test_refresh_history_uses_incremental_fetch(monkeypatch):
    """统计页 refresh_history=true 必须走增量抓取（此前每次全量重拉全区间）。"""
    from decimal import Decimal

    from app.models.exchange_rate import ExchangeRate
    from app.models.holding import Holding
    from app.models.security_price import SecurityPrice
    from app.models.transaction import Transaction
    from app.services import statistics_service

    db = SessionLocal()
    for model in (SecurityPrice, Holding, Transaction, ExchangeRate):
        db.query(model).delete()
    db.commit()
    try:
        db.add(Transaction(
            user_id=1, symbol="600000", name="增量标的", market="A股",
            transaction_type="BUY", quantity=Decimal("100"), price=Decimal("10"),
            fee=Decimal("0"), transaction_date=date(2026, 1, 5), currency="CNY",
        ))
        db.commit()

        calls = []

        def fake_incremental(db_, **kwargs):
            calls.append(kwargs["symbol"])
            return {"symbol": kwargs["symbol"], "market": kwargs["market"],
                    "success": True, "rows": 0, "skipped": True}

        monkeypatch.setattr(
            statistics_service,
            "fetch_and_store_security_price_history_incremental",
            fake_incremental,
        )
        result = statistics_service.calculate_performance_analytics(
            db, 1, {"600000": 10}, refresh_history=True,
        )
        assert calls == ["600000"]
        assert result["data_quality"]["sync_results"][0]["skipped"] is True
    finally:
        for model in (SecurityPrice, Holding, Transaction, ExchangeRate):
            db.query(model).delete()
        db.commit()
        db.close()


def test_exchange_rate_auto_refresh_checks_all_required_currencies(monkeypatch):
    """检视意见回归：仅有今日 USD/CNY 不算完整——HKD/SGD 任一缺失即须刷新
    （部分写入中断后下一轮检查自愈）；三币种齐备才零外呼跳过。"""
    from decimal import Decimal

    from app.models.exchange_rate import ExchangeRate
    from app.services import exchange_rate_service

    db = SessionLocal()
    db.query(ExchangeRate).delete()
    db.commit()
    db.close()

    calls = {"n": 0}

    def fake_fetch(db_):
        calls["n"] += 1
        return {"USD/CNY": Decimal("7.2")}

    monkeypatch.setattr(exchange_rate_service, "fetch_latest_rates_from_api", fake_fetch)

    # 空库 → 刷新
    assert exchange_rate_service.refresh_rates_if_stale() == 1
    assert calls["n"] == 1

    db = SessionLocal()
    try:
        # 仅今日 USD → 仍不完整，继续刷新（不得被掩盖）
        db.add(ExchangeRate(
            from_currency="USD", to_currency="CNY", rate=Decimal("7.2"),
            effective_date=date.today(), source="test", is_active=True,
        ))
        db.commit()
        assert exchange_rate_service.refresh_rates_if_stale() == 1
        assert calls["n"] == 2

        # 三币种齐备 → 零外呼跳过
        for currency, rate in (("HKD", "0.92"), ("SGD", "5.4")):
            db.add(ExchangeRate(
                from_currency=currency, to_currency="CNY", rate=Decimal(rate),
                effective_date=date.today(), source="test", is_active=True,
            ))
        db.commit()
        assert exchange_rate_service.refresh_rates_if_stale() == 0
        assert calls["n"] == 2
    finally:
        db.query(ExchangeRate).delete()
        db.commit()
        db.close()


def test_compose_passes_tushare_and_llm_settings():
    """配置回归：生产 Compose 必须显式透传新增的环境变量，否则 .env 修改
    只参与插值、不进容器，运行时永远用代码默认值。"""
    from pathlib import Path

    compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
    for var in (
        "TUSHARE_GLOBAL_MIN_INTERVAL_SECONDS=${TUSHARE_GLOBAL_MIN_INTERVAL_SECONDS:-0.35}",
        "LLM_REPORT_API_KEY=${LLM_REPORT_API_KEY:-}",
        "LLM_REPORT_MODEL=${LLM_REPORT_MODEL:-deepseek-v4-pro}",
    ):
        assert var in compose, var


def test_incremental_skips_when_cached_through_yesterday(monkeypatch):
    """缓存已到昨天 + 请求区间含今天：不得视为尾部缺口（当日日线尚不存在），
    重复刷新必须零外呼跳过——此前 256 只标的每次刷新各打一次是配额主因。"""
    from datetime import timedelta
    from decimal import Decimal

    from app.database import SessionLocal as SL
    from app.models.security_price import SecurityPrice
    from app.services import market_data_service as mds

    db = SL()
    db.query(SecurityPrice).filter_by(symbol="INCR01").delete()
    db.commit()
    try:
        yesterday = date.today() - timedelta(days=1)
        for offset in (3, 2, 1):
            db.add(SecurityPrice(
                symbol="INCR01", market="A股", ts_code="INCR01.SH",
                price_date=date.today() - timedelta(days=offset),
                currency="CNY", close_price=Decimal("10"), source="test",
            ))
        db.commit()

        def explode(*args, **kwargs):
            raise AssertionError("不得发生外呼")

        monkeypatch.setattr(mds, "fetch_and_store_security_price_history", explode)
        result = mds.fetch_and_store_security_price_history_incremental(
            db, symbol="INCR01", market="A股",
            start_date=date.today() - timedelta(days=3),
            end_date=date.today(),  # 含今天
        )
        assert result["skipped"] is True
        assert result["coverage_before"]["end_date"] == yesterday.isoformat()
    finally:
        db.query(SecurityPrice).filter_by(symbol="INCR01").delete()
        db.commit()
        db.close()


def test_sync_targets_clamp_exited_symbols_to_last_trade_date():
    """已清仓标的的同步终点钳到最后一笔交易日（回测只需持有区间内行情），
    退市/摘牌标的不再有永远补不上的尾部缺口；在持标的仍到全局终点，
    其 uncovered 保持为真失败以保住数据质量告警。"""
    from decimal import Decimal

    from app.models.holding import Holding
    from app.models.transaction import Transaction

    db = SessionLocal()
    db.query(Holding).filter(Holding.user_id == 2).delete()
    db.query(Transaction).filter(Transaction.user_id == 2).delete()
    db.commit()
    try:
        # 已清仓：买入后全部卖出，最后交易日 2026-03-01
        for txn_type, txn_date in (("BUY", date(2026, 1, 5)), ("SELL", date(2026, 3, 1))):
            db.add(Transaction(
                user_id=2, symbol="DEAD01", name="退市标的", market="港股",
                transaction_type=txn_type, quantity=Decimal("100"),
                price=Decimal("10"), fee=Decimal("0"),
                transaction_date=txn_date, currency="HKD",
            ))
        # 在持：有持仓行
        db.add(Transaction(
            user_id=2, symbol="ALIVE1", name="在持标的", market="美股",
            transaction_type="BUY", quantity=Decimal("10"), price=Decimal("5"),
            fee=Decimal("0"), transaction_date=date(2026, 2, 1), currency="USD",
        ))
        db.add(Holding(
            user_id=2, broker_account_id=None, symbol="ALIVE1", name="在持标的",
            market="美股", quantity=Decimal("10"), avg_cost=Decimal("5"),
            total_cost=Decimal("50"), currency="USD",
        ))
        db.commit()

        info = performance_history_jobs.get_history_sync_targets(db, 2)
        by_symbol = {t["symbol"]: t for t in info["targets"]}
        assert by_symbol["DEAD01"]["end_date"] == date(2026, 3, 1)  # 钳到清仓日
        assert by_symbol["ALIVE1"]["end_date"] == info["end_date"]  # 在持到全局终点
        # 起点同理钳到首笔交易日：买入前的历史不需要
        assert by_symbol["DEAD01"]["start_date"] == date(2026, 1, 5)
        assert by_symbol["ALIVE1"]["start_date"] == date(2026, 2, 1)
    finally:
        db.query(Holding).filter(Holding.user_id == 2).delete()
        db.query(Transaction).filter(Transaction.user_id == 2).delete()
        db.commit()
        db.close()


def test_uncovered_on_held_symbol_still_counts_as_failure(monkeypatch, clear_history_jobs):
    """通用 uncovered（活跃标的的源覆盖缺口/提供方故障）保持为失败，
    连败中止语义不变——不得被吞成"无数据"而让整批显示成功。"""
    monkeypatch.setattr(
        performance_history_jobs,
        "get_history_sync_targets",
        lambda db, user_id, start_date, end_date: {
            "start_date": datetime(2026, 7, 1).date(),
            "end_date": datetime(2026, 7, 10).date(),
            "targets": [
                {"symbol": f"S{i}", "market": "美股", "currency": "USD",
                 "end_date": datetime(2026, 7, 10).date()}
                for i in range(6)
            ],
        },
    )
    monkeypatch.setattr(
        performance_history_jobs,
        "fetch_and_store_security_price_history_incremental",
        lambda *a, **kw: {
            "symbol": kw["symbol"], "market": kw["market"], "success": False,
            "rows": 0, "error": "数据源未返回请求区间内的历史行情",
            "coverage_status": "uncovered",
        },
    )
    job = performance_history_jobs.start_performance_history_sync_job(2)
    performance_history_jobs.run_performance_history_sync_job(job["id"])
    stored = performance_history_jobs.get_performance_history_sync_job(job["id"], 2)
    assert stored["status"] == "failed"
    assert stored["failed_count"] == 5  # 5 连败中止语义保留


def test_effective_tail_end_skips_weekends(monkeypatch):
    """周末不产生假缺口：周日刷新、缓存到周五 → 零外呼跳过。
    （日历接口不可用时退化为工作日启发，该保护同样成立）"""
    from datetime import timedelta
    from decimal import Decimal

    from app.models.security_price import SecurityPrice
    from app.services import market_data_service as mds

    sunday = date(2026, 7, 26)
    assert sunday.weekday() == 6
    friday = date(2026, 7, 24)

    db = SessionLocal()
    db.query(SecurityPrice).filter_by(symbol="WKND01").delete()
    db.commit()
    mds._trade_cal_cache.clear()
    try:
        for offset in range(3):
            db.add(SecurityPrice(
                symbol="WKND01", market="A股", ts_code="WKND01.SH",
                price_date=friday - timedelta(days=offset),
                currency="CNY", close_price=Decimal("10"), source="test",
            ))
        db.commit()

        monkeypatch.setattr(mds, "_today", lambda: sunday)
        # 日历接口失败 → 退化为最近工作日（周五）
        monkeypatch.setattr(
            mds, "tushare_query_once",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("calendar down")),
        )

        def explode(*args, **kwargs):
            raise AssertionError("周末不得外呼")

        monkeypatch.setattr(mds, "fetch_and_store_security_price_history", explode)
        result = mds.fetch_and_store_security_price_history_incremental(
            db, symbol="WKND01", market="A股",
            start_date=friday - timedelta(days=2), end_date=sunday,
        )
        assert result["skipped"] is True
    finally:
        db.query(SecurityPrice).filter_by(symbol="WKND01").delete()
        db.commit()
        mds._trade_cal_cache.clear()
        db.close()


def test_trade_calendar_excludes_weekday_holiday(monkeypatch):
    """法定假日（落在工作日）由交易日历精确排除：缓存到节前最后交易日
    即零外呼跳过；日历每市场每天只调用一次（进程内缓存）。"""
    from datetime import timedelta
    from decimal import Decimal

    import pandas as pd

    from app.models.security_price import SecurityPrice
    from app.services import market_data_service as mds

    tuesday = date(2026, 7, 28)  # 周二；假设周一 07-27 为法定假日
    assert tuesday.weekday() == 1
    last_open = date(2026, 7, 24)  # 节前最后交易日 = 上周五

    calendar_calls = []

    def fake_calendar(api_name, **kwargs):
        calendar_calls.append(api_name)
        days = []
        d = tuesday - timedelta(days=20)
        while d < tuesday:
            is_open = 1 if (d.weekday() < 5 and d != date(2026, 7, 27)) else 0
            days.append({"cal_date": d.strftime("%Y%m%d"), "is_open": is_open})
            d += timedelta(days=1)
        return pd.DataFrame(days)

    db = SessionLocal()
    for sym in ("HOLI01", "HOLI02"):
        db.query(SecurityPrice).filter_by(symbol=sym).delete()
    db.commit()
    mds._trade_cal_cache.clear()
    try:
        for sym in ("HOLI01", "HOLI02"):
            db.add(SecurityPrice(
                symbol=sym, market="A股", ts_code=f"{sym}.SH",
                price_date=last_open, currency="CNY",
                close_price=Decimal("10"), source="test",
            ))
        db.commit()
        monkeypatch.setattr(mds, "_today", lambda: tuesday)
        monkeypatch.setattr(mds, "tushare_query_once", fake_calendar)

        def explode(*args, **kwargs):
            raise AssertionError("假日不得外呼")

        monkeypatch.setattr(mds, "fetch_and_store_security_price_history", explode)
        for sym in ("HOLI01", "HOLI02"):
            result = mds.fetch_and_store_security_price_history_incremental(
                db, symbol=sym, market="A股",
                start_date=last_open, end_date=tuesday,
            )
            assert result["skipped"] is True
        assert calendar_calls == ["trade_cal"]  # 日历每市场每天仅 1 次
    finally:
        for sym in ("HOLI01", "HOLI02"):
            db.query(SecurityPrice).filter_by(symbol=sym).delete()
        db.commit()
        mds._trade_cal_cache.clear()
        db.close()


def test_one_symbols_empty_result_never_suppresses_another(monkeypatch):
    """检视意见回归：同市场第一只标的空返回（停牌/摘牌/源不覆盖）后，
    第二只活跃标的的真实尾部缺口仍必须发生抓取并入库。"""
    from datetime import timedelta
    from decimal import Decimal

    from app.models.security_price import SecurityPrice
    from app.services import market_data_service as mds

    wednesday = date(2026, 7, 29)
    assert wednesday.weekday() == 2
    tuesday = date(2026, 7, 28)

    db = SessionLocal()
    for sym in ("SUSP01", "LIVE01"):
        db.query(SecurityPrice).filter_by(symbol=sym).delete()
    db.commit()
    mds._trade_cal_cache.clear()
    try:
        for sym in ("SUSP01", "LIVE01"):
            db.add(SecurityPrice(
                symbol=sym, market="A股", ts_code=f"{sym}.SH",
                price_date=tuesday - timedelta(days=1), currency="CNY",
                close_price=Decimal("10"), source="test",
            ))
        db.commit()
        monkeypatch.setattr(mds, "_today", lambda: wednesday)
        monkeypatch.setattr(
            mds, "tushare_query_once",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("calendar down")),
        )

        fetched = []

        def fetch(db_, *, symbol, market, start_date, end_date, currency=None):
            fetched.append(symbol)
            if symbol == "SUSP01":  # 停牌标的：空返回
                return {"symbol": symbol, "market": market, "success": True,
                        "rows": 0, "coverage_status": "no_data"}
            return {"symbol": symbol, "market": market, "success": True, "rows": 1}

        monkeypatch.setattr(mds, "fetch_and_store_security_price_history", fetch)
        r1 = mds.fetch_and_store_security_price_history_incremental(
            db, symbol="SUSP01", market="A股",
            start_date=tuesday - timedelta(days=1), end_date=wednesday,
        )
        r2 = mds.fetch_and_store_security_price_history_incremental(
            db, symbol="LIVE01", market="A股",
            start_date=tuesday - timedelta(days=1), end_date=wednesday,
        )
        assert fetched == ["SUSP01", "LIVE01"]  # 第二只必须仍然抓取
        assert r1["success"] and r2["success"]
    finally:
        for sym in ("SUSP01", "LIVE01"):
            db.query(SecurityPrice).filter_by(symbol=sym).delete()
        db.commit()
        mds._trade_cal_cache.clear()
        db.close()


def test_dual_gate_release_spacing_under_concurrency(monkeypatch, reset_rate_gate):
    """检视意见回归：并发 hk_daily + 非 HK API 的**实际放行**间隔必须
    ≥ 全局间隔——时间戳在真正放行时才更新，长 per-API 等待期间不得让
    另一线程凭"全局间隔已过"同时放行。"""
    import threading

    monkeypatch.setattr(
        stock_price_service.settings, "tushare_global_min_interval_seconds", 0.15
    )
    monkeypatch.setenv("TUSHARE_HK_MIN_INTERVAL_SECONDS", "0.4")

    releases = {}

    def call(api):
        wait_for_tushare_rate_limit(api)
        releases[api] = time.monotonic()

    # 先为 hk_daily 记一次时间戳，使后续 hk 调用进入长 per-API 等待
    wait_for_tushare_rate_limit("hk_daily")
    t1 = threading.Thread(target=call, args=("hk_daily",))
    t2 = threading.Thread(target=call, args=("daily",))
    t1.start()
    time.sleep(0.02)  # 确保 hk 线程先进入临界区
    t2.start()
    t1.join()
    t2.join()

    assert abs(releases["hk_daily"] - releases["daily"]) >= 0.14  # ≥ 全局间隔（留余量）


def test_incremental_empty_cache_today_only_window_does_not_crash():
    """空缓存 + 区间仅覆盖今天：钳制后无可同步区间，须返回可空序列化的
    skipped 结果而非 NoneType.isoformat 崩溃。"""
    from app.services import market_data_service as mds

    db = SessionLocal()
    try:
        result = mds.fetch_and_store_security_price_history_incremental(
            db, symbol="EMPTY01", market="A股",
            start_date=date.today(), end_date=date.today(),
        )
        assert result["skipped"] is True
        assert result["coverage_before"]["start_date"] is None
        assert result["coverage_before"]["end_date"] is None
    finally:
        db.close()
