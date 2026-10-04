import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.models import (
    Check, CheckRegion, CheckStatus, DnsMatchMode, DnsRecordType, HttpMethod, Monitor,
    MonitorStatus, MonitorType, Project, User,
)
from app.schemas.monitors import LiveCheckRequest, MonitorCreate, MonitorUpdate, MonitorResponse
from app.services.auth import get_current_user
from app.services.plans import check_monitor_limit, check_interval_limit

router = APIRouter()

# check-now makes all three regional workers run a check on demand. Limits so
# an API key (or an AI agent looping on it) cannot turn the worker fleet into
# a load generator: per user 30 per hour, per monitor one every 20 s. Redis
# backed, fail CLOSED like the public SSL tool.
CHECK_NOW_PER_HOUR = 30
CHECK_NOW_MIN_GAP = 20
REGION_VALUES = [r.value for r in CheckRegion]
# Each waiting check-now holds a DB connection for up to 90 s; cap how many
# can wait at once so they cannot starve the pool the dashboard uses.
CHECK_NOW_CONCURRENCY = 4
_check_now_sem = asyncio.Semaphore(CHECK_NOW_CONCURRENCY)


async def _check_now_allowed(user_id: str, monitor_id: str) -> str | None:
    from app.api.routes.tools import _redis
    try:
        r = _redis()
        if not await r.set(f"cp:checknow:m:{monitor_id}", 1, ex=CHECK_NOW_MIN_GAP, nx=True):
            return f"This monitor was checked on demand under {CHECK_NOW_MIN_GAP}s ago. Wait and retry."
        key = f"cp:checknow:u:{user_id}"
        n = await r.incr(key)
        if n == 1:
            await r.expire(key, 3600)
        if n > CHECK_NOW_PER_HOUR:
            return f"On-demand check limit reached ({CHECK_NOW_PER_HOUR} per hour)."
    except Exception:
        return "On-demand checks are temporarily unavailable. Try again shortly."
    return None


def _monitor_to_response(m: Monitor) -> MonitorResponse:
    return MonitorResponse(
        id=str(m.id),
        project_id=str(m.project_id),
        name=m.name,
        type=m.type.value if m.type else "http",
        url=m.url,
        method=m.method.value,
        expected_status=m.expected_status,
        interval_seconds=m.interval_seconds,
        timeout_seconds=m.timeout_seconds,
        headers=m.headers,
        body=m.body,
        target_host=m.target_host,
        target_port=m.target_port,
        warn_days_before_expiry=m.warn_days_before_expiry,
        dns_record_type=m.dns_record_type.value if m.dns_record_type else None,
        dns_expected_value=m.dns_expected_value,
        is_active=m.is_active,
        current_status=m.current_status.value,
        last_checked_at=m.last_checked_at.isoformat() if m.last_checked_at else None,
        created_at=m.created_at.isoformat(),
    )


async def _get_user_project(
    project_id: uuid.UUID, user: User, db: AsyncSession
) -> Project:
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


async def _get_user_monitor(
    monitor_id: uuid.UUID, user: User, db: AsyncSession
) -> Monitor:
    result = await db.execute(
        select(Monitor)
        .join(Project, Monitor.project_id == Project.id)
        .where(Monitor.id == monitor_id, Project.user_id == user.id)
    )
    monitor = result.scalar_one_or_none()
    if not monitor:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Monitor not found")
    return monitor


# --- Project-scoped routes ---


