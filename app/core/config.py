from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql+asyncpg://uptimebot:localdev@db:5432/uptimebot"
    database_url_sync: str = "postgresql://uptimebot:localdev@db:5432/uptimebot"

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # Auth
    secret_key: str = "change-me-in-production"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    algorithm: str = "HS256"

    # Workers
    worker_secret: str = "change-me-worker-secret"

    # App
    app_name: str = "UptimeBot"
    app_env: str = "development"

    class Config:
        env_file = ".env"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
