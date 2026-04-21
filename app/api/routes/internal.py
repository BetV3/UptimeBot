from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from sqlalchemy import delete, select, update, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.models.models import Check, CheckRegion, CheckStatus, Monitor, PendingCheck, WorkerHeartbeat
from app.schemas.internal import CheckResultsBatch
from app.workers.tasks import finalize_check_result

router = APIRouter()
settings = get_settings()

# A pending_check whose lease has been taken this many times without completing
# gets reaped by the sweeper task. 3 attempts ≈ give transient failures two
# retries before giving up.
MAX_ATTEMPTS = 3

# Slack added to monitor.timeout_seconds when computing a lease's expiry. Gives
# the worker time to POST /internal/results after the check returns.
LEASE_SLACK_SECONDS = 30


async def verify_worker_secret(x_worker_secret: str = Header(...)):
    if x_worker_secret != settings.worker_secret:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid worker secret")


@router.get("/jobs", dependencies=[Depends(verify_worker_secret)])
async def get_jobs(
    region: CheckRegion = Query(...),
    limit: int = Query(default=50, le=200),
    db: AsyncSession = Depends(get_db),
    x_worker_id: str = Header(default="unknown"),
):
    # Atomically lease eligible pending_checks. A row is eligible when it is
    # not dead, under the attempt cap, and either unleased or its lease has
    # expired. FOR UPDATE SKIP LOCKED lets multiple pollers coexist without
    # stepping on each other.
    claim_sql = text(
        """
        WITH candidates AS (
            SELECT pc.id, m.timeout_seconds
            FROM pending_checks pc
            JOIN monitors m ON m.id = pc.monitor_id
            WHERE pc.region = :region
              AND pc.dead = false
              AND pc.attempts < :max_attempts
              AND (pc.leased_at IS NULL OR pc.lease_expires_at < now())
            ORDER BY pc.scheduled_at ASC
            LIMIT :limit
            FOR UPDATE OF pc SKIP LOCKED
        )
        UPDATE pending_checks pc
        SET leased_at = now(),
            lease_expires_at = now() + make_interval(secs => candidates.timeout_seconds + :slack),
            worker_id = :worker_id,
            attempts = pc.attempts + 1
        FROM candidates
        WHERE pc.id = candidates.id
        RETURNING pc.id
        """
    )
    claimed = await db.execute(
        claim_sql,
        {
            "region": region.value,
            "limit": limit,
            "max_attempts": MAX_ATTEMPTS,
            "slack": LEASE_SLACK_SECONDS,
            "worker_id": x_worker_id,
        },
    )
    claimed_ids = [row[0] for row in claimed.fetchall()]
    if not claimed_ids:
        await db.commit()
        return {"jobs": []}

    # Load the claimed pending_checks + their monitors for the response payload.
    pending_result = await db.execute(
        select(PendingCheck).where(PendingCheck.id.in_(claimed_ids))
    )
    pending = pending_result.scalars().all()

    monitor_ids = list({pc.monitor_id for pc in pending})
    monitors_result = await db.execute(
        select(Monitor).where(Monitor.id.in_(monitor_ids))
    )
    monitors_map = {m.id: m for m in monitors_result.scalars().all()}

    await db.commit()

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
    x_worker_id: str = Header(default="unknown"),
):
    if not body.results:
        return {"saved": 0}

    # Save check results (always recorded — even a stale submission is useful
    # history; consensus logic treats it as one of the latest-per-region rows).
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

    # Delete the pending_checks this worker owns. A stale submission (whose
    # lease was reclaimed by someone else) will not match on worker_id and
    # therefore won't wipe the other worker's in-flight lease.
    now = datetime.now(timezone.utc)
    pending_ids = [r.pending_check_id for r in body.results]
    await db.execute(
        delete(PendingCheck)
        .where(
            PendingCheck.id.in_(pending_ids),
            PendingCheck.worker_id == x_worker_id,
        )
    )

    # Mark each monitor as having just received a result. next_check_at is
    # advanced by the scheduler when it enqueues, not here.
    monitor_ids = list({r.monitor_id for r in body.results})
    await db.execute(
        update(Monitor)
        .where(Monitor.id.in_(monitor_ids))
        .values(last_checked_at=now)
    )

    await db.commit()

    # Dispatch one finalize_check_result per unique monitor. The task runs
    # under a per-monitor advisory lock so concurrent submissions from
    # different regions serialize their consensus rollup. Commit first so
    # the Celery worker sees the checks we just wrote.
    for monitor_id in monitor_ids:
        finalize_check_result.delay(str(monitor_id))

    return {"saved": len(body.results)}


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
