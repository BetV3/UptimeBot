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

    # Set to true in production behind a trusted reverse proxy (Cloudflare
    # Tunnel + cloudflared, Caddy, nginx, etc.). When true, the rate
    # limiter resolves the client IP in this order: CF-Connecting-IP (set
    # by Cloudflare and overwritten by them, so authoritative when CF is
    # in the path), then the rightmost X-Forwarded-For entry (for a
    # single non-CF proxy hop), then the immediate TCP peer. When false,
    # falls back to the TCP peer unconditionally — correct for dev,
    # never trust forwarded headers without a proxy. See app/core/ratelimit.py.
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

    # Cloudflare Turnstile — proof-of-humanity on the endpoints that send
    # email. Leave both empty to disable the gate (dev/tests); setting the
    # secret key enables enforcement. See app/core/turnstile.py for why this
    # exists and why it fails closed.
    turnstile_site_key: str = ""
    turnstile_secret_key: str = ""

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
