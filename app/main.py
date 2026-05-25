from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.core.config import get_settings
from app.core.middleware import TransportMiddleware
from app.api.routes import alerts, api_keys, auth, billing, dashboard, health, incidents, internal, monitors, projects, status

settings = get_settings()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


def _client_ip(request) -> str:
    """Resolve the rate-limit key. Behind a reverse proxy (trust_forwarded_for
    enabled), take the leftmost IP from X-Forwarded-For — that's the original
    client. Without the flag, fall back to the immediate TCP peer; trusting
    a spoofable header without a proxy would let attackers bypass limits."""
    if settings.trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=_client_ip, default_limits=["60/minute"])

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

app.add_middleware(TransportMiddleware)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.get("/", response_class=HTMLResponse)
@limiter.exempt
async def landing_page(request: Request):
    return templates.TemplateResponse("landing.html", {"request": request})


@app.get("/docs/getting-started", response_class=HTMLResponse)
@limiter.exempt
async def getting_started_page(request: Request):
    return templates.TemplateResponse("getting_started.html", {"request": request})


@app.get("/legal/privacy", response_class=HTMLResponse)
@limiter.exempt
async def privacy_page(request: Request):
    return templates.TemplateResponse(
        "legal.html",
        {"request": request, "title": "Privacy Policy", "doc": "privacy"},
    )


@app.get("/legal/terms", response_class=HTMLResponse)
@limiter.exempt
async def terms_page(request: Request):
    return templates.TemplateResponse(
        "legal.html",
        {"request": request, "title": "Terms of Service", "doc": "terms"},
    )

# --- Routes ---
app.include_router(health.router, tags=["Health"])
app.include_router(auth.router, prefix="/auth", tags=["Auth"])
app.include_router(projects.router, prefix="/projects", tags=["Projects"])
app.include_router(monitors.router, tags=["Monitors"])
app.include_router(internal.router, prefix="/internal", tags=["Internal"])
app.include_router(incidents.router, tags=["Incidents"])
app.include_router(alerts.router, tags=["Alerts"])
app.include_router(status.router, tags=["Status Page"])
app.include_router(api_keys.router, prefix="/api-keys", tags=["API Keys"])
app.include_router(billing.router, prefix="/billing", tags=["Billing"])
app.include_router(dashboard.router, prefix="/dashboard", tags=["Dashboard"])
