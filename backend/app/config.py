from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    cors_origins: str = "http://localhost:5173,http://localhost:80,http://localhost"

    # Security settings
    secret_key: str
    access_token_expire_minutes: int = 30

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

    # 分红公告同步（Tushare dividend；仅 A/B 股）
    dividend_sync_lookback_days: int = 365
    dividend_sync_match_window_days: int = 30
    dividend_sync_periodic_enabled: bool = False

    # LLM report (DeepSeek / OpenAI-compatible; empty key disables the feature)
    llm_report_api_key: str = ""
    llm_report_base_url: str = "https://api.deepseek.com"
    llm_report_model: str = "deepseek-v4-pro"
    llm_report_timeout_seconds: int = 120
    # DeepSeek 推理 token 与输出共享此配额：8192 实测被长分析报告吃穿
    # （港股分析要求额外写明数据边界，report_markdown 截断或整体为空）
    llm_report_max_output_tokens: int = 16384

    # Security settings
    enable_docs: bool = True  # Set to False in production to disable API documentation
    require_https: bool = False  # Set to True in production to reject plaintext auth requests
    price_refresh_max_workers: int = 4
    # 主动刷新股价的新鲜度窗口：窗口内重复请求跳过（防连点浪费配额）
    price_refresh_freshness_seconds: int = 600
    background_job_retention_hours: int = 168
    background_job_stale_minutes: int = 60
    background_worker_enabled: bool = True
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
