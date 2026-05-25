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

    # Set to true in production behind a reverse proxy (nginx, Caddy, a CDN).
    # When true, the rate limiter reads the leftmost IP from X-Forwarded-For so
    # per-user limits actually work; when false, it falls back to the immediate
    # TCP peer (correct for dev, never trust X-Forwarded-For without a proxy).
    trust_forwarded_for: bool = False

    # Stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_starter_price_id: str = ""
    stripe_pro_price_id: str = ""
    app_url: str = "http://localhost:8000"

    # App
    app_name: str = "CheckPulse"
    app_env: str = "development"

    # Email (Resend in production, stdout in dev)
    resend_api_key: str = ""
    email_from_address: str = "CheckPulse <no-reply@checkpulse.dev>"
    verification_token_ttl_hours: int = 24

    class Config:
        env_file = (".env", ".env.local")
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
