from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from sqlalchemy import select, update, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.models.models import Check, CheckRegion, CheckStatus, Incident, Monitor, MonitorStatus, PendingCheck, WorkerHeartbeat
from app.schemas.internal import CheckResultsBatch
from app.services.alerts import dispatch_alerts

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
            "type": monitor.type.value,
            "url": monitor.url,
            "method": monitor.method.value,
            "expected_status": monitor.expected_status,
            "timeout_seconds": monitor.timeout_seconds,
            "headers": monitor.headers,
            "body": monitor.body,
            "target_host": monitor.target_host,
            "target_port": monitor.target_port,
            "warn_days_before_expiry": monitor.warn_days_before_expiry,
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
            cert_days_remaining=r.cert_days_remaining,
            cert_subject=r.cert_subject,
            cert_issuer=r.cert_issuer,
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
    Also handles incident detection and alert dispatch on transitions.
    """
    # Get current monitor state
    monitor_result = await db.execute(
        select(Monitor).where(Monitor.id == monitor_id)
    )
    monitor = monitor_result.scalar_one_or_none()
    if not monitor:
        return

    old_status = monitor.current_status

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
    monitor.current_status = new_status

    # Detect status transitions and manage incidents
    now = datetime.now(timezone.utc)

    if old_status != MonitorStatus.DOWN and new_status == MonitorStatus.DOWN:
        # UP/UNKNOWN -> DOWN: create incident
        incident = Incident(monitor_id=monitor.id, started_at=now)
        db.add(incident)
        await db.flush()
        await dispatch_alerts(monitor, incident, "down", db)

    elif old_status == MonitorStatus.DOWN and new_status == MonitorStatus.UP:
        # DOWN -> UP: resolve open incident
        result = await db.execute(
            select(Incident)
            .where(
                Incident.monitor_id == monitor.id,
                Incident.resolved_at.is_(None),
            )
            .order_by(Incident.started_at.desc())
            .limit(1)
        )
        incident = result.scalar_one_or_none()
        if incident:
            incident.resolved_at = now
            await dispatch_alerts(monitor, incident, "resolved", db)


@router.post("/heartbeat", dependencies=[Depends(verify_worker_secret)])
async def worker_heartbeat(
    region: CheckRegion = Query(...),
    hostname: str = Query(default=""),
    version: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
):
    """Workers call this periodically to report they're alive."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(WorkerHeartbeat).where(WorkerHeartbeat.region == region)
    )
    hb = result.scalar_one_or_none()
    if hb:
        hb.last_seen = now
        hb.hostname = hostname or hb.hostname
        hb.version = version or hb.version
    else:
        hb = WorkerHeartbeat(region=region, hostname=hostname, version=version, last_seen=now)
        db.add(hb)
    await db.commit()
    return {"status": "ok"}


@router.get("/workers", dependencies=[Depends(verify_worker_secret)])
async def list_workers(db: AsyncSession = Depends(get_db)):
    """Return status of all known workers."""
    result = await db.execute(select(WorkerHeartbeat))
    workers = result.scalars().all()
    now = datetime.now(timezone.utc)
    return {
        "workers": [
            {
                "region": w.region.value,
                "hostname": w.hostname,
                "version": w.version,
                "last_seen": w.last_seen.isoformat(),
                "stale": (now - w.last_seen).total_seconds() > 120,
            }
            for w in workers
        ]
    }
