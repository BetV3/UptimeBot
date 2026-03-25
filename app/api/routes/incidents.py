import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.models import Check, Incident, Monitor, Project, User
from app.schemas.incidents import IncidentResponse
from app.services.auth import get_current_user

router = APIRouter()


def _incident_to_response(incident: Incident, monitor_name: str) -> IncidentResponse:
    duration = None
    if incident.resolved_at and incident.started_at:
        duration = int((incident.resolved_at - incident.started_at).total_seconds())
    return IncidentResponse(
        id=str(incident.id),
        monitor_id=str(incident.monitor_id),
        monitor_name=monitor_name,
        started_at=incident.started_at.isoformat(),
        resolved_at=incident.resolved_at.isoformat() if incident.resolved_at else None,
        duration_seconds=duration,
    )


@router.get("/projects/{project_id}/incidents")
async def list_incidents(
    project_id: uuid.UUID,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Verify project ownership
    proj = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == current_user.id)
    )
    if not proj.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    offset = (page - 1) * per_page
    result = await db.execute(
        select(Incident, Monitor.name)
        .join(Monitor, Incident.monitor_id == Monitor.id)
        .where(Monitor.project_id == project_id)
        .order_by(Incident.started_at.desc())
        .offset(offset)
        .limit(per_page)
    )
    rows = result.all()

    total_result = await db.execute(
        select(func.count())
        .select_from(Incident)
        .join(Monitor, Incident.monitor_id == Monitor.id)
        .where(Monitor.project_id == project_id)
    )
    total = total_result.scalar()

    return {
        "incidents": [_incident_to_response(inc, name) for inc, name in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/incidents/{incident_id}")
async def get_incident(
    incident_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Fetch incident with ownership check
    result = await db.execute(
        select(Incident, Monitor.name)
        .join(Monitor, Incident.monitor_id == Monitor.id)
        .join(Project, Monitor.project_id == Project.id)
        .where(Incident.id == incident_id, Project.user_id == current_user.id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")

    incident, monitor_name = row

    # Get checks during the incident window for timeline
    query = (
        select(Check)
        .where(
            Check.monitor_id == incident.monitor_id,
            Check.checked_at >= incident.started_at,
        )
        .order_by(Check.checked_at.asc())
    )
    if incident.resolved_at:
        query = query.where(Check.checked_at <= incident.resolved_at)

    checks_result = await db.execute(query)
    checks = checks_result.scalars().all()

    resp = _incident_to_response(incident, monitor_name)
    return {
        **resp.model_dump(),
        "timeline": [
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
    }
