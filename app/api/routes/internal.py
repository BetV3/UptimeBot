from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from sqlalchemy import select, update, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.models.models import Check, CheckRegion, CheckStatus, Monitor, MonitorStatus, PendingCheck
from app.schemas.internal import CheckResultsBatch

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


@router.post("/results", dependencies=[Depends(verify_worker_secret)])
async def post_results(
    body: CheckResultsBatch,
    db: AsyncSession = Depends(get_db),
):
    if not body.results:
        return {"saved": 0}

    # Save check results
    for r in body.results:
        check = Check(
            monitor_id=r.monitor_id,
            region=CheckRegion(r.region.lower()),
            status=CheckStatus(r.status.lower()),
            status_code=r.status_code,
            response_time_ms=r.response_time_ms,
            error=r.error,
        )
        db.add(check)

    # Delete the claimed pending checks
    pending_ids = [r.pending_check_id for r in body.results]
    await db.execute(
        update(PendingCheck)
        .where(PendingCheck.id.in_(pending_ids))
        .values(claimed_at=datetime.now(timezone.utc))
    )

    await db.flush()

    # Update monitor statuses using consensus logic
    # Group results by monitor to determine status
    monitor_ids = list({r.monitor_id for r in body.results})
    for monitor_id in monitor_ids:
        await _update_monitor_status(monitor_id, db)

    await db.commit()
    return {"saved": len(body.results)}


async def _update_monitor_status(monitor_id: str, db: AsyncSession):
    """Update monitor status based on the most recent check round.

    Consensus logic: look at the latest check from each region.
    If 2+ regions report DOWN, the monitor is DOWN. Otherwise UP.
    """
    # Get the most recent check per region for this monitor
    regions = [CheckRegion.US, CheckRegion.EU, CheckRegion.ASIA]
    down_count = 0
    total_checked = 0

    for region in regions:
        result = await db.execute(
            select(Check.status)
            .where(Check.monitor_id == monitor_id, Check.region == region)
            .order_by(Check.checked_at.desc())
            .limit(1)
        )
        latest = result.scalar_one_or_none()
        if latest is not None:
            total_checked += 1
            if latest == CheckStatus.DOWN:
                down_count += 1

    if total_checked == 0:
        return

    new_status = MonitorStatus.DOWN if down_count >= 2 else MonitorStatus.UP
    await db.execute(
        update(Monitor)
        .where(Monitor.id == monitor_id)
        .values(current_status=new_status)
    )
