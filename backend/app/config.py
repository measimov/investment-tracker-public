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

    # Security settings
    enable_docs: bool = True  # Set to False in production to disable API documentation
    require_https: bool = False  # Set to True in production to reject plaintext auth requests
    price_refresh_max_workers: int = 4

    def get_cors_origins_list(self) -> List[str]:
        """Convert comma-separated CORS origins to list"""
        origins = [origin.strip() for origin in self.cors_origins.split(",")]
        # Production: do not use wildcard for security
        # Only add wildcard for local development
        # origins.append("*")
        return origins


settings = Settings()
