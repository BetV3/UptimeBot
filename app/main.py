from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import get_settings
from app.core.middleware import TransportMiddleware
from app.core.ratelimit import client_ip
from app.api.routes import alerts, api_keys, auth, billing, dashboard, health, incidents, internal, monitors, projects, status

settings = get_settings()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


limiter = Limiter(key_func=client_ip, default_limits=["60/minute"])

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

app.add_middleware(TransportMiddleware)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Static assets (favicon set, web manifest). Mounted rather than served by
# individual routes so adding an asset doesn't require a code change.
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/favicon.ico", include_in_schema=False)
@limiter.exempt
async def favicon():
    """Serve the icon from the well-known root path.

    Browsers request /favicon.ico directly — before, and independently of, any
    <link> tag — so without this route every page load logs a 404.
    """
    return FileResponse(
        STATIC_DIR / "favicon.ico",
        media_type="image/x-icon",
        headers={"Cache-Control": "public, max-age=604800"},
    )


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


# Bare paths people type, paste into Slack, or send to a prospect. The real
# content lives at an anchor or under /legal, so redirect rather than 404.
@app.get("/pricing", include_in_schema=False)
@limiter.exempt
async def pricing_redirect():
    return RedirectResponse("/#pricing", status_code=308)


@app.get("/privacy", include_in_schema=False)
@limiter.exempt
async def privacy_redirect():
    return RedirectResponse("/legal/privacy", status_code=308)


@app.get("/terms", include_in_schema=False)
@limiter.exempt
async def terms_redirect():
    return RedirectResponse("/legal/terms", status_code=308)


@app.get("/features", include_in_schema=False)
@limiter.exempt
async def features_redirect():
    return RedirectResponse("/#features", status_code=308)


@app.get("/docs", include_in_schema=False)
@limiter.exempt
async def docs_redirect():
    return RedirectResponse("/docs/getting-started", status_code=308)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Render 404/403/500 as a page for browsers, JSON for API clients.

    Previously every miss returned a bare {"detail": "Not Found"} with no
    navigation — including on /status/<slug>, which is a public, client-facing
    URL an agency shares with its own customers.
    """
    wants_html = "text/html" in request.headers.get("accept", "")
    is_api = request.url.path.startswith(("/api", "/internal", "/auth"))
    if exc.status_code >= 400 and wants_html and not is_api:
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "status_code": exc.status_code,
                "detail": exc.detail if isinstance(exc.detail, str) else "",
            },
            status_code=exc.status_code,
        )
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


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
