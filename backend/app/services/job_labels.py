"""后台任务类型与周期任务的中文名（#273）：告警推送与互斥提示共用这一份。

此前推送标题直接用内部标识（「周期任务 enqueue_periodic_dividend_sync 连续失败」「后台任务
report_digest_batch 失败」），家人在锁屏上看不懂；中文名只存在于批量分析的一个局部字典里。
`tests/test_periodic_outcomes.py` 断言每个注册的 job_type 与周期任务都有中文名。
"""

JOB_TYPE_LABELS = {
    "price_refresh": "刷新股价",
    "performance_history_sync": "同步历史行情",
    "dividend_sync": "同步分红公告",
    "llm_report": "AI 复盘报告",
    "security_analysis": "标的分析",
    "security_analysis_batch": "批量分析",
    "report_digest_backfill": "财报摘要回填",
    "report_digest_batch": "批量财报摘要回填",
    "opinion_summary": "观点摘要",
    "opinion_summary_batch": "批量观点摘要",
}

PERIODIC_TASK_LABELS = {
    "run_alert_checks": "系统告警检查",
    "refresh_quotes": "交易时段实时价刷新",
    "refresh_rates_if_stale": "汇率刷新",
    "refresh_reference_rates": "参考利率同步",
    "refresh_benchmark_tails": "基准指数补尾",
    "refresh_hk_dayquot": "港交所日报同步",
    "refresh_security_catalog": "标的全集同步",
    "refresh_security_industries": "行业分类同步",
    "send_event_notifications": "事件提醒推送",
    "enqueue_weekly_data_refresh": "每周数据刷新",
    "sync_price_tails": "日线尾部同步",
    "refresh_daily_basic": "A股估值快照",
    "enqueue_periodic_dividend_sync": "分红公告定期同步",
    "enqueue_due_scheduled_reports": "定期 AI 复盘",
    "sync_announcements": "官方公告同步",
}

SCHEDULER_GROUP_LABELS = {
    "default": "常规",
    "alerts": "告警检查",
    "quotes": "实时价",
}


def job_type_label(job_type: str) -> str:
    return JOB_TYPE_LABELS.get(job_type, job_type)


def periodic_task_label(name: str) -> str:
    return PERIODIC_TASK_LABELS.get(name, name)


def scheduler_group_label(group: str) -> str:
    return SCHEDULER_GROUP_LABELS.get(group, group)
