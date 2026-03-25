from fastapi import FastAPI
from app.core.config import get_settings
from app.api.routes import auth, health, internal, monitors, projects

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# --- Routes ---
app.include_router(health.router, tags=["Health"])
app.include_router(auth.router, prefix="/auth", tags=["Auth"])
app.include_router(projects.router, prefix="/projects", tags=["Projects"])
app.include_router(monitors.router, tags=["Monitors"])
app.include_router(internal.router, prefix="/internal", tags=["Internal"])

# Future route includes:
# app.include_router(alerts.router, prefix="/alerts", tags=["Alerts"])
# app.include_router(status.router, prefix="/status", tags=["Status Page"])
