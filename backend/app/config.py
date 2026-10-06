from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    cors_origins: str = "http://localhost:5173,http://localhost:80,http://localhost"

    # Security settings
    secret_key: str
    # 同样 gt=0：0 分钟的令牌寿命同样会让 create_access_token 的 falsy 分支
    # 悄悄退回默认值，与配置意图相反。
    access_token_expire_minutes: int = Field(default=30, gt=0)

    # Initial user passwords (only used during fresh deployments)
    admin_initial_password: str
    demo_initial_password: str

    # Market data
    tushare_token: str = ""
    # Tushare 全局最小调用间隔（所有接口与并发线程共享；0.35s ≈ 170 次/分，
    # 低于免费档常见的每分钟配额）。设 0 关闭全局闸。
    tushare_global_min_interval_seconds: float = 0.35
    # 接口级频率错误的自适应冷却（错误驱动，正常路径零开销）：首次 65s（覆盖
    # "每分钟"滑动窗口边界），连续命中指数退避至上限。冷却中的数据集在标的
    # 档案同步时跳过并如实标注，不拖垮整次分析。
    tushare_cooldown_base_seconds: float = 65.0
    tushare_cooldown_max_seconds: float = 900.0
    # 港股行情接口（hk_daily / hk_mins）的单接口最小间隔：低积分档是「每分钟 2 次」量级，
    # 31s 刚好跨过半分钟窗口。只影响这两个接口，与上面的全局闸叠加生效
    tushare_hk_min_interval_seconds: float = Field(default=31.0, ge=0)
    # Tushare HTTPS 数据接口地址：SDK 默认走旧的明文 http 端点，这里统一改写成 HTTPS。
    # 留空同样回退到这个默认值（stock_price_service.get_tushare_api_base_url）
    tushare_api_base_url: str = "https://api.waditu.com/dataapi"
    # 年报清单缓存 TTL：清单一年只变一次，缓存把批量分析的 cninfo 外呼降为零
    report_target_plan_ttl_hours: int = 24

    # 批量标的分析（持仓页一键分析）
    # 新鲜度窗口内已分析过的标的直接跳过（基本面季更、摘要永久缓存，一天内
    # 重复分析几乎必然产出同样结论）；force=true 可绕过
    security_analysis_freshness_hours: int = 24
    # 标的之间的固定停顿：相对每只 1.5+ 分钟可忽略，是给各源限速闸的安全阀
    security_analysis_batch_pause_seconds: float = 5.0
    # 批量任务的墙钟上限（心跳护栏）：超过即停止续租，交还 stale 回收
    security_analysis_batch_max_seconds: float = 4 * 3600

    # SEC EDGAR（美股基本面/10-K）：合规要求 UA 携带联系方式
    edgar_user_agent: str = ""

    # Tiingo（api.tiingo.com，免费档）：美股报价与日线。报价链 Tushare → Tiingo → 雪球，
    # 日线链 Tushare → Tiingo EOD → 腾讯 K 线。留空 = 关闭该数据源（显式降级，直接走下一源）。
    # 免费档额度约 50 次/小时、1000 次/天、每月 500 个不同 ticker：进程级最小间隔之外，
    # 撞 429 后进程内冷却一段时间不再外呼（tiingo_source.RATE_LIMIT_COOLDOWN_SECONDS）
    tiingo_api_token: str = ""
    tiingo_min_interval_seconds: float = Field(default=1.0, ge=0)
    tiingo_timeout_seconds: float = Field(default=15.0, gt=0)

    # 雪球数据源（stock.xueqiu.com）：免签名，但需登录态 Cookie，且不自动刷新。
    # 二选一，XUEQIU_COOKIES 优先；两者都空 = 关闭该数据源（所有入口显式降级）。
    #
    # 生产建议用 COOKIE_FILE 且存浏览器插件的完整导出：探活脚本要读 expirationDate
    # 才能在 xq_a_token（约 15 天寿命）过期**前**告警，{name: value} 形状没有这个
    # 字段，只能等接口开始报错才发现。
    xueqiu_cookies: str = ""
    xueqiu_cookie_file: str = ""
    # 上游限速：库内部按 [min, max] 随机间隔串行发请求，勿调低
    xueqiu_min_delay_seconds: float = 2.0
    xueqiu_max_delay_seconds: float = 4.0
    xueqiu_timeout_seconds: float = 20.0
    # Cookie 到期告警阈值（天），供 scripts/check_xueqiu_cookie_expiry.py 使用
    xueqiu_cookie_warn_days: float = 7.0
    xueqiu_cookie_critical_days: float = 3.0

    # 雪球观点摘要（数据来自本仓采集器 xueqiu_collector 写入的
    # xueqiu_archiver_utterances 表）
    xueqiu_opinion_recent_days: int = 30  # 近期窗口：转多/转空判断的分界
    xueqiu_opinion_lookback_days: int = 180  # 喂给 LLM 的总回看深度
    # 采集停摆预警阈值：最近一次成功的 scan_runs（无记录时退回 max(last_seen_at)）
    # 超过该时长即告警。采集器每小时一轮，48h = 容忍 WAF 冷却、Cookie 更换这类
    # 数小时级中断后再报
    xueqiu_opinion_stale_hours: int = 48

    # 雪球发言采集器（`manage.py xueqiu-collector`，compose 独立服务 xueqiu-collector）。
    # 与 stock.xueqiu.com 行情客户端分开：站点（xueqiu.com 需 md5__1038 签名）与频率
    # 都不同，只共用同一份 Cookie（XUEQIU_COOKIES / XUEQIU_COOKIE_FILE）。
    # 默认关闭：关闭时进程只空转写心跳（切换当天再打开）。
    xueqiu_collector_enabled: bool = False
    # Uptime Kuma push URL（可选）：每轮结束推送 up/down
    xueqiu_collector_push_url: str = ""
    # 每轮作者采集的间隔（分钟）；管理员可在观点页「立即运行」提前触发
    xueqiu_collector_cycle_minutes: int = 60
    # 全局礼貌限速：相邻两次请求之间随机停顿 [min, max] 秒，勿调低
    xueqiu_collector_min_delay_seconds: float = 10.0
    xueqiu_collector_max_delay_seconds: float = 25.0
    xueqiu_collector_timeout_seconds: float = 20.0
    # 每轮最多采集的作者数；作者之间随机间隔 [gap_min, gap_max] 秒
    xueqiu_collector_max_authors_per_run: int = 5
    xueqiu_collector_author_gap_min_seconds: int = 180
    xueqiu_collector_author_gap_max_seconds: int = 600
    # 回看窗口（天）：主页发言与评论区命中回复都只收这段时间内的
    xueqiu_collector_monitor_days: int = 30
    # 主页时间线页数上限；0 = 翻到早于回看窗口为止
    xueqiu_collector_profile_pages: int = 0
    # 每帖最多翻几页评论（每页 20 条）；超过 stale_post_days 的旧帖只翻 stale_comment_pages 页
    xueqiu_collector_max_comment_pages: int = 6
    xueqiu_collector_stale_post_days: int = 30
    xueqiu_collector_stale_comment_pages: int = 1
    # 同一帖在该时长内扫过评论即跳过
    xueqiu_collector_rescan_cooldown_hours: float = 12.0
    # 阿里云 WAF 挑战页：命中 max_waf_hits 次即中止本轮，冷却期内不开新一轮
    xueqiu_collector_waf_cooldown_seconds: int = 1800
    xueqiu_collector_max_waf_hits: int = 1
    # 心跳文件（healthcheck 用 mtime 判活），相对路径以进程工作目录为基准
    xueqiu_collector_heartbeat_file: str = "logs/xueqiu-collector.heartbeat"
    xueqiu_collector_health_max_age_minutes: int = 30
    # 每日按标的采集（公告/讨论 + 组合调仓；市场热帖 2026-09-28 下线）：同一进程、同一把 advisory lock、
    # 同一 WAF 冷却。标的范围 = 全体用户持仓∪自选 ∩ OPINION_MARKETS − 排除/现金管理规则。
    # 业务时区每天 run_after 之后跑一轮（落 state 表，重启不重跑）
    xueqiu_collector_symbols_enabled: bool = True
    xueqiu_collector_symbols_run_after: str = "07:30"
    # 每标的每类取最新几条（原 monitor_symbols 默认 20）
    xueqiu_collector_symbol_count: int = 20
    # 一轮有失败（含错误对象/未知结构/WAF/Cookie 不可用）时当天不记「已跑」，只把没成功的
    # 项留作待重试：距上一轮 retry_minutes 后重试，当日最多 max_attempts 轮（含首轮），
    # 用尽后记当日已跑、剩余失败项明日随整轮再采
    xueqiu_collector_symbols_retry_minutes: int = 60
    xueqiu_collector_symbols_max_attempts: int = 3

    # 分红公告同步（Tushare dividend；仅 A/B 股）
    dividend_sync_lookback_days: int = 365
    dividend_sync_match_window_days: int = 30
    dividend_sync_periodic_enabled: bool = False

    # 业务时区：把 DB 里的 UTC 时间戳落成"哪一天"、以及"今天"是哪天，
    # 都以此为准（core/timeutil）。不能依赖进程系统时区——生产容器是 UTC，
    # 无参数 astimezone() 在那里是空转，东八区跨日转换不会发生。
    display_timezone: str = "Asia/Shanghai"

    # LLM channels: official → Ark → Bailian → OpenRouter; empty keys skip channels.
    llm_report_api_key: str = ""
    llm_report_base_url: str = "https://api.deepseek.com"
    llm_report_model: str = "deepseek-flash"
    llm_report_timeout_seconds: int = Field(default=120, gt=0)
    llm_fallback_budget_seconds: int = Field(default=240, gt=0)
    llm_ark_api_key: str = Field(default="", repr=False)
    llm_ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    llm_ark_model: str = "deepseek-v4-1-flash-260910"
    llm_bailian_api_key: str = Field(default="", repr=False)
    llm_bailian_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_bailian_model: str = "deepseek-v4.1-flash"
    llm_openrouter_api_key: str = Field(default="", repr=False)
    llm_openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_openrouter_model: str = "deepseek/deepseek-v4.1-flash"
    # DeepSeek 推理 token 与输出共享此配额：8192 实测被长分析报告吃穿
    # （港股分析要求额外写明数据边界，report_markdown 截断或整体为空）
    llm_report_max_output_tokens: int = 16384
    # 港股报表科目映射（report_statement_service）单独的输出额度：03900 这类大报表推理会吃穿
    # 16384（finish_reason=length、content 为空）；映射输出本身很短，额度只是给推理留余量
    statement_max_output_tokens: int = 32768
    # 标的分析（security_analysis_jobs，单只与批量共用）单独的输出额度：港股输入含十年 PDF
    # 报表行 + 摘要，JSON 结构化产物 + 全文报告在 16384 里被截断（00799，finish_reason=length、
    # content 是半截 JSON）
    security_analysis_max_output_tokens: int = 32768

    # Security settings
    #
    # 默认 fail-closed：忘记配 env 的那次部署才是最需要保护的一次。compose 里
    # 本来就写着 false/true，反转默认只是把"靠 compose 记得写"变成"靠代码保证"。
    # 开发与 E2E 显式放宽（DEVELOPMENT.md、playwright.config.ts 均已写明）。
    enable_docs: bool = False  # 开发环境用 ENABLE_DOCS=true 打开 /docs
    require_https: bool = True  # 开发环境用 REQUIRE_HTTPS=false 允许明文登录
    # 是否采信反代请求头（X-Forwarded-Proto 判 HTTPS、X-Forwarded-For 取审计
    # 用的客户端 IP）。二者都是客户端可任意伪造的头，只有在反代确实会覆写/
    # 追加它们、且后端端口不直接暴露时才可信；compose 的拓扑满足这一点，故
    # 那里显式置 true。默认不信任：万一哪天 8000 被直接暴露，require_https
    # 不会被一个请求头绕过。
    #
    # 前提是 uvicorn 自己**不**处理这些头（启动命令带 --no-proxy-headers）：
    # 它默认开着且信任 127.0.0.1，会先把 scope.scheme 改成 https，那样这个
    # 开关就成了摆设。tests/test_proxy_header_trust.py 用真实 uvicorn 守着。
    trust_proxy_headers: bool = False
    # 会话自首次登录起的绝对上限（小时）。滑动续期共享同一个 jti、每次都把
    # expires_at 往后推，没有这个上限的话被窃 cookie 可以无限续命。
    #
    # gt=0 是必需的，不是防呆：配成 0 时登录仍返回 200，但会话行签发即过期；
    # 而零 timedelta 在 create_access_token 里是 falsy，会退回默认的 30 分钟，
    # 于是客户端拿到一张"看着有效"的 token，下一次 /me 立刻 401——最难查的
    # 那种坏法。让它在启动时直接失败。
    session_absolute_max_hours: int = Field(default=168, gt=0)  # 7 天
    # 港交所《每日行情报表》：港股收盘价的官方 T+1 源（免 token/Cookie）。
    # 周期同步只推进已跟踪标的（持仓∪自选）的尾部；站点存档约一个月，
    # 深历史仍走用户触发的 history-sync。每 tick 最多下载 N 份（25MB/份）
    hkex_dayquot_sync_enabled: bool = True
    hkex_dayquot_lookback_days: int = 10
    hkex_dayquot_max_reports_per_tick: int = 5
    # 汇率（#200）：人民币汇率中间价（中国货币网）为主源，第三方只比对。某币种最近一期
    # 中间价超过 N 天（覆盖国庆长假）才降级写入第三方报价；比对差异超过阈值（%）告警。
    # 阈值取 2：境内人民币即期可在中间价上下 2% 内波动，第三方（欧洲央行参考价）贴近市场价，
    # 与中间价差 0.5% 上下是常态（2026-09 上线实测 USD −0.53%、HKD −0.55%），只有超出
    # 波动区间才像数据错误（币种错配、陈旧值、单位错）
    fx_official_max_stale_days: int = 10
    fx_check_warn_pct: float = 2.0
    # 无风险利率（#200）：参考利率日序列的周期同步开关；夏普/索提诺默认使用的序列
    # （SHIBOR_3M = 本币 CNY；UST_3M 只展示）。请求显式传 risk_free_rate 时仍按常量计算
    reference_rate_sync_enabled: bool = True
    risk_free_series: str = "SHIBOR_3M"
    # 标的全集（security_catalog）：Tushare 三张基础表 + 港交所證券名單，每周刷新
    # （6h tick 按 last_success_at 判新鲜，重启不重拉）；manage.py sync-security-catalog 手动
    security_catalog_sync_enabled: bool = True
    security_catalog_sync_interval_hours: int = 168
    # 行业分类（security_industries）：官方为主（A股 Tushare stock_basic / 美股 EDGAR SIC）+
    # 东方财富 F10 补缺（港股、B股、官方缺失的 A股/美股）；24h tick，按 fetched_at 判新鲜，
    # 超过刷新天数才重拉；manage.py sync-security-industries [--force] 手动
    security_industry_sync_enabled: bool = True
    security_industry_refresh_days: int = Field(default=30, gt=0)
    # 告警通知（services/notification_service.py + alert_service.py）。推送走 Apprise：
    # NOTIFY_URLS 空格或逗号分隔多个渠道；Bark 直接填 App 里复制的
    # `https://api.day.app/<key>`（自动换成 Apprise 的 barks://），其他渠道（飞书、邮件…）
    # 填 Apprise URL 即可，不改代码。留空 = 不推送（告警仍记录，管理员页可见）。
    notify_urls: str = ""
    # 推送门槛：低于它的告警只记录不推送（info / warning / critical）
    notify_min_severity: str = "warning"
    # 仍未恢复的告警每隔 N 小时再提醒一次
    notify_reminder_hours: float = Field(default=24, gt=0)
    # 采集器启用时，超过 N 小时没有一次成功（ok/partial）的作者采集即告警
    notify_collector_stale_hours: float = Field(default=3, gt=0)
    # 告警检查周期任务（每 10 分钟）总开关
    alert_check_enabled: bool = True
    # 数据自动刷新（DEPLOYMENT.md「自动刷新一览」）：
    # 交易时段每 15 分钟刷新持仓与自选的实时价（按市场交易时段判定，收盘后再补一次）
    quote_auto_refresh_enabled: bool = True
    # A股/B股/美股日线尾部每小时检查、落后才补（港股由港交所日报负责）
    price_tail_sync_enabled: bool = True
    # A股估值快照（Tushare daily_basic）每个交易日收盘后一次（一次调用取全市场，只存跟踪标的）
    daily_basic_refresh_enabled: bool = True
    # 每周数据刷新：档案（非 LLM）+ 最新一期财报摘要/港股报表 + 观点摘要，凌晨入队后台任务
    weekly_data_refresh_enabled: bool = True
    # 事件提醒（新分红建议、除净日临近、持仓价格异动）走同一个 NOTIFY_URLS
    event_notifications_enabled: bool = True
    # 持仓单日涨跌幅（相对昨收）达到该百分比即推送，同一标的每个行情日一次
    notify_price_move_pct: float = Field(default=7, gt=0)
    # 除净日在今天起 N 天内的持仓分红提前提醒
    notify_ex_date_days_ahead: int = Field(default=3, ge=0)
    # 官方公告同步（巨潮/披露易/EDGAR，#306）：跟踪范围逐标的每 30 分钟增量拉取、分类入库
    announcement_sync_enabled: bool = True
    # 首次同步（无水位）的回溯天数
    announcement_backfill_days: int = Field(default=365, ge=1)
    # 原始报告文件缓存（巨潮/披露易 PDF、EDGAR 主文档；report_cache）：容器内的持久化挂载目录，
    # 不存在或不可写即关闭（开发/测试环境默认关闭）。解析规则升版重跑时命中本地、不再重新下载
    report_cache_dir: str = "/app/cache/reports"
    # 缓存总量上限（GB）：超过时按最近使用时间删，先删无人引用的
    report_cache_max_gb: float = Field(default=8, gt=0)
    # 无人引用（非当前跟踪标的的抽取/节选/摘要所需）的文件，超过这么多天没用即删除
    report_cache_unreferenced_days: int = Field(default=30, ge=1)
    # 重大公告推送（持仓或自选标的，走 NOTIFY_URLS；首次回溯的历史公告不推）
    announcement_notify_enabled: bool = True
    price_refresh_max_workers: int = 4
    # 主动刷新股价的新鲜度窗口：窗口内重复请求跳过（防连点浪费配额）
    price_refresh_freshness_seconds: int = 600
    background_job_retention_hours: int = 168
    background_job_stale_minutes: int = 60
    # 排队（queued）任务的放弃上限：排队不看心跳（慢车道跨用户串行，排几个小时是正常的），
    # 只防 worker 关闭时永远挂着（#272）
    background_job_queued_ttl_hours: int = 24
    background_worker_enabled: bool = True
    # 周期任务总开关：false 时 worker 只执行排队任务、不起周期调度线程（E2E 隔离外呼用；
    # 各任务自己的 *_ENABLED 开关仍然有效，且有的还管非周期路径，不能由它替代）
    periodic_tasks_enabled: bool = True
    background_job_poll_seconds: int = 5
    background_job_lease_seconds: int = 300
    background_job_max_attempts: int = 3
    background_job_retry_base_seconds: int = 30
    app_version: str = "1.0.0"
    build_sha: str = "unknown"

    def get_cors_origins_list(self) -> List[str]:
        """Convert comma-separated CORS origins to list"""
        origins = [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]
        # Production: do not use wildcard for security
        # Only add wildcard for local development
        # origins.append("*")
        return origins


settings = Settings()
