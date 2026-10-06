"""周期任务的唯一注册处（#273）。

此前 12 个任务在 main.py 里夹着 `# noqa: E402` 注册，另有 2 个靠 dividend_sync_jobs /
llm_report_jobs 的 import 副作用注册——哪个模块先被导入决定了注册是否发生，新增任务时也
没有一处能看全。这里显式列出全部任务、间隔与所属调度分组；`register_all()` 幂等，由
main.py 在创建应用时调用一次，`tests/test_periodic_outcomes.py` 断言注册集合。

分组 = 调度线程：告警检查与行情刷新各自独占一条线程，不排在慢任务后面（目录/行业同步、
港交所日报下载单次可达分钟级）；其余在默认组串行。新增周期任务：入口必须是
`@periodic_outcome_task`，加在下面的 `PERIODIC_TASKS` 里并声明分组。
"""

from __future__ import annotations

from typing import Callable, List, Tuple

from .job_worker import ALERTS_GROUP, DEFAULT_GROUP, QUOTES_GROUP, register_periodic_task


def _tasks() -> List[Tuple[str, Callable, float, str]]:
    """(告警键名, 入口, 间隔秒, 分组)。在函数里 import：避免服务模块在应用导入早期互相牵连。"""
    from .alert_checks import PERIODIC_INTERVAL_SECONDS as ALERT_SECONDS
    from .alert_checks import periodic_run_alert_checks
    from .announcement_sync import PERIODIC_INTERVAL_SECONDS as ANNOUNCEMENT_SECONDS
    from .announcement_sync import periodic_sync_announcements
    from .benchmark_service import PERIODIC_INTERVAL_SECONDS as BENCHMARK_SECONDS
    from .benchmark_service import periodic_refresh_benchmark_tails
    from .dividend_sync_jobs import PERIODIC_INTERVAL_SECONDS as DIVIDEND_SECONDS
    from .dividend_sync_jobs import periodic_enqueue_dividend_sync
    from .event_notifications import PERIODIC_INTERVAL_SECONDS as EVENT_SECONDS
    from .event_notifications import periodic_event_notifications
    from .exchange_rate_service import periodic_refresh_rates
    from .hkex_dayquot_source import PERIODIC_INTERVAL_SECONDS as DAYQUOT_SECONDS
    from .hkex_dayquot_source import periodic_refresh_hk_dayquot
    from .llm_report_scheduler import periodic_enqueue_scheduled_reports
    from .price_refresh_jobs import PERIODIC_INTERVAL_SECONDS as QUOTE_SECONDS
    from .price_refresh_jobs import periodic_refresh_quotes
    from .price_tail_sync import PERIODIC_INTERVAL_SECONDS as TAIL_SECONDS
    from .price_tail_sync import periodic_refresh_daily_basic, periodic_sync_price_tails
    from .reference_rate_service import PERIODIC_INTERVAL_SECONDS as REFERENCE_SECONDS
    from .report_cache import PERIODIC_INTERVAL_SECONDS as REPORT_CACHE_SECONDS
    from .report_cache import periodic_prune_report_cache
    from .reference_rate_service import periodic_refresh_reference_rates
    from .security_catalog_service import PERIODIC_INTERVAL_SECONDS as CATALOG_SECONDS
    from .security_catalog_service import periodic_refresh_security_catalog
    from .security_industry_service import PERIODIC_INTERVAL_SECONDS as INDUSTRY_SECONDS
    from .security_industry_service import periodic_refresh_security_industries
    from .weekly_data_refresh import PERIODIC_INTERVAL_SECONDS as WEEKLY_SECONDS
    from .weekly_data_refresh import periodic_enqueue_weekly_data_refresh

    return [
        # 系统告警检查（采集器/Cookie/汇率/周期任务/后台任务/调度线程）：独占线程，
        # 其他任务卡住时它仍能按时跑、并把停摆报出来
        ("run_alert_checks", periodic_run_alert_checks, ALERT_SECONDS, ALERTS_GROUP),
        # 交易时段实时价：独占线程，保证收盘后那次刷新落在窗口内
        ("refresh_quotes", periodic_refresh_quotes, QUOTE_SECONDS, QUOTES_GROUP),
        # 汇率每日快照：6 小时检查一次，最近一期中间价已有则零外呼
        ("refresh_rates_if_stale", periodic_refresh_rates, 6 * 3600, DEFAULT_GROUP),
        # 参考利率：首次按最早交易日回填，之后每 12 小时补尾
        (
            "refresh_reference_rates",
            periodic_refresh_reference_rates,
            REFERENCE_SECONDS,
            DEFAULT_GROUP,
        ),
        # 基准指数尾部补齐（冷启动回填由用户区间驱动）
        (
            "refresh_benchmark_tails",
            periodic_refresh_benchmark_tails,
            BENCHMARK_SECONDS,
            DEFAULT_GROUP,
        ),
        # 港交所每日行情报表：港股收盘价的官方 T+1 源
        ("refresh_hk_dayquot", periodic_refresh_hk_dayquot, DAYQUOT_SECONDS, DEFAULT_GROUP),
        # 标的全集：tick 6h，按 last_success_at 判新鲜
        (
            "refresh_security_catalog",
            periodic_refresh_security_catalog,
            CATALOG_SECONDS,
            DEFAULT_GROUP,
        ),
        # 行业分类：24h 一查，按 fetched_at 判新鲜
        (
            "refresh_security_industries",
            periodic_refresh_security_industries,
            INDUSTRY_SECONDS,
            DEFAULT_GROUP,
        ),
        # 事件提醒（新分红建议、除净日临近）：每 10 分钟
        ("send_event_notifications", periodic_event_notifications, EVENT_SECONDS, DEFAULT_GROUP),
        # 每周数据刷新：1 小时 tick，凌晨窗口内按 scheduled_task_state 判每用户是否到期
        (
            "enqueue_weekly_data_refresh",
            periodic_enqueue_weekly_data_refresh,
            WEEKLY_SECONDS,
            DEFAULT_GROUP,
        ),
        # 跟踪标的的日线尾部 + A 股估值快照：每小时 tick，同一交易日不重复外呼
        ("sync_price_tails", periodic_sync_price_tails, TAIL_SECONDS, DEFAULT_GROUP),
        ("refresh_daily_basic", periodic_refresh_daily_basic, TAIL_SECONDS, DEFAULT_GROUP),
        # 分红公告同步入队（默认关闭）：按 scheduled_task_state 24 小时一次
        (
            "enqueue_periodic_dividend_sync",
            periodic_enqueue_dividend_sync,
            DIVIDEND_SECONDS,
            DEFAULT_GROUP,
        ),
        # 定期 LLM 复盘入队：小时级检查到期与失败退避
        ("enqueue_due_scheduled_reports", periodic_enqueue_scheduled_reports, 3600, DEFAULT_GROUP),
        # 官方公告（巨潮/披露易/EDGAR）：跟踪标的每 30 分钟增量，水位按标的记在
        # scheduled_task_state；单 tick 首次回溯最多 5 只
        ("sync_announcements", periodic_sync_announcements, ANNOUNCEMENT_SECONDS, DEFAULT_GROUP),
        # 原始报告文件缓存的生命周期清理：每日一次（无人引用超期 + 总量上限）
        ("prune_report_cache", periodic_prune_report_cache, REPORT_CACHE_SECONDS, DEFAULT_GROUP),
    ]


def register_all() -> List[str]:
    """注册全部周期任务（幂等：同名重复注册是 no-op），返回注册的任务名。"""
    names = []
    for name, entry, interval_seconds, group in _tasks():
        register_periodic_task(entry, interval_seconds, name=name, group=group)
        names.append(name)
    return names