@router.post(
    "/projects/{project_id}/monitors",
    response_model=MonitorResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_monitor(
    project_id: uuid.UUID,
    body: MonitorCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_user_project(project_id, current_user, db)
    # Same validators as the dashboard form, so the API (and the MCP server
    # built on it) cannot create a monitor the UI would refuse.
    from app.api.routes.dashboard import (
        _check_duplicate_monitor_name, _validate_dns_fields, _validate_monitor_fields,
        _validate_ssl_fields,
    )
    try:
        mtype = MonitorType((body.type or "http").lower())
    except ValueError:
        raise HTTPException(status_code=400, detail="type must be one of: http, ssl, dns.")
    host_clean = port_clean = warn_clean = None
    dns_rtype = dns_expected = dns_resolver = None
    dns_match = DnsMatchMode.ALL
    if mtype == MonitorType.HTTP:
        name_clean, url_clean, http_method = _validate_monitor_fields(
            body.name, body.url or "", body.interval_seconds, body.timeout_seconds, body.method.upper())
        if not 100 <= body.expected_status <= 599:
            raise HTTPException(status_code=400, detail="expected_status must be 100-599.")
        expected = body.expected_status
    elif mtype == MonitorType.SSL:
        if not 1 <= body.target_port <= 65535:
            raise HTTPException(status_code=400, detail="Port must be between 1 and 65535.")
        name_clean, host_clean, port_clean, warn_clean = _validate_ssl_fields(
            body.name, body.target_host or "", body.target_port, body.warn_days_before_expiry,
            body.interval_seconds, body.timeout_seconds)
        url_clean, http_method, expected = f"https://{host_clean}:{port_clean}", HttpMethod.GET, 200
    else:
        name_clean, host_clean, dns_rtype, dns_expected, dns_resolver, dns_match = _validate_dns_fields(
            body.name, body.target_host or "", body.dns_record_type, body.dns_expected_value or "",
            body.dns_resolver or "", body.dns_match_mode, body.interval_seconds, body.timeout_seconds)
        url_clean, http_method, expected = f"dns://{host_clean}/{dns_rtype.value}", HttpMethod.GET, 200
    await _check_duplicate_monitor_name(db, str(project_id), name_clean)
    await check_monitor_limit(current_user, db)
    check_interval_limit(current_user, body.interval_seconds)

    monitor = Monitor(
        project_id=project_id,
        name=name_clean,
        type=mtype,
        url=url_clean,
        method=http_method,
        expected_status=expected,
        interval_seconds=body.interval_seconds,
        timeout_seconds=body.timeout_seconds,
        target_host=host_clean,
        target_port=port_clean,
        warn_days_before_expiry=warn_clean,
        dns_record_type=dns_rtype,
        dns_expected_value=dns_expected,
        dns_resolver=dns_resolver,
        dns_match_mode=dns_match,
        headers=body.headers,
        body=body.body,
        next_check_at=func.now(),
    )
    db.add(monitor)
    await db.commit()
    await db.refresh(monitor)
    return _monitor_to_response(monitor)


@router.get("/projects/{project_id}/monitors", response_model=list[MonitorResponse])
async def list_monitors(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_user_project(project_id, current_user, db)
    result = await db.execute(
        select(Monitor).where(Monitor.project_id == project_id).order_by(Monitor.created_at.desc())
    )
    return [_monitor_to_response(m) for m in result.scalars().all()]


# --- Monitor-scoped routes ---


@router.get("/monitors/{monitor_id}", response_model=MonitorResponse)
async def get_monitor(
    monitor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    monitor = await _get_user_monitor(monitor_id, current_user, db)
    return _monitor_to_response(monitor)


@router.patch("/monitors/{monitor_id}", response_model=MonitorResponse)
async def update_monitor(
    monitor_id: uuid.UUID,
    body: MonitorUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    monitor = await _get_user_monitor(monitor_id, current_user, db)
    update_data = body.model_dump(exclude_unset=True)
    if "interval_seconds" in update_data:
        check_interval_limit(current_user, update_data["interval_seconds"])
    if "method" in update_data:
        update_data["method"] = HttpMethod(update_data["method"])
    for field, value in update_data.items():
        setattr(monitor, field, value)
    await db.commit()
    await db.refresh(monitor)
    return _monitor_to_response(monitor)


@router.delete("/monitors/{monitor_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_monitor(
    monitor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    monitor = await _get_user_monitor(monitor_id, current_user, db)
    await db.delete(monitor)
    await db.commit()


@router.post("/monitors/{monitor_id}/pause", response_model=MonitorResponse)
async def pause_monitor(
    monitor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    monitor = await _get_user_monitor(monitor_id, current_user, db)
    monitor.is_active = False
    await db.commit()
    await db.refresh(monitor)
    return _monitor_to_response(monitor)


@router.post("/monitors/{monitor_id}/resume", response_model=MonitorResponse)
async def resume_monitor(
    monitor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    monitor = await _get_user_monitor(monitor_id, current_user, db)
    monitor.is_active = True
    monitor.next_check_at = func.now()
    await db.commit()
    await db.refresh(monitor)
    return _monitor_to_response(monitor)


# --- On-demand check (used by the MCP server's check_now tool) ---


def _check_row(c: Check) -> dict:
    return {
        "region": c.region.value,
        "status": c.status.value,
        "status_code": c.status_code,
        "response_time_ms": c.response_time_ms,
        "error": c.error,
        "cert_days_remaining": c.cert_days_remaining,
        "dns_resolved_values": c.dns_resolved_values,
        "checked_at": c.checked_at.isoformat(),
    }


@router.post("/monitors/{monitor_id}/check-now")
async def check_now(
    monitor_id: uuid.UUID,
    body: LiveCheckRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Queue a check in every region now and wait (up to wait_seconds) for results.

    Uses the normal pending_checks lease path, so it is the same check the
    scheduler would run: real workers in us/eu/asia, the same 2-of-3 rule,
    and the result lands in history and can open or resolve an incident.
    """
    monitor = await _get_user_monitor(monitor_id, current_user, db)
    wait = (body or LiveCheckRequest()).wait_seconds
    if _check_now_sem.locked():
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="On-demand checks are busy right now. Retry in a few seconds.")
    reason = await _check_now_allowed(str(current_user.id), str(monitor_id))
    if reason:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=reason)
    async with _check_now_sem:
        return await _run_check_now(monitor, wait, db)


async def _run_check_now(monitor: Monitor, wait: int, db: AsyncSession) -> dict:
    from sqlalchemy import text as sa_text
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from app.models.models import PendingCheck

    started = datetime.now(timezone.utc)
    for region in CheckRegion:
        await db.execute(
            pg_insert(PendingCheck)
            .values(id=uuid.uuid4(), monitor_id=monitor.id, region=region, scheduled_at=started)
            .on_conflict_do_nothing(index_elements=["monitor_id", "region"],
                                    index_where=sa_text("dead = false"))
        )
    await db.commit()

    latest: dict[str, Check] = {}
    deadline = started + timedelta(seconds=wait)
    while True:
        rows = (await db.execute(
            select(Check).where(Check.monitor_id == monitor.id, Check.checked_at >= started)
            .order_by(Check.checked_at.desc())
        )).scalars().all()
        await db.commit()  # end the read so the next poll sees new rows
        for c in rows:
            latest.setdefault(c.region.value, c)
        if len(latest) == len(REGION_VALUES) or datetime.now(timezone.utc) >= deadline:
            break
        await asyncio.sleep(2)

    down = sum(1 for c in latest.values() if c.status == CheckStatus.DOWN)
    if len(latest) == len(REGION_VALUES):
        verdict = "down" if down >= 2 else "up"
    elif down >= 2:
        verdict = "down"
    elif len(latest) - down >= 2:
        verdict = "up"
    else:
        verdict = "incomplete"
    return {
        "monitor_id": str(monitor.id),
        "name": monitor.name,
        "type": monitor.type.value,
        "verdict": verdict,
        "rule": "down only when at least 2 of 3 regions fail",
        "regions_reported": sorted(latest),
        "regions_missing": [r for r in REGION_VALUES if r not in latest],
        "results": [_check_row(latest[r]) for r in REGION_VALUES if r in latest],
        "waited_seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
    }


# --- Check history ---


@router.get("/monitors/{monitor_id}/checks")
async def list_checks(
    monitor_id: uuid.UUID,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_user_monitor(monitor_id, current_user, db)

    offset = (page - 1) * per_page
    result = await db.execute(
        select(Check)
        .where(Check.monitor_id == monitor_id)
        .order_by(Check.checked_at.desc())
        .offset(offset)
        .limit(per_page)
    )
    checks = result.scalars().all()

    total_result = await db.execute(
        select(func.count()).select_from(Check).where(Check.monitor_id == monitor_id)
    )
    total = total_result.scalar()

    return {
        "checks": [
            {
                "id": str(c.id),
                "region": c.region.value,
                "status": c.status.value,
                "status_code": c.status_code,
                "response_time_ms": c.response_time_ms,
                "error": c.error,
                "checked_at": c.checked_at.isoformat(),
            }
            for c in checks
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/monitors/{monitor_id}/checks/summary")
async def checks_summary(
    monitor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_user_monitor(monitor_id, current_user, db)

    now = datetime.now(timezone.utc)
    periods = {
        "24h": timedelta(hours=24),
        "7d": timedelta(days=7),
        "30d": timedelta(days=30),
        "90d": timedelta(days=90),
    }

    summary = {}
    for label, delta in periods.items():
        since = now - delta
        total_result = await db.execute(
            select(func.count())
            .select_from(Check)
            .where(Check.monitor_id == monitor_id, Check.checked_at >= since)
        )
        total = total_result.scalar()

        if total == 0:
            summary[label] = {"uptime_pct": None, "total_checks": 0}
            continue

        up_result = await db.execute(
            select(func.count())
            .select_from(Check)
            .where(
                Check.monitor_id == monitor_id,
                Check.checked_at >= since,
                Check.status == CheckStatus.UP,
            )
        )
        up_count = up_result.scalar()
        summary[label] = {
            "uptime_pct": round((up_count / total) * 100, 3),
            "total_checks": total,
        }

    return summary
