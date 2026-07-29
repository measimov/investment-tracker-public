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

    # LLM report (DeepSeek / OpenAI-compatible; empty key disables the feature)
    llm_report_api_key: str = ""
    llm_report_base_url: str = "https://api.deepseek.com"
    llm_report_model: str = "deepseek-v4-pro"
    llm_report_timeout_seconds: int = 120
    llm_report_max_output_tokens: int = 8192

    # Security settings
    enable_docs: bool = True  # Set to False in production to disable API documentation
    require_https: bool = False  # Set to True in production to reject plaintext auth requests
    price_refresh_max_workers: int = 4
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
