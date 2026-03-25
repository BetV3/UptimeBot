from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.models.models import PendingCheck, CheckRegion, Monitor

router = APIRouter()
settings = get_settings()


async def verify_worker_secret(x_worker_secret: str = Header(...)):
    if x_worker_secret != settings.worker_secret:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid worker secret")


@router.get("/jobs", dependencies=[Depends(verify_worker_secret)])
async def get_jobs(
    region: CheckRegion = Query(...),
    limit: int = Query(default=50, le=200),
    db: AsyncSession = Depends(get_db),
):
    # Fetch unclaimed pending checks for this region
    result = await db.execute(
        select(PendingCheck)
        .where(
            PendingCheck.region == region,
            PendingCheck.claimed_at.is_(None),
        )
        .order_by(PendingCheck.scheduled_at.asc())
        .limit(limit)
    )
    pending = result.scalars().all()

    if not pending:
        return {"jobs": []}

    # Mark them as claimed
    ids = [pc.id for pc in pending]
    now = datetime.now(timezone.utc)
    await db.execute(
        update(PendingCheck)
        .where(PendingCheck.id.in_(ids))
        .values(claimed_at=now)
    )
    await db.commit()

    # Build response with monitor details
    monitor_ids = list({pc.monitor_id for pc in pending})
    monitors_result = await db.execute(
        select(Monitor).where(Monitor.id.in_(monitor_ids))
    )
    monitors_map = {m.id: m for m in monitors_result.scalars().all()}

    jobs = []
    for pc in pending:
        monitor = monitors_map.get(pc.monitor_id)
        if not monitor:
            continue
        jobs.append({
            "pending_check_id": str(pc.id),
            "monitor_id": str(pc.monitor_id),
            "url": monitor.url,
            "method": monitor.method.value,
            "expected_status": monitor.expected_status,
            "timeout_seconds": monitor.timeout_seconds,
            "headers": monitor.headers,
            "body": monitor.body,
            "region": pc.region.value,
        })

    return {"jobs": jobs}
