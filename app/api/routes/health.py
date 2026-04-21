import time

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from redis import Redis

from app.core.database import get_db
from app.core.config import get_settings

router = APIRouter()
settings = get_settings()

# Must match app/workers/tasks.py.
BEAT_HEARTBEAT_KEY = "checkpulse:beat:last_seen"
# Beat runs schedule_pending_checks every 15s; 90s gives ~6 missed ticks
# before we alert. Keep this well under the Redis TTL (300s) so a stuck
# beat shows up before the key silently expires.
BEAT_HEARTBEAT_MAX_AGE_SECONDS = 90


@router.get("/health")
async def health_check(db: AsyncSession = Depends(get_db)):
    # Check database
    db_ok = False
    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass

    # Check Redis
    redis_ok = False
    try:
        r = Redis.from_url(settings.redis_url)
        r.ping()
        redis_ok = True
    except Exception:
        pass

    status_label = "healthy" if (db_ok and redis_ok) else "degraded"

    return {
        "status": status_label,
        "checks": {
            "database": "ok" if db_ok else "error",
            "redis": "ok" if redis_ok else "error",
        },
    }


@router.get("/healthz/beat")
async def beat_liveness():
    """Return 200 if the Celery beat scheduler has run schedule_pending_checks
    recently. External uptime monitors should hit this every minute and alert
    on 503 — it catches a stuck beat process that would otherwise silently
    stop scheduling checks.
    """
    try:
        r = Redis.from_url(settings.redis_url)
        raw = r.get(BEAT_HEARTBEAT_KEY)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"redis unreachable: {e}")

    if raw is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="beat heartbeat missing")

    last_seen = int(raw)
    age = int(time.time()) - last_seen
    if age > BEAT_HEARTBEAT_MAX_AGE_SECONDS:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=f"beat heartbeat stale ({age}s old)")

    return {"status": "ok", "last_seen": last_seen, "age_seconds": age}
